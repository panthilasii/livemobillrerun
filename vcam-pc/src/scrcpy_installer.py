"""Auto-installer for the bundled scrcpy mirror tool.

Why this module exists
----------------------

Customers buy NP Create to *not* have to think about technical
plumbing. Asking them to ``brew install scrcpy`` or download a
zip from GitHub adds friction that for a meaningful chunk of our
audience translates into "I'll just call the admin on Line".
That makes "Mirror หน้าจอ" effectively unusable for the customer
segment that needs it most (people who can barely use their
laptop).

So we install scrcpy ourselves, on first click of the Mirror
button:

* Pinned to a specific upstream version so we never surprise a
  customer with breaking changes between v3.x releases.
* SHA-256 verified against constants baked in below — defends
  against a CDN compromise / man-in-the-middle on the customer's
  ISP, and gives us repeatable installs.
* Downloaded from the official Genymobile/scrcpy GitHub release
  assets so we're not running our own mirror (= one fewer thing
  for ops to babysit, and customers' firewalls already trust
  GitHub).
* Stashed under ``~/.npcreate/tools/scrcpy-<version>/`` so an
  uninstall is just ``rm -rf`` of the NP Create data dir, no
  system-wide surprises.

Lookup order (in :func:`platform_tools.find_scrcpy`)
----------------------------------------------------

1. Bundled-in-customer-zip (`.tools/<os>/scrcpy/scrcpy(.exe)`).
2. User-data dir installed by THIS module.
3. ``shutil.which("scrcpy")`` (lets power users on Linux still
   prefer the system package).
4. Windows scoop/choco well-known paths.

"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import socket
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


log = logging.getLogger(__name__)


# ── pinned upstream release ────────────────────────────────────────


# Bumping this requires:
# 1. Updating the URL/sha256 below to match the new release assets
#    on github.com/Genymobile/scrcpy/releases/<tag>.
# 2. Re-running the installer integration tests against a clean
#    user-data dir to confirm the archive layout hasn't changed
#    (rom1v has been consistent since v3.0 so this is mostly a
#    hash-update task).
SCRCPY_VERSION = "3.3.4"


# Per-platform asset metadata. Each tuple is
# ``(url, sha256, archive_kind, expected_size_bytes)``.
#
# ``archive_kind`` is 'tar.gz' or 'zip'. We store it explicitly
# instead of inferring from the URL because future releases might
# switch formats and we want the dispatch table to be the single
# source of truth.
@dataclass(frozen=True)
class _Asset:
    url: str
    sha256: str
    kind: str           # "tar.gz" | "zip"
    size: int


# Sourced from https://github.com/Genymobile/scrcpy/releases/tag/v3.3.4
# (the "SHA256SUMS.txt" asset is the same set of hashes; we pin them
# at code-review time rather than fetching the live SHA file so a
# compromised release never gets accepted automatically).
_RELEASES: dict[str, _Asset] = {
    "macos-aarch64": _Asset(
        url=(
            "https://github.com/Genymobile/scrcpy/releases/download/"
            f"v{SCRCPY_VERSION}/scrcpy-macos-aarch64-v{SCRCPY_VERSION}.tar.gz"
        ),
        sha256="8fef43520405dd523c74e1530ac68febcc5a405ea89712c874936675da8513dd",
        kind="tar.gz",
        size=9_486_795,
    ),
    "macos-x86_64": _Asset(
        url=(
            "https://github.com/Genymobile/scrcpy/releases/download/"
            f"v{SCRCPY_VERSION}/scrcpy-macos-x86_64-v{SCRCPY_VERSION}.tar.gz"
        ),
        sha256="cf9b3453a33279b6009dfb256b1a84c374bd4c30a71edd74bacab28d72a5d929",
        kind="tar.gz",
        size=10_155_380,
    ),
    "windows-x64": _Asset(
        url=(
            "https://github.com/Genymobile/scrcpy/releases/download/"
            f"v{SCRCPY_VERSION}/scrcpy-win64-v{SCRCPY_VERSION}.zip"
        ),
        sha256="d8a155b7c180b7ca4cdadd40712b8750b63f3aab48cb5b8a2a39ac2d0d4c5d38",
        kind="zip",
        size=7_287_033,
    ),
    "linux-x86_64": _Asset(
        url=(
            "https://github.com/Genymobile/scrcpy/releases/download/"
            f"v{SCRCPY_VERSION}/scrcpy-linux-x86_64-v{SCRCPY_VERSION}.tar.gz"
        ),
        sha256="0305d98c06178c67e12427bbf340c436d0d58c9e2a39bf9ffbbf8f54d7ef95a5",
        kind="tar.gz",
        size=12_854_835,
    ),
}


# ── disk layout ────────────────────────────────────────────────────


def user_tools_root() -> Path:
    """Return ``~/.npcreate/tools`` — owner of every auto-installed
    binary. Mirrors the path family already used by
    ``license_key.ACTIVATION_PATH = ~/.npcreate/activation.json`` so
    the customer's data lives under one well-known root they (or
    we) can backup / wipe / inspect.
    """
    return Path.home() / ".npcreate" / "tools"


def install_dir(version: str = SCRCPY_VERSION) -> Path:
    """Where THIS version of scrcpy is unpacked.

    Versioned suffix lets us:
    * Have multiple versions side-by-side during a rollout,
    * Detect "version is already installed" without re-extracting,
    * Garbage-collect old versions cheaply (just delete the dirs
      whose suffix doesn't match the current ``SCRCPY_VERSION``).
    """
    return user_tools_root() / f"scrcpy-{version}"


# ── platform detection ─────────────────────────────────────────────


class InstallerError(Exception):
    """Raised on any installer-side failure with a customer-readable
    message. The UI surfaces ``str(exc)`` directly so it must stay
    Thai-friendly."""


def detect_platform_key() -> str:
    """Return the key into ``_RELEASES`` for THIS machine, or raise
    if we don't ship a binary for it.

    We default to the more common arch on each OS (aarch64 on macOS
    since 2020, x86_64 elsewhere). Customers running rare combos
    (Windows ARM64, ancient Intel Macs without AVX) get a clear
    error message that nudges them at the manual-install fallback.
    """
    sysname = sys.platform
    machine = platform.machine().lower()

    if sysname == "darwin":
        # Apple Silicon reports 'arm64'; older Intel reports 'x86_64'.
        # Rosetta-translated processes report 'x86_64' too, which is
        # actually what we want — they need the x86_64 binary.
        if machine in ("arm64", "aarch64"):
            return "macos-aarch64"
        return "macos-x86_64"

    if sysname.startswith("win"):
        # Windows ARM64 customers exist but are vanishingly rare in
        # our segment (Snapdragon laptops). Fall through to win64
        # since x86_64 emulation is fast enough for scrcpy on those
        # SKUs and the alternative is a hard error.
        return "windows-x64"

    if sysname.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        raise InstallerError(
            "ยังไม่มี scrcpy สำเร็จรูปสำหรับ Linux "
            f"{machine} — ใช้ ``apt install scrcpy`` แทน"
        )

    raise InstallerError(
        f"ระบบปฏิบัติการนี้ ({sysname} / {machine}) "
        "ยังไม่รองรับการติดตั้ง scrcpy อัตโนมัติ"
    )


# ── lookup helpers ─────────────────────────────────────────────────


def _binary_name() -> str:
    return "scrcpy.exe" if sys.platform.startswith("win") else "scrcpy"


def find_user_installed() -> Optional[Path]:
    """Return Path to scrcpy binary inside ``~/.npcreate/tools/``,
    if any version is present. Picks the newest by mtime when more
    than one version dir co-exists (rollouts, downgrade tests)."""
    root = user_tools_root()
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for version_dir in sorted(root.glob("scrcpy-*")):
        if not version_dir.is_dir():
            continue
        # The archive layout puts the binary one or two dirs deep:
        #   macOS: <ver>/scrcpy/scrcpy
        #   Win:   <ver>/scrcpy-win64-v3.3.4/scrcpy.exe
        # Find by walking — capped because the install dirs are tiny.
        for hit in version_dir.rglob(_binary_name()):
            if hit.is_file():
                candidates.append(hit)
                break
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def is_installed(version: str = SCRCPY_VERSION) -> bool:
    """Quick "is THIS pinned version already on disk?" check.

    Used by the UI to decide between "install" and "ready to mirror"
    without re-walking the filesystem on every render."""
    target = install_dir(version)
    if not target.is_dir():
        return False
    bin_name = _binary_name()
    for hit in target.rglob(bin_name):
        if hit.is_file():
            return True
    return False


# ── installer ──────────────────────────────────────────────────────


# Progress callback signature:
#   ``cb(stage: str, current: int, total: int)``
# - stage in {'download', 'verify', 'extract', 'done'}
# - current/total in bytes for download, bytes processed for the
#   others (or 0 / 0 if not measurable for that stage).
ProgressCB = Callable[[str, int, int], None]


def _http_download(
    url: str,
    dest: Path,
    expected_size: int,
    progress: Optional[ProgressCB] = None,
    timeout: float = 30.0,
) -> None:
    """Stream ``url`` into ``dest``. Reports byte progress through
    ``progress`` so the UI can render a bar.

    We deliberately don't use ``urllib.request.urlretrieve``
    because its built-in hook is per-block but doesn't expose
    the total size on systems where Content-Length is missing.
    Doing the loop ourselves means we can fall back on the
    static ``expected_size`` we already have from the release
    manifest.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"user-agent": "NPCreate-installer/1.0"},
        )
        from . import _ssl as _ssl_helper
        ctx = _ssl_helper.default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            advertised = int(
                resp.headers.get("content-length") or expected_size or 0
            )
            total = advertised or expected_size
            written = 0
            chunk = 1 << 15  # 32 KB
            with dest.open("wb") as fh:
                while True:
                    block = resp.read(chunk)
                    if not block:
                        break
                    fh.write(block)
                    written += len(block)
                    if progress:
                        try:
                            progress("download", written, total)
                        except Exception:
                            log.debug("progress cb raised", exc_info=True)
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout) as e:
        raise InstallerError(
            f"ดาวน์โหลด scrcpy ไม่สำเร็จ — เช็คอินเทอร์เน็ตแล้วลองใหม่ ({e})"
        ) from e
    except OSError as e:
        raise InstallerError(
            f"เขียนไฟล์ลงดิสก์ไม่สำเร็จ — มีพื้นที่ว่างไหม ({e})"
        ) from e


