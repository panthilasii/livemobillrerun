"""Per-device scrcpy mirror sessions.

User story
----------

Customer's phone is laid flat on the desk so the camera points
straight up at the lightbox/turntable. They DON'T want to pick it
up to set up the live, scroll TikTok comments, or tap "Go Live".
They want to drive the phone with mouse + keyboard from the PC.

scrcpy (Genymobile) is the de-facto solution: streams the phone's
display over the existing ADB transport, accepts mouse + keyboard
events back, and lets us blank the phone's own screen so battery
isn't wasted lighting up a face-down OLED.

Module shape
------------

* ``find_scrcpy()`` lives in ``platform_tools`` so other code
  (settings page, About dialog) can probe availability cheaply.
* ``MirrorSession`` is the in-memory record of one running mirror
  (subprocess handle + metadata).
* Sessions are tracked per ``adb_id`` in a module-level dict; we
  give callers ``get_session(adb_id)`` so the UI can render the
  toggle state without holding its own state.
* ``start_mirror`` and ``stop_mirror`` are the two entry points
  used by the UI button.

Process lifecycle
-----------------

scrcpy runs as a separate process — its window is a real native
window owned by the OS, not embedded in our Tk window. That's a
deliberate choice:

* Embedding scrcpy's SDL window inside Tk requires either OS-level
  reparenting (fragile) or a custom render path that loses scrcpy's
  hardware decoder advantage. Both make a 50-line wrapper turn
  into 5000 lines of native interop.
* A separate window lets the customer drag the mirror to a second
  monitor, snap it next to the dashboard, or full-screen it for a
  detail check.

Termination is best-effort. We poll ``Popen.poll()`` on a 1 s tick
to clean up sessions whose window the customer closed manually
(scrcpy quits cleanly when the X button is hit). We also call
``terminate()`` on shutdown / device-disconnect so a USB unplug
doesn't leave an orphan process drawing a black window.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import platform_tools

log = logging.getLogger(__name__)


# ── module state ────────────────────────────────────────────────────


# Active sessions keyed by adb serial. Guarded by a lock because the
# 1-second UI tick + the user clicking "stop" can race on the same
# entry; without the lock we'd risk a double-terminate AttributeError.
_sessions: dict[str, "MirrorSession"] = {}
_sessions_lock = threading.RLock()

# Reaper thread: every second, walks _sessions and prunes any whose
# subprocess exited (user closed the scrcpy window manually). One
# global thread instead of per-session timers because tk's `after`
# can't be relied on from background workers.
_reaper_thread: Optional[threading.Thread] = None
_reaper_stop = threading.Event()
# Optional callback list — UI subscribes so it can re-render the
# Mirror button when a session disappears without an explicit stop.
_on_change_callbacks: list[Callable[[str], None]] = []


# ── data ────────────────────────────────────────────────────────────


@dataclass
class StartMirrorResult:
    """Returned by :func:`start_mirror`. Mirrors the shape of
    ``live_control.StartLiveResult`` so UI handlers can use the
    same try/except pattern across both surfaces."""

    ok: bool
    error: str = ""
    install_url: str = ""
    pid: Optional[int] = None
    cmd: list[str] = field(default_factory=list)


@dataclass
class MirrorSession:
    adb_id: str
    label: str
    pid: int
    started_at: float
    proc: subprocess.Popen
    cmd: list[str]

    def is_running(self) -> bool:
        return self.proc.poll() is None


# ── reaper ──────────────────────────────────────────────────────────


def _reap_loop() -> None:
    """Background loop: drop sessions whose subprocess exited."""
    while not _reaper_stop.wait(1.0):
        try:
            stale: list[str] = []
            with _sessions_lock:
                for adb_id, sess in _sessions.items():
                    if not sess.is_running():
                        stale.append(adb_id)
                for adb_id in stale:
                    log.info(
                        "scrcpy mirror exited externally for %s (pid=%s)",
                        adb_id, _sessions[adb_id].pid,
                    )
                    del _sessions[adb_id]
            for adb_id in stale:
                _emit_change(adb_id)
        except Exception:
            log.exception("scrcpy reaper crashed (will retry)")


def _ensure_reaper_running() -> None:
    """Start the reaper thread on first session, idempotent."""
    global _reaper_thread
    if _reaper_thread is not None and _reaper_thread.is_alive():
        return
    _reaper_stop.clear()
    _reaper_thread = threading.Thread(
        target=_reap_loop, name="scrcpy-reaper", daemon=True,
    )
    _reaper_thread.start()


# ── public callbacks ───────────────────────────────────────────────


def subscribe(cb: Callable[[str], None]) -> None:
    """Register a callback to be invoked (with adb_id) whenever a
    mirror session starts or stops. The UI uses this to re-render
    the Mirror button without polling.

    Callbacks run on the reaper / start-stop thread; the UI must
    marshal back to the Tk main thread itself (typically via
    ``self.after(0, ...)``).
    """
    if cb not in _on_change_callbacks:
        _on_change_callbacks.append(cb)


def _emit_change(adb_id: str) -> None:
    for cb in list(_on_change_callbacks):
        try:
            cb(adb_id)
        except Exception:
            log.exception("scrcpy_mirror change-cb raised")


# ── primary API ─────────────────────────────────────────────────────


def is_available() -> bool:
    """Cheap check for "is scrcpy installed?". Used by the UI to
    decide whether to enable or grey-out the Mirror button."""
    return platform_tools.find_scrcpy() is not None


def get_session(adb_id: str) -> Optional[MirrorSession]:
    with _sessions_lock:
        sess = _sessions.get(adb_id)
        if sess is None:
            return None
        # Defensive: report None for a process that's already
        # exited but the reaper hasn't gotten to yet.
        if not sess.is_running():
            return None
        return sess


def is_mirroring(adb_id: str) -> bool:
    return get_session(adb_id) is not None


def start_mirror(
    adb_path: str,
    adb_id: str,
    *,
    label: str = "",
    max_size: int = 1080,
    max_fps: int = 30,
    bit_rate_mbps: int = 6,
    turn_screen_off: bool = True,
    stay_awake: bool = True,
    no_audio: bool = True,
    always_on_top: bool = False,
    extra_args: Optional[list[str]] = None,
) -> StartMirrorResult:
    """Spawn scrcpy for ``adb_id``. Idempotent — calling twice on
    the same device returns the existing session without spawning
    a second process.

    The defaults are tuned for "phone face-down on the desk":

    * ``turn_screen_off=True`` — phone OLED dark, battery happy.
      The pixels still stream because that's a software path.
    * ``stay_awake=True`` — overrides the "screen off → suspend"
      timer that some Android skins enforce.
    * ``no_audio=True`` — TikTok already has its own audio path
      (the live broadcast); mirroring audio adds latency and
      double-routes Bluetooth headphones.
    * ``max_size=1080`` — 1080 px on the long side. 4K mirrors
      eat 200 MB/min and don't help an admin tap "Go Live".

    The resulting process is detached enough that the customer
    closing the scrcpy window doesn't take the dashboard with it,
    but tracked enough that ``stop_mirror`` (or app exit) can
    terminate it cleanly.
    """
    if not adb_id:
        return StartMirrorResult(ok=False, error="missing_device")

    # Re-use existing session if it's alive — clicking "Mirror"
    # twice should not spawn a second window.
    existing = get_session(adb_id)
    if existing is not None:
        return StartMirrorResult(
            ok=True, pid=existing.pid, cmd=existing.cmd,
        )

    binary = platform_tools.find_scrcpy()
    if binary is None:
        return StartMirrorResult(
            ok=False,
            error="scrcpy_not_installed",
            install_url=_install_url_for_platform(),
        )

    title = label.strip() or f"NP Create — {adb_id}"
    cmd: list[str] = [
        str(binary),
        f"--serial={adb_id}",
        f"--window-title={title}",
        f"--max-size={int(max_size)}",
        f"--max-fps={int(max_fps)}",
        f"--video-bit-rate={int(bit_rate_mbps)}M",
    ]
    if no_audio:
        cmd.append("--no-audio")
    if turn_screen_off:
        cmd.append("--turn-screen-off")
    if stay_awake:
        cmd.append("--stay-awake")
    if always_on_top:
        cmd.append("--always-on-top")

    # Some scrcpy builds need an explicit hint to find adb when
    # PATH points at a different version than the bundled one we
    # use everywhere else. Setting ADB env (not --adb=) keeps us
    # compatible across scrcpy major versions where the flag name
    # changed.
    env = None
    if adb_path:
        import os
        env = dict(os.environ)
        env["ADB"] = str(adb_path)

    if extra_args:
        cmd.extend(extra_args)

    log.info("scrcpy launch: %s", " ".join(shlex.quote(c) for c in cmd))

    try:
        # ``start_new_session`` so a Ctrl-C in the dashboard's parent
        # shell doesn't propagate into scrcpy and tear down the
        # mirror unexpectedly.
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        log.warning("scrcpy spawn failed: %s", exc)
        return StartMirrorResult(
            ok=False, error=f"spawn_failed: {exc}",
            install_url=_install_url_for_platform(),
        )

    # Give scrcpy ~250 ms to either come up or immediately fail
    # (e.g. "device not found"). If the process has already exited,
    # we report the failure; otherwise we treat it as up.
    time.sleep(0.25)
    if proc.poll() is not None:
        log.warning(
            "scrcpy exited immediately for %s (rc=%s)",
            adb_id, proc.returncode,
        )
        return StartMirrorResult(
            ok=False,
            error=f"scrcpy_exited_rc{proc.returncode}",
        )

    sess = MirrorSession(
        adb_id=adb_id,
        label=title,
        pid=proc.pid,
        started_at=time.time(),
        proc=proc,
        cmd=cmd,
    )
    with _sessions_lock:
        _sessions[adb_id] = sess
    _ensure_reaper_running()
    _emit_change(adb_id)
    return StartMirrorResult(ok=True, pid=proc.pid, cmd=cmd)


def stop_mirror(adb_id: str, timeout: float = 3.0) -> bool:
    """Politely terminate the session for ``adb_id``.

    Returns True if a session was stopped (or none was running).
    Falls back to ``kill()`` after ``timeout`` seconds if scrcpy
    refuses to exit on SIGTERM (rare — usually a hung GPU driver).
    """
    with _sessions_lock:
        sess = _sessions.pop(adb_id, None)
    if sess is None:
        return True

    try:
        sess.proc.terminate()
        try:
            sess.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning(
                "scrcpy mirror for %s ignored TERM, killing", adb_id,
            )
            sess.proc.kill()
    except Exception:
        log.exception("error while stopping scrcpy mirror for %s", adb_id)
        return False
    finally:
        _emit_change(adb_id)
    return True


def stop_all() -> None:
    """Tear down every active session. Called from the dashboard's
    shutdown hook so we don't leak scrcpy processes when the
    customer closes NP Create."""
    with _sessions_lock:
        ids = list(_sessions.keys())
    for adb_id in ids:
        stop_mirror(adb_id, timeout=1.0)
    _reaper_stop.set()


# ── helpers ─────────────────────────────────────────────────────────


def _install_url_for_platform() -> str:
    """Return a deep-link to the most appropriate scrcpy install
    instructions for THIS OS. Used in the "scrcpy not found" UI
    so customers go straight to the right place."""
    import sys
    if sys.platform == "darwin":
        # Homebrew is the canonical Mac install path. We deep-link
        # to the formula page so customers see "brew install
        # scrcpy" rendered ready to copy.
        return "https://formulae.brew.sh/formula/scrcpy"
    if sys.platform.startswith("win"):
        # GitHub releases page lists the official Windows zip and
        # a pinned scoop/choco one-liner near the top.
        return "https://github.com/Genymobile/scrcpy/releases"
    return "https://github.com/Genymobile/scrcpy#linux"
