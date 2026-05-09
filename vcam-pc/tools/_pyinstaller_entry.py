"""PyInstaller entry stub for NP Create.

PyInstaller resolves the entry script at build time and embeds its
``__main__`` block into the bootloader. We need a tiny shim so that
when the customer double-clicks ``NP-Create.exe`` / ``NP-Create.app``:

1. ``--studio`` is forced on (we never want the CLI behind a GUI
   binary — there's no terminal to talk to).
2. ``sys.path`` includes the bundled ``src/`` package, regardless
   of where PyInstaller chose to lay the resources out.
3. Any uncaught exception during startup ends up in a Tkinter
   dialog instead of vanishing into a closed console (very common
   PyInstaller usability gotcha — without this the customer sees
   "the icon flashed and nothing happened").
"""

from __future__ import annotations

import os
import sys
import traceback


def _ensure_src_on_path() -> None:
    # When frozen, PyInstaller sets ``sys._MEIPASS`` to the temp
    # extraction dir. The ``src`` package was added via
    # ``--add-data <repo>/src:src`` so it lands at MEIPASS/src.
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        # Source mode (running from `python tools/_pyinstaller_entry.py`
        # for testing). Fall back to the project root one level up.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if base not in sys.path:
        sys.path.insert(0, base)


def _show_fatal(text: str) -> None:
    """Last-ditch error dialog when even the GUI failed to start."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("NP Create — เกิดข้อผิดพลาด", text)
        root.destroy()
    except Exception:
        # No GUI possible. Print to stderr — at least cmd.exe in
        # debug mode will show it.
        print(text, file=sys.stderr)


def main() -> int:
    _ensure_src_on_path()
    try:
        from src.main import main as real_main  # noqa: WPS433
    except Exception:
        _show_fatal(
            "ไม่สามารถโหลดโปรแกรมได้ครับ\n\n"
            f"{traceback.format_exc()}\n\n"
            "กรุณาส่งข้อความนี้ให้แอดมินทาง Line @npcreate"
        )
        return 1

    # Inject --studio if the user (or Finder/Explorer) didn't pass
    # any args. PyInstaller forwards launch args verbatim.
    if not any(a.startswith("--") for a in sys.argv[1:]):
        sys.argv.append("--studio")

    try:
        return real_main() or 0
    except SystemExit:
        raise
    except Exception:
        _show_fatal(
            "โปรแกรมขัดข้องระหว่างเปิด:\n\n"
            f"{traceback.format_exc()}\n\n"
            "ส่งข้อความนี้ให้แอดมินทาง Line @npcreate"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
