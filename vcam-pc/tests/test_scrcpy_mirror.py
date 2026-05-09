"""Tests for the scrcpy mirror session manager.

We don't actually run scrcpy in CI — that would require an Android
device, a screen, and an interactive process. Instead we mock
``subprocess.Popen`` and ``platform_tools.find_scrcpy`` so we can
assert:

* The right CLI flags get composed for the documented use case
  ("phone face-down — turn screen off + stay awake").
* Sessions are tracked + de-duped per ADB serial.
* ``stop_mirror`` terminates politely then kills on timeout.
* The reaper drops sessions whose process exited externally.
* The "scrcpy not installed" path returns the install URL instead
  of raising.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src import scrcpy_mirror as scm


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with no in-flight sessions or callbacks.

    The module is process-global by design (UI and live tick share
    one registry), so test isolation must be enforced by the
    suite, not the module. We additionally tear down the reaper
    thread between tests so the next test sees a fresh "no reaper
    running" state — leaving a half-asleep reaper from a prior
    test would race the next ``_ensure_reaper_running`` decision
    and silently skip the restart.
    """
    def _drain_reaper() -> None:
        scm._reaper_stop.set()
        t = scm._reaper_thread
        if t is not None:
            t.join(timeout=2.0)
        scm._reaper_thread = None

    _drain_reaper()
    with scm._sessions_lock:
        scm._sessions.clear()
    scm._on_change_callbacks.clear()
    yield
    _drain_reaper()
    with scm._sessions_lock:
        for s in list(scm._sessions.values()):
            try:
                s.proc.kill()
            except Exception:
                pass
        scm._sessions.clear()
    scm._on_change_callbacks.clear()


def _fake_proc(alive: bool = True) -> MagicMock:
    """Build a MagicMock that quacks like a subprocess.Popen."""
    p = MagicMock()
    p.pid = 12345
    p.returncode = None if alive else 0
    p.poll.return_value = None if alive else 0
    p.terminate = MagicMock()
    p.kill = MagicMock()
    p.wait = MagicMock(return_value=0)
    return p


# ── start_mirror happy path ────────────────────────────────────────


def test_start_mirror_composes_expected_flags(monkeypatch):
    """The defaults must include --turn-screen-off + --stay-awake
    + --no-audio because that's the headline UX promise (phone
    face down, no battery drain, no audio echo)."""
    fake_proc = _fake_proc(alive=True)
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/usr/local/bin/scrcpy"),
    )
    with patch.object(scm.subprocess, "Popen", return_value=fake_proc) as popen:
        result = scm.start_mirror(
            "/usr/bin/adb", "ABCDEF1234", label="คุณสมชาย",
        )
    assert result.ok is True
    assert result.pid == 12345

    cmd = popen.call_args.args[0]
    assert cmd[0].endswith("scrcpy")
    assert "--serial=ABCDEF1234" in cmd
    assert "--window-title=คุณสมชาย" in " ".join(cmd)
    assert "--turn-screen-off" in cmd
    assert "--stay-awake" in cmd
    assert "--no-audio" in cmd
    # Resolution + bitrate sanity (defaults).
    assert "--max-size=1080" in cmd
    assert "--video-bit-rate=6M" in cmd


def test_start_mirror_returns_existing_session_on_double_click(monkeypatch):
    """If the user clicks the Mirror button twice in a row, the
    second call must NOT spawn a second window — that would clutter
    the desktop and steal focus mid-broadcast."""
    fake_proc = _fake_proc(alive=True)
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/usr/local/bin/scrcpy"),
    )
    with patch.object(scm.subprocess, "Popen", return_value=fake_proc) as popen:
        scm.start_mirror("/usr/bin/adb", "DEV-A")
        scm.start_mirror("/usr/bin/adb", "DEV-A")  # again
    assert popen.call_count == 1


def test_start_mirror_returns_install_url_when_scrcpy_missing(monkeypatch):
    monkeypatch.setattr(scm.platform_tools, "find_scrcpy", lambda: None)
    result = scm.start_mirror("/usr/bin/adb", "DEV-A")
    assert result.ok is False
    assert result.error == "scrcpy_not_installed"
    assert result.install_url.startswith("http")


def test_start_mirror_handles_immediate_subprocess_exit(monkeypatch):
    """A device that's been unplugged BETWEEN the device-list
    refresh and the Popen call makes scrcpy exit instantly. We
    need to surface this as a friendly error instead of leaving a
    dead session in the registry."""
    fake_proc = _fake_proc(alive=False)  # already exited
    fake_proc.returncode = 1
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/usr/local/bin/scrcpy"),
    )
    with patch.object(scm.subprocess, "Popen", return_value=fake_proc):
        result = scm.start_mirror("/usr/bin/adb", "DEV-A")
    assert result.ok is False
    assert "scrcpy_exited" in result.error
    assert scm.get_session("DEV-A") is None


