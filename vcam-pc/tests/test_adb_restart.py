"""Tests for v1.8.0's ``AdbController.restart_server`` helper.

Why we test this in isolation
-----------------------------
The wizard's "🔄 รีสตาร์ท ADB" button is the customer's escape hatch
for the most common Windows ADB sticking-point: a stale daemon that
won't transition the phone out of ``unauthorized`` even after the
customer taps Allow. If ``restart_server`` returns False on the
happy path, or worse silently swallows a failed kill-server, the
button looks broken to customers and they're back to "เสียบสาย กด
Allow แล้วระบบไม่เชื่อมโทรศัพท์".
"""

from __future__ import annotations

import subprocess
from unittest import mock

from src import adb as adb_mod


def _ok(stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["adb", "x"], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stdout="", stderr="oops", returncode=1):
    return subprocess.CompletedProcess(
        args=["adb", "x"], returncode=returncode,
        stdout=stdout, stderr=stderr,
    )


class TestRestartServer:
    def _ctl(self):
        # Bypass the resolver — we don't want to depend on a real
        # adb being on PATH in CI. The methods we test only call
        # ``self._run`` which we mock anyway.
        c = adb_mod.AdbController.__new__(adb_mod.AdbController)
        c.adb_path = "/x/adb"
        return c

    def test_happy_path(self):
        c = self._ctl()
        with mock.patch.object(c, "_run") as run:
            run.side_effect = [
                _ok(stdout=""),                          # kill-server
                _ok(stdout="* daemon started successfully"),  # start-server
            ]
            assert c.restart_server() is True
            assert run.call_args_list[0].args == ("kill-server",)
            assert run.call_args_list[1].args == ("start-server",)

    def test_kill_server_failure_does_not_abort(self):
        """``adb kill-server`` returns non-zero when the daemon was
        already dead. That's success for our purposes (the goal is
        "no daemon running", and we got there). We must still
        proceed to ``adb start-server``."""
        c = self._ctl()
        with mock.patch.object(c, "_run") as run:
            run.side_effect = [
                _fail(stderr="cannot connect to daemon"),
                _ok(),
            ]
            assert c.restart_server() is True

    def test_start_server_failure_returns_false(self):
        c = self._ctl()
        with mock.patch.object(c, "_run") as run:
            run.side_effect = [
                _ok(),                                      # kill ok
                _fail(stderr="adb: failed to start daemon"),  # start bad
            ]
            assert c.restart_server() is False

    def test_kill_timeout_returns_false(self):
        """A hanging ``kill-server`` is unusual but does happen on
        Windows when a Defender realtime scan stalls the process.
        We surface it as a soft failure so the wizard offers retry
        rather than hanging the UI."""
        c = self._ctl()
        with mock.patch.object(c, "_run") as run:
            run.side_effect = subprocess.TimeoutExpired(
                cmd=["adb", "kill-server"], timeout=5,
            )
            assert c.restart_server() is False

    def test_start_timeout_returns_false(self):
        c = self._ctl()
        with mock.patch.object(c, "_run") as run:
            run.side_effect = [
                _ok(),  # kill succeeds
                subprocess.TimeoutExpired(
                    cmd=["adb", "start-server"], timeout=10,
                ),
            ]
            assert c.restart_server() is False
