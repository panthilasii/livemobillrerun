"""NP Create -- in-app self-updater.

The customer experience we're aiming for
----------------------------------------

Game-launcher style: open the app, see a banner "อัปเดตใหม่ v1.5.1
พร้อมใช้งาน", click "อัปเดต", wait ~30 seconds, app restarts on
the new version. The customer never has to re-download a 250 MB ZIP
through Line OA again.

Update strategy: source-only deltas
-----------------------------------

A "patch" is a ZIP of the ``vcam-pc/src/`` directory at the new
version. The bundled toolchain (``.tools/``, the prebuilt vcam APK,
ffmpeg, JDK) almost never changes between minor releases. Shipping
just the Python source keeps each patch in the few-hundred-KB range
-- fast even on hotel WiFi.

When the toolchain DOES change (very rare: new ffmpeg major, new
JDK), we ship a full installer instead and the manifest sets
``kind="full"``. The desktop app then opens the browser at the
download URL rather than auto-applying, because replacing the
running ``.exe`` while it's executing is platform-specific tricky.

Trust chain
-----------

Every manifest is **signed with the same Ed25519 keypair** used by
the licensing and announcement subsystems:

* The customer build embeds ``_pubkey.py`` (32-byte verify key).
* The admin keeps ``.private_key`` on their machine (never shipped).
* ``tools/publish_update.py`` signs the manifest at release time.

Hijacking the update channel therefore requires the admin's
private key -- a stolen TLS cert or a malicious DNS resolver is not
enough. This is the same threat model as macOS / Windows code
signing, just done with primitives we already had.

Failure modes (all safe)
------------------------

* Network down → no banner, app still works.
* Bad signature → log warning, skip update, app still works.
* Download corrupt → SHA256 mismatch, skip apply, app still works.
* Apply fails halfway → ``src.bak`` rolled back automatically.

The auto-updater is BEST EFFORT. Nothing here can prevent the
customer from launching the version they already have.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import sqlite3 as _sqlite_unused  # noqa: F401  (silences a bogus lint)
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import _ed25519
from ._pubkey import PUBLIC_KEY_HEX
from .branding import BRAND

log = logging.getLogger(__name__)


# ── configuration ───────────────────────────────────────────────


# Where the signed manifest lives. Override via env var so QA can
# point a single dev build at a staging URL without rebuilding.
DEFAULT_MANIFEST_URL = os.environ.get(
    "NP_UPDATE_MANIFEST_URL",
    "https://npcreate.github.io/updates/manifest.json",
)

# How often the app polls for new updates while running. 6 hours is
# the right floor for a desktop app: low enough that an emergency
# patch surfaces same-day, high enough that we don't smash the CDN.
POLL_INTERVAL_S = 6 * 3600

# Hard cap on a downloaded patch -- malicious feeds can't OOM us.
MAX_PATCH_BYTES = 50 * 1024 * 1024   # 50 MB

# Where we stash the downloaded ZIP before applying. ``tempfile``'s
# default location is fine; the apply step moves into ``src/``.
_STAGING_DIR_NAME = "npcreate_update_staging"


# ── data classes ────────────────────────────────────────────────


@dataclass(frozen=True)
class UpdateManifest:
    """Decoded + verified manifest.

    Fields
    ------
    version
        Semver-ish ``"X.Y.Z"`` of the new build.
    kind
        ``"source"`` (Python-only patch) or ``"full"`` (installer
        download URL, customer takes over manually).
    download_url
        HTTPS URL of the patch ZIP / installer.
    sha256_hex
        Hex digest of the file at ``download_url`` so we can verify
        end-to-end integrity (network MITM, half-finished transfer,
        flipped bit on disk).
    notes_th
        Thai-language changelog shown to the customer.
    min_compat_version
        Lowest currently-installed version that can apply this
        patch. Lets us mark a patch as "must use full installer"
        when (e.g.) we changed the SQLite schema in a way the new
        Python code can't migrate from the old one.
    published_at
        ISO 8601 timestamp -- shown alongside the changelog.
    """

    version: str
    kind: str
    download_url: str
    sha256_hex: str
    notes_th: str
    min_compat_version: str = "1.0.0"
    published_at: str = ""


# ── version comparison ─────────────────────────────────────────


def _parse(v: str) -> tuple[int, ...]:
    """Parse ``"1.5.0"`` → ``(1, 5, 0)``. Trailing pre-release tags
    like ``"1.5.0-beta"`` are stripped (we don't ship pre-release
    via the auto-updater)."""
    head = v.split("-", 1)[0].split("+", 1)[0]
    out: list[int] = []
    for chunk in head.split("."):
        try:
            out.append(int(chunk))
        except ValueError:
            return ()
    return tuple(out)


def is_newer(candidate: str, current: str) -> bool:
    """``True`` iff ``candidate`` should replace ``current``.

    Returns ``False`` on parse failure -- never auto-update from a
    manifest with a malformed version string. The customer can
    install full installer manually if we ever ship something
    weird.
    """
    a = _parse(candidate)
    b = _parse(current)
    if not a or not b:
        return False
    return a > b


# ── manifest fetch + verify ────────────────────────────────────


class UpdateError(Exception):
    """Raised internally when manifest fetch/verify fails. The
    public ``check()`` swallows these into a None return -- callers
    don't need to reason about network errors vs sig errors."""


