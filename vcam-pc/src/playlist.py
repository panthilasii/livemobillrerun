"""Playlist file generator for FFmpeg's concat demuxer.

We collect every video file in `videos/`, write a temporary `playlist.txt`,
and return its path. FFmpeg consumes it with `-f concat -safe 0 -i …`.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts"}


def list_videos(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [
        p
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower())
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]
    return files


def write_playlist(videos: list[Path], loop: bool = True) -> Path:
    """Write FFmpeg concat-demuxer compatible playlist file.

    The `loop` kwarg is consumed at the FFmpeg layer (`-stream_loop -1`),
    not here — but we accept it for API symmetry.
    """
    fd, path = tempfile.mkstemp(prefix="vcam_playlist_", suffix=".txt", text=True)
    pl = Path(path)
    with pl.open("w", encoding="utf-8") as f:
        for v in videos:
            # Escape single quotes per FFmpeg concat demuxer rules.
            escaped = str(v.resolve()).replace("'", r"'\''")
            f.write(f"file '{escaped}'\n")
    log.info("Wrote playlist with %d items: %s", len(videos), pl)
    return pl
