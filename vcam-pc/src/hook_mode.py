"""Hook Mode pipeline — produce an MP4 the Xposed CameraHook can play.

Once the user has rooted their phone and enabled `vcam-app` as an
LSPosed module under TikTok's scope, the hook expects to find a single
playable MP4 at one of these well-known paths (see
`com.livemobillrerun.vcam.hook.VideoFeeder.activeVideoPath()`):

```
/data/local/tmp/vcam_final.mp4         ← preferred (root-only)
/storage/emulated/0/vcam_final.mp4
/sdcard/vcam_final.mp4                  ← what we use, USB-pushable
```

This module re-encodes the user's PC playlist into a TikTok-friendly
H.264+AAC MP4 with `+faststart` (moov box at the front, so MediaPlayer
can begin playing during the upload) and pushes it to `/sdcard` over
ADB.

The build is *one-shot*: encode → push → done. No live streaming, no
sockets, no foreground service on the phone. The hook reads a static
file and loops it forever.

Activation is decoupled into two ADB commands the GUI runs separately
when the user is ready:

```
adb shell touch /data/local/tmp/vcam_enabled    # turn ON
adb shell rm    /data/local/tmp/vcam_enabled    # turn OFF
```

Or, if `vcam-app` is installed and exported the receiver, the same
result can be sent as a broadcast — see `set_mode_via_broadcast`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DeviceProfile, StreamConfig

log = logging.getLogger(__name__)

# Where the hook looks first that we can actually `adb push` to AND
# the host TikTok process is guaranteed to have read access for.
#
# Raw /sdcard/ used to be the target, but Android 11+ scoped storage
# means /sdcard/foo.mp4 is *visible* to TikTok (its `File.exists()`
# returns true) while `MediaPlayer.setDataSource` opens it as the
# media-store user and gets EACCES. Using a path under
# /sdcard/Android/data/<pkg>/files/ side-steps the problem entirely:
# adb shell can write here, and TikTok owns the directory so reads
# always succeed.
TIKTOK_PACKAGE_DEFAULT = "com.ss.android.ugc.trill"
TARGET_PATH_TEMPLATE = "/sdcard/Android/data/{pkg}/files/vcam_final.mp4"
TARGET_PATH_ON_PHONE = TARGET_PATH_TEMPLATE.format(pkg=TIKTOK_PACKAGE_DEFAULT)
# Standalone audio override — picked up by AudioFeeder if present.
# Extension is the source file's actual extension; the phone's
# MediaExtractor sniffs the container so we don't have to transcode.
AUDIO_TARGET_TEMPLATE = "/sdcard/Android/data/{pkg}/files/vcam_audio.{ext}"
AUDIO_VALID_EXTS = ("mp3", "m4a", "aac", "wav", "ogg")
# Public audio location — picked up by the user's existing background
# music player (Mi Music / Spotify Local / VLC / etc). Useful as a
# fallback when in-process AudioFeeder injection isn't engaged (e.g.
# TikTok using AAudio native path that Java Xposed can't reach).
PUBLIC_AUDIO_TARGET_TEMPLATE = "/sdcard/Music/vcam_audio.{ext}"
ENABLED_FLAG_PATH = "/data/local/tmp/vcam_enabled"


def target_for_package(pkg: str = TIKTOK_PACKAGE_DEFAULT) -> str:
    """Where the patched TikTok build looks for our MP4."""
    return TARGET_PATH_TEMPLATE.format(pkg=pkg)


def audio_target_for_package(
    ext: str,
    pkg: str = TIKTOK_PACKAGE_DEFAULT,
) -> str:
    """Where to push a standalone audio override file."""
    ext = ext.lower().lstrip(".")
    if ext not in AUDIO_VALID_EXTS:
        raise ValueError(
            f"unsupported audio extension {ext!r}; allowed: {AUDIO_VALID_EXTS}"
        )
    return AUDIO_TARGET_TEMPLATE.format(pkg=pkg, ext=ext)


@dataclass
class HookEncodeResult:
    ok: bool
    output_path: Path
    duration_s: float
    bytes: int
    log_tail: str = ""


@dataclass
class HookPushResult:
    ok: bool
    bytes: int
    elapsed_s: float
    target: str = TARGET_PATH_ON_PHONE
    error: str = ""


@dataclass
class HookStatus:
    """What the GUI shows in the Hook Mode panel."""
    file_present: bool = False
    file_size: int = 0
    file_mtime: int = 0
    enabled_flag: bool = False
    notes: list[str] = field(default_factory=list)


class HookModePipeline:
    """Encode the playlist into an MP4 + push it to the phone."""

    def __init__(self, cfg: StreamConfig) -> None:
        self.cfg = cfg

    # ────────────────────────────────────────────────────────────
    #  encode
    # ────────────────────────────────────────────────────────────

    def encode_playlist(
        self,
        playlist_file: Path,
        profile: DeviceProfile,
        output_path: Path,
        max_seconds: int = 0,
        apply_profile_rotation: bool = False,
    ) -> HookEncodeResult:
        """Re-encode the FFmpeg concat playlist into a single MP4.

        Settings match TikTok's expected ingest:
          - H.264 baseline, yuv420p, 30 fps
          - AAC LC stereo 44.1 kHz, 128 kbps
          - +faststart so the moov box ends up at the front of the
            file and MediaPlayer doesn't have to read the whole thing
            before it can begin playback.

        Output geometry
        ---------------
        TikTok Live attaches our MP4 via Camera2's
        ``CaptureRequest.addTarget`` — i.e. the Surface we feed is the
        camera's *raw sensor target*, which on every Android phone we
        care about is ``1280 × 720`` landscape with the content rotated
        by ``sensor_orientation`` (270° for the front cam, 90° for the
        rear). TikTok's display layer then rotates that landscape frame
        into the portrait viewport you see on the phone.

        To make our injected video appear *upright* through that same
        rotation, we encode at ``1280 × 720`` landscape and pre-rotate
        the source by 90° CW (``transpose=1``). The downstream display
        rotation cancels ours out and the user sees an upright portrait
        clip. The phone-side ``FlipRenderer`` can then nudge it further
        if a particular device's sensor orientation differs.

        ``apply_profile_rotation`` is left as a manual override for the
        deprecated HAL-hook path (where the file is consumed *as the
        camera*, not piped through the encoder) and defaults to False.
        """
        ffmpeg = self.cfg.ffmpeg_path
        if shutil.which(ffmpeg) is None:
            return HookEncodeResult(False, output_path, 0.0, 0,
                                    f"ffmpeg not found on PATH: {ffmpeg}")

        # The output is *always* 1280×720 landscape from now on — that
        # matches the Camera2 surface dimensions TikTok hands us. The
        # cfg.width/height fields refer to the user's portrait
        # *playlist* size; we swap them when emitting the MP4.
        out_w, out_h = 1280, 720

        # video filter chain.
        vf: list[str] = []
        if (
            apply_profile_rotation
            and profile.rotation_filter
            and profile.rotation_filter != "none"
        ):
            vf.append(profile.rotation_filter)
        # Rotate 90° CW so TikTok's downstream sensor-orientation
        # rotation lands us upright. transpose=1 on a portrait source
        # turns it into landscape with content "lying on its right".
        vf.append("transpose=1")
        vf.append(
            f"scale={out_w}:{out_h}:"
            "force_original_aspect_ratio=decrease:flags=lanczos"
        )
        vf.append(f"fps={self.cfg.fps}")
        vf.append(
            f"pad={out_w}:{out_h}:"
            f"(ow-iw)/2:(oh-ih)/2:color=black"
        )
        vf.append("setsar=1")

        keyint = max(1, int(self.cfg.fps * self.cfg.keyint_seconds))
        cmd: list[str] = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "warning",
            "-nostdin",
        ]
        if self.cfg.loop_playlist and max_seconds > 0:
            # Use stream_loop only when capping duration, otherwise we'd
            # write an infinite file.
            cmd += ["-stream_loop", "-1"]
        cmd += [
            "-f", "concat",
            "-safe", "0",
            "-i", str(playlist_file),
        ]
        if max_seconds > 0:
            cmd += ["-t", str(max_seconds)]

        cmd += [
            "-vf", ",".join(vf),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-profile:v", "baseline",
            "-level", "4.0",
            "-pix_fmt", "yuv420p",
            "-r", str(self.cfg.fps),
            "-g", str(keyint),
            "-keyint_min", str(keyint),
            "-sc_threshold", "0",
            "-b:v", self.cfg.video_bitrate,
            "-maxrate", self.cfg.video_maxrate,
            "-bufsize", self.cfg.video_bufsize,
            # Audio — TikTok expects audio. Re-encode to AAC LC.
            "-c:a", "aac",
            "-b:a", "128k",
            "-ac", "2",
            "-ar", "44100",
            # MP4 with moov-at-front for streaming-friendly playback.
            "-movflags", "+faststart",
            "-f", "mp4",
            str(output_path),
        ]

        log.info("Hook MP4 encode: %s", " ".join(cmd))
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return HookEncodeResult(False, output_path, 0.0, 0,
                                    "ffmpeg timed out after 10 min")
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-10:]
            return HookEncodeResult(
                False, output_path, elapsed, 0,
                "\n".join(tail),
            )
        size = output_path.stat().st_size if output_path.is_file() else 0
        return HookEncodeResult(True, output_path, elapsed, size, "")

    # ────────────────────────────────────────────────────────────
    #  push
    # ────────────────────────────────────────────────────────────

    def push_to_phone(
        self,
        local_mp4: Path,
        serial: str | None = None,
        target: str = TARGET_PATH_ON_PHONE,
    ) -> HookPushResult:
        """`adb push` the encoded MP4 to the phone.

        Ensures the parent directory exists first — when the phone has
        never opened TikTok, `Android/data/<pkg>/files/` may not exist
        yet and `adb push` would fail with `couldn't create directory`.
        """
        adb = self.cfg.adb_path
        if shutil.which(adb) is None:
            return HookPushResult(False, 0, 0.0, target,
                                  f"adb not found: {adb}")
        if not local_mp4.is_file():
            return HookPushResult(False, 0, 0.0, target,
                                  f"local file missing: {local_mp4}")
        size = local_mp4.stat().st_size

        # mkdir -p the target dir before pushing.
        parent_dir = target.rsplit("/", 1)[0]
        mkdir_cmd = [adb]
        if serial:
            mkdir_cmd += ["-s", serial]
        mkdir_cmd += ["shell", "mkdir", "-p", parent_dir]
        try:
            subprocess.run(mkdir_cmd, capture_output=True, text=True,
                           timeout=5, check=False)
        except subprocess.TimeoutExpired:
            pass

        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += ["push", str(local_mp4), target]

        log.info("Hook push: %s", " ".join(cmd))
        t0 = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=120, check=False)
        except subprocess.TimeoutExpired:
            return HookPushResult(False, size, 0.0, target,
                                  "adb push timed out (>120s)")
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return HookPushResult(False, size, elapsed, target, "\n".join(err))

        # Best-effort: poke the running TikTok process so it reloads
        # immediately instead of waiting for the watchdog's 2 s tick.
        # MIUI's scoped-storage layer often suppresses mtime updates
        # when overwriting an existing file with adjacent content, so
        # without this nudge the user sees the *old* clip even after
        # a successful push. (The watchdog now also checks file size,
        # so this broadcast is a fast-path optimisation, not a
        # correctness requirement.)
        self._broadcast_force_reload(serial=serial)

        return HookPushResult(True, size, elapsed, target, "")

    def _broadcast_force_reload(
        self,
        serial: str | None = None,
        tiktok_pkg: str = TIKTOK_PACKAGE_DEFAULT,
        audio_reload: bool = False,
    ) -> None:
        """Fire ``com.livemobillrerun.vcam.SET_MODE forceReload=true`` at
        the running TikTok process so the in-process receiver inside
        the LSPatched APK rebuilds MediaPlayer with the freshly-pushed
        MP4. We deliberately use ``-p <tiktok_pkg>`` (package filter)
        instead of ``-n component``, because the receiver is registered
        at runtime by the hook (no manifest entry to target).

        ``audio_reload`` adds an ``--ez audioReload true`` extra so
        the AudioFeeder picks up a freshly-pushed override file.
        """
        adb = self.cfg.adb_path
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += [
            "shell", "am", "broadcast",
            "-a", "com.livemobillrerun.vcam.SET_MODE",
            "-p", tiktok_pkg,
            "--ei", "mode", "2",
            "--ez", "forceReload", "true",
        ]
        if audio_reload:
            cmd += ["--ez", "audioReload", "true"]
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=5, check=False)
        except subprocess.TimeoutExpired:
            log.debug("force-reload broadcast timed out (harmless)")

    # ────────────────────────────────────────────────────────────
    #  push standalone audio override
    # ────────────────────────────────────────────────────────────

    def push_audio_to_phone(
        self,
        local_audio: Path,
        serial: str | None = None,
        package: str = TIKTOK_PACKAGE_DEFAULT,
    ) -> HookPushResult:
        """Push a standalone audio file (MP3/WAV/AAC/M4A/OGG) to the
        well-known ``vcam_audio.<ext>`` location inside TikTok's
        sandboxed files dir. AudioFeeder on the phone treats it as
        an override of the MP4's own audio track.

        The destination filename's extension is taken from
        ``local_audio.suffix`` so MediaExtractor on Android can sniff
        the container correctly. We also wipe any *other*
        ``vcam_audio.*`` files in the same dir first — otherwise the
        feeder's path scanner might still pick the older one because
        we don't enforce ordering between extensions.
        """
        adb = self.cfg.adb_path
        if shutil.which(adb) is None:
            return HookPushResult(
                False, 0, 0.0, "",
                f"adb not found: {adb}",
            )
        if not local_audio.is_file():
            return HookPushResult(
                False, 0, 0.0, "",
                f"local audio file missing: {local_audio}",
            )
        ext = local_audio.suffix.lower().lstrip(".")
        try:
            target = audio_target_for_package(ext, pkg=package)
        except ValueError as e:
            return HookPushResult(False, 0, 0.0, "", str(e))

        size = local_audio.stat().st_size

        # mkdir -p the parent dir + clear stale audio overrides.
        parent_dir = target.rsplit("/", 1)[0]
        cleanup = (
            f"mkdir -p {parent_dir} && "
            "for f in "
            + " ".join(
                f"{parent_dir}/vcam_audio.{x}" for x in AUDIO_VALID_EXTS
            )
            + "; do rm -f $f 2>/dev/null; done"
        )
        prep_cmd = [adb]
        if serial:
            prep_cmd += ["-s", serial]
        prep_cmd += ["shell", cleanup]
        try:
            subprocess.run(
                prep_cmd, capture_output=True, text=True,
                timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            pass

        # adb push the file.
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += ["push", str(local_audio), target]

        log.info("push audio: %s", " ".join(cmd))
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=120, check=False,
            )
        except subprocess.TimeoutExpired:
            return HookPushResult(
                False, size, 0.0, target,
                "adb push timed out (>120s)",
            )
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return HookPushResult(False, size, elapsed, target, "\n".join(err))

        # Mirror the same file to /sdcard/Music/ so the user's existing
        # background music player (Mi Music / Spotify Local / VLC) can
        # find it. Then ping MediaScanner so it shows up immediately
        # without forcing the user to reboot or rescan manually.
        public_target = PUBLIC_AUDIO_TARGET_TEMPLATE.format(ext=ext)
        public_parent = public_target.rsplit("/", 1)[0]
        public_prep = [adb]
        if serial:
            public_prep += ["-s", serial]
        public_prep += [
            "shell",
            f"mkdir -p {public_parent} && "
            "for f in "
            + " ".join(
                f"{public_parent}/vcam_audio.{x}" for x in AUDIO_VALID_EXTS
            )
            + "; do rm -f $f 2>/dev/null; done",
        ]
        try:
            subprocess.run(
                public_prep, capture_output=True, text=True,
                timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            pass

        public_push = [adb]
        if serial:
            public_push += ["-s", serial]
        public_push += ["push", str(local_audio), public_target]
        try:
            subprocess.run(
                public_push, capture_output=True, text=True,
                timeout=120, check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("public-mirror push timed out (file still available at %s)", target)

        scan_cmd = [adb]
        if serial:
            scan_cmd += ["-s", serial]
        scan_cmd += [
            "shell",
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d file://{public_target}",
        ]
        try:
            subprocess.run(
                scan_cmd, capture_output=True, text=True,
                timeout=5, check=False,
            )
        except subprocess.TimeoutExpired:
            pass

        # Tell the running TikTok process to swap audio sources (if
        # AudioFeeder is engaged).
        self._broadcast_force_reload(
            serial=serial, tiktok_pkg=package, audio_reload=True,
        )
        return HookPushResult(True, size, elapsed, target, "")

    def remove_audio_from_phone(
        self,
        serial: str | None = None,
        package: str = TIKTOK_PACKAGE_DEFAULT,
    ) -> bool:
        """Delete every ``vcam_audio.<ext>`` override on the phone —
        both the in-app sandbox copy and the public ``/sdcard/Music``
        mirror. AudioFeeder will fall back to the MP4's own audio
        track on the next reload."""
        adb = self.cfg.adb_path
        scoped_dir = f"/sdcard/Android/data/{package}/files"
        public_dir = "/sdcard/Music"
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += [
            "shell",
            "for f in "
            + " ".join(
                f"{d}/vcam_audio.{x}"
                for d in (scoped_dir, public_dir)
                for x in AUDIO_VALID_EXTS
            )
            + "; do rm -f $f 2>/dev/null; done",
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=5, check=False)
        except subprocess.TimeoutExpired:
            return False
        # Trigger a reload so AudioFeeder switches back to MP4 audio.
        self._broadcast_force_reload(
            serial=serial, tiktok_pkg=package, audio_reload=True,
        )
        return True

    # ────────────────────────────────────────────────────────────
    #  activation
    # ────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool, serial: str | None = None) -> bool:
        """Toggle the on-disk activation flag.

        Note that `/data/local/tmp/` is normally written via `adb shell`
        (uid 2000), not `adb push`. This avoids the SELinux denials you
        get if you try to push there directly on a non-rooted phone.
        """
        adb = self.cfg.adb_path
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        if enabled:
            cmd += ["shell", "touch", ENABLED_FLAG_PATH]
        else:
            cmd += ["shell", "rm", "-f", ENABLED_FLAG_PATH]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=5, check=False)
        except subprocess.TimeoutExpired:
            log.warning("adb shell timeout while toggling enabled flag")
            return False
        if r.returncode != 0:
            log.warning("set_enabled failed: %s", r.stderr.strip())
            return False
        return True

    def set_mode_via_broadcast(
        self,
        mode: int,
        video_path: str = TARGET_PATH_ON_PHONE,
        loop: bool = True,
        rotation: float = 0.0,
        zoom: float = 1.0,
        flip_x: bool = False,
        flip_y: bool = False,
        audio: bool = True,
        serial: str | None = None,
        package: str = "com.livemobillrerun.vcam",
    ) -> bool:
        """Fire `com.livemobillrerun.vcam.SET_MODE` to VCamModeReceiver.

        Only useful once `vcam-app` is installed AND the broadcast
        receiver is exported (which it is — see AndroidManifest.xml).
        Until LSPosed is loaded into TikTok the broadcast does nothing
        observable; we still send it because it's harmless.
        """
        adb = self.cfg.adb_path
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += [
            "shell", "am", "broadcast",
            "-a", f"{package}.SET_MODE",
            "-n", f"{package}/.hook.VCamModeReceiver",
            "--ei", "mode", str(mode),
            "--es", "videoPath", video_path,
            "--ez", "loop", str(loop).lower(),
            "--ef", "rotation", f"{rotation:.2f}",
            "--ef", "zoom", f"{zoom:.2f}",
            "--ez", "flipX", str(flip_x).lower(),
            "--ez", "flipY", str(flip_y).lower(),
            "--ez", "audio", str(audio).lower(),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=8, check=False)
        except subprocess.TimeoutExpired:
            log.warning("set_mode broadcast timed out")
            return False
        log.info("set_mode broadcast rc=%s out=%r", r.returncode,
                 r.stdout.strip())
        return r.returncode == 0

    # ────────────────────────────────────────────────────────────
    #  status
    # ────────────────────────────────────────────────────────────

    def status(self, serial: str | None = None) -> HookStatus:
        """Probe the phone for current Hook Mode state.

        Cheap to call — used by the GUI to refresh the panel.
        """
        adb = self.cfg.adb_path
        out = HookStatus()

        def _shell(cmd: str) -> str:
            args = [adb]
            if serial:
                args += ["-s", serial]
            args += ["shell", cmd]
            try:
                r = subprocess.run(args, capture_output=True, text=True,
                                   timeout=5, check=False)
            except subprocess.TimeoutExpired:
                return ""
            return (r.stdout or "").strip()

        # File present? size? mtime?
        # `stat -c '%s %Y' /sdcard/vcam_final.mp4`
        stat_out = _shell(f"stat -c '%s %Y' {TARGET_PATH_ON_PHONE} 2>/dev/null")
        if stat_out:
            try:
                size_str, mtime_str = stat_out.split()
                out.file_present = True
                out.file_size = int(size_str)
                out.file_mtime = int(mtime_str)
            except ValueError:
                out.notes.append(f"unparsable stat: {stat_out!r}")

        # Activation flag (writable by adb shell, not by adb push).
        flag_out = _shell(f"ls {ENABLED_FLAG_PATH} 2>/dev/null")
        out.enabled_flag = ENABLED_FLAG_PATH in flag_out

        return out


# ────────────────────────────────────────────────────────────
#  helpers
# ────────────────────────────────────────────────────────────

def default_local_mp4(cfg: StreamConfig) -> Path:
    """Where we cache the encoded MP4 on the PC side."""
    cache_dir = cfg.videos_path.parent / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "vcam_final.mp4"


def human_bytes(n: int) -> str:
    """1234 → '1.2 KB', 12_345_678 → '11.8 MB'."""
    if n < 1024:
        return f"{n} B"
    f = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        f /= 1024.0
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
    return f"{n} B"
