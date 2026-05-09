"""Windows-only launcher entrypoint (Thai-safe).

Why this module exists
----------------------
Windows ``cmd.exe`` parses ``.bat`` files using the **OEM codepage**
(usually CP874 on Thai locale, CP437 on English locale, etc.) — not
the codepage set by ``chcp``. ``chcp 65001`` only changes the
**output** codepage; the parser still reads bytes off disk via the
locale OEM codepage. That means any non-ASCII byte in ``run.bat``
gets garbled and the parser tries to execute the resulting tokens as
commands, producing errors like::

    '\\xe0\\xb8\\xa1' is not recognized as an internal or external command

The only reliable fix on Windows is to keep ``run.bat`` **strictly
ASCII** and move every Thai-language message into Python, which
opens files as UTF-8 explicitly and ignores the OEM codepage.

So ``run.bat`` does the bare minimum (find Python, hand off to here),
and this module:

1. Forces stdout/stderr to UTF-8 — matters on Python <3.13 where
   the default console encoder may still be cp874.
2. Runs ``pip install -r requirements.txt`` on first run, with a
   log file in ``%TEMP%`` for support.
3. Spawns the CTk Studio app via ``src.main --studio`` and exits.

If anything fails, we fall back to a tiny Tk dialog so the customer
still sees a Thai error even if the cmd window has already scrolled
the terminal output past view.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

# Ensure stdout/stderr accept UTF-8 (CTk + Thai labels rely on this).
# On older Python the default cmd encoder is cp874 and would crash
# the moment we print a Thai character.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - very old Python
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace"
    )


PROJECT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT / "requirements.txt"
INSTALL_FLAG = PROJECT / ".install_done"
INSTALL_LOG = Path(os.environ.get("TEMP", str(PROJECT))) / "NP-Create-install.log"


def _print_thai(*lines: str) -> None:
    """Print each line on its own — wrapper just makes call sites
    read like documentation."""
    for line in lines:
        print(line, flush=True)


def _show_error_dialog(title: str, message: str) -> None:
    """Last-resort Tk popup. We try Tk because cmd output may already
    be hidden if the user clicked Close, and customers do that."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        # If even Tk fails (no Tk, no display, etc.) we already
        # printed to stderr above — nothing more to do.
        pass


def _run_pip_install() -> int:
    """Install the runtime requirements once. Returns the pip exit
    code; 0 means success or already-installed."""
    if not REQUIREMENTS.is_file():
        # Requirements file missing means a malformed bundle. Skip
        # silently — the import error from `src.main` will surface
        # the real issue.
        return 0

    if INSTALL_FLAG.is_file():
        # We've completed install at least once on this machine.
        # ``pip install`` is a no-op on subsequent runs anyway, but
        # skipping the call entirely shaves ~3-5s off cold launch.
        return 0

    _print_thai(
        "",
        "  NP Create — กำลังติดตั้งส่วนประกอบครั้งแรก",
        "  (ใช้ Internet ประมาณ 30 วินาที — ห้ามปิดหน้าต่าง)",
        "",
    )

    cmd = [
        sys.executable, "-m", "pip", "install",
        "--upgrade", "--quiet", "--user",
        "-r", str(REQUIREMENTS),
    ]
    try:
        with INSTALL_LOG.open("w", encoding="utf-8") as fh:
            res = subprocess.run(
                cmd, stdout=fh, stderr=subprocess.STDOUT,
                check=False,
            )
    except OSError as exc:
        _print_thai(
            f"  [!] เรียก pip ไม่ได้: {exc}",
            "  ส่งข้อความนี้ให้แอดมินทาง Line ครับ",
        )
        return 1

    if res.returncode != 0:
        _print_thai(
            "",
            "  [!] ติดตั้งส่วนประกอบไม่สำเร็จ",
            f"  log อยู่ที่ {INSTALL_LOG}",
            "  ส่งไฟล์นี้ให้แอดมินทาง Line ได้เลยครับ",
            "",
        )
        return res.returncode

    # Drop a marker so subsequent launches skip pip entirely.
    try:
        INSTALL_FLAG.touch()
    except OSError:
        # Read-only install? Just keep running — pip will be a no-op
        # next time anyway because the packages are already in
        # site-packages.
        pass
    return 0


def _launch_studio() -> int:
    """Start the Tk Studio app in this same process so cmd window
    closes when the user closes the GUI."""
    # Make ``src`` importable.
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    try:
        from src import main as studio_main  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - import failure
        _print_thai(
            "",
            f"  [!] โหลดโปรแกรมไม่สำเร็จ: {exc}",
            "  ส่งข้อความนี้ให้แอดมินทาง Line ครับ",
        )
        _show_error_dialog(
            "NP Create",
            f"โหลดโปรแกรมไม่สำเร็จ\n\n{exc}\n\n"
            "ส่งข้อความนี้ให้แอดมินทาง Line ครับ",
        )
        return 2

    # ``main()`` may or may not return — wrap to surface any uncaught
    # exception via a Tk popup so Customers don't lose the message.
    try:
        rc = studio_main.main(["--studio"])
    except SystemExit as se:
        return int(getattr(se, "code", 0) or 0)
    except Exception as exc:  # pragma: no cover - runtime crash
        _print_thai(f"  [!] โปรแกรมหยุดทำงาน: {exc}")
        _show_error_dialog(
            "NP Create",
            f"โปรแกรมหยุดทำงาน\n\n{exc}",
        )
        return 3
    return int(rc or 0)


def main() -> int:
    rc = _run_pip_install()
    if rc != 0:
        return rc
    return _launch_studio()


if __name__ == "__main__":
    sys.exit(main())
