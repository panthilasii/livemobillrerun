"""Encode + push progress reporting.

The hook pipeline streams progress to a callback so the Dashboard
can paint a real percentage bar. These tests pin the parser
behaviour without spawning ffmpeg or adb -- we drive the parser
directly with canned ``-progress pipe:1`` output.

What we lock in
---------------

* ffmpeg ``out_time_us=N`` → ``percent = N / 1e6 / duration_s``
* The parser only fires the callback on **meaningful** changes
  (>= 0.5 percentage points) so Tk doesn't get a flood of repaints
  on fast encodes.
* Final ``progress=end`` → percent = 1.0 with "Encode เสร็จ".
* If ``duration_s == 0`` (probe failed), no percentage is reported
  but the encode still runs to completion.
* If the callback raises, the encode ITSELF must not crash --
  callback bugs cannot break the customer's primary workflow.

Implementation note for ``t0`` arguments below: the parser uses
``deadline = t0 + timeout_s`` against ``time.monotonic()``. If a
test passed ``t0=0`` while the real monotonic clock has been
running for hours (typical on a long-lived CI worker), the
deadline would already be in the past and the parser would bail
out before seeing any input. We pass a freshly-sampled
``time.monotonic()`` everywhere -- the test isn't measuring
timeout behaviour, just parser semantics.
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.hook_mode import HookEncodeResult, HookModePipeline


# ── helpers ─────────────────────────────────────────────────────


class _FakeStdout:
    """Stand-in for ``proc.stdout`` (a line-iterable text stream)."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)


class _FakeProc:
    """Mimic just enough of ``subprocess.Popen`` for the parser
    under test. We never spawn a real process here -- the parser
    consumes ``stdout`` and queries ``returncode`` / ``wait()``."""

    def __init__(self, stdout_lines: list[str], returncode: int = 0) -> None:
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self._waited = False

    def wait(self, timeout: float | None = None) -> int:
        self._waited = True
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


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


def _now() -> float:
    return time.monotonic()


# ── parser semantics ────────────────────────────────────────────


class TestEncodeProgress:
    def test_out_time_us_maps_to_correct_percent(self, pipeline, tmp_path):
        """A 60 s clip with out_time_us=30_000_000 should report
        50% (30 s / 60 s)."""
        calls: list[tuple[float, str]] = []
        proc = _FakeProc(
            stdout_lines=[
                "frame=900\n",
                "out_time_us=30000000\n",
                "progress=continue\n",
                "frame=1800\n",
                "out_time_us=60000000\n",
                "progress=end\n",
            ],
        )
        with patch("subprocess.Popen", return_value=proc):
            pipeline._run_ffmpeg_with_progress(
                cmd=["ffmpeg"],
                output_path=tmp_path / "out.mp4",
                duration_s=60.0,
                timeout_s=600,
                progress_cb=lambda p, m: calls.append((p, m)),
                t0=_now(),
            )
        pcts = [p for p, _ in calls]
        assert any(abs(p - 0.5) < 0.02 for p in pcts), (
            f"missing 50% sample: {pcts}"
        )
        assert calls[-1][0] == 1.0, f"final sample must be 1.0: {calls[-1]}"
        assert calls[-1][1] == "Encode เสร็จ"

    def test_quiet_when_duration_unknown(self, pipeline, tmp_path):
        """If ffprobe failed (duration_s == 0), don't fabricate a
        percentage -- the bar should stay where the caller put it
        rather than jump randomly. We DO still expect the final
        ``progress=end`` callback at 1.0 so the UI can clear its
        spinner."""
        calls: list[tuple[float, str]] = []
        proc = _FakeProc(
            stdout_lines=[
                "frame=100\n",
                "out_time_us=5000000\n",
                "progress=continue\n",
                "progress=end\n",
            ],
        )
        with patch("subprocess.Popen", return_value=proc):
            pipeline._run_ffmpeg_with_progress(
                cmd=["ffmpeg"],
                output_path=tmp_path / "out.mp4",
                duration_s=0.0,
                timeout_s=600,
                progress_cb=lambda p, m: calls.append((p, m)),
                t0=_now(),
            )
        non_end = [c for c in calls if c[1] != "Encode เสร็จ"]
        assert non_end == [], (
            f"unexpected mid-stream samples without duration: {non_end}"
        )

    def test_callback_throttled(self, pipeline, tmp_path):
        """The parser must coalesce sub-half-percent ticks. ffmpeg
        emits a -progress block every ~0.5 s, so on a 30 s encode
        we'd otherwise get 60 callbacks for a bar with ~200 visible
        pixels of resolution. Throttling keeps Tk repainting cheap."""
        calls: list[tuple[float, str]] = []
        lines: list[str] = []
        for i in range(1, 1001):
            us = int(60_000_000 * (i / 1000))
            lines += [
                f"frame={i}\n",
                f"out_time_us={us}\n",
                "progress=continue\n",
            ]
        lines.append("progress=end\n")
        proc = _FakeProc(stdout_lines=lines)
        with patch("subprocess.Popen", return_value=proc):
            pipeline._run_ffmpeg_with_progress(
                cmd=["ffmpeg"],
                output_path=tmp_path / "out.mp4",
                duration_s=60.0,
                timeout_s=600,
                progress_cb=lambda p, m: calls.append((p, m)),
                t0=_now(),
            )
        assert len(calls) < 250, (
            f"throttling failed: {len(calls)} callbacks for 1000 ticks"
        )
        assert calls[-1][0] == 1.0

    def test_callback_failure_does_not_crash(self, pipeline, tmp_path):
        """A buggy progress callback (e.g. UI widget destroyed mid-
        encode) must NOT propagate up and kill the encoder. The
        encode is the customer's primary workflow; observability
        is secondary."""
        proc = _FakeProc(
            stdout_lines=[
                "out_time_us=5000000\n",
                "out_time_us=10000000\n",
                "progress=end\n",
            ],
        )

        def _bad_cb(_p: float, _m: str) -> None:
            raise RuntimeError("UI widget gone")

        with patch("subprocess.Popen", return_value=proc):
            res = pipeline._run_ffmpeg_with_progress(
                cmd=["ffmpeg"],
                output_path=tmp_path / "out.mp4",
                duration_s=10.0,
                timeout_s=600,
                progress_cb=_bad_cb,
                t0=_now(),
            )
        assert isinstance(res, HookEncodeResult)
        assert proc._waited
