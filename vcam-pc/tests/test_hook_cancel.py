"""Encode + push cancellation contract (CRITICAL #1, v1.8.6).

Pre-fix, ``HookModePipeline.encode_playlist`` and ``push_to_phone``
spawned ffmpeg / adb children and had no way to be told "stop now".
On app close the daemon worker thread died with the process but
its subprocess child kept running — customers ended up with
orphaned 100 %-CPU ffmpeg encoders pinning a core and orphaned
``adb push`` transfers blocking the next session's USB.

The fix routes a ``threading.Event`` from
``EncodePushTask.cancel_event`` down through ``run_encode_push``
into the pipeline's polling loops. When the event trips:

* the encode loop sees it on its next ``-progress pipe:1`` line,
  ``proc.kill()``s ffmpeg, waits ≤ 5 s for reaping, and returns
  ``HookEncodeResult(ok=False, log_tail="ยกเลิกระหว่าง encode …")``;
* the push loop checks the event between ``proc.wait(timeout=0.1)``
  ticks (we switched from blocking ``subprocess.run`` to ``Popen``
  + poll for exactly this reason), kills the adb child, and
  returns ``HookPushResult(ok=False, error="ยกเลิกระหว่าง push …")``.

These tests pin both branches without spawning real ffmpeg / adb —
we patch ``subprocess.Popen`` with a fake proc whose ``wait``
times out forever, then trip ``cancel_event`` from another thread
and assert the polling loop notices, calls ``kill``, and returns
the cancelled result.
"""
from __future__ import annotations

import io
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.hook_mode import HookModePipeline


# ── shared helpers ───────────────────────────────────────────────────


class _BlockingProc:
    """Mimics a long-running ``subprocess.Popen``.

    ``wait(timeout=0.1)`` always raises ``TimeoutExpired`` until
    ``kill()`` is called, mirroring the real adb push semantics
    when the binary is mid-transfer. After ``kill``, ``wait``
    returns -9 (SIGKILL) like a real Popen.
    """

    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.returncode: int | None = None
        self._killed = threading.Event()
        self.kill_calls = 0
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._killed.is_set():
            self.returncode = -9
            return -9
        # No-op sleep for the short polling window so the test's
        # cancel-from-other-thread has time to fire.
        if timeout is not None:
            time.sleep(min(timeout, 0.05))
        if self._killed.is_set():
            self.returncode = -9
            return -9
        raise subprocess.TimeoutExpired(cmd=["fake"], timeout=timeout or 0)

    def kill(self) -> None:
        self.kill_calls += 1
        self._killed.set()
        self.returncode = -9

    def communicate(self, timeout: float | None = None):
        return "", ""

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def pipeline() -> HookModePipeline:
    cfg = MagicMock()
    cfg.encode_width = 1920
    cfg.encode_height = 1080
    cfg.fps = 30
    cfg.keyint_seconds = 2
    cfg.video_bitrate = "4M"
    cfg.video_maxrate = "4500k"
    cfg.video_bufsize = "8M"
    cfg.loop_playlist = False
    cfg.mirror_horizontal = True
    return HookModePipeline(cfg)


# ── encode_playlist — cancel during -progress reader ─────────────────


class _StdoutBlockingForever:
    """Iterator that yields the SAME progress block forever, with a
    short sleep between yields. The encode reader reads one line,
    checks the cancel event, then loops — so this lets us simulate
    a real ffmpeg that's mid-encode and not exiting on its own.
    """

    def __init__(self) -> None:
        self._stop = False

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(0.02)
        return "out_time_us=1000000\n"


class _FfmpegProc:
    def __init__(self) -> None:
        self.stdout = _StdoutBlockingForever()
        self.stderr = io.StringIO("")
        self.returncode: int | None = None
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


