#!/usr/bin/env python3
"""NP Create — CI helper: download every tool the customer install
needs (platform-tools, JDK 21, lspatch.jar) into ``.tools/<os>/``.

Why this exists separately from ``setup_windows_tools.py`` and
``setup_macos_tools.py``
========================================================================

The two existing helpers were authored for the **admin's dev box**
where lspatch.jar already lives at ``.tools/lspatch/lspatch.jar``
(populated by ``install_lspatch.sh``). They copy / symlink from that
local cache into the per-OS subdir.

GitHub Actions runners are *fresh* every build — there's no local
``.tools/`` to copy from. So we need a self-contained downloader
that grabs every artifact straight from upstream:

* **platform-tools** — adb / fastboot from
  https://dl.google.com/android/repository/
* **JDK 21**         — Adoptium Temurin from GitHub releases
* **lspatch.jar**    — JingMatrix/LSPatch GitHub releases

scrcpy is handled by the sibling ``setup_scrcpy.py`` (already
wired into release.yml). This script complements it.

Usage::

    python tools/setup_ci_tools.py --os windows
    python tools/setup_ci_tools.py --os macos
    python tools/setup_ci_tools.py             # current OS

Outputs into ``<workspace>/.tools/<os>/{platform-tools,jdk-21,lspatch}/``.
Idempotent — skips already-downloaded artifacts unless ``--force``.

Encoding note (Windows)
-----------------------
All ``print()`` calls use ASCII-only markers (``->``, ``[OK]``,
``[!]``) instead of arrows / checkmarks. The Inno-Setup-bound
``windows-latest`` runner inherits cp1252 stdio and would crash
on emoji even with ``PYTHONUTF8=1`` in some PyInstaller bootstrap
paths — defence in depth.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WORKSPACE = PROJECT.parent

# Use the existing CA-tolerant downloader so corporate CI mirrors
# with custom certs still work. ``_download_helper`` is a private
# sibling of this script.
sys.path.insert(0, str(HERE))
from _download_helper import download as _safe_download  # noqa: E402

CACHE = WORKSPACE / ".cache" / "ci-tools"


# ── upstream URLs (pinned where possible, dynamic for lspatch) ─────


# Adoptium Temurin 21 LTS. Pinning to a specific build (.5+11) for
# reproducibility — bump via PR after smoke-testing. The naming
# convention is consistent enough that we can templatise per-OS.
JDK_VERSION = "21.0.5+11"
JDK_VER_PATH = "21.0.5%2B11"           # URL-encoded form ('+' -> %2B)
JDK_VER_FILE = "21.0.5_11"             # filename form ('+' -> '_')

JDK_URLS = {
    "windows": (
        f"https://github.com/adoptium/temurin21-binaries/releases/"
        f"download/jdk-{JDK_VER_PATH}/"
        f"OpenJDK21U-jdk_x64_windows_hotspot_{JDK_VER_FILE}.zip"
    ),
    "macos": (
        f"https://github.com/adoptium/temurin21-binaries/releases/"
        f"download/jdk-{JDK_VER_PATH}/"
        f"OpenJDK21U-jdk_aarch64_mac_hotspot_{JDK_VER_FILE}.tar.gz"
    ),
}

PT_URLS = {
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "macos":   "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}

LSPATCH_RELEASE_API = (
    "https://api.github.com/repos/JingMatrix/LSPatch/releases/latest"
)


# ── helpers ────────────────────────────────────────────────────────


def _download(url: str, dst: Path, force: bool = False) -> Path:
    if dst.is_file() and not force:
        size_mb = dst.stat().st_size / 1024 / 1024
        print(f"  [OK] cached {dst.name} ({size_mb:,.1f} MB)")
        return dst
    print(f"  -> downloading {url}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    _safe_download(url, dst)
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"  [OK] wrote {dst.name} ({size_mb:,.1f} MB)")
    return dst


def _extract_zip(zpath: Path, into: Path, strip_first: bool = False) -> None:
    """Unpack ``zpath`` into ``into``. If ``strip_first`` is True,
    drop the single top-level directory inside the archive (matches
    the Adoptium layout: jdk-21.0.5+11/{bin,lib,...})."""
    into.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        if strip_first:
            tops = {n.split("/", 1)[0] for n in zf.namelist() if n.strip()}
            if len(tops) != 1:
                raise SystemExit(
                    f"expected single top dir in {zpath.name}, got {tops}"
                )
            top_name = next(iter(tops))
        else:
            top_name = ""
        for member in zf.infolist():
            name = member.filename
            if strip_first:
                if not name.startswith(top_name + "/"):
                    continue
                name = name[len(top_name) + 1:]
            if not name:
                continue
            target = into / name
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _extract_tar(tpath: Path, into: Path, strip_first: bool = False) -> None:
    """Unpack ``tpath`` (a .tar.gz / .tgz) into ``into``."""
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tpath, "r:gz") as tf:
        members = tf.getmembers()
        if strip_first:
            tops = {m.name.split("/", 1)[0] for m in members if m.name.strip()}
            if len(tops) != 1:
                raise SystemExit(
                    f"expected single top dir in {tpath.name}, got {tops}"
                )
            top_name = next(iter(tops))
        else:
            top_name = ""
        for m in members:
            name = m.name
            if strip_first:
                if not name.startswith(top_name + "/"):
                    continue
                name = name[len(top_name) + 1:]
            if not name or m.isdir():
                # We rebuild dir tree implicitly when extracting files.
                continue
            target = into / name
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(m)
            if extracted is None:
                continue
            with target.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)
            # Preserve the executable bit on macOS / Linux JDK layouts —
            # ``java`` inside ``bin/`` is +x on the upstream tarball
            # and we want to keep it that way.
            if m.mode & 0o111:
                target.chmod(target.stat().st_mode | 0o755)


def _resolve_lspatch_url() -> str:
    """Return the latest LSPatch jar download URL via GitHub API."""
    print("  -> querying GitHub for latest LSPatch release")
    req = urllib.request.Request(
        LSPATCH_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "np-create-ci-setup/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    for asset in data.get("assets", []):
        if asset.get("name") == "lspatch.jar":
            url = asset.get("browser_download_url")
            if url:
                return url
    raise SystemExit("could not resolve lspatch.jar download URL")


# ── per-tool installers ────────────────────────────────────────────


def install_platform_tools(os_name: str, force: bool) -> None:
    print()
    print("[platform-tools]")
    dest = WORKSPACE / ".tools" / os_name / "platform-tools"
    if dest.is_dir() and any(dest.iterdir()) and not force:
        print(f"  [OK] already at {dest.relative_to(WORKSPACE)}")
        return
    url = PT_URLS[os_name]
    cache_path = CACHE / f"platform-tools-{os_name}.zip"
    _download(url, cache_path, force)
    if dest.is_dir():
        shutil.rmtree(dest)
    print(f"  -> extracting to {dest.relative_to(WORKSPACE)}")
    # The zip already has a top-level ``platform-tools/`` dir, so we
    # extract into the parent and let it land at the right place.
    _extract_zip(cache_path, dest.parent, strip_first=False)


def install_jdk(os_name: str, force: bool) -> None:
    print()
    print("[jdk-21]")
    dest = WORKSPACE / ".tools" / os_name / "jdk-21"
    if dest.is_dir() and not force:
        # Existence is good enough — bin/java is asserted in verify().
        print(f"  [OK] already at {dest.relative_to(WORKSPACE)}")
        return
    url = JDK_URLS[os_name]
    if os_name == "windows":
        cache_path = CACHE / f"jdk-{JDK_VER_FILE}-windows.zip"
        _download(url, cache_path, force)
        if dest.is_dir():
            shutil.rmtree(dest)
        print(f"  -> extracting to {dest.relative_to(WORKSPACE)}")
        _extract_zip(cache_path, dest, strip_first=True)
    else:
        cache_path = CACHE / f"jdk-{JDK_VER_FILE}-macos.tar.gz"
        _download(url, cache_path, force)
        if dest.is_dir():
            shutil.rmtree(dest)
        print(f"  -> extracting to {dest.relative_to(WORKSPACE)}")
        # macOS Adoptium tarball has a single top dir
        # ``jdk-21.0.5+11/`` containing ``Contents/Home/bin/java``.
        _extract_tar(cache_path, dest, strip_first=True)


def install_lspatch(os_name: str, force: bool) -> None:
    print()
    print("[lspatch]")
    # lspatch.jar is JVM bytecode — same file works on every OS, but
    # we drop a copy into each per-OS dir so build_release.py doesn't
    # need cross-OS lookups when zipping.
    dest_dir = WORKSPACE / ".tools" / os_name / "lspatch"
    dest_jar = dest_dir / "lspatch.jar"
    if dest_jar.is_file() and not force:
        size_mb = dest_jar.stat().st_size / 1024 / 1024
        print(f"  [OK] already at {dest_jar.relative_to(WORKSPACE)} "
              f"({size_mb:,.1f} MB)")
        return
    url = _resolve_lspatch_url()
    cache_path = CACHE / "lspatch.jar"
    _download(url, cache_path, force)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cache_path, dest_jar)
    size_mb = dest_jar.stat().st_size / 1024 / 1024
    print(f"  [OK] installed {dest_jar.relative_to(WORKSPACE)} "
          f"({size_mb:,.1f} MB)")


# ── main ───────────────────────────────────────────────────────────


def _verify(os_name: str) -> int:
    """Best-effort sanity check after install. Returns 0 if OK."""
    sfx = ".exe" if os_name == "windows" else ""
    base = WORKSPACE / ".tools" / os_name
    checks: list[tuple[str, Path]] = [
        ("platform-tools/adb", base / "platform-tools" / f"adb{sfx}"),
        ("lspatch.jar       ", base / "lspatch" / "lspatch.jar"),
    ]
    if os_name == "windows":
        checks.append(("jdk-21/bin/java   ", base / "jdk-21" / "bin" / "java.exe"))
    else:
        # Adoptium macOS layout: jdk-21/Contents/Home/bin/java
        checks.append(("jdk-21/.../java   ",
                       base / "jdk-21" / "Contents" / "Home" / "bin" / "java"))

    print()
    print("Verification:")
    bad = 0
    for label, path in checks:
        ok = path.is_file()
        mark = "[OK]" if ok else "[!] "
        print(f"  {mark} {label} : {path.relative_to(WORKSPACE)}")
        if not ok:
            bad += 1
    return 0 if bad == 0 else 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--os", dest="os_name",
        choices=("windows", "macos"),
        help="target OS (default: current OS)",
    )
    p.add_argument(
        "--force", action="store_true",
        help="redownload + re-extract even if cached",
    )
    p.add_argument(
        "--skip", action="append", default=[],
        choices=("platform-tools", "jdk", "lspatch"),
        help="skip a specific tool (repeatable)",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    os_name = args.os_name
    if not os_name:
        if sys.platform == "win32":
            os_name = "windows"
        elif sys.platform == "darwin":
            os_name = "macos"
        else:
            raise SystemExit(
                "Linux not supported by this CI helper "
                "(use the dev workflow's setup_*.sh scripts)."
            )

    print("NP Create -- CI tools setup")
    print(f"  os     : {os_name}")
    print(f"  cache  : {CACHE}")
    print(f"  target : {WORKSPACE / '.tools' / os_name}")

    CACHE.mkdir(parents=True, exist_ok=True)

    if "platform-tools" not in args.skip:
        install_platform_tools(os_name, args.force)
    if "jdk" not in args.skip:
        install_jdk(os_name, args.force)
    if "lspatch" not in args.skip:
        install_lspatch(os_name, args.force)

    rc = _verify(os_name)
    if rc == 0:
        print()
        print("Done. Tools ready for build_release.py / installer.iss / build_dmg.sh.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