def test_start_mirror_rejects_empty_serial():
    result = scm.start_mirror("/usr/bin/adb", "")
    assert result.ok is False
    assert result.error == "missing_device"


# ── stop_mirror ────────────────────────────────────────────────────


def test_stop_mirror_terminates_and_clears_session(monkeypatch):
    fake_proc = _fake_proc(alive=True)
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/scrcpy"),
    )
    with patch.object(scm.subprocess, "Popen", return_value=fake_proc):
        scm.start_mirror("/usr/bin/adb", "DEV-A")
    assert scm.is_mirroring("DEV-A") is True

    ok = scm.stop_mirror("DEV-A")
    assert ok is True
    fake_proc.terminate.assert_called_once()
    assert scm.is_mirroring("DEV-A") is False


def test_stop_mirror_kills_after_timeout_when_terminate_ignored(monkeypatch):
    """If scrcpy's render thread is hung (we've seen it once on
    macOS Sonoma w/ external GPU), TERM is ignored. Make sure we
    escalate to KILL within the timeout instead of blocking the
    UI thread forever."""
    import subprocess as real_sp
    fake_proc = _fake_proc(alive=True)
    fake_proc.wait.side_effect = real_sp.TimeoutExpired("scrcpy", 1)
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/scrcpy"),
    )
    with patch.object(scm.subprocess, "Popen", return_value=fake_proc):
        scm.start_mirror("/usr/bin/adb", "DEV-A")

    scm.stop_mirror("DEV-A", timeout=0.1)
    fake_proc.terminate.assert_called_once()
    fake_proc.kill.assert_called_once()


def test_stop_mirror_for_unknown_device_is_noop():
    assert scm.stop_mirror("never-started") is True


# ── reaper / change callbacks ───────────────────────────────────────


def test_reaper_drops_externally_exited_session(monkeypatch):
    """When the customer closes the scrcpy window manually, the
    reaper thread must clean up the registry within ~1.5 s and
    fire the change callback so the UI re-renders."""
    fake_proc = _fake_proc(alive=True)
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/scrcpy"),
    )
    callback_events: list[str] = []
    scm.subscribe(lambda dev: callback_events.append(dev))

    with patch.object(scm.subprocess, "Popen", return_value=fake_proc):
        scm.start_mirror("/usr/bin/adb", "DEV-A")

    # Simulate the customer closing the window.
    fake_proc.poll.return_value = 0

    # Reaper is a 1 s loop; give it up to 3 s before declaring it
    # broken so a slow CI box doesn't flake. We wait specifically
    # for the SECOND callback (start fired one when we created the
    # session; the reaper fires the second on cleanup). Polling
    # ``is_mirroring`` would race here because it inspects
    # ``proc.poll()`` directly and flips to False as soon as the
    # test mutates the mock — long before the reaper actually
    # processes the deletion.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if callback_events.count("DEV-A") >= 2:
            break
        time.sleep(0.05)

    assert callback_events.count("DEV-A") >= 2, (
        f"reaper never fired exit callback (events={callback_events})"
    )
    assert scm.is_mirroring("DEV-A") is False


def test_subscribe_does_not_double_register():
    cb = lambda dev: None  # noqa: E731
    scm.subscribe(cb)
    scm.subscribe(cb)
    assert scm._on_change_callbacks.count(cb) == 1


# ── stop_all ───────────────────────────────────────────────────────


def test_stop_all_terminates_every_active_session(monkeypatch):
    fake1 = _fake_proc(alive=True)
    fake2 = _fake_proc(alive=True)
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/scrcpy"),
    )
    with patch.object(scm.subprocess, "Popen", side_effect=[fake1, fake2]):
        scm.start_mirror("/usr/bin/adb", "DEV-A")
        scm.start_mirror("/usr/bin/adb", "DEV-B")
    scm.stop_all()
    fake1.terminate.assert_called_once()
    fake2.terminate.assert_called_once()
    assert scm.is_mirroring("DEV-A") is False
    assert scm.is_mirroring("DEV-B") is False


# ── platform_tools.find_scrcpy() ───────────────────────────────────


def test_is_available_reflects_find_scrcpy(monkeypatch):
    monkeypatch.setattr(scm.platform_tools, "find_scrcpy", lambda: None)
    assert scm.is_available() is False
    monkeypatch.setattr(
        scm.platform_tools, "find_scrcpy",
        lambda: __import__("pathlib").Path("/scrcpy"),
    )
    assert scm.is_available() is True
