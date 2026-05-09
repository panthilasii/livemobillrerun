"""Hook-status probe -- TikTok install / patch / running detection.

The probe shells out to adb four times; we mock ``subprocess.run``
to return canned outputs that match what real Android phones
actually print, then verify the parsing + heuristics.

Coverage targets
----------------

* No TikTok variant installed → ``installed=False``, no error.
* TikTok installed but signed by Play Store cert → ``patched=False``.
* TikTok signed by LSPatch debug-keystore → ``patched=True``.
* ``pidof`` returning a numeric pid → ``running=True``.
* ``pidof`` exit code 1 with no output → ``running=False``.
* ADB completely unreachable → ``error`` populated, all bools False.
* ``HookStatus.label_th`` produces the right Thai label per state.
* ``HookStatus.color`` matches the theme palette.
* When multiple TikTok variants are installed, we pick the FIRST
  one in ``TIKTOK_PACKAGES`` order (stability guarantee for the
  badge -- otherwise the customer's badge would flicker between
  variants on each probe).
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from src import hook_status as hs


# ── helpers ─────────────────────────────────────────────────────


def _result(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["adb"], returncode=rc, stdout=stdout, stderr=stderr,
    )


class _Sequence:
    """Replay-by-call mock for ``subprocess.run``: each invocation
    pops the next canned response off the queue. Test failures
    surface immediately as 'unexpected adb call'."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if not self.queue:
            raise AssertionError(
                f"unexpected adb call: {args!r} (queue empty)"
            )
        nxt = self.queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


# ── public probe ────────────────────────────────────────────────


class TestProbeNoTikTok:
    def test_returns_not_installed(self):
        seq = _Sequence(
            _result(0, "package:com.android.systemui\npackage:com.google.android.gms\n"),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb", "ABC123")
        assert r.installed is False
        assert r.patched is False
        assert r.running is False
        assert r.error == ""


class TestProbeUnpatchedInstalled:
    def test_installed_signed_by_play_store(self):
        # Customer has TikTok but never patched it. Signing
        # fingerprint is whatever Google issued (definitely NOT
        # the LSPatch debug-keystore prefix).
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, "    signatures:[1234abcd5678]"),
            _result(0, "    versionName=29.5.3"),
            _result(1, ""),  # pidof: not running
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.installed
        assert r.package == "com.ss.android.ugc.trill"
        assert r.patched is False
        assert r.version_name == "29.5.3"
        assert r.running is False


class TestProbePatchedRunning:
    def test_lspatch_keystore_detected(self):
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, "    signatures:[e0b8d3e51f99]"),  # LSPatch prefix
            _result(0, "    versionName=29.5.3"),
            _result(0, "12345\n"),                          # pidof: pid 12345
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.installed
        assert r.patched is True
        assert r.running is True
        assert r.is_ready is True
        assert r.fingerprint.startswith("e0b8d3e5")


class TestProbePatchedNotRunning:
    def test_patched_but_user_hasnt_opened_tiktok(self):
        seq = _Sequence(
            _result(0, "package:com.ss.android.ugc.trill\n"),
            _result(0, "    signatures:[e0b8d3e5deadbeef]"),
            _result(0, "    versionName=29.5.3"),
            _result(1, ""),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.patched is True
        assert r.running is False
        assert r.is_ready is False


class TestProbeAdbTimeout:
    def test_timeout_returns_error_status(self):
        # Even the very first call (pm list packages) fails.
        seq = _Sequence(
            subprocess.TimeoutExpired(cmd="adb", timeout=6.0),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb", "OFFLINE")
        assert r.error
        assert r.installed is False


class TestProbeMultipleVariants:
    def test_picks_first_in_canonical_order(self):
        """Both Trill (Thai) and Lite installed -- we must pick
        Trill because it appears first in TIKTOK_PACKAGES."""
        seq = _Sequence(
            _result(0,
                "package:com.zhiliaoapp.musically.go\n"
                "package:com.ss.android.ugc.trill\n",
            ),
            _result(0, "    signatures:[deadcafe]"),
            _result(0, "    versionName=29.5.3"),
            _result(1, ""),
        )
        with patch.object(subprocess, "run", side_effect=seq):
            r = hs.probe("adb")
        assert r.package == "com.ss.android.ugc.trill"


# ── HookStatus presentation ─────────────────────────────────────


class TestLabelAndColor:
    def test_not_installed(self):
        s = hs.HookStatus()
        assert "ยังไม่ติดตั้ง" in s.label_th
        # Neutral gray
        assert s.color == "#7A7A88"

    def test_installed_unpatched_amber(self):
        s = hs.HookStatus(installed=True, package="com.ss.android.ugc.trill")
        assert "ยังไม่ Patch" in s.label_th
        assert s.color == "#FFB84D"

    def test_patched_not_running_amber(self):
        s = hs.HookStatus(
            installed=True, patched=True, version_name="29.5.3",
            package="com.ss.android.ugc.trill",
        )
        assert "รอเปิด TikTok" in s.label_th
        assert s.color == "#FFB84D"

    def test_patched_running_green(self):
        s = hs.HookStatus(
            installed=True, patched=True, running=True,
            version_name="29.5.3", package="com.ss.android.ugc.trill",
        )
        assert "vcam ทำงานอยู่" in s.label_th
        assert s.color == "#A6FF4D"
        assert s.is_ready is True

    def test_error_red(self):
        s = hs.HookStatus(error="adb timeout")
        assert "ตรวจสอบไม่ได้" in s.label_th
        assert s.color == "#FF5C5C"


# ── _parse_pm_list ──────────────────────────────────────────────


class TestParsePmList:
    def test_strips_prefix(self):
        out = hs._parse_pm_list(
            "package:com.x\npackage:com.y\nsomething else\n"
        )
        assert out == {"com.x", "com.y"}

    def test_handles_blank_lines(self):
        assert hs._parse_pm_list("") == set()
        assert hs._parse_pm_list("\n\n  \n") == set()

    def test_handles_extra_whitespace(self):
        out = hs._parse_pm_list("  package:com.foo  \n")
        assert out == {"com.foo"}
