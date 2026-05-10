"""Tests for v1.8.0's ``wifi_adb.adb_pair`` — Mode C Android 11+
wireless-debugging pair helper.

We never run a real ``adb pair`` here (no phone in CI); instead we
mock ``subprocess.run`` and assert on the command we'd have
invoked + how we parse adb's stdout/stderr replies.
"""

from __future__ import annotations

import subprocess
from unittest import mock

from src import wifi_adb


def _fake_run(stdout="", stderr="", returncode=0):
    """Build a CompletedProcess that mimics adb's ``pair`` reply."""
    return subprocess.CompletedProcess(
        args=["adb", "pair", "x"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestAdbPair:
    def test_success_message_returns_true(self):
        with mock.patch.object(
            wifi_adb.shutil, "which", return_value="/x/adb",
        ), mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stdout="Successfully paired to 192.168.1.42:38765 [guid=adb-...]"
            ),
        ) as m:
            ok, msg = wifi_adb.adb_pair(
                "/x/adb", "192.168.1.42", 38765, "123456",
            )
        assert ok is True
        assert "successfully paired" in msg.lower()
        # Verify the command line we built — pair takes the
        # IP:port target and reads the code from stdin.
        args = m.call_args
        assert args.args[0] == ["/x/adb", "pair", "192.168.1.42:38765"]
        assert args.kwargs.get("input") == "123456\n"

    def test_failure_message_returns_false(self):
        with mock.patch.object(
            wifi_adb.shutil, "which", return_value="/x/adb",
        ), mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stderr="Failed: Wrong pairing code", returncode=1,
            ),
        ):
            ok, msg = wifi_adb.adb_pair(
                "/x/adb", "192.168.1.42", 38765, "000000",
            )
        assert ok is False
        assert "wrong pairing code" in msg.lower()

    def test_missing_adb_short_circuits(self):
        with mock.patch.object(
            wifi_adb.shutil, "which", return_value=None,
        ):
            ok, msg = wifi_adb.adb_pair(
                "/missing/adb", "192.168.1.42", 38765, "123456",
            )
        assert ok is False
        assert "ไม่พบ adb" in msg

    def test_blank_code_rejected(self):
        with mock.patch.object(
            wifi_adb.shutil, "which", return_value="/x/adb",
        ):
            ok, msg = wifi_adb.adb_pair(
                "/x/adb", "192.168.1.42", 38765, "   ",
            )
        assert ok is False
        assert "pairing code" in msg.lower()

    def test_timeout_returns_friendly_message(self):
        with mock.patch.object(
            wifi_adb.shutil, "which", return_value="/x/adb",
        ), mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["adb", "pair"], timeout=30,
            ),
        ):
            ok, msg = wifi_adb.adb_pair(
                "/x/adb", "192.168.1.42", 38765, "123456",
            )
        assert ok is False
        assert "หมดเวลา" in msg or "timeout" in msg.lower()
