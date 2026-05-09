"""Hook-mode video filter chain ordering.

The exact order of filters in the ``ffmpeg -vf`` chain is
load-bearing for live-stream correctness:

* ``hflip`` MUST run **before** ``transpose=1`` so the mirror axis
  stays horizontal. Reversing the order rotates the flip axis 90°
  and produces an upside-down picture once TikTok mirrors it back.
* ``scale`` MUST run before ``pad`` so the letterboxing math works
  on the already-fitted frame; otherwise pad would crop or stretch.

These tests exist to catch regressions where someone rearranges
the filter list "for clarity" and silently breaks the broadcast.
"""
from __future__ import annotations

from pathlib import Path

from src.config import DeviceProfile, StreamConfig
from src.hook_mode import HookModePipeline


def _pipeline(**overrides) -> HookModePipeline:
    cfg = StreamConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    # Bypass __post_init__ side-effects (config write etc.) by
    # constructing the pipeline directly.
    return HookModePipeline(cfg)


def _profile() -> DeviceProfile:
    return DeviceProfile(name="test", rotation_filter="none")


class TestVideoFilterOrdering:
    def test_hflip_precedes_transpose_when_mirror_on(self):
        pipe = _pipeline(mirror_horizontal=True)
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert "hflip" in vf, "mirror_horizontal=True must inject hflip"
        assert vf.index("hflip") < vf.index("transpose=1"), (
            "hflip must run before transpose=1 to keep the mirror "
            "axis horizontal in the source frame"
        )

    def test_no_hflip_when_mirror_off(self):
        pipe = _pipeline(mirror_horizontal=False)
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert "hflip" not in vf, (
            "mirror_horizontal=False must skip hflip entirely"
        )
        assert "transpose=1" in vf, "transpose=1 is always required"

    def test_scale_precedes_pad(self):
        pipe = _pipeline()
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        scale_idx = next(i for i, f in enumerate(vf) if f.startswith("scale="))
        pad_idx = next(i for i, f in enumerate(vf) if f.startswith("pad="))
        assert scale_idx < pad_idx, (
            "scale must run before pad; otherwise letterboxing math "
            "is computed against the wrong frame size"
        )

    def test_setsar_is_last(self):
        pipe = _pipeline()
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert vf[-1] == "setsar=1", (
            "setsar=1 must be the final filter; TikTok's MediaPlayer "
            "rejects non-square pixels on some Android builds"
        )

    def test_default_resolution_is_1080p(self):
        # Sanity: encoder defaults match the v1.4.2 product decision.
        cfg = StreamConfig()
        assert cfg.encode_width == 1920
        assert cfg.encode_height == 1080

    def test_default_mirror_is_on(self):
        # Customers shipping to TikTok almost always need this; we
        # default it on and let them turn it off in Settings.
        cfg = StreamConfig()
        assert cfg.mirror_horizontal is True

    def test_filter_chain_resolution_propagates(self):
        # 720p preset.
        pipe = _pipeline(encode_width=1280, encode_height=720)
        vf = pipe._build_video_filter(_profile(), False, 1280, 720)
        assert any("scale=1280:720" in f for f in vf)
        assert any("pad=1280:720" in f for f in vf)
