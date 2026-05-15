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

v1.8.x recurrence fix
---------------------
Customers kept hitting the "รีสตาร์ท ADB ไม่สำเร็จ" dialog with the
generic "close scrcpy / Android Studio / Vysor" advice — none of
which they had installed. Real culprit was usually Bluestacks /
MEmu / Microsoft Phone Link / Mi PC Suite / Samsung Smart Switch
holding port 5037. The new tests below pin the port-holder
detection and the actionable error message that names the
specific process.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

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


def _make_controller() -> adb_mod.AdbController:
    """Construct an ``AdbController`` without touching the real
    filesystem (the ``_resolve`` machinery sniffs platform_tools
    which we don't want to depend on in CI)."""
    c = adb_mod.AdbController.__new__(adb_mod.AdbController)
    c.adb_path = "/x/adb"
    c.last_restart_error = ""
    return c


class TestRestartServer:
    def _ctl(self):
        return _make_controller()

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
            # Happy path must wipe any stale error from a previous
            # failed restart — the UI keys off this field.
            assert c.last_restart_error == ""

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
        with mock.patch.object(c, "_run") as run, \
             mock.patch.object(
                 adb_mod.AdbController, "_find_port_5037_holder",
                 return_value=None,
             ):
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
            assert "kill-server" in c.last_restart_error
            assert "Defender" in c.last_restart_error or "zombie" in c.last_restart_error

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
            assert "start-server" in c.last_restart_error


# ── v1.8.x diagnostic — name the process holding port 5037 ────


class TestLastRestartError:
    """Pin the customer-facing diagnostic that names the specific
    process holding port 5037. Without this, customers see a
    generic "close scrcpy / Android Studio / Vysor" message and
    keep failing because they don't have any of those installed —
    they have Bluestacks / MEmu / Phone Link / Mi PC Suite."""

    def test_start_failure_with_known_holder_names_process_and_pid(self):
        """When the OS probe identifies the holder, the customer-
        facing error must include both the executable name and the
        PID so they can find it in Task Manager."""
        c = _make_controller()
        with mock.patch.object(c, "_run") as run, \
             mock.patch.object(
                 adb_mod.AdbController, "_find_port_5037_holder",
                 return_value=(4242, "HD-Player.exe"),
             ):
            run.side_effect = [
                _ok(),
                _fail(
                    stderr="cannot bind listener: Address already in use",
                ),
            ]
            assert c.restart_server() is False
            err = c.last_restart_error
            # Names the specific process and PID.
            assert "HD-Player.exe" in err
            assert "4242" in err
            # Coaches the customer to Task Manager — that's the
            # single most useful next action.
            assert "Task Manager" in err
            # And the underlying adb stderr is preserved so support
            # can see whether it was a bind error vs something else.
            assert "Address already in use" in err

    def test_start_failure_without_known_holder_falls_back_to_generic_list(self):
        """When the OS probe couldn't identify a holder (e.g. lsof
        not installed on a stripped-down Linux), we must still give
        the customer SOMETHING useful — the v1.8.0 list of common
        culprits, now expanded to include the modern offenders we
        learned about in the customer reports."""
        c = _make_controller()
        with mock.patch.object(c, "_run") as run, \
             mock.patch.object(
                 adb_mod.AdbController, "_find_port_5037_holder",
                 return_value=None,
             ):
            run.side_effect = [
                _ok(),
                _fail(stderr="adb: failed to start daemon"),
            ]
            assert c.restart_server() is False
            err = c.last_restart_error
            # Modern offenders we kept seeing in support tickets
            # must be in the generic list — that's the whole point.
            assert "Bluestacks" in err or "MEmu" in err \
                or "Microsoft Phone Link" in err

    def test_last_restart_error_is_cleared_on_success(self):
        """A previous failed restart must not leave its hint
        visible after a subsequent successful one — the UI would
        flash a stale error otherwise."""
        c = _make_controller()
        c.last_restart_error = "old failure message"
        with mock.patch.object(c, "_run") as run:
            run.side_effect = [_ok(), _ok()]
            assert c.restart_server() is True
        assert c.last_restart_error == ""


# ── port-5037 holder probe (cross-platform) ───────────────────


class TestFindPortHolderUnix:
    """Test the macOS / Linux ``lsof``-based probe directly so we
    don't need a real bound port to verify parsing."""

    LSOF_TYPICAL = "p12345\ncadb\nn127.0.0.1:5037\n"
    LSOF_BLUESTACKS = "p9876\ncHD-Adb.exe\nn*:5037\n"

    def test_parses_typical_lsof_output(self, monkeypatch):
        def _fake_run(cmd, *args, **kw):
            return _ok(stdout=self.LSOF_TYPICAL)
        monkeypatch.setattr(adb_mod.subprocess, "run", _fake_run)
        monkeypatch.setattr(adb_mod.shutil, "which", lambda _b: "/usr/bin/lsof")
        result = adb_mod.AdbController._find_port_holder_unix()
        assert result == (12345, "adb")

    def test_parses_bluestacks_format(self, monkeypatch):
        """Bluestacks ships its own adb daemon under a different
        executable name — must still parse out cleanly."""
        def _fake_run(cmd, *args, **kw):
            return _ok(stdout=self.LSOF_BLUESTACKS)
        monkeypatch.setattr(adb_mod.subprocess, "run", _fake_run)
        monkeypatch.setattr(adb_mod.shutil, "which", lambda _b: "/usr/bin/lsof")
        result = adb_mod.AdbController._find_port_holder_unix()
        assert result == (9876, "HD-Adb.exe")

    def test_returns_none_when_lsof_missing(self, monkeypatch):
        """Stripped-down Linux containers don't always have lsof.
        The probe must fail gracefully rather than blow up — its
        absence isn't a load-bearing failure."""
        monkeypatch.setattr(adb_mod.shutil, "which", lambda _b: None)
        assert adb_mod.AdbController._find_port_holder_unix() is None

    def test_returns_none_when_lsof_returns_nothing(self, monkeypatch):
        """Port not bound by anything → empty lsof output → None."""
        def _fake_run(cmd, *args, **kw):
            return _ok(stdout="")
        monkeypatch.setattr(adb_mod.subprocess, "run", _fake_run)
        monkeypatch.setattr(adb_mod.shutil, "which", lambda _b: "/usr/bin/lsof")
        assert adb_mod.AdbController._find_port_holder_unix() is None