def test_encode_loop_kills_ffmpeg_on_cancel_event(pipeline, tmp_path):
    """The ffmpeg progress reader must check ``cancel_event`` on
    every line; when it trips, kill the child and return ok=False
    with a Thai cancelled diagnostic. Without this, an app-close
    mid-encode leaves a dangling 100 %-CPU ffmpeg child."""
    proc = _FfmpegProc()
    cancel_event = threading.Event()

    # Fire cancel from a sidecar thread after the reader has had
    # time to enter its loop. 50 ms is well above the iterator's
    # 20 ms tick so the next __next__ unblocks before we check.
    def _trip():
        time.sleep(0.05)
        cancel_event.set()

    threading.Thread(target=_trip, daemon=True).start()

    with patch("subprocess.Popen", return_value=proc):
        result = pipeline._run_ffmpeg_with_progress(
            cmd=["ffmpeg"],
            output_path=tmp_path / "out.mp4",
            duration_s=60.0,
            timeout_s=600,
            progress_cb=None,
            t0=time.monotonic(),
            cancel_event=cancel_event,
        )

    assert result.ok is False
    assert proc.kill_calls >= 1, (
        "ffmpeg child must be killed on cancel — leaving it alive "
        "is exactly the orphan-process bug we're fixing"
    )
    assert "ยกเลิก" in result.log_tail


def test_encode_without_cancel_event_runs_normally(pipeline, tmp_path):
    """Backwards compatibility — the ``cancel_event`` parameter is
    optional, and callers that don't pass one (e.g. legacy single-
    task code paths in the wild) must keep working unchanged."""

    class _FiniteStdout:
        def __iter__(self):
            yield "out_time_us=30000000\n"
            yield "progress=end\n"

    proc = MagicMock()
    proc.stdout = _FiniteStdout()
    proc.stderr = io.StringIO("")
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    proc.kill = MagicMock()

    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)

    with patch("subprocess.Popen", return_value=proc):
        result = pipeline._run_ffmpeg_with_progress(
            cmd=["ffmpeg"],
            output_path=out,
            duration_s=60.0,
            timeout_s=600,
            progress_cb=None,
            t0=time.monotonic(),
            # explicitly default — no cancel_event
        )

    assert result.ok is True
    assert proc.kill.call_count == 0, (
        "without cancel_event, kill() must never fire on the happy path"
    )


# ── push_to_phone — cancel during Popen poll loop ────────────────────


def test_push_loop_kills_adb_on_cancel_event(pipeline, tmp_path, monkeypatch):
    """``push_to_phone`` switched from blocking ``subprocess.run``
    to ``Popen`` + ``proc.wait(timeout=0.1)`` poll loop in v1.8.6
    so the cancel event has somewhere to be checked. Verify the
    loop kills the adb child and returns ok=False on cancel.
    """
    src_mp4 = tmp_path / "v.mp4"
    src_mp4.write_bytes(b"x" * 4096)

    # Pretend adb resolves so the early bailout for missing tooling
    # doesn't short-circuit before we get to the poll loop.
    monkeypatch.setattr(pipeline, "_resolve_adb", lambda: "/fake/adb")
    monkeypatch.setattr(
        pipeline, "_spawn_push_sampler",
        lambda **_kw: None,
    )
    # Tighten the per-size timeout so the test never has to wait
    # the production 5+ minute default if the cancel logic is
    # broken.
    monkeypatch.setattr(pipeline, "_push_timeout", lambda _size: 60)

    proc = _BlockingProc()
    cancel_event = threading.Event()

    def _trip():
        time.sleep(0.05)
        cancel_event.set()

    threading.Thread(target=_trip, daemon=True).start()

    # ``mkdir -p`` is a one-shot subprocess.run BEFORE the Popen
    # poll loop; let it pass through with a benign success.
    real_run = subprocess.run

    def _fake_run(cmd, *a, **kw):
        # Only the mkdir call comes through here now (the push
        # itself is via Popen). Return a synthetic CompletedProcess.
        cp = subprocess.CompletedProcess(cmd, 0, "", "")
        return cp

    monkeypatch.setattr(subprocess, "run", _fake_run)
    try:
        with patch("subprocess.Popen", return_value=proc):
            result = pipeline.push_to_phone(
                local_mp4=src_mp4,
                serial="AAA",
                target="/sdcard/test.mp4",
                progress_cb=None,
                tiktok_pkg="com.example",
                cancel_event=cancel_event,
            )
    finally:
        monkeypatch.setattr(subprocess, "run", real_run)

    assert result.ok is False
    assert proc.kill_calls >= 1, (
        "adb push child must be killed on cancel — otherwise the "
        "1 GB transfer keeps the USB busy past app exit"
    )
    assert "ยกเลิก" in result.error