def _http_get(url: str, *, timeout: float = 8.0,
              max_bytes: int = MAX_PATCH_BYTES,
              progress_cb: Optional[Callable[[int, int], None]] = None,
              ) -> bytes:
    """Fetch ``url`` with a size cap + optional progress callback.

    Progress is reported as ``(bytes_so_far, total_bytes_or_-1)`` --
    total may be -1 if the server doesn't send Content-Length.
    Raises ``UpdateError`` for everything (network + size cap +
    HTTP errors) so callers don't have to discriminate.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"NP-Create/{BRAND.version}",
            "Accept": "application/octet-stream, application/json",
        },
    )
    try:
        # certifi-backed context — see src/_ssl.py for rationale.
        from . import _ssl as _ssl_helper
        ctx = _ssl_helper.default_context()
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"fetch failed: {exc}") from exc

    total = -1
    cl = resp.headers.get("Content-Length")
    if cl:
        try:
            total = int(cl)
        except ValueError:
            pass
    if total > max_bytes:
        raise UpdateError(f"file too large: {total} > {max_bytes}")

    chunks: list[bytes] = []
    got = 0
    chunk_size = 64 * 1024
    while True:
        chunk = resp.read(chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
        if got > max_bytes:
            raise UpdateError(f"file exceeded cap mid-download: {got}")
        if progress_cb is not None:
            try:
                progress_cb(got, total)
            except Exception:
                # A buggy progress callback must never break the
                # download itself.
                log.exception("update progress callback")
    return b"".join(chunks)


def _verify_manifest_envelope(envelope: dict) -> dict:
    """Verify the Ed25519 signature on a manifest envelope.

    Schema::

        {
          "format_version": 1,
          "payload": "<base64url JSON of UpdateManifest fields>",
          "signature": "<hex Ed25519 sig>"
        }

    The ``payload`` is base64-url-encoded so it survives the
    GitHub-Pages line-ending normalisation that occasionally
    rewrites raw JSON. Returns the *parsed* payload dict.
    """
    sig_hex = envelope.get("signature")
    payload_b64 = envelope.get("payload")
    if not sig_hex or not payload_b64:
        raise UpdateError("manifest missing signature or payload")
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        sig = bytes.fromhex(sig_hex)
        pub = bytes.fromhex(PUBLIC_KEY_HEX)
    except (ValueError, TypeError) as exc:
        raise UpdateError(f"manifest decode error: {exc}") from exc

    if not _ed25519.verify(pub, payload_bytes, sig):
        raise UpdateError("manifest signature verification failed")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"manifest payload not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("manifest payload not a JSON object")
    return payload


def fetch_manifest(url: str = DEFAULT_MANIFEST_URL) -> Optional[UpdateManifest]:
    """Download the manifest and return it if it's valid AND newer
    than the running build. Returns ``None`` if there's nothing to
    apply for any reason (no update, network down, bad sig, parse
    error). Errors are logged, never raised.
    """
    try:
        raw = _http_get(url, max_bytes=512 * 1024)   # manifest is tiny
        envelope = json.loads(raw.decode("utf-8"))
        payload = _verify_manifest_envelope(envelope)
    except (UpdateError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.info("auto-update: manifest unavailable (%s)", exc)
        return None

    try:
        m = UpdateManifest(
            version=str(payload["version"]),
            kind=str(payload.get("kind", "source")),
            download_url=str(payload["download_url"]),
            sha256_hex=str(payload["sha256"]),
            notes_th=str(payload.get("notes_th", "")),
            min_compat_version=str(payload.get("min_compat_version", "1.0.0")),
            published_at=str(payload.get("published_at", "")),
        )
    except KeyError as exc:
        log.warning("auto-update: manifest missing key %s", exc)
        return None

    if not is_newer(m.version, BRAND.version):
        log.info(
            "auto-update: running %s, latest %s -- nothing to do",
            BRAND.version, m.version,
        )
        return None

    if not is_newer(BRAND.version, _ge_str(m.min_compat_version, "0.0.0")):
        # Inverted logic: if the current version is BELOW the
        # min_compat_version, the patch can't be safely applied.
        # Force the customer to use the full installer for this
        # one (the manifest UI will still surface it).
        if _parse(BRAND.version) < _parse(m.min_compat_version):
            log.warning(
                "auto-update: %s < min_compat %s -- need full installer",
                BRAND.version, m.min_compat_version,
            )
            # We still return the manifest so the UI can tell the
            # user to download the full installer, but mark kind.
            m = UpdateManifest(**{**m.__dict__, "kind": "full"})

    return m


def _ge_str(a: str, b: str) -> str:
    """Return ``a`` unchanged -- helper exists purely so the
    ``is_newer(BRAND.version, _ge_str(...))`` line above reads
    obvious. (No, we don't actually need both arguments; the
    indirection is for future range checks.)"""
    return a


# ── download + apply ───────────────────────────────────────────


def download_patch(
    manifest: UpdateManifest,
    *,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download the patch ZIP, verify SHA256, and return the path
    to the on-disk staging file. Raises ``UpdateError`` on any
    failure -- callers should NOT auto-apply if this raises.
    """
    if manifest.kind != "source":
        raise UpdateError(
            f"cannot auto-apply kind={manifest.kind!r}; "
            "open download_url in browser instead"
        )

    data = _http_get(
        manifest.download_url,
        max_bytes=MAX_PATCH_BYTES,
        progress_cb=progress_cb,
    )

    digest = hashlib.sha256(data).hexdigest()
    if digest.lower() != manifest.sha256_hex.lower():
        raise UpdateError(
            f"sha256 mismatch: got {digest} expected {manifest.sha256_hex}"
        )

    staging = Path(tempfile.gettempdir()) / _STAGING_DIR_NAME
    staging.mkdir(parents=True, exist_ok=True)
    out = staging / f"npcreate-src-{manifest.version}.zip"
    out.write_bytes(data)
    return out


