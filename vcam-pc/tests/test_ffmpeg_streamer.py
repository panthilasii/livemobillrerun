"""Unit tests for the FFmpeg command-line builder."""

from __future__ import annotations

from pathlib import Path

from src.config import DeviceProfile, StreamConfig
from src.ffmpeg_streamer import FFmpegStreamer


def _build(rotation: str = "transpose=2,vflip", **cfg_kwargs: object) -> list[str]:
    cfg = StreamConfig(**cfg_kwargs)  # type: ignore[arg-type]
    profile = DeviceProfile(name="t", rotation_filter=rotation)
    streamer = FFmpegStreamer(cfg)
    return streamer.build_cmd(Path("/tmp/playlist.txt"), profile)


def test_basic_command_shape() -> None:
    cmd = _build()
    assert cmd[0].endswith("ffmpeg")
    assert cmd[-1] == "pipe:1"
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "concat"
    assert cmd[-3] == "-f" and cmd[-2] == "h264"


def test_loop_flag_only_when_requested() -> None:
    cmd_loop = _build(loop_playlist=True)
    assert "-stream_loop" in cmd_loop and cmd_loop[cmd_loop.index("-stream_loop") + 1] == "-1"

    cmd_noloop = _build(loop_playlist=False)
    assert "-stream_loop" not in cmd_noloop


def test_audio_is_dropped() -> None:
    cmd = _build()
    assert "-an" in cmd


def test_h264_baseline_for_compatibility() -> None:
    cmd = _build()
    i = cmd.index("-profile:v")
    assert cmd[i + 1] == "baseline"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"


def test_rotation_appears_in_filter_chain() -> None:
    cmd = _build(rotation="transpose=2,vflip")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("transpose=2,vflip,")
    assert "scale=1280:720" in vf
    assert "fps=30" in vf
    assert "pad=1280:720" in vf


def test_rotation_none_omitted_from_filter_chain() -> None:
    cmd = _build(rotation="none")
    vf = cmd[cmd.index("-vf") + 1]
    assert not vf.startswith("transpose")
    assert "scale=1280:720" in vf


def test_keyint_is_fps_times_seconds() -> None:
    cmd = _build(fps=30, keyint_seconds=2)
    g = cmd[cmd.index("-g") + 1]
    assert g == "60"
    kmin = cmd[cmd.index("-keyint_min") + 1]
    assert kmin == "60"


def test_resolution_propagates() -> None:
    cmd = _build(resolution="1920x1080")
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080" in vf
    assert "pad=1920:1080" in vf


def test_bitrate_settings_propagate() -> None:
    cmd = _build(video_bitrate="3000k", video_maxrate="3500k", video_bufsize="6000k")
    assert cmd[cmd.index("-b:v") + 1] == "3000k"
    assert cmd[cmd.index("-maxrate") + 1] == "3500k"
    assert cmd[cmd.index("-bufsize") + 1] == "6000k"
