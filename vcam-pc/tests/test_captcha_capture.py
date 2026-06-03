"""``src.captcha.capture`` — screencap parsing via mocked adb."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from src.captcha import capture  # noqa: E402


def _png_bytes(w: int = 32, h: int = 48, color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_grab_screen_decodes_png():
    png = _png_bytes(40, 60)
    with patch("subprocess.run", return_value=_completed(0, png)) as m:
        img = capture.grab_screen("/usr/bin/adb", "ABC123")
    assert img is not None
    assert img.size == (40, 60)
    # serial must be threaded through as ``-s`` and the safe exec-out form used.
    argv = m.call_args[0][0]
    assert "-s" in argv and "ABC123" in argv
    assert argv[-3:] == ["exec-out", "screencap", "-p"]


def test_grab_screen_rejects_non_png():
    with patch("subprocess.run", return_value=_completed(0, b"not-a-png")):
        assert capture.grab_screen("/usr/bin/adb", "X") is None


def test_grab_screen_handles_adb_error():
    with patch("subprocess.run", return_value=_completed(1, b"", b"device offline")):
        assert capture.grab_screen("/usr/bin/adb", "X") is None


def test_grab_screen_handles_timeout():
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=10),
    ):
        assert capture.grab_screen("/usr/bin/adb", "X") is None