class TestFindPortHolderWindows:
    """Test the Windows-only ``netstat`` + ``tasklist`` probe."""

    NETSTAT_SAMPLE = (
        "Active Connections\n"
        "\n"
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1024\n"
        "  TCP    127.0.0.1:5037         0.0.0.0:0              LISTENING       4242\n"
        "  TCP    127.0.0.1:5050         0.0.0.0:0              LISTENING       7777\n"
    )
    TASKLIST_BLUESTACKS = (
        '"HD-Player.exe","4242","Console","1","123,456 K"\n'
    )

    def test_parses_netstat_and_tasklist(self, monkeypatch):
        calls = {"n": 0}

        def _fake_run(cmd, *args, **kw):
            calls["n"] += 1
            if cmd[0] == "netstat":
                return _ok(stdout=self.NETSTAT_SAMPLE)
            if cmd[0] == "tasklist":
                # Must pass the PID we extracted from netstat.
                assert "PID eq 4242" in cmd
                return _ok(stdout=self.TASKLIST_BLUESTACKS)
            raise AssertionError(f"unexpected cmd {cmd!r}")

        monkeypatch.setattr(adb_mod.subprocess, "run", _fake_run)
        result = adb_mod.AdbController._find_port_holder_windows()
        assert result == (4242, "HD-Player.exe")

    def test_returns_none_when_5037_not_in_netstat(self, monkeypatch):
        """Port simply isn't bound — netstat lists other ports but
        not 5037. Must NOT return a stray non-5037 PID."""
        sample = (
            "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1024\n"
            "  TCP    127.0.0.1:5050         0.0.0.0:0              LISTENING       7777\n"
        )

        def _fake_run(cmd, *args, **kw):
            return _ok(stdout=sample)

        monkeypatch.setattr(adb_mod.subprocess, "run", _fake_run)
        assert adb_mod.AdbController._find_port_holder_windows() is None

    def test_tasklist_failure_still_returns_pid_with_unknown_name(
        self, monkeypatch,
    ):
        """When tasklist can't resolve the PID (race condition —
        process exited between netstat and tasklist), we still
        want to surface the PID so the customer has SOMETHING to
        look up."""

        def _fake_run(cmd, *args, **kw):
            if cmd[0] == "netstat":
                return _ok(stdout=self.NETSTAT_SAMPLE)
            return _fail(stderr="INFO: No tasks running with the specified criteria.")

        monkeypatch.setattr(adb_mod.subprocess, "run", _fake_run)
        result = adb_mod.AdbController._find_port_holder_windows()
        assert result is not None
        pid, name = result
        assert pid == 4242
        assert name == "unknown"


class TestFindPortHolderDispatch:
    """The public ``_find_port_5037_holder`` must dispatch by
    ``sys.platform`` and swallow probe errors so a failed probe
    never blocks the restart pipeline."""

    def test_dispatches_to_windows_on_win32(self, monkeypatch):
        monkeypatch.setattr(adb_mod.sys, "platform", "win32")
        sentinel = (1234, "x.exe")
        monkeypatch.setattr(
            adb_mod.AdbController, "_find_port_holder_windows",
            staticmethod(lambda: sentinel),
        )
        # Make the unix probe explode if accidentally called.
        monkeypatch.setattr(
            adb_mod.AdbController, "_find_port_holder_unix",
            staticmethod(lambda: (_ for _ in ()).throw(AssertionError("wrong path"))),
        )
        assert adb_mod.AdbController._find_port_5037_holder() == sentinel

    @pytest.mark.parametrize("plat", ["darwin", "linux", "freebsd"])
    def test_dispatches_to_unix_on_non_windows(self, monkeypatch, plat):
        monkeypatch.setattr(adb_mod.sys, "platform", plat)
        sentinel = (99, "adb")
        monkeypatch.setattr(
            adb_mod.AdbController, "_find_port_holder_unix",
            staticmethod(lambda: sentinel),
        )
        monkeypatch.setattr(
            adb_mod.AdbController, "_find_port_holder_windows",
            staticmethod(lambda: (_ for _ in ()).throw(AssertionError("wrong path"))),
        )
        assert adb_mod.AdbController._find_port_5037_holder() == sentinel

    def test_swallows_probe_exception_and_returns_none(self, monkeypatch):
        """A buggy / unavailable probe must NOT raise out of the
        public helper — that would crash restart_server() and the
        customer would see an exception instead of a friendly
        Thai-language dialog."""
        monkeypatch.setattr(adb_mod.sys, "platform", "linux")
        monkeypatch.setattr(
            adb_mod.AdbController, "_find_port_holder_unix",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))),
        )
        assert adb_mod.AdbController._find_port_5037_holder() is None
