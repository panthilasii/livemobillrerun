"""Configuration loader for vcam-pc.

Reads `config.json` (global settings) and `device_profiles.json`
(per-device rotation filters). Both live next to the project root, not
inside `src/`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
PROFILES_PATH = PROJECT_ROOT / "device_profiles.json"


@dataclass
class StreamConfig:
    # ``ffmpeg_path`` and ``adb_path`` default to bare names so a
    # user's system install on PATH wins. The resolver
    # (``platform_tools.find_*``) handles the bundled-binary fall-back.
    ffmpeg_path: str = "ffmpeg"
    adb_path: str = "adb"
    tcp_port: int = 8888
    resolution: str = "720x1280"
    fps: int = 30
    video_bitrate: str = "2000k"
    video_maxrate: str = "2500k"
    video_bufsize: str = "4000k"
    keyint_seconds: int = 2
    loop_playlist: bool = True
    auto_adb_reverse: bool = True
    videos_dir: str = "videos"
    default_profile: str = "Redmi 13C"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "StreamConfig":
        if not path.is_file():
            log.warning("config.json not found at %s — using defaults", path)
            return cls()
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Drop unknown keys so we don't blow up on schema drift.
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in data.items() if k in valid}
        return cls(**clean)

    def save(self, path: Path = CONFIG_PATH) -> None:
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=2, ensure_ascii=False)

    @property
    def width(self) -> int:
        return int(self.resolution.split("x", 1)[0])

    @property
    def height(self) -> int:
        return int(self.resolution.split("x", 1)[1])

    @property
    def videos_path(self) -> Path:
        p = Path(self.videos_dir)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


@dataclass
class DeviceProfile:
    name: str
    model: str = "generic"
    soc_hint: str = ""
    rotation_filter: str = "none"
    notes: str = ""


@dataclass
class ProfileLibrary:
    profiles: list[DeviceProfile] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = PROFILES_PATH) -> "ProfileLibrary":
        if not path.is_file():
            log.warning("device_profiles.json missing at %s — empty library", path)
            return cls(profiles=[DeviceProfile(name="Generic / unknown")])
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        profiles = [
            DeviceProfile(
                name=p.get("name", "?"),
                model=p.get("model", "generic"),
                soc_hint=p.get("soc_hint", ""),
                rotation_filter=p.get("rotation_filter", "none"),
                notes=p.get("notes", ""),
            )
            for p in data.get("profiles", [])
        ]
        return cls(profiles=profiles)

    def get(self, name: str) -> DeviceProfile | None:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def names(self) -> list[str]:
        return [p.name for p in self.profiles]
