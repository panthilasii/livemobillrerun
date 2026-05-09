#!/usr/bin/env python3
"""NP Create — admin helper: bundle scrcpy into ``.tools/<os>/scrcpy/``
so the customer ZIP includes it out-of-the-box.

Why bundle instead of relying on the in-app auto-installer
----------------------------------------------------------

The desktop app already knows how to download + verify + install
scrcpy on first Mirror click (see :mod:`src.scrcpy_installer`).
That works great for customers with a healthy internet connection.

But our buyer profile leans heavily toward "can barely use a PC",
running on flaky home wifi or behind ISP captive portals. The
30-second first-click download is one more thing that can fail
in confusing ways:

* Out of disk space → cryptic OSError mid-extract.
* DNS hijack on the customer's router → SSL cert mismatch.
* Corporate / school firewall → connection reset.

So for the customer ZIP we ALSO ship a pre-downloaded copy under
``.tools/<os>/scrcpy/``. ``platform_tools.find_scrcpy()`` looks
there *first*, so customers who get the bundle never download
anything, work fully offline after the initial install, and
never see the install dialog. The auto-installer remains as a
safety net for customers who got an older ZIP before bundling
shipped, or for the admin-only build target where we keep the
ZIP small.

Why we share the manifest with src/scrcpy_installer.py
------------------------------------------------------

Both this script and the runtime installer must agree on
"which version" and "which sha256". We import the manifest
directly from the installer module so a single ``SCRCPY_VERSION =
'3.x.y'`` bump propagates to both the build pipeline and the
customer-side fallback path.

Usage
-----

    python tools/setup_scrcpy.py             # all OSes
    python tools/setup_scrcpy.py --os macos  # one OS
    python tools/setup_scrcpy.py --force     # redownload + re-extract

The output layout matches what ``find_scrcpy`` looks for:

    .tools/macos/scrcpy/scrcpy           + dylibs + scrcpy-server
    .tools/windows/scrcpy/scrcpy.exe     + dlls + scrcpy-server
    .tools/linux/scrcpy/scrcpy           + libs

build_release.py picks these up automatically via SHIP_TOOLS_PATTERNS.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WORKSPACE = PROJECT.parent

# Reuse the runtime manifest so the build + the in-app fallback
# agree on version + sha256 — that way bumping ``SCRCPY_VERSION`` in
# the installer module is the single edit needed.
sys.path.insert(0, str(PROJECT))
from src import scrcpy_installer as _inst  # noqa: E402

sys.path.insert(0, str(HERE))
from _download_helper import download as _safe_download  # noqa: E402


CACHE = WORKSPACE / ".cache" / "scrcpy"


# Map ``platform_key`` (used by the runtime installer) → the
# ``os_name`` directory used by build_release.py / .tools/.
#
# We intentionally only include macos-aarch64 for the macOS bundle
# slot. Apple Silicon is the dominant SKU we ship to; Intel macOS
# customers get the auto-download fallback which fetches the
# x86_64 binary on first click (which works because find_scrcpy()
# falls through to ~/.npcreate/tools/ when the bundled binary is
# missing OR the wrong architecture). If/when Intel macOS becomes
# meaningful we can ship a fat zip with both slices side by side.
_BUNDLE_TARGETS: dict[str, str] = {
    "macos": "macos-aarch64",
    "windows": "windows-x64",
    "linux": "linux-x86_64",
}


def _verify_sha256(path: Path, expected: str) -> None:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    got = h.hexdigest()
    if got.lower() != expected.lower():
        raise SystemExit(
            f"sha256 mismatch for {path.name}\n"
            f"  expected {expected}\n"
            f"  got      {got}\n"
            f"Refusing to bundle a tampered archive into the customer ZIP."
        )


def _extract_into(archive: Path, kind: str, dest: Path) -> None:
    """Extract the archive into ``dest`` using the same safety
    checks as the runtime installer (no zip-slip, no path
    traversal). We strip the top-level directory so the customer
    ZIP layout is the predictable ``scrcpy/scrcpy(.exe)`` rather
    than scrcpy's per-version-tagged folder name.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # Find the common top-level prefix in the archive so we can
    # strip it. macOS releases use ``scrcpy/`` already; Windows
    # uses ``scrcpy-win64-vX.Y.Z/``; Linux uses
    # ``scrcpy-linux-x86_64-vX.Y.Z/``. Stripping makes the bundled
    # path stable regardless of the upstream naming convention.
    if kind == "tar.gz":
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            top = _common_prefix([m.name for m in members])
            for m in members:
                stripped = _strip_prefix(m.name, top)
                if not stripped:
                    continue
                _safe_path(dest, stripped)
                m.name = stripped
                # Python 3.12+ honours ``filter='data'``; older
                # builds ignore the kwarg via TypeError fallback.
                try:
                    tf.extract(m, dest, filter="data")
                except TypeError:
                    tf.extract(m, dest)
    elif kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            top = _common_prefix(names)
            for name in names:
                stripped = _strip_prefix(name, top)
                if not stripped:
                    continue
                target = _safe_path(dest, stripped)
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
    else:
        raise SystemExit(f"unknown archive kind: {kind}")