def _verify_sha256(
    path: Path,
    expected: str,
    progress: Optional[ProgressCB] = None,
) -> None:
    """Stream-hash ``path`` and compare against ``expected``.

    Raises InstallerError on mismatch. We delete the bad file
    immediately so the next attempt doesn't believe it has a
    half-good cached download lying around.
    """
    h = hashlib.sha256()
    total = path.stat().st_size
    processed = 0
    try:
        with path.open("rb") as fh:
            while True:
                block = fh.read(1 << 16)
                if not block:
                    break
                h.update(block)
                processed += len(block)
                if progress:
                    try:
                        progress("verify", processed, total)
                    except Exception:
                        log.debug("progress cb raised", exc_info=True)
    except OSError as e:
        raise InstallerError(f"อ่านไฟล์ที่ดาวน์โหลดไม่ได้ ({e})") from e

    got = h.hexdigest()
    if got.lower() != expected.lower():
        try:
            path.unlink()
        except OSError:
            pass
        raise InstallerError(
            "ไฟล์ที่ดาวน์โหลดเสียหาย (sha256 ไม่ตรง) — "
            "เช็คอินเทอร์เน็ตแล้วลองใหม่"
        )


def _extract_archive(
    archive: Path,
    kind: str,
    dest_dir: Path,
    progress: Optional[ProgressCB] = None,
) -> None:
    """Unpack ``archive`` into ``dest_dir``. Supports tar.gz + zip.

    We pre-create + clean ``dest_dir`` so a half-extracted previous
    attempt doesn't leave stale members behind that could confuse
    ``find_user_installed``.
    """
    # Defence-in-depth: refuse to extract above ``dest_dir`` even
    # if the archive contains a malicious member (zip-slip, tar
    # symlink). Both tarfile + zipfile can blindly write outside
    # the target if we don't guard.
    dest_dir = dest_dir.resolve()
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)

    def _safe_join(base: Path, member_name: str) -> Path:
        candidate = (base / member_name).resolve()
        if not str(candidate).startswith(str(base) + os.sep) and candidate != base:
            raise InstallerError(
                f"ไฟล์ในแพ็คเกจไม่ปลอดภัย ({member_name}) — ยกเลิกการติดตั้ง"
            )
        return candidate

    try:
        if kind == "tar.gz":
            with tarfile.open(archive, "r:gz") as tf:
                members = tf.getmembers()
                total = len(members) or 1
                # Python 3.12 default deprecates "no filter"; pass
                # 'data' which strips dangerous metadata (setuid,
                # absolute paths, links) on top of our own
                # _safe_join check. Tarfile<3.12 accepts the kwarg
                # since 3.11.4; older Pythons get an explicit
                # try/except below.
                use_filter = True
                for i, m in enumerate(members):
                    _safe_join(dest_dir, m.name)
                    if use_filter:
                        try:
                            tf.extract(m, dest_dir, filter="data")
                        except TypeError:
                            use_filter = False
                            tf.extract(m, dest_dir)
                    else:
                        tf.extract(m, dest_dir)
                    if progress:
                        try:
                            progress("extract", i + 1, total)
                        except Exception:
                            log.debug("progress cb raised", exc_info=True)
        elif kind == "zip":
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                total = len(names) or 1
                for i, name in enumerate(names):
                    _safe_join(dest_dir, name)
                    zf.extract(name, dest_dir)
                    if progress:
                        try:
                            progress("extract", i + 1, total)
                        except Exception:
                            log.debug("progress cb raised", exc_info=True)
        else:
            raise InstallerError(f"unknown archive kind: {kind}")
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
        raise InstallerError(f"แตกไฟล์ scrcpy ไม่สำเร็จ ({e})") from e


