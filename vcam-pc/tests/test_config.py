"""Unit tests for src.config — no FFmpeg / ADB needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import DeviceProfile, ProfileLibrary, StreamConfig


def test_stream_config_defaults() -> None:
    cfg = StreamConfig()
    assert cfg.tcp_port == 8888
    assert cfg.fps == 30
    # Portrait 720×1280 — TikTok Live is vertical-first, so the
    # default resolution flipped from landscape early in v1.0.
    assert cfg.width == 720
    assert cfg.height == 1280
    assert cfg.loop_playlist is True


def test_stream_config_load_drops_unknown_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "tcp_port": 9999,
                "fps": 60,
                "videos_dir": "vids",
                "garbage_key": "ignored",  # must be silently dropped
                "another_unknown": 42,
            }
        )
    )
    cfg = StreamConfig.load(p)
    assert cfg.tcp_port == 9999
    assert cfg.fps == 60
    assert cfg.videos_dir == "vids"


def test_stream_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = StreamConfig.load(tmp_path / "nope.json")
    assert cfg.tcp_port == 8888


def test_resolution_parsing() -> None:
    cfg = StreamConfig(resolution="1920x1080")
    assert (cfg.width, cfg.height) == (1920, 1080)


def test_resolution_invalid_format_raises() -> None:
    cfg = StreamConfig(resolution="not-a-resolution")
    with pytest.raises(ValueError):
        _ = cfg.width


def test_profile_library_load_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "profiles.json"
    p.write_text(
        json.dumps(
            {
                "profiles": [
                    {"name": "Foo", "rotation_filter": "transpose=1"},
                    {"name": "Bar", "rotation_filter": "none", "notes": "n/a"},
                ]
            }
        )
    )
    lib = ProfileLibrary.load(p)
    assert lib.names() == ["Foo", "Bar"]
    foo = lib.get("Foo")
    assert isinstance(foo, DeviceProfile)
    assert foo.rotation_filter == "transpose=1"
    assert lib.get("missing") is None


def test_profile_library_missing_file_returns_fallback(tmp_path: Path) -> None:
    lib = ProfileLibrary.load(tmp_path / "nope.json")
    assert "Generic / unknown" in lib.names()
