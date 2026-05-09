"""``src.live_control`` -- start_live / stop_live behaviour.

Network-free: we mock ``TikTokAutoController`` for ``start_live``
and ``subprocess.run`` for ``stop_live``, then verify:

* Successful Start Now produces ``ok=True`` with a Thai summary.
* A failed step at ``go_live`` surfaces a Thai-language hint that
  tells the customer their account isn't eligible (rather than
  a stack trace they can't read).
* Stop strategy escalates BACK → BACK+confirm → force-stop.
* ``format_elapsed`` produces ``MM:SS`` < 1h, ``H:MM:SS`` ≥ 1h.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src import live_control
from src.tiktok_controller import StepResult


# ── format_elapsed ──────────────────────────────────────────────


class TestFormatElapsed:
    @pytest.mark.parametrize("seconds,expected", [
        (0,     "00:00"),
        (5,     "00:05"),
        (59,    "00:59"),
        (60,    "01:00"),
        (95,    "01:35"),
        (599,   "09:59"),
        (3599,  "59:59"),
        (3600,  "1:00:00"),
        (3661,  "1:01:01"),
        (7200,  "2:00:00"),
        (90061, "25:01:01"),  # 25 hours -- broadcasts that long
                              # are "marathon live" content.
    ])
    def test_basic(self, seconds, expected):
        assert live_control.format_elapsed(seconds) == expected

    def test_negative_clamps(self):
        assert live_control.format_elapsed(-30) == "00:00"


# ── start_live ──────────────────────────────────────────────────


class TestStartLive:
    @staticmethod
    def _step(name: str, ok: bool, detail: str = "") -> StepResult:
        return StepResult(name=name, ok=ok, detail=detail)

    def test_full_success(self):
        ctrl = MagicMock()
        ctrl.run_to_screen_share.return_value = [
            self._step("find_package", True, "com.ss.android.ugc.trill"),
            self._step("launch", True),
            self._step("live_tab", True),
            self._step("go_live", True),
            self._step("screen_share", True),
            self._step("start_now", True),
        ]
        with patch("src.tiktok_controller.TikTokAutoController",
                   return_value=ctrl):
            r = live_control.start_live("adb", "ABC")
        assert r.ok
        assert "เริ่มไลฟ์เรียบร้อย" in r.summary
        ctrl.run_to_screen_share.assert_called_once_with(confirm_start=True)

    def test_partial_success_returns_false(self):
        """Stopped at screen_share -- the customer's account
        likely doesn't have the screen-share toggle. We still
        return ``ok=False`` so the timer doesn't start."""
        ctrl = MagicMock()
        ctrl.run_to_screen_share.return_value = [
            self._step("find_package", True),
            self._step("launch", True),
            self._step("live_tab", True),
            self._step("go_live", True),
            self._step("screen_share", False, "not found"),
        ]
        with patch("src.tiktok_controller.TikTokAutoController",
                   return_value=ctrl):
            r = live_control.start_live("adb", "ABC")
        assert not r.ok
        assert "Screen Share" in r.summary

    def test_no_tiktok_installed_clear_message(self):
        ctrl = MagicMock()
        ctrl.run_to_screen_share.return_value = [
            self._step("find_package", False, "no TikTok variant installed"),
        ]
        with patch("src.tiktok_controller.TikTokAutoController",
                   return_value=ctrl):
            r = live_control.start_live("adb", "ABC")
        assert not r.ok
        assert "ไม่พบ TikTok" in r.summary

    def test_account_ineligible_says_so(self):
        ctrl = MagicMock()
        ctrl.run_to_screen_share.return_value = [
            self._step("find_package", True),
            self._step("launch", True),
            self._step("live_tab", True),
            self._step("go_live", False, "Go-Live button not found"),
        ]
        with patch("src.tiktok_controller.TikTokAutoController",
                   return_value=ctrl):
            r = live_control.start_live("adb", "ABC")
        assert "ไลฟ์ไม่ได้" in r.summary

    def test_controller_crash_surfaces_as_failure(self):
        ctrl = MagicMock()
        ctrl.run_to_screen_share.side_effect = RuntimeError("boom")
        with patch("src.tiktok_controller.TikTokAutoController",
                   return_value=ctrl):
            r = live_control.start_live("adb", "ABC")
        assert not r.ok
        assert "boom" in r.summary


# ── stop_live ───────────────────────────────────────────────────


def _result(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["adb"], returncode=rc, stdout=out, stderr=err,
    )


class _FakeRun:
    """Sequenced ``subprocess.run`` mock. ``stop_live`` shells out
    several times in a row -- BACK key, dump, cat, tap, etc. We
    feed canned responses by index."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if not self.queue:
            return _result(0, "")  # silent success for trailing calls
        return self.queue.pop(0)


class TestStopLiveStrategies:
    def test_first_back_finds_confirm_button(self):
        """Strategy 1 (BACK + tap End) succeeds on the first try.
        Sequence: BACK, dump, cat (returns XML with End button),
        tap, ..."""
        xml = (
            '<node text="End live?" bounds="[100,100][300,200]" />'
            '<node text="ยืนยัน" bounds="[400,500][800,700]" />'
        )
        seq = _FakeRun(
            _result(0),                # BACK
            _result(0),                # uiautomator dump
            _result(0, xml),           # cat
            _result(0),                # tap End
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = live_control.stop_live(
                "adb", "ABC", "com.ss.android.ugc.trill",
            )
        assert r.ok
        assert r.strategy == "back+confirm"
        # Verify we tapped the centre of the "ยืนยัน" button.
        tap_args = seq.calls[3]
        assert "tap" in tap_args
        assert "600" in tap_args   # (400+800)/2
        assert "600" in tap_args   # (500+700)/2

    def test_force_stop_when_no_dialog_visible(self):
        """No End button found anywhere -- strategy 3 (am
        force-stop) must run."""
        empty_xml = "<node />"
        seq = _FakeRun(
            _result(0),                # 1st BACK
            _result(0),                # dump 1
            _result(0, empty_xml),     # cat 1
            _result(0),                # 2nd BACK
            _result(0),                # dump 2
            _result(0, empty_xml),     # cat 2
            _result(0),                # am force-stop
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = live_control.stop_live(
                "adb", "ABC", "com.ss.android.ugc.trill",
                settle_s=0,
            )
        assert r.ok
        assert r.strategy == "force_stop"
        # Verify the force-stop call hit the right package.
        force_call = seq.calls[-1]
        assert "force-stop" in force_call
        assert "com.ss.android.ugc.trill" in force_call

    def test_total_failure_returns_not_ok(self):
        """Even force-stop fails (e.g. device went offline mid-
        stop). Must NOT raise; surface a clear error."""
        empty_xml = "<node />"
        seq = _FakeRun(
            _result(0), _result(0), _result(0, empty_xml),
            _result(0), _result(0), _result(0, empty_xml),
            _result(1, "", "device not found"),  # force-stop fails
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = live_control.stop_live(
                "adb", "ABC", "com.ss.android.ugc.trill",
                settle_s=0,
            )
        assert not r.ok
        assert "ปิดไลฟ์ไม่สำเร็จ" in r.summary
