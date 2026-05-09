#!/usr/bin/env python3
"""NP Create — admin helper: package the app as a single-file
``NP-Create.exe`` (Windows) or ``NP-Create.app`` (macOS) so the
customer doesn't need to install Python at all.

Why we keep the .bat / .command launcher *as well* as PyInstaller
================================================================

* PyInstaller bundle is ~80 MB on top of the existing 480 MB tools
  zip (so cost is real). It's worth that cost when the customer is
  the kind of person who can't install Python — but for power users
  it's just dead weight.
* PyInstaller frequently flags as "Trojan:Win32/Wacatac" in Windows
  Defender for the first ~24 h of distribution because it's an
  uncommon binary signed by no one. The launcher avoids this.
* CustomTkinter resources (themes, fonts) need explicit ``--add-data``
  pickups; getting that right takes a build pass per OS.

So we ship **both**:

* `run.bat` / `run.command` — primary, lightweight, transparent.
* `NP-Create.exe` / `NP-Create.app` — fallback, "just double-click".

This script handles the second one. Run it on the target OS
(cross-build is not supported by PyInstaller).

Usage::

    python tools/build_pyinstaller.py             # current OS
    python tools/build_pyinstaller.py --onedir    # less Defender-flagged

Outputs into ``vcam-pc/dist/pyinstaller/<NP-Create.app|exe>``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
WORKSPACE = PROJECT.parent

OUT_DIR = PROJECT / "dist" / "pyinstaller"
BUILD_DIR = PROJECT / "build" / "pyinstaller"
SPEC_DIR = HERE / "_pyinstaller"


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[i] PyInstaller not installed; installing now…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user",
             "pyinstaller>=6.5"],
            check=True,
        )


def _platform_args() -> list[str]:
    """Per-OS PyInstaller knobs we always want."""
    if sys.platform == "win32":
        # --noconsole  : no flash of cmd.exe behind the GUI
        # --uac-admin  : ADB drivers sometimes require it on Windows 11
        return ["--noconsole"]
    if sys.platform == "darwin":
        # --windowed   : produces a proper .app, not a CLI
        # --osx-bundle-identifier : avoids "no identifier" Gatekeeper flags
        return [
            "--windowed",
            "--osx-bundle-identifier", "com.npcreate.studio",
        ]
    return ["--windowed"]


def _add_data_args() -> list[str]:
    """Files we need bundled into the .app/.exe alongside Python.

    Format on Windows: 'src;dst', on macOS/Linux: 'src:dst'
    (PyInstaller hard-codes this OS-specific separator — yes, really.)

    What we DO bundle into the binary (read-only assets accessible
    via ``sys._MEIPASS``):

    * ``assets/`` — logos, icons, theme images. Always small (~MB).
    * ``src/_pubkey.py`` — the embedded Ed25519 license-key public
      key. The customer must not be able to swap this; bundling
      makes the exe tamper-evident.

    What we DELIBERATELY DO NOT bundle:

    * ``.tools/`` (~400 MB on macOS, ~250 MB on Windows) — adb,
      JDK 21, ffmpeg, lspatch, scrcpy. These ride alongside the
      binary instead, dropped there by the platform installer
      (Inno Setup on Windows, ``build_dmg.sh`` on macOS). Three
      reasons:

      1. PyInstaller --onefile would inflate the .exe to >300 MB
         and force a multi-second startup as it extracts the
         bundle to %TEMP% on every launch.
      2. Antivirus heuristics flag big PyInstaller blobs as
         "Trojan:Win32/Wacatac"; smaller binaries scan faster
         and trigger fewer false positives.
      3. Tools change cadence is independent of app code —
         shipping them as a sibling lets us refresh adb / scrcpy
         without rebuilding the .exe.

    * ``apk/`` (~80 MB) — the prebuilt vcam-app APK. Same logic:
      it's a data payload, not an executable resource.

    Both ``.tools/`` and ``apk/`` are picked up at runtime by
    ``platform_tools.find_*`` resolvers anchored on
    ``PROJECT_ROOT`` (= ``Path(sys.executable).parent`` in frozen
    mode — see ``src/config.py``). The Inno Setup script
    (``tools/installer.iss``) and the .dmg builder
    (``tools/build_dmg.sh``) are responsible for placing them.
    """
    sep = ";" if sys.platform == "win32" else ":"
    items: list[tuple[Path, str]] = []
    assets = PROJECT / "assets"
    if assets.is_dir():
        items.append((assets, "assets"))
    pubkey = PROJECT / "src" / "_pubkey.py"
    if pubkey.is_file():
        items.append((pubkey, "src"))
    out: list[str] = []
    for src, dst in items:
        out.extend(["--add-data", f"{src}{sep}{dst}"])
    return out


def _hidden_imports() -> list[str]:
    """Modules PyInstaller can't auto-detect (dynamic imports etc.)."""
    return [
        "--hidden-import", "customtkinter",
        "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "src._ed25519",
        "--hidden-import", "src._pubkey",
        "--collect-data", "customtkinter",
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--onedir", action="store_true",
        help="produce a folder bundle instead of a single-file exe "
             "(less likely to be flagged by AV, faster startup)",
    )
    p.add_argument(
        "--clean", action="store_true",
        help="wipe build/ and dist/pyinstaller/ before building",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    _ensure_pyinstaller()

    if args.clean:
        for d in (BUILD_DIR, OUT_DIR):
            if d.is_dir():
                shutil.rmtree(d)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    icon_arg: list[str] = []
    if sys.platform == "win32":
        ico = PROJECT / "assets" / "logo.ico"
        if ico.is_file():
            icon_arg = ["--icon", str(ico)]
    elif sys.platform == "darwin":
        icns = PROJECT / "assets" / "logo.icns"
        if icns.is_file():
            icon_arg = ["--icon", str(icns)]
        else:
            # Fallback: use a 256-px PNG. PyInstaller will accept
            # PNG on macOS but the result looks blurry on Retina —
            # build a real .icns once you have time. See the
            # Apple `iconutil` man page.
            png = PROJECT / "assets" / "logo_256.png"
            if png.is_file():
                icon_arg = ["--icon", str(png)]

    name = "NP-Create"
    # PyInstaller 6.x deprecated --onefile + --windowed on macOS
    # because a .app *is* a directory bundle by definition. Force
    # onedir on macOS regardless of --onedir flag, but keep --onefile
    # honored on Windows where it produces a single .exe blob users
    # can drag onto the desktop.
    use_onedir = args.onedir or sys.platform == "darwin"
    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--name", name,
        ("--onedir" if use_onedir else "--onefile"),
        "--distpath", str(OUT_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        *_platform_args(),
        *_add_data_args(),
        *_hidden_imports(),
        *icon_arg,
        "--paths", str(PROJECT),
        # Entry point: a thin wrapper that flips --studio on so the
        # customer always lands in the GUI, never the CLI.
        str(HERE / "_pyinstaller_entry.py"),
    ]
    print("-> running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=PROJECT)

    print()
    print("Done. Output:")
    if sys.platform == "darwin":
        app = OUT_DIR / f"{name}.app"
        print(f"  • {app} (drag to Applications)")
    elif sys.platform == "win32":
        ext = "" if args.onedir else ".exe"
        print(f"  • {OUT_DIR / (name + ext)}")
    else:
        print(f"  • {OUT_DIR / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
