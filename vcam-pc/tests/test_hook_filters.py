"""Hook-mode video filter chain ordering.

The exact order of filters in the ``ffmpeg -vf`` chain is
load-bearing for live-stream correctness:

* **Rear default:** ``transpose=1`` then ``vflip`` (no ``hflip``).
* **Legacy front:** ``hflip`` MUST run **before** ``transpose=1`` so
  the mirror axis stays horizontal in the source frame.
* v1.8.21's default match-source path MUST NOT inject ``pad`` —
  padding caused portrait clips to look smaller than the source.
* v1.8.28 normalises the match-source output to a fixed height
  (``encode_scale_height``, default 1080) via ``scale=-2:H`` —
  still NO pad, aspect preserved. ``encode_scale_height=0`` restores
  the pure native-resolution behaviour.
* Legacy fixed-box mode still uses ``scale`` before ``pad`` so the
  letterboxing math works on the already-fitted frame; otherwise
  pad would crop or stretch.

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
        pipe = _pipeline(mirror_horizontal=True, hook_encode_rear_facing=False)
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert "hflip" in vf, "mirror_horizontal=True must inject hflip"
        assert vf.index("hflip") < vf.index("transpose=1"), (
            "hflip must run before transpose=1 to keep the mirror "
            "axis horizontal in the source frame"
        )

    def test_no_hflip_when_mirror_off_legacy(self):
        pipe = _pipeline(mirror_horizontal=False, hook_encode_rear_facing=False)
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert "hflip" not in vf, (
            "mirror_horizontal=False must skip hflip entirely"
        )
        assert "transpose=1" in vf, "transpose=1 is always required"
        assert "vflip" not in vf, "legacy chain has no vflip"

    def test_rear_facing_default_transpose2_then_vflip_no_hflip(self):
        pipe = _pipeline()
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert "hflip" not in vf
        assert vf.index("transpose=2") < vf.index("vflip")

    def test_match_source_default_scales_to_1080_no_pad(self):
        # v1.8.28: the default now normalises to 1080p height
        # (``encode_scale_height=1080``) preserving aspect (``-2``)
        # with NO letterbox pad — the "ต้องการให้เป็น 1080" request.
        pipe = _pipeline()
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert any(f.startswith("scale=-2:1080") for f in vf), (
            "default encode must scale to 1080 height preserving aspect"
        )
        assert not any(f.startswith("pad=") for f in vf), (
            "match-source encode must not letterbox-pad; padding is "
            "what made 9:16 customer clips look smaller than the source"
        )

    def test_match_source_zero_keeps_native_resolution(self):
        # ``encode_scale_height=0`` restores the pure v1.8.21
        # match-source behaviour: even-round only, never up/downscale.
        pipe = _pipeline(encode_scale_height=0)
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert any(
            f.startswith("scale=trunc(iw/2)*2:trunc(ih/2)*2")
            for f in vf
        )
        assert not any(f.startswith("scale=-2:") for f in vf)
        assert not any(f.startswith("pad=") for f in vf)

    def test_match_source_720_scale_height(self):
        pipe = _pipeline(encode_scale_height=720)
        vf = pipe._build_video_filter(_profile(), False, 1920, 1080)
        assert any(f.startswith("scale=-2:720") for f in vf)
        assert not any(f.startswith("pad=") for f in vf)

    def test_legacy_fixed_box_scale_precedes_pad(self):
        pipe = _pipeline(encode_match_source=False)
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

    def test_default_scale_height_and_crf(self):
        # v1.8.28: default output normalised to 1080p at CRF 16
        # (sharper + bigger file to survive TikTok's re-encode).
        cfg = StreamConfig()
        assert cfg.encode_scale_height == 1080
        assert cfg.encode_crf == 16

    def test_defaults_rear_hook_encode_path(self):
        cfg = StreamConfig()
        assert cfg.hook_encode_rear_facing is True
        assert cfg.mirror_horizontal is False

    def test_legacy_filter_chain_resolution_propagates(self):
        # 720p preset in the legacy fixed-box path.
        pipe = _pipeline(
            encode_width=1280,
            encode_height=720,
            encode_match_source=False,
        )
        vf = pipe._build_video_filter(_profile(), False, 1280, 720)
        assert any("scale=1280:720" in f for f in vf)
        assert any("pad=1280:720" in f for f in vf)