def apply_patch(
    patch_zip: Path,
    *,
    src_dir: Optional[Path] = None,
) -> None:
    """Atomically replace ``src_dir`` with the contents of
    ``patch_zip``.

    Atomicity strategy
    ------------------

    1. Extract zip to ``staging/`` inside a temp dir.
    2. Sanity-check: the extracted tree must contain ``main.py``
       (we're replacing ``src/``).
    3. ``mv`` the live ``src/`` to ``src.bak/`` (single inode swap
       on every modern filesystem -- atomic).
    4. ``mv`` ``staging/`` to ``src/``.
    5. Remove ``src.bak/`` only AFTER the next launch confirms the
       new build boots cleanly. Until then it's the rollback target.

    On any failure between steps 3 and 4, we restore from
    ``src.bak/`` and re-raise so the caller can show an error.
    Steps 1-2 fail before mutating the live install.
    """
    if src_dir is None:
        # Default: this module's parent directory IS src/. Walk up
        # one level to find it.
        src_dir = Path(__file__).resolve().parent
    src_dir = src_dir.resolve()

    if src_dir.name != "src":
        raise UpdateError(
            f"refusing to replace non-'src' directory: {src_dir}"
        )

    parent = src_dir.parent
    bak = parent / "src.bak"
    new = parent / "src.new"

    # Step 1: extract to a sibling staging dir (NOT temp dir, because
    # cross-filesystem moves aren't atomic and src/ might live on a
    # different disk than /tmp).
    if new.exists():
        shutil.rmtree(new)
    new.mkdir(parents=True)

    try:
        with zipfile.ZipFile(patch_zip, "r") as zf:
            # Strip a single optional leading prefix so we tolerate
            # both ``src/foo.py`` AND ``foo.py`` archives.
            members = zf.namelist()
            prefix = _common_prefix_to_strip(members)
            for name in members:
                if name.endswith("/"):
                    continue
                rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
                # Block path traversal -- any ``..`` segment is
                # cause to abort, never trust a downloaded zip
                # blindly.
                if ".." in Path(rel).parts:
                    raise UpdateError(f"unsafe path in patch: {name}")
                target = new / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except (zipfile.BadZipFile, OSError) as exc:
        if new.exists():
            shutil.rmtree(new, ignore_errors=True)
        raise UpdateError(f"patch extract failed: {exc}") from exc

    # Step 2: sanity check.
    if not (new / "main.py").is_file():
        shutil.rmtree(new, ignore_errors=True)
        raise UpdateError(
            "patch missing src/main.py -- refusing to apply "
            "(would brick the install)"
        )

    # Step 3-4: swap directories.
    if bak.exists():
        shutil.rmtree(bak)
    try:
        os.rename(src_dir, bak)
    except OSError as exc:
        shutil.rmtree(new, ignore_errors=True)
        raise UpdateError(f"could not move src -> src.bak: {exc}") from exc

    try:
        os.rename(new, src_dir)
    except OSError as exc:
        # Try to restore the backup -- the live install must always
        # have a valid src/ directory after this function returns
        # (success OR failure).
        try:
            os.rename(bak, src_dir)
        except OSError:
            log.exception(
                "CRITICAL: could not restore src.bak -- manual "
                "intervention required"
            )
        raise UpdateError(f"could not promote src.new -> src: {exc}") from exc

    log.info(
        "auto-update: applied patch (src.bak left for rollback) -> %s",
        src_dir,
    )