def _common_prefix(names: list[str]) -> str:
    """Return the leading directory shared by every member, or ''.

    Handles two shapes the upstream releases use:

    * tar.gz members may include a bare ``"scrcpy-macos-aarch64-vX"``
      entry (the top-level dir itself, no trailing slash) plus
      ``"scrcpy-macos-aarch64-vX/scrcpy"`` etc. The split-on-slash
      approach must accept either form when picking the candidate.
    * zip members usually lack the explicit dir entry, so the
      first member is already ``"scrcpy-win64-vX/scrcpy.exe"``.
    """
    if not names:
        return ""
    candidate = ""
    for n in names:
        head = n.split("/", 1)[0]
        if not head:
            return ""
        if not candidate:
            candidate = head
        elif head != candidate:
            return ""
    return candidate + "/" if candidate else ""


def _strip_prefix(name: str, prefix: str) -> str:
    if not prefix:
        return name
    if name == prefix.rstrip("/"):
        return ""
    if name.startswith(prefix):
        return name[len(prefix):]
    return name


def _safe_path(base: Path, member: str) -> Path:
    candidate = (base / member).resolve()
    base_resolved = base.resolve()
    if not str(candidate).startswith(
        str(base_resolved) + (
            "\\" if sys.platform.startswith("win") else "/"
        )
    ) and candidate != base_resolved:
        raise SystemExit(
            f"refusing to extract member outside dest dir: {member!r}"
        )
    return candidate


def _post_install_chmod(dest: Path, os_name: str) -> None:
    if os_name == "windows":
        return
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        # ``scrcpy`` itself + any helper that LOOKS like a binary
        # — we err on the side of "more chmod +x" since extra
        # exec bits on a dylib are harmless.
        if path.name.startswith("scrcpy") or path.suffix in ("", ".dylib", ".so"):
            try:
                path.chmod(path.stat().st_mode | 0o111)
            except OSError:
                pass


def install_one(os_name: str, force: bool = False) -> Path:
    if os_name not in _BUNDLE_TARGETS:
        raise SystemExit(f"unknown os: {os_name}")
    platform_key = _BUNDLE_TARGETS[os_name]
    asset = _inst._RELEASES.get(platform_key)
    if asset is None:
        raise SystemExit(
            f"no release asset registered for {platform_key} "
            f"in src/scrcpy_installer._RELEASES"
        )

    CACHE.mkdir(parents=True, exist_ok=True)
    archive_name = (
        f"scrcpy-{platform_key}-v{_inst.SCRCPY_VERSION}"
        + (".tar.gz" if asset.kind == "tar.gz" else ".zip")
    )
    archive_path = CACHE / archive_name

    if archive_path.is_file() and not force:
        size_mb = archive_path.stat().st_size / 1024 / 1024
        print(f"  [OK] cached {archive_path.name} ({size_mb:,.1f} MB)")
    else:
        print(f"  -> downloading {asset.url}")
        _safe_download(asset.url, archive_path)
        size_mb = archive_path.stat().st_size / 1024 / 1024
        print(f"  [OK] wrote {archive_path.name} ({size_mb:,.1f} MB)")

    print(f"  -> verifying sha256...")
    _verify_sha256(archive_path, asset.sha256)
    print(f"  [OK] sha256 OK")

    dest = WORKSPACE / ".tools" / os_name / "scrcpy"
    print(f"  -> extracting to {dest}")
    _extract_into(archive_path, asset.kind, dest)
    _post_install_chmod(dest, os_name)

    bin_name = "scrcpy.exe" if os_name == "windows" else "scrcpy"
    binary = dest / bin_name
    if not binary.is_file():
        # Some upstream archives nest one more level even after
        # the prefix strip (rare). Walk and find the binary.
        hits = list(dest.rglob(bin_name))
        if not hits:
            raise SystemExit(
                f"extraction completed but {bin_name} not found under {dest}"
            )
        binary = hits[0]
    size_mb = binary.stat().st_size / 1024 / 1024
    print(f"  [OK] installed {binary.relative_to(WORKSPACE)} ({size_mb:,.1f} MB)")
    return binary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Bundle scrcpy into .tools/<os>/scrcpy/ for the customer ZIP",
    )
    p.add_argument(
        "--os", dest="os_names", action="append",
        choices=tuple(_BUNDLE_TARGETS.keys()),
        help="target OS (repeatable; default = all)",
    )
    p.add_argument("--force", action="store_true",
                   help="redownload + re-extract even if cached")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    targets = args.os_names or list(_BUNDLE_TARGETS.keys())
    print("NP Create — scrcpy setup")
    print(f"  version : {_inst.SCRCPY_VERSION}")
    print(f"  targets : {', '.join(targets)}")
    print(f"  cache   : {CACHE}")

    for osn in targets:
        print(f"\n— {osn} —")
        install_one(osn, force=args.force)

    print()
    print("Done. The bundled scrcpy(s) will be picked up automatically by:")
    print("  • src.platform_tools.find_scrcpy()    (first lookup)")
    print("  • src.scrcpy_installer (fallback)     (if .tools/ missing)")
    print("  • tools/build_release.py              (auto-included in customer ZIP)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
