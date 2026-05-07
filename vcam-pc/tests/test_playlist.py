"""Unit tests for src.playlist."""

from __future__ import annotations

from pathlib import Path

from src.playlist import VIDEO_EXTS, list_videos, write_playlist


def test_list_videos_empty_dir(tmp_path: Path) -> None:
    assert list_videos(tmp_path) == []


def test_list_videos_missing_dir(tmp_path: Path) -> None:
    assert list_videos(tmp_path / "does_not_exist") == []


def test_list_videos_filters_by_extension(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.MP4").write_bytes(b"x")
    (tmp_path / "c.webm").write_bytes(b"x")
    (tmp_path / "ignore.txt").write_bytes(b"x")
    (tmp_path / "ignore.jpg").write_bytes(b"x")
    (tmp_path / "subdir").mkdir()

    found = [p.name for p in list_videos(tmp_path)]
    assert sorted(found) == ["a.mp4", "b.MP4", "c.webm"]


def test_list_videos_sorted_case_insensitive(tmp_path: Path) -> None:
    for name in ["Charlie.mp4", "alpha.mp4", "Bravo.mp4"]:
        (tmp_path / name).write_bytes(b"x")
    names = [p.name for p in list_videos(tmp_path)]
    assert names == ["alpha.mp4", "Bravo.mp4", "Charlie.mp4"]


def test_video_exts_covers_common_formats() -> None:
    assert ".mp4" in VIDEO_EXTS
    assert ".mov" in VIDEO_EXTS
    assert ".mkv" in VIDEO_EXTS


def test_write_playlist_quotes_paths(tmp_path: Path) -> None:
    v1 = tmp_path / "first.mp4"
    v2 = tmp_path / "second one.mp4"
    v1.write_bytes(b"x")
    v2.write_bytes(b"x")

    pl = write_playlist([v1, v2])
    try:
        text = pl.read_text(encoding="utf-8")
        assert text.count("\n") == 2
        # FFmpeg concat demuxer expects: file '<absolute path>'
        assert f"file '{v1.resolve()}'" in text
        assert f"file '{v2.resolve()}'" in text
    finally:
        pl.unlink(missing_ok=True)


def test_write_playlist_escapes_single_quotes(tmp_path: Path) -> None:
    weird = tmp_path / "it's_a_video.mp4"
    weird.write_bytes(b"x")

    pl = write_playlist([weird])
    try:
        text = pl.read_text(encoding="utf-8")
        # FFmpeg concat-demuxer single-quote escape: '\''
        assert r"'\''" in text
    finally:
        pl.unlink(missing_ok=True)