def _common_prefix_to_strip(members: list[str]) -> str:
    """If every member of the zip starts with the same ``foo/``
    segment AND that segment isn't ``__MACOSX``/etc, return it so
    the caller can strip. Returns ``""`` for archives that have
    files at the root.
    """
    if not members:
        return ""
    first = members[0]
    slash = first.find("/")
    if slash < 0:
        return ""
    prefix = first[:slash + 1]
    if any(not m.startswith(prefix) for m in members):
        return ""
    if prefix.startswith("__MACOSX"):
        return ""
    return prefix


def relaunch() -> None:
    """Spawn a fresh copy of the desktop app and exit the current
    process. Called right after ``apply_patch`` succeeds.

    We use ``subprocess.Popen`` (not ``execv``) so the new process
    runs at a clean PID -- some macOS Tk widgets get sticky if we
    swap the executable mid-mainloop. Exit happens AFTER spawn
    succeeds so a failed relaunch leaves the old build running.
    """
    args = [sys.executable, "-m", "src.main", "--studio"]
    try:
        subprocess.Popen(args, cwd=str(Path(__file__).resolve().parent.parent))
    except OSError as exc:
        log.exception("relaunch spawn failed: %s", exc)
        return
    # Give the child a moment to bind any sockets it needs (e.g.
    # the dashboard server on 8765) before our process tears down.
    time.sleep(0.5)
    os._exit(0)


# ── background poller ──────────────────────────────────────────


class UpdatePoller:
    """Background thread that polls the update channel at a fixed
    cadence and notifies the UI when a new version appears.

    The poller never auto-applies -- that's the user's decision
    (clicking the banner). It only fetches + verifies the manifest.
    Apply happens on the Tk thread when the user clicks.
    """

    def __init__(
        self,
        on_update: Callable[[UpdateManifest], None],
        *,
        url: str = DEFAULT_MANIFEST_URL,
        interval_s: int = POLL_INTERVAL_S,
    ) -> None:
        self.on_update = on_update
        self.url = url
        self.interval_s = max(60, int(interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_seen_version: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="np-auto-update", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # First poll on startup, then every interval_s. We add a
        # 30 s startup delay so the desktop app's other init work
        # (window paint, ADB scan) finishes before we do network.
        if self._stop.wait(timeout=30):
            return
        while not self._stop.is_set():
            try:
                m = fetch_manifest(self.url)
                if m and m.version != self._last_seen_version:
                    self._last_seen_version = m.version
                    try:
                        self.on_update(m)
                    except Exception:
                        log.exception("on_update callback")
            except Exception:
                log.exception("update poll iteration")
            if self._stop.wait(timeout=self.interval_s):
                return


__all__ = [
    "UpdateManifest",
    "UpdateError",
    "UpdatePoller",
    "DEFAULT_MANIFEST_URL",
    "fetch_manifest",
    "download_patch",
    "apply_patch",
    "relaunch",
    "is_newer",
]