def _post_install_chmod(version_dir: Path) -> None:
    """Make sure the scrcpy binary + helpers are executable.

    macOS tar.gz preserves modes correctly; Windows zip doesn't
    have unix modes anyway. But on Linux, some tarballs lose the
    +x bit when extracted with non-default umask. We just chmod
    everything that LOOKS like a binary; chmod is idempotent and
    cheap.
    """
    if sys.platform.startswith("win"):
        return
    bin_name = _binary_name()
    for path in version_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name == bin_name or path.name.startswith("scrcpy"):
            try:
                path.chmod(path.stat().st_mode | 0o111)
            except OSError:
                pass


def install(
    version: str = SCRCPY_VERSION,
    progress: Optional[ProgressCB] = None,
    force: bool = False,
) -> Path:
    """Download + verify + extract scrcpy. Returns the binary path.

    Idempotent: if ``is_installed(version)`` is already True and
    ``force`` is False, returns the existing binary path without
    touching the network. The UI's "install" button calls this on
    a worker thread so the Tk loop stays responsive.

    Raises ``InstallerError`` on any failure, with a Thai-language
    message safe to display verbatim in a customer-facing dialog.
    """
    if is_installed(version) and not force:
        existing = find_user_installed()
        if existing is not None:
            return existing

    key = detect_platform_key()
    asset = _RELEASES.get(key)
    if asset is None:
        raise InstallerError(
            f"ไม่พบลิงก์ดาวน์โหลด scrcpy สำหรับ {key} — "
            "อัพเดท NP Create เป็นเวอร์ชันล่าสุดแล้วลองใหม่"
        )

    target = install_dir(version)
    target.mkdir(parents=True, exist_ok=True)

    # Download into a temp file in the SAME parent as ``target`` so
    # the rename at the end is atomic (cross-FS moves can't be
    # atomic and would defeat the point of the temp-dir pattern).
    tmp_dir = Path(tempfile.mkdtemp(prefix=".scrcpy-dl-", dir=target.parent))
    try:
        archive_path = tmp_dir / f"scrcpy-{version}.{asset.kind.split('.')[-1]}"
        if asset.kind == "tar.gz":
            archive_path = tmp_dir / f"scrcpy-{version}.tar.gz"
        log.info(
            "downloading scrcpy %s for %s from %s",
            version, key, asset.url,
        )
        _http_download(asset.url, archive_path, asset.size, progress=progress)
        _verify_sha256(archive_path, asset.sha256, progress=progress)
        _extract_archive(archive_path, asset.kind, target, progress=progress)
    finally:
        # Always tidy the download tmp dir; even on success we don't
        # need the .tar.gz / .zip kept around (sha256 is enough to
        # re-fetch it later if a forensic needs it).
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass

    _post_install_chmod(target)

    binary = find_user_installed()
    if binary is None:
        raise InstallerError(
            "ติดตั้งสำเร็จ แต่หา binary scrcpy ในไฟล์ที่แตกแล้วไม่เจอ — "
            "ลบโฟลเดอร์ ~/.npcreate/tools แล้วลองใหม่"
        )
    if progress:
        try:
            progress("done", 1, 1)
        except Exception:
            pass
    return binary


