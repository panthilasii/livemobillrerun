"""Grab the phone's framebuffer over ADB as a Pillow image.

We use ``adb -s <serial> exec-out screencap -p`` rather than the
classic ``adb shell screencap -p > file`` because ``exec-out``
streams the PNG bytes back **without** the shell's line-ending
translation. The legacy ``shell`` form mangles every ``0x0A`` into
``0x0D 0x0A`` on some Windows adb builds, producing a corrupt PNG —
the exact same trap the APK pull ladder in ``lspatch_pipeline.py``
avoids by switching to ``exec-out cat``.
"""

from __future__ import annotations

import io
import logging
import subprocess
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)


def grab_screen(
    adb_path: str,
    adb_serial: str,
    *,
    timeout: float = 10.0,
) -> Optional[Image.Image]:
    """Capture the current phone screen.

    Returns a decoded RGB :class:`PIL.Image.Image`, or ``None`` if the
    capture or decode failed (device gone, adb error, non-PNG output).
    Never raises — callers run this in a tight poll loop and a single
    bad frame must not kill the loop.
    """
    raw = grab_screen_png(adb_path, adb_serial, timeout=timeout)
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("RGB")
    except Exception:
        log.debug("screencap PNG decode failed (%d bytes)", len(raw), exc_info=True)
        return None


def grab_screen_png(
    adb_path: str,
    adb_serial: str,
    *,
    timeout: float = 10.0,
) -> Optional[bytes]:
    """Return the raw PNG bytes from ``screencap -p`` (or ``None``)."""
    args = [adb_path]
    if adb_serial:
        args += ["-s", adb_serial]
    args += ["exec-out", "screencap", "-p"]
    try:
        r = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        log.debug("screencap subprocess failed", exc_info=True)
        return None
    if r.returncode != 0:
        log.debug("screencap rc=%s err=%r", r.returncode, (r.stderr or b"")[:120])
        return None
    data = r.stdout or b""
    # A valid PNG always starts with this 8-byte signature; bail early
    # on the occasional empty / truncated capture so the PIL decoder
    # doesn't log a noisy stack trace.
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        log.debug("screencap returned non-PNG payload (%d bytes)", len(data))
        return None
    return data


__all__ = ["grab_screen", "grab_screen_png"]
