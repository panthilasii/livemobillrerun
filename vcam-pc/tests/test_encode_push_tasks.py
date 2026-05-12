"""Per-device encode + push task model (v1.8.6).

The dashboard's encode + push flow used to be a single-task
bottleneck — one button, one progress bar, one shared cache MP4
path. v1.8.6 split it per-serial so the customer can fire jobs
across multiple phones in parallel. This file pins down the
contract for the underlying task object + registry so a future
"simplify" refactor can't silently regress to the old single-slot
behaviour.

What we lock in
~~~~~~~~~~~~~~~

* ``device_local_mp4`` returns a unique path per serial — two
  parallel ffmpeg children must not collide on the same output.
* The path lives under ``cache/`` next to the user's videos folder,
  matching the convention of ``hook_mode.default_local_mp4``.
* Serials with characters illegal on Windows (``:`` for ``ip:port``)
  are sanitised so the resulting filename is portable.
* ``EncodePushTask`` state transitions through queued → encoding →
  pushing → done/error are reported via ``status_label_thai`` for
  the sidebar badge.
* The registry's ``has_running`` rejects double-clicks for a
  serial whose task is mid-flight, but still allows a *new* task
  for that serial after the previous one finished.
* ``clear_finished`` drops only terminal-state tasks (running ones
  must survive a sidebar rebuild — losing them would orphan the
  worker thread's UI updates).
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.config import StreamConfig
from src.encode_push_tasks import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_ENCODING,
    STATE_ERROR,
    STATE_PUSHING,
    STATE_QUEUED,
    EncodePushRegistry,
    EncodePushTask,
    device_local_mp4,
    mark_state,
)


# ── device_local_mp4 ──────────────────────────────────────────────


def _cfg_with_videos(tmp_path: Path) -> StreamConfig:
    cfg = StreamConfig.load()
    # ``videos_path`` is a property derived from ``cfg.videos_dir``
    # (the raw config field). Point it at our tmp dir so we don't
    # touch the real customer ``videos/`` folder.
    cfg.videos_dir = str(tmp_path / "videos")
    Path(cfg.videos_dir).mkdir(parents=True, exist_ok=True)
    return cfg


def test_per_serial_paths_are_unique(tmp_path):
    cfg = _cfg_with_videos(tmp_path)
    a = device_local_mp4(cfg, "AAA111")
    b = device_local_mp4(cfg, "BBB222")
    assert a != b
    assert a.parent == b.parent  # both under the same cache dir
    assert a.name == "vcam_AAA111.mp4"
    assert b.name == "vcam_BBB222.mp4"


def test_cache_dir_is_created_on_first_call(tmp_path):
    cfg = _cfg_with_videos(tmp_path)
    cache = Path(cfg.videos_dir).parent / "cache"
    # Pre-condition: cache dir doesn't exist yet.
    assert not cache.is_dir()

    p = device_local_mp4(cfg, "AAA111")
    assert p.parent.is_dir(), (
        "device_local_mp4 must auto-create cache/ so a fresh "
        "install doesn't have to ship an empty folder"
    )
    assert p.parent == cache


def test_serial_with_ip_port_sanitised_for_windows(tmp_path):
    """``:`` is illegal in Windows filenames. WiFi rows have
    ``adb_id`` shaped like ``192.168.1.50:5555`` — if we ever
    accidentally use that as the cache key, ``open()`` on
    Windows raises OSError. Make sure sanitisation triggers."""
    cfg = _cfg_with_videos(tmp_path)
    p = device_local_mp4(cfg, "192.168.1.50:5555")
    assert ":" not in p.name
    assert p.name.startswith("vcam_") and p.suffix == ".mp4"


def test_empty_serial_falls_back_to_unknown(tmp_path):
    cfg = _cfg_with_videos(tmp_path)
    p = device_local_mp4(cfg, "")
    # Blank input must NOT produce a hidden ``.mp4`` like
    # ``vcam_.mp4`` -- some shells / globs misbehave. We replace
    # with a sentinel.
    assert p.name == "vcam_unknown.mp4"


# ── EncodePushTask state ──────────────────────────────────────────


def _make_task(serial: str = "AAA") -> EncodePushTask:
    return EncodePushTask(
        serial=serial,
        adb_id=serial,
        source=Path("/tmp/x.mp4"),
        output=Path("/tmp/y.mp4"),
        tiktok_pkg="com.example",
    )


@pytest.mark.parametrize("state,running", [
    (STATE_QUEUED, True),
    (STATE_ENCODING, True),
    (STATE_PUSHING, True),
    (STATE_DONE, False),
    (STATE_ERROR, False),
    (STATE_CANCELLED, False),
])
def test_is_running_matches_state(state, running):
    t = _make_task()
    t.state = state
    assert t.is_running() is running


@pytest.mark.parametrize("state,terminal", [
    (STATE_QUEUED, False),
    (STATE_ENCODING, False),
    (STATE_PUSHING, False),
    (STATE_DONE, True),
    (STATE_ERROR, True),
    (STATE_CANCELLED, True),
])
def test_is_terminal_recognises_cancelled(state, terminal):
    """The dashboard re-renders the encode card when a task hits a
    terminal state. Cancellation is terminal too — leaving it as
    "still running" would freeze the button at "กำลัง encode…"
    forever after the customer asked us to stop."""
    t = _make_task()
    t.state = state
    assert t.is_terminal() is terminal


def test_status_label_includes_pct_during_encode():
    t = _make_task()
    t.state = STATE_ENCODING
    t.progress = 0.23
    label = t.status_label_thai()
    assert "encode" in label.lower() or "encode" in label
    assert "23" in label, (
        "encode label must surface the percentage for the sidebar "
        "badge — otherwise three parallel rows look identical"
    )


def test_status_label_for_cancelled_says_ยกเลิก():
    """The sidebar badge must distinguish "ยกเลิก" from "ล้มเหลว"
    so support tickets that say "ฉันกดปิดโปรแกรม แล้วเห็น ✗" don't
    get filed as bug reports — cancel is voluntary, not a failure."""
    t = _make_task()
    t.state = STATE_CANCELLED
    assert "ยกเลิก" in t.status_label_thai()


def test_status_label_for_pushing_uses_push_emoji():
    t = _make_task()
    t.state = STATE_PUSHING
    t.progress = 0.75  # combined progress, pushing phase
    label = t.status_label_thai()
    assert "push" in label.lower()
    assert "75" in label


# ── mark_state contract ───────────────────────────────────────────


def test_mark_state_bumps_revision_so_ui_can_detect_changes():
    t = _make_task()
    rev_before = t.revision
    mark_state(t, STATE_ENCODING, progress=0.1, message="hello")
    assert t.revision == rev_before + 1
    assert t.state == STATE_ENCODING
    assert t.progress == 0.1
    assert t.message == "hello"


def test_mark_state_clamps_progress_to_unit_range():
    t = _make_task()
    mark_state(t, STATE_ENCODING, progress=1.5)  # ffmpeg overshoot
    assert t.progress == 1.0
    mark_state(t, STATE_ENCODING, progress=-0.2)
    assert t.progress == 0.0


def test_mark_state_records_finish_timestamp():
    t = _make_task()
    mark_state(t, STATE_DONE)
    assert t.finished_at > 0


def test_mark_state_callback_exception_does_not_break_transition():
    """A buggy ``on_update`` (e.g. a Tk widget that was destroyed
    mid-flight) must NOT abort the state transition — otherwise
    one race could permanently freeze the registry."""
    t = _make_task()

    def boom(_t):
        raise RuntimeError("widget destroyed")

    mark_state(t, STATE_DONE, on_update=boom)
    assert t.state == STATE_DONE


# ── Registry ──────────────────────────────────────────────────────


def test_registry_get_returns_none_for_unknown():
    reg = EncodePushRegistry()
    assert reg.get("AAA") is None


def test_registry_upsert_then_get():
    reg = EncodePushRegistry()
    t = _make_task("AAA")
    reg.upsert(t)
    assert reg.get("AAA") is t


def test_has_running_only_true_during_active_states():
    reg = EncodePushRegistry()
    t = _make_task("AAA")
    reg.upsert(t)
    assert reg.has_running("AAA") is True

    t.state = STATE_DONE
    assert reg.has_running("AAA") is False, (
        "completed task must not block a fresh click — customer "
        "expects to be able to re-encode after success"
    )

    t.state = STATE_ERROR
    assert reg.has_running("AAA") is False


def test_remove_returns_dropped_task():
    reg = EncodePushRegistry()
    t = _make_task("AAA")
    reg.upsert(t)
    assert reg.remove("AAA") is t
    assert reg.get("AAA") is None
    assert reg.remove("AAA") is None  # idempotent


def test_clear_finished_drops_only_terminal_states():
    reg = EncodePushRegistry()
    running = _make_task("AAA")
    running.state = STATE_ENCODING
    done = _make_task("BBB")
    done.state = STATE_DONE
    err = _make_task("CCC")
    err.state = STATE_ERROR

    reg.upsert(running)
    reg.upsert(done)
    reg.upsert(err)

    dropped = reg.clear_finished()
    assert dropped == 2
    assert reg.get("AAA") is running, (
        "running task must NOT be cleared — losing it orphans the "
        "worker thread's UI updates"
    )
    assert reg.get("BBB") is None
    assert reg.get("CCC") is None


def test_remove_drops_running_task_too():
    """Regression for the v1.8.6 "ghost ✓ on re-pair" bug.

    First-pass v1.8.6 left running tasks in the registry on
    delete on the assumption that "the worker thread will keep
    going so leave it alone". That created a UX hazard:

    * Customer deletes phone S during encode.
    * Worker finishes a few seconds later, stamps STATE_DONE.
    * Customer plugs the same phone back in → poller re-upserts
      the entry → sidebar reads ``encode_tasks.get(S)`` and shows
      "✓ ส่งคลิปสำเร็จ" on a row the customer never touched.

    The Dashboard's delete handler now calls ``remove(serial)``
    unconditionally so the registry entry goes the moment the
    devices.json entry does. The worker thread is still allowed
    to finish — its updates simply have no UI to land on, which
    is the desired behaviour given the customer asked for the
    entry to disappear.
    """
    reg = EncodePushRegistry()
    running = _make_task("AAA")
    running.state = STATE_ENCODING

    reg.upsert(running)
    assert reg.has_running("AAA") is True

    dropped = reg.remove("AAA")
    assert dropped is running
    assert reg.get("AAA") is None
    assert reg.has_running("AAA") is False, (
        "remove() must drop tasks regardless of state — leaving "
        "running ones in the registry produces ghost badges on "
        "re-paired devices"
    )


def test_request_cancel_sets_event_and_is_idempotent():
    t = _make_task()
    assert not t.is_cancel_requested()
    t.request_cancel()
    assert t.is_cancel_requested()
    # Idempotent — second call must not raise / reset.
    t.request_cancel()
    assert t.is_cancel_requested()


def test_cancel_all_running_signals_only_active_states():
    """``StudioApp._on_close`` calls this. Pre-fix, daemon worker
    threads kept ffmpeg / adb children alive after parent process
    exit because nobody told them the customer was leaving. The
    contract: every running task gets ``cancel_event.set()``;
    terminal tasks are left alone."""
    reg = EncodePushRegistry()
    queued = _make_task("Q")
    queued.state = STATE_QUEUED
    encoding = _make_task("E")
    encoding.state = STATE_ENCODING
    pushing = _make_task("P")
    pushing.state = STATE_PUSHING
    done = _make_task("D")
    done.state = STATE_DONE
    err = _make_task("X")
    err.state = STATE_ERROR
    cancelled = _make_task("C")
    cancelled.state = STATE_CANCELLED

    for t in (queued, encoding, pushing, done, err, cancelled):
        reg.upsert(t)

    n = reg.cancel_all_running()
    assert n == 3, "exactly the 3 running tasks should be signalled"
    assert queued.cancel_event.is_set()
    assert encoding.cancel_event.is_set()
    assert pushing.cancel_event.is_set()
    assert not done.cancel_event.is_set()
    assert not err.cancel_event.is_set()
    assert not cancelled.cancel_event.is_set()


def test_mark_state_records_finish_timestamp_for_cancelled():
    """Cancelled is terminal — the registry's ``clear_finished``
    relies on ``finished_at > 0`` to decide what to garbage-collect.
    A bug here would leak cancelled tasks across sidebar rebuilds."""
    t = _make_task()
    mark_state(t, STATE_CANCELLED, message="ยกเลิก")
    assert t.finished_at > 0
    assert t.state == STATE_CANCELLED


def test_snapshot_safe_during_concurrent_modification():
    """``snapshot`` returns a list copy so iterating it from the
    Tk thread (sidebar render) doesn't race with worker threads
    mutating the registry on completion."""
    reg = EncodePushRegistry()
    for i in range(10):
        reg.upsert(_make_task(f"S{i}"))

    seen: list[int] = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            for i in range(10, 30):
                reg.upsert(_make_task(f"S{i}"))
            for i in range(10, 30):
                reg.remove(f"S{i}")

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(50):
            snap = reg.snapshot()
            seen.append(len(snap))  # must not raise
    finally:
        stop.set()
        t.join(timeout=2)

    # Every snapshot has *at least* the original 10 tasks since
    # we never remove them.
    assert all(n >= 10 for n in seen)