# ── async wrapper for UI ───────────────────────────────────────────


def install_async(
    version: str = SCRCPY_VERSION,
    progress: Optional[ProgressCB] = None,
    on_complete: Optional[Callable[[Optional[Path], Optional[Exception]], None]] = None,
    force: bool = False,
) -> threading.Thread:
    """Run :func:`install` on a daemon thread.

    The UI uses this so the Tk main loop keeps painting the
    progress bar. ``progress`` is called from the worker thread —
    callers that want to update widgets must marshal back to the
    Tk main thread (typically ``root.after(0, ...)``).
    """
    def _worker():
        binary: Optional[Path] = None
        err: Optional[Exception] = None
        try:
            binary = install(version, progress=progress, force=force)
        except Exception as e:
            log.exception("scrcpy install failed")
            err = e
        if on_complete is not None:
            try:
                on_complete(binary, err)
            except Exception:
                log.exception("install on_complete cb raised")

    t = threading.Thread(
        target=_worker, daemon=True, name="scrcpy-installer",
    )
    t.start()
    return t


# ── house-keeping helpers ──────────────────────────────────────────


def estimated_download_mb(version: str = SCRCPY_VERSION) -> int:
    """Friendly "~N MB" number for the install dialog. Best-effort
    — falls back to 10 MB if we don't have the asset entry yet."""
    try:
        return max(1, _RELEASES[detect_platform_key()].size // (1024 * 1024))
    except (InstallerError, KeyError):
        return 10


def gc_old_versions(keep: str = SCRCPY_VERSION) -> int:
    """Delete every ``scrcpy-<other>/`` dir under user-data root.

    Run optionally on app start to keep disk usage bounded. Returns
    the number of directories removed. Best-effort — failures are
    logged but never raised."""
    root = user_tools_root()
    if not root.is_dir():
        return 0
    n = 0
    for d in root.glob("scrcpy-*"):
        if not d.is_dir() or d.name == f"scrcpy-{keep}":
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
        except OSError:
            log.warning("gc_old_versions: could not remove %s", d)
    return n
