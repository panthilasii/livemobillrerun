"""Adaptive encode + push timeouts.

The hook-mode pipeline used to hard-code 600 s for ffmpeg encode
and 120 s for ``adb push``. Those caps silently killed any work on
multi-GB clips: customers reported "stuck at encode" because the
process was SIGTERMed mid-stream after 10 minutes and the UI just
showed an empty error.

These tests pin the new heuristics so future refactors can't
quietly regress to fixed timeouts.
"""
from __future__ import annotations

from src.hook_mode import HookModePipeline


class TestEncodeTimeout:
    def test_floor_is_ten_minutes(self):
        # A 5-second clip must still grant the original 10-minute
        # floor -- short clips on slow disks were never the bug,
        # we don't want to make them worse.
        assert HookModePipeline._encode_timeout(5.0, 50 * 1024 ** 2) == 600

    def test_scales_with_clip_duration(self):
        # 17-minute (~1.9 GB at 15 Mbps) clip is the customer's
        # actual reported failure. ``4× duration + buffers`` gives
        # us comfortable headroom on slower laptops.
        d_sec = 17 * 60                  # 17 min source
        size = int(1.9 * 1024 ** 3)      # 1.9 GB
        t = HookModePipeline._encode_timeout(d_sec, size)
        # 4*17min = 68min; +60s setup; +30s/GB * 1.9 ~= 57s.
        # Total ~70 minutes, well above the 10-min floor that used
        # to kill the process.
        assert t >= 60 * 60, f"expected >= 1 hour for 1.9 GB clip, got {t}s"

    def test_negative_inputs_clamped(self):
        # Bad ffprobe output yielded -1 in production once -- guard
        # against the math going negative.
        assert HookModePipeline._encode_timeout(-5, -1024) >= 600


class TestPushTimeout:
    def test_floor_is_two_minutes(self):
        # 50 MB clip on USB 3.0 takes ~2 s. Don't shrink the floor
        # below 120 s -- short pushes can still stall on enumeration.
        assert HookModePipeline._push_timeout(50 * 1024 ** 2) == 120

    def test_scales_with_file_size(self):
        # 1.9 GB at the documented 3 MB/s slowest-case throughput
        # = ~635 s = ~11 minutes. The old hard-coded 120 s blew up
        # well before the push could even finish.
        size = int(1.9 * 1024 ** 3)
        t = HookModePipeline._push_timeout(size)
        assert t >= 600, (
            f"expected >= 10 min for 1.9 GB push, got {t}s"
        )

    def test_negative_size_clamped(self):
        assert HookModePipeline._push_timeout(-1) == 120
