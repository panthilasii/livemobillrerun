"""Per-device encode + push task registry (v1.8.6).

The dashboard's "▶ Encode + Push" button used to be a *single-task*
flow: only one device could be encoding/pushing at any time, the
output MP4 was written to a shared ``cache/vcam_final.mp4`` path,
and the customer had to wait for one phone to finish before kicking
off the next. With 5+ devices that meant ~5 × 90 s = 7-8 min of
strict sequencing — even though the bottleneck on most modern PCs
isn't the encoder itself (libx264 ``veryfast`` runs at ~6× realtime)
but the customer's tap-and-wait loop.

This module replaces the global single-slot with a **per-serial
registry** of in-flight ``EncodePushTask`` objects, plus a
**per-serial cache path** so two parallel ffmpeg processes don't
race on the same output file.

Threading model
~~~~~~~~~~~~~~~

* The Tk UI thread reads task fields (``state``, ``progress``,
  ``message``) for the currently-selected device on every refresh.
  Reads are *unlocked* — CPython's GIL makes single-attribute
  loads atomic, and we only ever paint stale-but-consistent state.
* A worker thread (one per task) is the *only* writer to a given
  task's fields. So multi-writer races on a single task can't
  occur by construction.
* The registry's ``_tasks`` dict (add / remove / lookup) IS guarded
  by a lock because the UI thread occasionally walks it (e.g. for
  the sidebar badge render across all devices).

We intentionally keep this module Tk-free so it can be unit-
tested without spinning up a window. The UI side just spawns
``encode_push_runner.run_encode_push`` on a daemon thread and
re-paints when the task's ``on_update`` callback fires.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import StreamConfig

log = logging.getLogger(__name__)


# ── states ──────────────────────────────────────────────────────────
#
# We use string literals instead of an enum to keep the registry
# JSON-friendly (so a future "resume from crash" feature can pickle
# tasks if it ever needs to). Five states cover every transition
# the UI cares about:
#
#   queued    — registered, thread not yet started
#   encoding  — ffmpeg child running, ``progress`` 0..0.5
#   pushing   — adb push child running, ``progress`` 0.5..1.0
#   done      — both succeeded; ``message`` holds the success line
#   error     — either step failed; ``error`` holds Thai diagnostic
#
STATE_QUEUED = "queued"
STATE_ENCODING = "encoding"
STATE_PUSHING = "pushing"
STATE_DONE = "done"
STATE_ERROR = "error"
# Terminal state set when ``cancel_event`` was tripped before the
# task could complete naturally — typically because the customer
# closed the app mid-encode (v1.8.6 hardening: previously daemon
# threads died with the process and left orphaned ffmpeg / adb
# children consuming CPU until manually killed).
STATE_CANCELLED = "cancelled"

_RUNNING_STATES = frozenset({STATE_QUEUED, STATE_ENCODING, STATE_PUSHING})
_TERMINAL_STATES = frozenset({STATE_DONE, STATE_ERROR, STATE_CANCELLED})

# ``Callable`` alias used by the runner to push UI refreshes back to
# the Tk thread. Receives the (mutated) task itself; the UI is free
# to read whichever fields it needs to repaint.
TaskUpdateCB = Optional[Callable[["EncodePushTask"], None]]


@dataclass
class EncodePushTask:
    """One in-flight (or finished) encode + push job.

    Field semantics
    ~~~~~~~~~~~~~~~

    ``serial``         — canonical USB serial (devices.json key).
                         Stable across USB ↔ WiFi transport flips,
                         which is why we use it as the registry key
                         instead of the (volatile) adb_id.
    ``adb_id``         — what to feed to ``adb -s …``. Captured at
                         task-start time; if the customer flips
                         transport mid-task the push step might fail
                         and we surface the diagnostic.
    ``source``         — local clip the customer picked.
    ``output``         — per-serial cache MP4. Different serials get
                         different paths so concurrent encodes don't
                         clobber each other's bytes.
    ``tiktok_pkg``     — captured per device so the on-phone target
                         path matches whichever TikTok variant
                         (international / Lite / Douyin) that
                         specific phone has installed.
    ``state``          — see STATE_* constants above.
    ``progress``       — 0..1 combined; encode contributes 0..0.5
                         and push contributes 0.5..1.0 so the UI
                         can show a single bar without juggling
                         two phases.
    ``message``        — short Thai status the UI shows under the
                         button (e.g. "กำลัง encode… 45%").
    ``error``          — populated only when ``state == "error"``.
    ``started_at`` /
    ``finished_at``    — wall-clock seconds (``time.time()``). Used
                         by the sidebar badge to estimate time-left
                         and by the success modal to print elapsed.
    ``bytes_pushed`` /
    ``elapsed_push_s`` — surfaced in the success message so the
                         customer can see "20 MB ใน 4.3 วิ" — a
                         familiar shape from the v1.8.0 single-
                         task copy.
    """

    serial: str
    adb_id: str
    source: Path
    output: Path
    tiktok_pkg: str
    state: str = STATE_QUEUED
    progress: float = 0.0
    message: str = "เตรียม encode…"
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    bytes_pushed: int = 0
    elapsed_push_s: float = 0.0
    encoded_bytes: int = 0
    elapsed_encode_s: float = 0.0
    # Stamp incremented on every mutation. Lets the UI cheaply
    # detect "did anything change for this device since the last
    # paint" without comparing every field. Useful for the 1 s
    # dashboard tick that re-renders the sidebar badge.
    revision: int = 0
    # Cooperative cancellation flag. Tripped by ``request_cancel``
    # (called from ``StudioApp._on_close`` for every running task,
    # or in the future from a per-row "หยุด" button). The runner
    # checks this between phases AND passes it down to the
    # ``HookModePipeline`` so its ffmpeg / adb subprocess polling
    # loops can kill their children mid-flight instead of letting
    # a daemon thread orphan a 100 %-CPU encoder when the parent
    # exits. See ``hook_mode.encode_playlist`` /
    # ``hook_mode.push_to_phone`` for the polling contract.
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def is_running(self) -> bool:
        return self.state in _RUNNING_STATES

    def is_done(self) -> bool:
        return self.state == STATE_DONE

    def is_error(self) -> bool:
        return self.state == STATE_ERROR

    def is_cancelled(self) -> bool:
        return self.state == STATE_CANCELLED

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def request_cancel(self) -> None:
        """Ask the worker thread to bail out at the next checkpoint.

        Idempotent — calling this twice is fine; the runner only
        reads ``cancel_event.is_set()``. Does NOT change the
        state directly: the runner is the single writer of state
        transitions, and we don't want to race a worker that's
        already mid-mark_state.
        """
        self.cancel_event.set()

    def is_cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    def status_label_thai(self) -> str:
        """Compact one-liner the sidebar badge can show.

        Designed to fit alongside the existing transport / live
        chips in the sub-row, so we keep it short and emoji-led
        for fast scanning across a 5-device sidebar.
        """
        if self.state == STATE_QUEUED:
            return "⏳ คิว encode"
        if self.state == STATE_ENCODING:
            return f"⚙️ encode {int(self.progress * 100)}%"
        if self.state == STATE_PUSHING:
            return f"📤 push {int(self.progress * 100)}%"
        if self.state == STATE_DONE:
            return "✓ ส่งคลิปสำเร็จ"
        if self.state == STATE_ERROR:
            return "✗ encode/push ล้มเหลว"
        if self.state == STATE_CANCELLED:
            return "■ ยกเลิก"
        return self.state


class EncodePushRegistry:
    """Thread-safe map of canonical-serial → ``EncodePushTask``.

    Why a dedicated class (vs. a bare dict on the app)
    --------------------------------------------------

    * Locks the dict for add / remove / list-snapshot operations
      so the dashboard's 1 s sidebar tick can safely walk every
      task without colliding with a worker thread that's about to
      mutate the registry on completion.
    * Offers a ``snapshot()`` that returns a stable list copy —
      iterating ``self._tasks.values()`` directly would raise
      ``RuntimeError: dictionary changed size during iteration``
      if a worker thread completed a task mid-render.
    * Centralises the fail-fast guard "is one already running for
      this serial" so the per-device button can reject double-
      clicks without re-implementing the check at every call site.

    Note: per-task field mutation (state / progress / message /
    error) is done **directly** by the runner that owns the task —
    those writes don't go through the registry lock because we
    rely on Python's per-attribute atomicity. The registry only
    cares about who's *in* it, not what their internal state is.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, EncodePushTask] = {}
        self._lock = threading.Lock()

    def get(self, serial: str) -> Optional[EncodePushTask]:
        with self._lock:
            return self._tasks.get(serial)

    def has_running(self, serial: str) -> bool:
        """True iff a task for ``serial`` is queued / encoding /
        pushing. Used by the button handler to reject double-
        clicks before they spin up a redundant ffmpeg child."""
        with self._lock:
            t = self._tasks.get(serial)
            return t is not None and t.is_running()

    def upsert(self, task: EncodePushTask) -> None:
        with self._lock:
            self._tasks[task.serial] = task

    def remove(self, serial: str) -> Optional[EncodePushTask]:
        with self._lock:
            return self._tasks.pop(serial, None)

    def snapshot(self) -> list[EncodePushTask]:
        """Stable list copy — safe to iterate while workers mutate."""
        with self._lock:
            return list(self._tasks.values())

    def clear_finished(self) -> int:
        """Drop every terminal-state task. Returns the number removed.

        Called when the dashboard rebuilds the sidebar so old "✓"
        / "✗" / "■" badges don't linger forever after the customer
        has seen and acknowledged them. The contract is "if the
        user triggers a fresh action, stale terminal-state badges
        go away" — a transient ✓ for 1-2 s after success is fine,
        but a ✗ from yesterday would be confusing.
        """
        with self._lock:
            stale = [s for s, t in self._tasks.items() if not t.is_running()]
            for s in stale:
                del self._tasks[s]
            return len(stale)

    def cancel_all_running(self) -> int:
        """Fire ``cancel_event`` for every running task.

        Returns the number of tasks signalled. Callers (typically
        ``StudioApp._on_close``) usually wait briefly afterwards
        for daemon threads to drain — the runner's cooperative
        cancel hands the worker thread a chance to ``proc.kill()``
        the ffmpeg / adb child cleanly instead of leaving it as
        an orphan process when the parent exits.

        The lock is held for the iteration only; ``set()`` itself
        is atomic on ``threading.Event`` so workers can read the
        event concurrently without racing.
        """
        signalled = 0
        with self._lock:
            for t in self._tasks.values():
                if t.is_running():
                    t.cancel_event.set()
                    signalled += 1
        return signalled


