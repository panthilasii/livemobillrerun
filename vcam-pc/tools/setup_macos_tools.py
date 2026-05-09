#!/usr/bin/env python3
"""NP Create — admin helper: populate ``.tools/macos/`` with the
adb / JDK / lspatch combo so the customer macOS bundle is fully
self-contained, mirroring what ``setup_windows_tools.py`` does for
Windows.

Why mirroring is needed
-----------------------

Historically the macOS dev box stored everything under the *flat*
legacy layout::

    .tools/
        android-sdk/platform-tools/adb
        jdk-21/Contents/Home/bin/java
        lspatch/lspatch.jar

…because that's where the old `install_*.sh` scripts dropped them.
After the cross-platform refactor (v1.3) we standardised on::

    .tools/<os>/
        platform-tools/adb
        jdk-21/Contents/Home/bin/java
        lspatch/lspatch.jar
        ffmpeg

The build script *only* ships files under ``.tools/<os>/`` to keep
the customer zip small. So if we leave the legacy layout in place,
the macOS customer bundle ends up missing adb/JDK/lspatch entirely.

This script bridges the two: it symlinks (or copies, if symlinks
fail across volumes) the existing macOS tools into the new layout.
Idempotent — re-runnable without harm.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WORKSPACE = PROJECT.parent

TOOLS_LEGACY = WORKSPACE / ".tools"
TOOLS_MACOS = WORKSPACE / ".tools" / "macos"


# (legacy source, dest under .tools/macos/)
LINKS = [
    ("android-sdk/platform-tools", "platform-tools"),
    ("jdk-21",                     "jdk-21"),
    ("lspatch",                    "lspatch"),
]


def _link(src: Path, dst: Path, force: bool) -> None:
    if dst.exists() or dst.is_symlink():
        if not force:
            print(f"  ✓ already present: {dst.relative_to(WORKSPACE)}")
            return
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Symlink first — instant, no disk-space cost.
        dst.symlink_to(src.resolve(), target_is_directory=src.is_dir())
        print(f"  ✓ symlinked {dst.relative_to(WORKSPACE)} → "
              f"{src.relative_to(WORKSPACE)}")
    except OSError:
        # Symlinks across volumes/quirky filesystems can fail;
        # fall back to a real copy. Slower but always works.
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        print(f"  ✓ copied  {dst.relative_to(WORKSPACE)} (symlink unavailable)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--force", action="store_true",
                   help="recreate existing links/copies")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    print("NP Create — macOS tools setup")
    print(f"  legacy : {TOOLS_LEGACY}")
    print(f"  target : {TOOLS_MACOS}")
    TOOLS_MACOS.mkdir(parents=True, exist_ok=True)

    for src_rel, dst_rel in LINKS:
        src = TOOLS_LEGACY / src_rel
        dst = TOOLS_MACOS / dst_rel
        if not src.exists():
            print(f"  ✗ {src.relative_to(WORKSPACE)} not found — skipping")
            continue
        _link(src, dst, args.force)

    # Sanity check the resolver finds everything we need.
    sys.path.insert(0, str(PROJECT))
    from src.platform_tools import discover, current_os  # noqa: E402

    print()
    print(f"Resolver verification (current OS = {current_os()}):")
    p = discover()
    for label, val in [
        ("adb         ", p.adb),
        ("ffmpeg      ", p.ffmpeg),
        ("java (JDK21)", p.java),
        ("lspatch.jar ", p.lspatch_jar),
        ("vcam-app.apk", p.vcam_apk),
    ]:
        mark = "✓" if val else "✗"
        print(f"  {mark} {label} {val or '(missing)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
