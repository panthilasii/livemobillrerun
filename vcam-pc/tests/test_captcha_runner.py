"""``src.captcha.solver`` orchestration + ``runner`` loop lifecycle."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from src.captcha import runner, solver  # noqa: E402
from src.captcha.models import SolvePlan  # noqa: E402


# ── solve_once orchestration ─────────────────────────────────────

def _img():
    return Image.new("RGB", (400, 800), (30, 30, 30))


def test_solve_once_no_captcha():
    with patch("src.captcha.solver.capture.grab_screen", return_value=_img()), \
         patch("src.captcha.solver.detect_cv.detect", return_value=None):
        out = solver.solve_once(adb_path="adb", adb_serial="S")
    assert out.status == "no_captcha"


def test_solve_once_capture_error():
    with patch("src.captcha.solver.capture.grab_screen", return_value=None):
        out = solver.solve_once(adb_path="adb", adb_serial="S")
    assert out.status == "error"


def test_solve_once_solved_when_gone_after_drag():
    plan = SolvePlan(src=(60, 700), dst=(250, 700))
    # First detect finds the captcha; the post-drag re-check finds nothing.
    detect_results = [plan, None]

    def _detect(_img):
        return detect_results.pop(0)

    with patch("src.captcha.solver.capture.grab_screen", return_value=_img()), \
         patch("src.captcha.solver.detect_cv.detect", side_effect=_detect), \
         patch("src.captcha.solver.drag.perform_drag", return_value=True) as pd:
        out = solver.solve_once(
            adb_path="adb", adb_serial="S", verify_delay=0, _sleep=lambda s: None
        )
    assert out.status == "solved"
    assert pd.called


def test_solve_once_failed_when_still_visible():
    plan = SolvePlan(src=(60, 700), dst=(250, 700))
    with patch("src.captcha.solver.capture.grab_screen", return_value=_img()), \
         patch("src.captcha.solver.detect_cv.detect", return_value=plan), \
         patch("src.captcha.solver.drag.perform_drag", return_value=True):
        out = solver.solve_once(
            adb_path="adb", adb_serial="S", verify_delay=0, _sleep=lambda s: None
        )
    assert out.status == "failed"


def test_solve_once_falls_back_to_gemini():
    plan = SolvePlan(src=(60, 700), dst=(250, 700), source="gemini")
    with patch("src.captcha.solver.capture.grab_screen", return_value=_img()), \
         patch("src.captcha.solver.detect_cv.detect", return_value=None), \
         patch("src.captcha.solver.gemini.locate", return_value=plan) as g, \
         patch("src.captcha.solver.drag.perform_drag", return_value=True):
        out = solver.solve_once(
            adb_path="adb", adb_serial="S", gemini_api_key="key",
            verify_delay=0, _sleep=lambda s: None,
        )
    # CV returned None -> gemini consulted (detect + verify = 2 calls).
    assert g.called
    assert out.status in {"solved", "failed"}


def test_gemini_not_called_without_key():
    with patch("src.captcha.solver.capture.grab_screen", return_value=_img()), \
         patch("src.captcha.solver.detect_cv.detect", return_value=None), \
         patch("src.captcha.solver.gemini.locate") as g:
        out = solver.solve_once(adb_path="adb", adb_serial="S")
    assert not g.called
    assert out.status == "no_captcha"


# ── runner lifecycle ─────────────────────────────────────────────

def test_registry_start_stop():
    reg = runner.AutoSolveRegistry()
    calls = {"n": 0}

    def _fake_solve(**kw):
        calls["n"] += 1
        from src.captcha.models import SolveOutcome
        return SolveOutcome(status="no_captcha")

    with patch("src.captcha.runner.solver.solve_once", side_effect=_fake_solve):
        started = reg.start(
            "SER", adb_path="adb", adb_serial="adbid", poll_s=1.0,
        )
        assert started is True
        # Starting again while alive is a no-op.
        assert reg.start("SER", adb_path="adb", adb_serial="adbid") is False
        # Give the loop a moment to spin at least one iteration.
        time.sleep(0.2)
        assert reg.is_running("SER")
        assert "SER" in reg.running_serials()
        reg.stop("SER")
        # The cooperative stop wakes the loop's ``Event.wait`` instantly.
        deadline = time.monotonic() + 2.0
        while reg.is_running("SER") and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not reg.is_running("SER")
    assert calls["n"] >= 1


def test_registry_stop_all():
    reg = runner.AutoSolveRegistry()
    from src.captcha.models import SolveOutcome
    with patch(
        "src.captcha.runner.solver.solve_once",
        return_value=SolveOutcome(status="no_captcha"),
    ):
        reg.start("A", adb_path="adb", adb_serial="a", poll_s=1.0)
        reg.start("B", adb_path="adb", adb_serial="b", poll_s=1.0)
        time.sleep(0.1)
        assert set(reg.running_serials()) == {"A", "B"}
        reg.stop_all()
        deadline = time.monotonic() + 2.0
        while reg.running_serials() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert reg.running_serials() == []
