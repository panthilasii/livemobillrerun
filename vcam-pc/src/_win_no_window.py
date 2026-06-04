"""Kill the Windows ``cmd.exe`` console flash for every child process.

The problem
-----------
On Windows, when a **GUI** app (no console of its own) launches a
*console-subsystem* executable like ``adb.exe`` or ``ffmpeg.exe`` via
``subprocess``, Windows allocates a brand-new console window for the
child unless we explicitly say not to. NP Create polls ``adb devices``
every ~2 seconds and fires ``adb push`` / ``adb shell screencap`` /
``ffmpeg`` constantly, so the customer sees a black console window
**pop up and vanish over and over** ("จอการทำงาน adb ชอบเด้ง"). It looks
broken even though it isn't, and on slower machines the flicker steals
focus from the live session.

The fix
-------
Set the ``CREATE_NO_WINDOW`` creation flag on every child. Rather than
edit ~80 individual ``subprocess.run`` / ``subprocess.Popen`` call sites
(and risk missing future ones), we wrap ``subprocess.Popen.__init__``
once at startup so the flag is merged into *every* spawn. ``run``,
``call``, ``check_output`` all funnel through ``Popen``, so this covers
them too.

No-op on macOS / Linux (the flag and the problem are Windows-only).
"""
from __future__ import annotations

import subprocess
import sys

# Win32 ``CREATE_NO_WINDOW``. Defined on ``subprocess`` since Py3.7 on
# Windows; we hard-code the value as a fallback so this module imports
# cleanly on non-Windows hosts (where the attribute doesn't exist).
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_installed = False


def merge_flags(creationflags: int | None) -> int:
    """OR ``CREATE_NO_WINDOW`` into an existing ``creationflags`` value.

    Idempotent — calling it on a value that already has the bit set
    returns the same value. Safe to apply to the rare call site
    (``rtmp_server``) that already passes the flag explicitly.
    """
    return (creationflags or 0) | CREATE_NO_WINDOW


def install() -> bool:
    """Patch ``subprocess.Popen`` so every child gets ``CREATE_NO_WINDOW``.

    Returns ``True`` if the patch was applied, ``False`` if it was a
    no-op (non-Windows, or already installed). Idempotent.
    """
    global _installed
    if _installed:
        return False
    if sys.platform != "win32":
        return False

    _orig_init = subprocess.Popen.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["creationflags"] = merge_flags(kwargs.get("creationflags", 0))
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _patched_init  # type: ignore[assignment]
    _installed = True
    return True
