"""Encode + push kernel — per-device parallel contract (v1.8.6).

The runner is the pure-Python core that ``DashboardPage`` spawns
on a daemon thread per device click. With the right inputs each
call is independent, which is what unlocks the "fire 5 phones
without waiting" UX the v1.8.6 ask called for.

Test matrix
~~~~~~~~~~~

1. Happy path           — encode ✓ then push ✓ → STATE_DONE,
                          progress 1.0, on_update fired for every
                          phase
2. Encode fails         — STATE_ERROR, push never called
3. Push fails           — STATE_ERROR, message preserves diag
4. Source missing       — STATE_ERROR before playlist write
5. Encode raises        — caught, STATE_ERROR (runner must NEVER
                          let an exception escape and crash the
                          worker thread silently)
6. Push raises          — caught, STATE_ERROR
7. Progress mapping     — encode 0..1 maps to task.progress 0..0.5;
                          push 0..1 maps to 0.5..1.0
8. Parallel runs        — two threads with different output paths
                          both reach STATE_DONE, no shared state
                          corruption
9. Playlist cleanup     — tempfile is unlinked even on early exit
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.config import DeviceProfile, StreamConfig
from src.encode_push_runner import run_encode_push
from src.encode_push_tasks import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_ENCODING,
    STATE_ERROR,
    STATE_PUSHING,
    EncodePushTask,
)
from src.hook_mode import HookEncodeResult, HookPushResult


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path):
    c = StreamConfig.load()
    c.videos_dir = str(tmp_path / "videos")
    Path(c.videos_dir).mkdir(parents=True, exist_ok=True)
    c.loop_playlist = False
    return c


@pytest.fixture
def profile():
    return DeviceProfile(name="generic")


@pytest.fixture
def src_clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * 1024)
    return p


@pytest.fixture
def make_task(tmp_path, src_clip):
    def _make(serial: str = "AAA", suffix: str = "") -> EncodePushTask:
        out = tmp_path / f"out_{serial}{suffix}.mp4"
        return EncodePushTask(
            serial=serial,
            adb_id=serial,
            source=src_clip,
            output=out,
            tiktok_pkg="com.example",
        )
    return _make


class _StubPipeline:
    """Stand-in for ``HookModePipeline`` that returns scripted
    results without spawning ffmpeg/adb. We track every call so
    tests can assert on call counts (e.g. push must NOT fire when
    encode failed).
    """

    def __init__(
        self,
        *,
        encode_result: HookEncodeResult | None = None,
        push_result: HookPushResult | None = None,
        encode_raises: BaseException | None = None,
        push_raises: BaseException | None = None,
        encode_progress: list[tuple[float, str]] | None = None,
        push_progress: list[tuple[float, str]] | None = None,
        encode_delay_s: float = 0.0,
    ) -> None:
        self.encode_result = encode_result or HookEncodeResult(
            ok=True, output_path=Path("/tmp/x"), duration_s=1.0,
            bytes=2048, log_tail="",
        )
        self.push_result = push_result or HookPushResult(
            ok=True, bytes=2048, elapsed_s=1.0, target="",
        )
        self.encode_raises = encode_raises
        self.push_raises = push_raises
        self.encode_progress = encode_progress or []
        self.push_progress = push_progress or []
        self.encode_delay_s = encode_delay_s
        self.encode_calls = 0
        self.push_calls = 0

    def encode_playlist(
        self, playlist_file, profile, output_path, progress_cb=None,
        cancel_event=None, **_kwargs,
    ):
        self.encode_calls += 1
        # Honour the cancel contract just like the real pipeline:
        # if the event trips, kill the (mock) child and return
        # ok=False so the runner can branch into STATE_CANCELLED.
        # The delay is broken into 10 ms slices so cancel response
        # is bounded — the production code does the same in its
        # ffmpeg progress reader (one check per stdout line, which
        # arrives every ~500 ms for a real -progress block).
        if self.encode_delay_s:
            slept = 0.0
            slice_s = 0.01
            while slept < self.encode_delay_s:
                if cancel_event is not None and cancel_event.is_set():
                    return HookEncodeResult(
                        ok=False, output_path=Path("/tmp/x"),
                        duration_s=0.0, bytes=0,
                        log_tail="ยกเลิกระหว่าง encode (cancelled)",
                    )
                time.sleep(slice_s)
                slept += slice_s
        if self.encode_raises:
            raise self.encode_raises
        if progress_cb is not None:
            for pct, msg in self.encode_progress:
                if cancel_event is not None and cancel_event.is_set():
                    return HookEncodeResult(
                        ok=False, output_path=Path("/tmp/x"),
                        duration_s=0.0, bytes=0,
                        log_tail="ยกเลิกระหว่าง encode (cancelled)",
                    )
                progress_cb(pct, msg)
        return self.encode_result

    def push_to_phone(
        self, local_mp4, serial=None, target="", progress_cb=None,
        tiktok_pkg="", cancel_event=None, **_kwargs,
    ):
        self.push_calls += 1
        if self.push_raises:
            raise self.push_raises
        if progress_cb is not None:
            for pct, msg in self.push_progress:
                if cancel_event is not None and cancel_event.is_set():
                    return HookPushResult(
                        ok=False, bytes=0, elapsed_s=0.0, target="",
                        error="ยกเลิกระหว่าง push (cancelled)",
                    )
                progress_cb(pct, msg)
        return self.push_result


def _stub_write_playlist(tmp_path: Path):
    """Produce a unique playlist tempfile per call so concurrent
    runs don't collide. Returns the function suitable for
    ``write_playlist_fn`` injection."""
    counter = {"n": 0}
    lock = threading.Lock()

    def _impl(videos, loop):
        with lock:
            counter["n"] += 1
            n = counter["n"]
        p = tmp_path / f"playlist_{n}.txt"
        p.write_text("# fake playlist\n", encoding="utf-8")
        return p
    return _impl


# ── 1. Happy path ────────────────────────────────────────────────────


def test_encode_then_push_reaches_done(make_task, cfg, profile, tmp_path):
    task = make_task("AAA")
    pipe = _StubPipeline()
    updates: list[str] = []

    def on_update(t: EncodePushTask) -> None:
        updates.append(t.state)

    out = run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        on_update=on_update,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )

    assert out is task
    assert task.state == STATE_DONE
    assert task.progress == 1.0
    assert pipe.encode_calls == 1
    assert pipe.push_calls == 1
    # Every transition must fire the callback so the UI repaints.
    assert STATE_ENCODING in updates
    assert STATE_PUSHING in updates
    assert STATE_DONE in updates


def test_done_message_includes_size_and_elapsed(
    make_task, cfg, profile, tmp_path,
):
    task = make_task("AAA")
    pipe = _StubPipeline(
        push_result=HookPushResult(
            ok=True, bytes=20 * 1024 * 1024, elapsed_s=4.2, target="",
        ),
    )
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert "20.0 MB" in task.message
    assert "4.2" in task.message


# ── 2. Encode fails ──────────────────────────────────────────────────


def test_encode_fails_means_no_push(make_task, cfg, profile, tmp_path):
    task = make_task("AAA")
    pipe = _StubPipeline(
        encode_result=HookEncodeResult(
            ok=False, output_path=task.output, duration_s=0.0,
            bytes=0, log_tail="codec not found",
        ),
    )
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_ERROR
    assert "codec not found" in task.error
    assert pipe.push_calls == 0, (
        "push must NEVER fire after encode failed -- otherwise we'd "
        "push a half-written file and corrupt the customer's library"
    )


# ── 3. Push fails ────────────────────────────────────────────────────


def test_push_fails_after_encode(make_task, cfg, profile, tmp_path):
    task = make_task("AAA")
    pipe = _StubPipeline(
        push_result=HookPushResult(
            ok=False, bytes=0, elapsed_s=0.0, target="",
            error="device offline",
        ),
    )
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_ERROR
    assert "device offline" in task.error
    assert pipe.encode_calls == 1
    assert pipe.push_calls == 1


# ── 4. Missing source ────────────────────────────────────────────────


def test_missing_source_short_circuits(tmp_path, cfg, profile):
    task = EncodePushTask(
        serial="AAA",
        adb_id="AAA",
        source=tmp_path / "does_not_exist.mp4",
        output=tmp_path / "out.mp4",
        tiktok_pkg="com.example",
    )
    pipe = _StubPipeline()
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_ERROR
    # ``error`` carries the diagnostic with the missing path so
    # support can grep logs; ``message`` is the short Thai blurb
    # the dashboard surfaces under the button.
    assert "ไม่พบไฟล์คลิป" in task.error
    assert "ไฟล์คลิป" in task.message
    assert pipe.encode_calls == 0
    assert pipe.push_calls == 0


# ── 5. Encode raises ─────────────────────────────────────────────────


def test_encode_exception_caught_no_propagation(
    make_task, cfg, profile, tmp_path,
):
    """If the runner ever lets an exception escape, the daemon
    thread dies silently and the registry's ``has_running`` would
    return True forever — locking out future clicks for that
    serial. The fix: every code path returns a terminal-state task."""
    task = make_task("AAA")
    pipe = _StubPipeline(encode_raises=RuntimeError("boom"))
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_ERROR
    assert "boom" in task.error


# ── 6. Push raises ───────────────────────────────────────────────────


def test_push_exception_caught(make_task, cfg, profile, tmp_path):
    task = make_task("AAA")
    pipe = _StubPipeline(push_raises=OSError("usb dropped"))
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_ERROR
    assert "usb dropped" in task.error


# ── 7. Progress mapping ──────────────────────────────────────────────


def test_encode_progress_maps_to_first_half(
    make_task, cfg, profile, tmp_path,
):
    task = make_task("AAA")
    pipe = _StubPipeline(
        encode_progress=[(0.5, "encode 50 %")],
        push_progress=[],
    )
    seen_during_encode: list[float] = []

    def on_update(t: EncodePushTask) -> None:
        if t.state == STATE_ENCODING:
            seen_during_encode.append(t.progress)

    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        on_update=on_update,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    # Encode pct=0.5 must paint task.progress=0.25 (= 0.5 * 0.5)
    # so the combined bar shows the encode taking up the first
    # half of the progress lane.
    assert any(abs(p - 0.25) < 1e-9 for p in seen_during_encode), (
        f"encode progress 0.5 should map to 0.25 combined, "
        f"saw: {seen_during_encode}"
    )


def test_push_progress_maps_to_second_half(
    make_task, cfg, profile, tmp_path,
):
    task = make_task("AAA")
    pipe = _StubPipeline(
        encode_progress=[],
        push_progress=[(0.5, "push 50 %")],
    )
    seen_during_push: list[float] = []

    def on_update(t: EncodePushTask) -> None:
        if t.state == STATE_PUSHING:
            seen_during_push.append(t.progress)

    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        on_update=on_update,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    # Push pct=0.5 must paint combined progress=0.75.
    assert any(abs(p - 0.75) < 1e-9 for p in seen_during_push), (
        f"push progress 0.5 should map to 0.75 combined, "
        f"saw: {seen_during_push}"
    )


# ── 8. Parallel runs ─────────────────────────────────────────────────


def test_two_parallel_tasks_complete_independently(
    cfg, profile, src_clip, tmp_path,
):
    """The headline v1.8.6 contract: two encode+push runs on
    different serials must complete independently when fired on
    separate threads. We add a small artificial delay to the stub
    encode so both threads are *actually* in-flight at the same
    time (otherwise the first could complete before the second
    starts and the test would pass for the wrong reason).
    """
    task_a = EncodePushTask(
        serial="AAA",
        adb_id="AAA",
        source=src_clip,
        output=tmp_path / "out_AAA.mp4",
        tiktok_pkg="com.example",
    )
    task_b = EncodePushTask(
        serial="BBB",
        adb_id="BBB",
        source=src_clip,
        output=tmp_path / "out_BBB.mp4",
        tiktok_pkg="com.example",
    )

    # Both tasks share a single stub pipeline (mirroring the real
    # ``app.hook`` shared instance) — proving that the pipeline's
    # statelessness is what makes parallel safe.
    pipe = _StubPipeline(encode_delay_s=0.05)

    write_pl = _stub_write_playlist(tmp_path)

    threads = [
        threading.Thread(
            target=run_encode_push,
            kwargs=dict(
                pipeline=pipe, cfg=cfg, profile=profile, task=task,
                write_playlist_fn=write_pl,
            ),
            daemon=True,
        )
        for task in (task_a, task_b)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert task_a.state == STATE_DONE
    assert task_b.state == STATE_DONE
    assert pipe.encode_calls == 2
    assert pipe.push_calls == 2
    assert task_a.output != task_b.output, (
        "outputs must be distinct so libx264 can't race on the "
        "same descriptor — that was the v1.8.5 corruption hazard"
    )


# ── 9. Playlist cleanup ──────────────────────────────────────────────


def test_playlist_tempfile_cleaned_up_after_success(
    make_task, cfg, profile, tmp_path,
):
    written: list[Path] = []

    def write_pl(videos, loop):
        p = tmp_path / f"pl_{len(written)}.txt"
        p.write_text("# fake\n", encoding="utf-8")
        written.append(p)
        return p

    task = make_task("AAA")
    pipe = _StubPipeline()
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=write_pl,
    )
    assert task.state == STATE_DONE
    for p in written:
        assert not p.exists(), (
            "playlist tempfile must be unlinked after success — "
            "leaking these adds up to one per click and lives in "
            "the OS temp dir until customers notice disk pressure"
        )


# ── 10. Cancellation (CRITICAL #1 fix) ──────────────────────────────
#
# These tests pin down the orphan-child fix. Pre-v1.8.6 the runner
# had no idea the customer had clicked the close button, so daemon
# threads kept ffmpeg + adb children alive past parent process exit.
# The contract under test:
#
# * task.cancel_event.set() before the runner starts → STATE_CANCELLED
#   without spawning encode (fast path; relevant when the registry
#   has many queued tasks at app close).
# * cancel during encode loop → pipeline kills its child, runner
#   marks STATE_CANCELLED (NOT STATE_ERROR — those aren't user-visible
#   failures, they're voluntary stops).
# * cancel between encode and push → push never fires.
# * cancel during push → pipeline kills its child, STATE_CANCELLED.
# * cancel_all_running on a registry w/ N running tasks signals every
#   one in O(N), and follow-up cancel is idempotent.


def test_cancel_before_run_skips_encode_entirely(
    make_task, cfg, profile, tmp_path,
):
    task = make_task("AAA")
    task.request_cancel()
    pipe = _StubPipeline()
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_CANCELLED
    assert pipe.encode_calls == 0, (
        "cancel-before-start must short-circuit BEFORE ffmpeg "
        "spawns; otherwise app-close still leaves orphan children"
    )
    assert pipe.push_calls == 0


def test_cancel_during_encode_yields_cancelled_state(
    make_task, cfg, profile, tmp_path,
):
    task = make_task("AAA")
    # Encode delays for 1 s in 10 ms slices; we trip cancel after
    # 50 ms so the stub's loop notices on the next slice and returns
    # ok=False, mirroring the real ffmpeg progress reader contract.
    pipe = _StubPipeline(encode_delay_s=1.0)

    def _cancel_after_a_bit():
        time.sleep(0.05)
        task.request_cancel()

    threading.Thread(target=_cancel_after_a_bit, daemon=True).start()
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_CANCELLED, (
        f"expected STATE_CANCELLED, got {task.state!r}: "
        f"{task.message} / {task.error}"
    )
    assert pipe.push_calls == 0, "push must not run after cancel"


def test_cancel_between_encode_and_push(
    make_task, cfg, profile, tmp_path,
):
    """The runner re-checks ``cancel_event`` AFTER encode returns
    ok=True, so a cancel that arrives in the few-µs window between
    phases still aborts cleanly — instead of pushing 1 GB of bytes
    we asked it not to."""
    task = make_task("AAA")
    pipe = _StubPipeline()

    # Hook the encode call so the moment it returns ok=True we
    # request cancel. The runner's between-phase check then fires.
    real_encode = pipe.encode_playlist

    def _patched_encode(*a, **kw):
        result = real_encode(*a, **kw)
        task.request_cancel()
        return result

    pipe.encode_playlist = _patched_encode

    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_CANCELLED
    assert pipe.encode_calls == 1
    assert pipe.push_calls == 0


def test_cancel_during_push_yields_cancelled_state(
    make_task, cfg, profile, tmp_path,
):
    task = make_task("AAA")
    pipe = _StubPipeline(
        push_progress=[(0.1, "p10"), (0.2, "p20"), (0.5, "p50")],
    )
    real_push = pipe.push_to_phone

    def _patched_push(*a, **kw):
        # Trip cancel as the very first thing inside push so the
        # progress loop sees it on its first iteration and returns
        # ok=False — exactly the contract the real Popen+poll loop
        # honours when ``proc.kill()`` fires from outside.
        task.request_cancel()
        return real_push(*a, **kw)

    pipe.push_to_phone = _patched_push

    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=_stub_write_playlist(tmp_path),
    )
    assert task.state == STATE_CANCELLED, (
        f"expected STATE_CANCELLED, got {task.state!r}: {task.message}"
    )


def test_registry_cancel_all_running_signals_every_running_task():
    """``StudioApp._on_close`` relies on this — must not skip any
    in-flight task or we'll orphan a subset of children."""
    from src.config import StreamConfig
    from src.encode_push_tasks import (
        STATE_DONE,
        STATE_ENCODING,
        STATE_PUSHING,
        STATE_QUEUED,
        EncodePushRegistry,
        device_local_mp4,
    )

    cfg = StreamConfig.load()
    reg = EncodePushRegistry()

    def _t(serial: str, state: str) -> EncodePushTask:
        t = EncodePushTask(
            serial=serial, adb_id=serial,
            source=Path("/tmp/in.mp4"),
            output=device_local_mp4(cfg, serial),
            tiktok_pkg="com.example",
            state=state,
        )
        reg.upsert(t)
        return t

    queued = _t("Q", STATE_QUEUED)
    encoding = _t("E", STATE_ENCODING)
    pushing = _t("P", STATE_PUSHING)
    done = _t("D", STATE_DONE)

    n = reg.cancel_all_running()
    assert n == 3, "must signal exactly the 3 running tasks"
    assert queued.cancel_event.is_set()
    assert encoding.cancel_event.is_set()
    assert pushing.cancel_event.is_set()
    assert not done.cancel_event.is_set(), (
        "terminal-state tasks must NOT be re-cancelled — they're "
        "already done and signalling them would needlessly bump "
        "the cancel-pending counters in any future telemetry"
    )

    # Idempotent — calling twice returns 0 the second time only
    # because the workers have already noticed (in this test they
    # haven't, since there are no workers — so it's still 3). The
    # contract is "set the event for every running task"; the
    # method does NOT clear state, so a second call against the
    # same registry returns the same number. Assert we don't crash.
    n2 = reg.cancel_all_running()
    assert n2 == 3


def test_playlist_tempfile_cleaned_up_after_encode_failure(
    make_task, cfg, profile, tmp_path,
):
    written: list[Path] = []

    def write_pl(videos, loop):
        p = tmp_path / f"pl_{len(written)}.txt"
        p.write_text("# fake\n", encoding="utf-8")
        written.append(p)
        return p

    task = make_task("AAA")
    pipe = _StubPipeline(
        encode_result=HookEncodeResult(
            ok=False, output_path=task.output, duration_s=0.0,
            bytes=0, log_tail="oops",
        ),
    )
    run_encode_push(
        pipeline=pipe, cfg=cfg, profile=profile, task=task,
        write_playlist_fn=write_pl,
    )
    assert task.state == STATE_ERROR
    for p in written:
        assert not p.exists(), (
            "tempfile cleanup must run via finally regardless of "
            "encode/push outcome -- otherwise a string of failed "
            "encodes piles up garbage in the temp directory"
        )