# ── per-serial cache path ───────────────────────────────────────────


# Serials can technically contain characters that are illegal in
# filenames on Windows (``:`` for ip:port, control chars from buggy
# adb implementations, …). We sanitise to a safe alphabet so the
# resulting ``vcam_<safe>.mp4`` is always openable. Empty strings
# fall back to ``unknown`` so we never produce a hidden ``.mp4``.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise_serial(serial: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", serial.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "unknown"


def device_local_mp4(cfg: StreamConfig, serial: str) -> Path:
    """Where to cache the encoded MP4 for ``serial``.

    Mirrors ``hook_mode.default_local_mp4`` (same parent ``cache/``
    dir under the user's videos folder) but adds a serial-derived
    suffix so two parallel encodes can't clobber each other's
    bytes. The directory is created on first call so a fresh
    install doesn't have to ship an empty ``cache/`` folder.

    Why we don't use ``tempfile``
    -----------------------------
    Tempfiles get cleaned up on reboot by some platforms (macOS
    purges ``/var/folders/...`` aggressively after a few days)
    and the cache MP4 is large (50-500 MB). Putting it next to
    the user's videos folder also makes "where did my encoded
    file go" obvious — they can preview it manually if a push
    debug session is needed.
    """
    cache_dir = cfg.videos_path.parent / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = _sanitise_serial(serial)
    return cache_dir / f"vcam_{safe}.mp4"


# ── helpers used by the runner / UI ─────────────────────────────────


def mark_state(
    task: EncodePushTask,
    state: str,
    *,
    progress: Optional[float] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
    on_update: TaskUpdateCB = None,
) -> None:
    """Atomic-ish state transition + UI callback.

    Centralised here (rather than open-coded in the runner) so
    every state change goes through the same path: bump revision,
    log, fire ``on_update``. Forgetting to bump revision means the
    UI's "did anything change?" tick will skip the repaint, which
    used to manifest as a stuck "กำลัง encode…" message even
    though the engine had moved on to push.
    """
    task.state = state
    if progress is not None:
        # Clamp to 0..1 so a stray ffmpeg out_time_us > duration
        # doesn't push the bar past 100 % visually.
        task.progress = max(0.0, min(1.0, progress))
    if message is not None:
        task.message = message
    if error is not None:
        task.error = error
    if state in _TERMINAL_STATES:
        task.finished_at = time.time()
    elif state == STATE_ENCODING and task.started_at == 0.0:
        task.started_at = time.time()
    task.revision += 1
    if on_update is not None:
        try:
            on_update(task)
        except Exception:
            log.exception("encode-push on_update callback crashed")
