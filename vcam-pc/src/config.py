"""Configuration loader for vcam-pc.

Reads `config.json` (global settings) and `device_profiles.json`
(per-device rotation filters). Both live next to the project root, not
inside `src/`.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

def _resolve_install_root() -> Path:
    """Single source of truth for "where do my data files live?"

    Three runtime modes are possible:

    1. **Source tree** (developer, ``python -m src.cli``):
       ``__file__`` resolves under ``vcam-pc/src/config.py`` so
       ``parent.parent`` is ``vcam-pc/`` — the historical layout.

    2. **Customer runs ZIP layout via run.bat / run.command** —
       same as (1): ``__file__`` is still ``vcam-pc/src/config.py``
       relative to the unzipped tree.

    3. **PyInstaller frozen build** (.exe via Inno Setup, .app via
       .dmg drag-to-Applications): ``sys.frozen`` is set. We anchor
       on ``Path(sys.executable).parent`` — the directory the
       installer dropped the binary into. Inno Setup installs
       to ``%LOCALAPPDATA%\\NP Create\\`` and lays ``.tools\\`` /
       ``apk\\`` next to ``NP-Create.exe``; ``build_dmg.sh``
       produces a ``.app`` whose ``Contents/MacOS/`` / ``Contents/
       Resources/`` siblings hold the same data. Either way the
       *binary's parent dir* is where read/write state belongs,
       which matters because:

       - ``sys._MEIPASS`` (the alternative anchor) is a *temp*
         directory wiped at exit on --onefile builds; writing
         ``config.json`` there silently loses settings on every
         relaunch.
       - The binary's parent dir survives reboots and is the
         only persistent location every PyInstaller mode shares.

       Bundled read-only assets (``src/``, ``assets/``) are pulled
       in via ``--add-data`` in ``tools/build_pyinstaller.py`` and
       resolve from ``sys._MEIPASS`` automatically through the
       Python import system — those don't need PROJECT_ROOT.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_data_root(install_root: Path) -> Path:
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "NP Create"
        p.mkdir(parents=True, exist_ok=True)
        bundled_cfg = install_root / "config.json"
        target_cfg = p / "config.json"
        if bundled_cfg.is_file() and not target_cfg.is_file():
            try:
                import shutil
                shutil.copy2(bundled_cfg, target_cfg)
            except OSError:
                pass
        return p
    return install_root


INSTALL_ROOT = _resolve_install_root()
DATA_ROOT = _resolve_data_root(INSTALL_ROOT)
PROJECT_ROOT = INSTALL_ROOT
CONFIG_PATH = DATA_ROOT / "config.json"
PROFILES_PATH = INSTALL_ROOT / "device_profiles.json"


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
    # Hook-mode MP4 output dimensions (landscape). The phone's
    # rotation chain turns this back into a portrait clip on screen.
    # 1920×1080 = "1080p" — the highest quality TikTok's ingest will
    # accept reliably across mid-range Android phones. Drop to
    # 1280×720 ("720p") on older / lower-RAM devices if the encode
    # takes too long or playback stutters.
    encode_width: int = 1920
    encode_height: int = 1080

    # Horizontal mirror before ``transpose=1`` (legacy **front**
    # camera ingest only). The LSPatch hook injects on the **rear**
    # lens only (v1.8.8+), so defaults use ``hook_encode_rear_facing``
    # below and leave this off unless you set
    # ``hook_encode_rear_facing`` to false and need the old selfie
    # cancellation path.
    mirror_horizontal: bool = False

    # When true (default), hook-mode FFmpeg targets TikTok's **rear**
    # camera buffer (``transpose=2`` then ``vflip``, skips ``hflip``)
    # — matches confirmed ``device_profiles.json`` chains for common
    # MediaTek Redmi/Poco phones. Set false in ``config.json`` for the
    # legacy front-camera ``hflip`` + ``transpose=1`` chain.
    hook_encode_rear_facing: bool = True
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
            cfg = cls()
        else:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # Drop unknown keys so we don't blow up on schema drift.
            valid = {f.name for f in cls.__dataclass_fields__.values()}
            clean = {k: v for k, v in data.items() if k in valid}
            cfg = cls(**clean)
        return cfg._with_resolved_tools()

    def _with_resolved_tools(self) -> "StreamConfig":
        """Resolve ``adb_path`` / ``ffmpeg_path`` from the cross-platform
        bundle if the bare names ("adb" / "ffmpeg") don't exist on PATH.

        Why this lives in config-load (not at every callsite)
        -----------------------------------------------------

        Every module that touches ADB/FFmpeg used to call
        ``shutil.which(self.cfg.adb_path)`` independently. That
        worked on dev boxes (binaries on PATH) but failed on customer
        macOS/Windows machines that only have the bundled tools under
        ``.tools/<os>/``. Each callsite raised its own "adb not found"
        error, leading to confusing dead-ends for non-technical users.

        Resolving once at load time means *every* downstream subprocess
        call ends up using the absolute path of the bundled binary
        without each module having to repeat the fallback logic.

        We never overwrite a fully-qualified path the user pinned
        themselves — only the bare-name defaults.
        """
        import shutil
        try:
            from . import platform_tools
        except Exception:
            return self

        # ADB
        if self.adb_path in ("", "adb") or shutil.which(self.adb_path) is None:
            bundled = platform_tools.find_adb()
            if bundled is not None:
                self.adb_path = str(bundled)

        # ffmpeg
        if (
            self.ffmpeg_path in ("", "ffmpeg")
            or shutil.which(self.ffmpeg_path) is None
        ):
            bundled = platform_tools.find_ffmpeg()
            if bundled is not None:
                self.ffmpeg_path = str(bundled)

        return self

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
