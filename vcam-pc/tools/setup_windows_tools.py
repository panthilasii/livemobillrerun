#!/usr/bin/env python3
"""NP Create — admin helper: populate ``.tools/windows/``.

Downloads (or hard-links) the three things the Windows customer
ZIP needs:

* JDK 21 (Adoptium Temurin Windows x64) — ~190 MB
* Android platform-tools Windows zip      — ~17 MB
* lspatch.jar (cross-platform; copied from .tools/macos)

After this script the admin can run::

    python tools/build_release.py --target customer --os windows

…and the resulting zip will be a self-contained ~400 MB bundle.

Re-runnable: skips already-downloaded artifacts. Pass ``--force``
to redownload.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WORKSPACE = PROJECT.parent

WIN_TOOLS = WORKSPACE / ".tools" / "windows"

# Pinned URLs — bumping these is a deliberate admin action.
JDK_URL = (
    "https://github.com/adoptium/temurin21-binaries/releases/"
    "download/jdk-21.0.5%2B11/"
    "OpenJDK21U-jdk_x64_windows_hotspot_21.0.5_11.zip"
)
PT_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"


def _download(url: str, dst: Path, force: bool = False) -> Path:
    if dst.is_file() and not force:
        print(f"  ✓ cached {dst.name} ({dst.stat().st_size / 1024 / 1024:,.1f} MB)")
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  → downloading {url}")
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f, length=1 << 20)
    tmp.replace(dst)
    print(f"  ✓ wrote {dst.name} ({dst.stat().st_size / 1024 / 1024:,.1f} MB)")
    return dst


def _extract_zip(zpath: Path, into: Path, strip_first_dir: bool = False) -> None:
    """Unpack ``zpath`` into ``into``. If ``strip_first_dir`` is True,
    drop the single top-level directory inside the archive (matches
    the Adoptium layout: jdk-21.0.5+11/{bin,lib,…})."""
    into.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        if strip_first_dir:
            top = {n.split("/", 1)[0] for n in zf.namelist() if n.strip()}
            assert len(top) == 1, f"expected single top dir, got {top}"
            top_name = next(iter(top))
        else:
            top_name = ""
        for member in zf.infolist():
            name = member.filename
            if strip_first_dir:
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--force", action="store_true",
                   help="redownload even if files already cached")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    cache = WORKSPACE / ".cache" / "windows-tools"
    cache.mkdir(parents=True, exist_ok=True)

    print("NP Create — Windows tools setup")
    print(f"  target : {WIN_TOOLS}")

    # ── JDK ──────────────────────────────────────────────────────
    jdk_dest = WIN_TOOLS / "jdk-21"
    if jdk_dest.is_dir() and not args.force:
        print(f"  ✓ JDK 21 already at {jdk_dest}")
    else:
        jdk_zip = _download(
            JDK_URL, cache / "jdk21-windows-x64.zip", args.force,
        )
        if jdk_dest.is_dir():
            shutil.rmtree(jdk_dest)
        print("  → unpacking JDK")
        _extract_zip(jdk_zip, jdk_dest, strip_first_dir=True)

    # ── platform-tools (adb.exe) ────────────────────────────────
    pt_dest = WIN_TOOLS / "platform-tools"
    if pt_dest.is_dir() and not args.force:
        print(f"  ✓ platform-tools already at {pt_dest}")
    else:
        pt_zip = _download(
            PT_URL, cache / "platform-tools-windows.zip", args.force,
        )
        if pt_dest.is_dir():
            shutil.rmtree(pt_dest)
        print("  → unpacking platform-tools")
        _extract_zip(pt_zip, pt_dest.parent, strip_first_dir=False)
        # The zip contains a single "platform-tools/" top dir which
        # is exactly what we want at WIN_TOOLS — no rename needed.

    # ── lspatch.jar (cross-platform) ────────────────────────────
    src_jar = WORKSPACE / ".tools" / "macos" / "lspatch" / "lspatch.jar"
    if not src_jar.is_file():
        src_jar = WORKSPACE / ".tools" / "lspatch" / "lspatch.jar"
    dst_jar = WIN_TOOLS / "lspatch" / "lspatch.jar"
    dst_jar.parent.mkdir(parents=True, exist_ok=True)
    if src_jar.is_file():
        if not dst_jar.is_file() or args.force:
            shutil.copy2(src_jar, dst_jar)
            print(f"  ✓ copied lspatch.jar from {src_jar}")
        else:
            print("  ✓ lspatch.jar already present")
    else:
        print(
            "  ✗ lspatch.jar not found in .tools/.\n"
            "    Run vcam-pc/tools/install_lspatch.sh first.",
            file=sys.stderr,
        )
        return 3

    # ── verify ──────────────────────────────────────────────────
    print()
    print("Verification:")
    java_exe = jdk_dest / "bin" / "java.exe"
    adb_exe = pt_dest / "adb.exe"
    for label, path in [
        ("java.exe   ", java_exe),
        ("adb.exe    ", adb_exe),
        ("lspatch.jar", dst_jar),
    ]:
        mark = "✓" if path.is_file() else "✗"
        print(f"  {mark} {label} {path}")

    print()
    print("Now you can run:")
    print("  python tools/build_release.py --target customer --os windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
