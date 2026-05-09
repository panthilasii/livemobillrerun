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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import DeviceProfile, StreamConfig

# Type alias: ``progress_cb(percent_0_to_1, status_text)``. Callers
# pass a small lambda that bounces back to their UI thread; the hook
# pipeline only invokes it -- it never assumes Tk or any other UI is
# available.
ProgressCB = Optional[Callable[[float, str], None]]

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
    #  binary resolution
    # ────────────────────────────────────────────────────────────
    #
    # Why these helpers exist
    # ~~~~~~~~~~~~~~~~~~~~~~~
    # Older revisions called ``shutil.which(self.cfg.adb_path)``
    # directly. That works only if ``adb`` happens to live on the
    # user's PATH — which is *true on dev boxes* but *false on most
    # customer machines* who haven't manually installed the Android
    # platform-tools.
    #
    # We ship a bundled adb / ffmpeg under ``.tools/<os>/`` and
    # ``platform_tools.find_adb()`` knows how to find them across the
    # legacy + new layouts. Hooking that resolver in here lets the
    # customer double-click ``run.command`` and have audio/video push
    # immediately, with no PATH gymnastics.
    #
    # Order of preference:
    #   1. ``cfg.<x>_path`` if it resolves on PATH (= user override)
    #   2. Bundled binary returned by ``platform_tools.find_<x>()``
    #   3. None — caller must surface a clear "binary not found" error
    #
    # Returning the *resolved absolute path* (not just bool) means
    # callers can use it directly in subprocess command lists.

    def _resolve_adb(self) -> str | None:
        """Return an absolute path to a runnable ``adb`` or None."""
        # Honour an explicit user override first — they may have
        # pinned a specific platform-tools build for compatibility.
        configured = self.cfg.adb_path
        if configured and shutil.which(configured) is not None:
            resolved = shutil.which(configured)
            return resolved
        # Fall back to the bundled binary under .tools/<os>/.
        try:
            from .platform_tools import find_adb
        except Exception:
            return None
        bundled = find_adb()
        return str(bundled) if bundled else None

    def _resolve_ffmpeg(self) -> str | None:
        """Return an absolute path to a runnable ``ffmpeg`` or None."""
        configured = self.cfg.ffmpeg_path
        if configured and shutil.which(configured) is not None:
            return shutil.which(configured)
        try:
            from .platform_tools import find_ffmpeg
        except Exception:
            return None
        bundled = find_ffmpeg()
        return str(bundled) if bundled else None

    def _probe_playlist_duration(
        self, playlist_file: Path, ffmpeg: str,
    ) -> tuple[float, int]:
        """Sum duration (seconds) and total byte size of every entry
        in a concat-demuxer playlist.

        Returns ``(total_seconds, total_bytes)``. On any parse / probe
        failure we return ``(0.0, 0)`` and the caller falls back to a
        conservative default timeout. We deliberately do NOT raise --
        a bad probe should never block the encode itself.

        Why we need this
        ----------------
        Encode + push timeouts must scale with the input. A 1.9 GB,
        ~17 min source clip needs an order of magnitude more wall
        clock than a 30-second test clip; a single hard-coded 600 s
        cap silently kills the ffmpeg process and the customer just
        sees "stuck at encode" with no useful error.
        """
        if not playlist_file.is_file():
            return 0.0, 0

        # ffprobe is the canonical tool but it's not always shipped
        # alongside ffmpeg in stripped-down bundles. Fall back to the
        # ffmpeg binary itself with ``-i`` and parse its stderr; that
        # works on every ffmpeg build we ship.
        ffprobe = self._sibling_tool(ffmpeg, "ffprobe")

        total_sec = 0.0
        total_bytes = 0
        try:
            text = playlist_file.read_text(encoding="utf-8")
        except OSError:
            return 0.0, 0

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("file "):
                continue
            # ``file '...'`` -- strip the quoting and unescape the
            # single-quote sequence that write_playlist uses.
            path_str = line[5:].strip()
            if path_str.startswith("'") and path_str.endswith("'"):
                path_str = path_str[1:-1].replace(r"'\''", "'")
            p = Path(path_str)
            if not p.is_file():
                continue
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass

            dur = self._probe_one(p, ffprobe, ffmpeg)
            if dur > 0:
                total_sec += dur

        return total_sec, total_bytes

    @staticmethod
    def _sibling_tool(ffmpeg_path: str, name: str) -> str | None:
        """Look for a sibling tool (typically ``ffprobe``) next to
        the resolved ffmpeg binary. Returns None if not found; the
        caller should fall back to ffmpeg-stderr parsing."""
        try:
            sibling = Path(ffmpeg_path).resolve().with_name(name)
        except (ValueError, OSError):
            return None
        if sibling.is_file():
            return str(sibling)
        # Also check PATH as a last resort -- bundled ffmpeg may live
        # without ffprobe but the customer's system might have it.
        which = shutil.which(name)
        return which

    @staticmethod
    def _probe_one(p: Path, ffprobe: str | None, ffmpeg: str) -> float:
        """Return duration in seconds for one video file, 0.0 on
        failure. Tries ffprobe JSON first (cheap, exact), falls back
        to parsing ``ffmpeg -i`` stderr."""
        if ffprobe:
            try:
                res = subprocess.run(
                    [
                        ffprobe, "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        str(p),
                    ],
                    capture_output=True, text=True,
                    timeout=15, check=False,
                )
                val = (res.stdout or "").strip()
                if val:
                    return float(val)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass

        # Fallback: ffmpeg -i emits ``Duration: HH:MM:SS.xx`` to stderr.
        try:
            res = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(p)],
                capture_output=True, text=True,
                timeout=15, check=False,
            )
            stderr = res.stderr or ""
            for line in stderr.splitlines():
                line = line.strip()
                if line.startswith("Duration:"):
                    # ``Duration: 00:17:23.45, start: ...``
                    hms = line.split(",", 1)[0].split(":", 1)[1].strip()
                    h, m, s = hms.split(":")
                    return int(h) * 3600 + int(m) * 60 + float(s)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return 0.0

    @staticmethod
    def _encode_timeout(duration_s: float, total_bytes: int) -> int:
        """Pick an ffmpeg encode timeout that won't kill the process
        before it can finish.

        Heuristic:
          * libx264 ``veryfast`` typically runs ~1-2× realtime on a
            mid-range PC. We allow 4× as the worst-case (cold cache,
            CPU contention, slow disk).
          * Plus a 60 s setup buffer for ffprobe + scaler init.
          * Plus 30 s per GB of disk I/O headroom.
          * Floor at 600 s (10 min) so existing short-clip behavior
            doesn't change.
        """
        gb = max(0, total_bytes) / (1024 ** 3)
        budget = 4.0 * max(0.0, duration_s) + 60.0 + 30.0 * gb
        return max(600, int(budget))

    def _spawn_push_sampler(
        self,
        adb: str,
        serial: str | None,
        target: str,
        total_bytes: int,
        progress_cb: Callable[[float, str], None],
        stop_evt: threading.Event,
    ) -> None:
        """Background thread that polls ``stat`` on the target file
        once a second and reports growth back through
        ``progress_cb``.

        adb's own ``[XX%]`` progress on stderr is suppressed when
        stderr is piped (which we have to do to read it at all), so
        we cannot rely on it. ``stat`` is a tiny shell command --
        ~3 KB round-trip per second -- and works identically over
        USB and WiFi adb.
        """

        def _human_mb(b: int) -> str:
            return f"{b / (1024 ** 2):.1f} MB"

        total_mb_str = _human_mb(total_bytes)

        def _run() -> None:
            last_pct = 0.0
            while not stop_evt.wait(timeout=1.0):
                stat_cmd = [adb]
                if serial:
                    stat_cmd += ["-s", serial]
                stat_cmd += ["shell", "stat", "-c", "%s", target]
                try:
                    res = subprocess.run(
                        stat_cmd, capture_output=True, text=True,
                        timeout=4, check=False,
                    )
                except (subprocess.TimeoutExpired, OSError):
                    continue
                if res.returncode != 0:
                    continue
                try:
                    cur = int((res.stdout or "0").strip())
                except ValueError:
                    continue
                pct = max(0.0, min(0.99, cur / max(1, total_bytes)))
                if abs(pct - last_pct) < 0.01:
                    continue
                last_pct = pct
                try:
                    progress_cb(
                        pct,
                        f"กำลัง push… {int(pct * 100)}% "
                        f"({_human_mb(cur)} / {total_mb_str})",
                    )
                except Exception:
                    log.exception("push progress callback")
                    return

        threading.Thread(
            target=_run, daemon=True, name="adb-push-sampler",
        ).start()

    @staticmethod
    def _push_timeout(file_bytes: int) -> int:
        """Pick an ``adb push`` timeout based on observed throughput.

        Heuristic:
          * USB 2.0 ADB realistically pushes 8-12 MB/s, USB 3.0 hits
            30-40 MB/s, WiFi ADB 1-3 MB/s. We assume the slowest of
            the three (3 MB/s) so even WiFi-bound customers don't hit
            the cap.
          * Plus 60 s overhead for the initial USB enumeration +
            mkdir + post-push fsync handshake.
          * Floor at 120 s for backwards compat with small clips.
        """
        mb = max(0, file_bytes) / (1024 ** 2)
        budget = mb / 3.0 + 60.0
        return max(120, int(budget))

    def _build_video_filter(
        self,
        profile: DeviceProfile,
        apply_profile_rotation: bool,
        out_w: int,
        out_h: int,
    ) -> list[str]:
        """Build the ffmpeg ``-vf`` chain for the hook-mode encoder.

        Order is load-bearing:

        1. ``profile.rotation_filter`` (rare; only for the deprecated
           HAL-hook path where the file *is* the camera).
        2. ``hflip`` — pre-cancel TikTok's implicit front-camera
           mirror. Must run BEFORE ``transpose=1`` so the flip axis
           is horizontal in the source frame; flipping after the
           90° rotation would invert the vertical axis and produce
           an upside-down picture once TikTok rotates it back.
        3. ``transpose=1`` — 90° CW rotation so the downstream
           Camera2 sensor-orientation rotation lands us upright.
        4. ``scale + pad`` — letterbox the source to the configured
           encode dims (default 1920×1080 landscape, becomes
           1080×1920 portrait on screen).
        5. ``fps`` then ``setsar=1`` — fixed-rate output, square
           pixels (TikTok's MediaPlayer rejects non-square pixels
           on some Android builds).
        """
        vf: list[str] = []
        if (
            apply_profile_rotation
            and profile.rotation_filter
            and profile.rotation_filter != "none"
        ):
            vf.append(profile.rotation_filter)
        # Math (where R = rotate 90° CW, H = our hflip,
        # M = TikTok's selfie mirror, both M and H are involutions):
        #   file    = transpose(hflip(src)) = R(H(src))
        #   display = M(R⁻¹(file))           = M(H(src))
        #                                    = src       (since M = H)
        if getattr(self.cfg, "mirror_horizontal", True):
            vf.append("hflip")
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
        return vf

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
        progress_cb: ProgressCB = None,
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
        camera's *raw sensor target*, which on every supported Android
        phone is a landscape buffer with the content rotated by
        ``sensor_orientation`` (270° for the front cam, 90° for the
        rear). TikTok's display layer then rotates that landscape
        frame into the portrait viewport you see on the phone.

        To make our injected video appear *upright* through that same
        rotation, we encode landscape (``cfg.encode_width`` ×
        ``cfg.encode_height`` — default 1920×1080) and pre-rotate the
        source by 90° CW (``transpose=1``). The downstream display
        rotation cancels ours out and the user sees an upright
        portrait clip. The phone-side ``FlipRenderer`` can then nudge
        it further if a particular device's sensor orientation
        differs.

        ``apply_profile_rotation`` is left as a manual override for the
        deprecated HAL-hook path (where the file is consumed *as the
        camera*, not piped through the encoder) and defaults to False.
        """
        ffmpeg = self._resolve_ffmpeg()
        if ffmpeg is None:
            return HookEncodeResult(
                False, output_path, 0.0, 0,
                "ffmpeg ไม่พบในระบบ\n"
                "ลองรัน: python3 tools/setup_ffmpeg.py\n"
                "หรือลง ffmpeg เพิ่มเอง (brew install ffmpeg)",
            )

        # Output is landscape — that matches the Camera2 surface
        # dimensions TikTok hands us. The cfg.width/height fields
        # refer to the user's portrait *playlist* size; we read the
        # encode dims separately. Default is 1920×1080 ("1080p"),
        # which becomes 1080×1920 portrait after the phone's rotation
        # chain. Drop to 1280×720 ("720p") in Settings on slower
        # phones if the encoder can't keep up.
        out_w = max(2, int(self.cfg.encode_width or 1920))
        out_h = max(2, int(self.cfg.encode_height or 1080))
        # H.264 requires even dimensions.
        out_w -= out_w % 2
        out_h -= out_h % 2

        vf = self._build_video_filter(
            profile, apply_profile_rotation, out_w, out_h,
        )

        keyint = max(1, int(self.cfg.fps * self.cfg.keyint_seconds))
        # ``-progress pipe:1`` makes ffmpeg dump structured key=value
        # blocks to stdout every ~500 ms (out_time_us, frame, fps, …).
        # Combined with ``-nostats`` it suppresses the verbose human-
        # readable progress on stderr so we get a clean machine-
        # parseable stream. The parser runs in a worker thread; if
        # it fails, the encode itself isn't affected.
        cmd: list[str] = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel", "warning",
            "-nostdin",
            "-progress", "pipe:1",
            "-nostats",
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

        # Probe the playlist so we can size the timeout to match the
        # input. Hard-coding 600 s used to silently kill encodes of
        # source clips longer than ~5 min on slower laptops.
        duration_s, total_bytes = self._probe_playlist_duration(
            playlist_file, ffmpeg,
        )
        timeout_s = self._encode_timeout(duration_s, total_bytes)

        log.info(
            "Hook MP4 encode: %s  (timeout=%ds, in=%.1fMB, dur=%.1fs)",
            " ".join(cmd), timeout_s,
            total_bytes / (1024 ** 2), duration_s,
        )
        if progress_cb is not None:
            progress_cb(0.0, "เริ่ม encode…")

        t0 = time.monotonic()
        return self._run_ffmpeg_with_progress(
            cmd=cmd,
            output_path=output_path,
            duration_s=duration_s,
            timeout_s=timeout_s,
            progress_cb=progress_cb,
            t0=t0,
        )

    def _run_ffmpeg_with_progress(
        self,
        cmd: list[str],
        output_path: Path,
        duration_s: float,
        timeout_s: int,
        progress_cb: ProgressCB,
        t0: float,
    ) -> HookEncodeResult:
        """Spawn ffmpeg, stream its ``-progress pipe:1`` output to
        compute a 0..1 percentage, and bound wall-clock to
        ``timeout_s`` -- killing the child on timeout so we don't
        leak orphaned encoders.

        We never raise from this method -- callers expect a
        ``HookEncodeResult`` regardless of crash mode (timeout, OS
        error, ffmpeg failure). UI code keys off ``ok`` and
        ``log_tail`` to render success / error.
        """
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            return HookEncodeResult(
                False, output_path, 0.0, 0,
                f"ไม่สามารถเปิด ffmpeg ได้: {exc}",
            )

        # Drain stderr in a separate thread so it can't deadlock the
        # OS pipe buffer when ffmpeg gets chatty (e.g. 1080p encodes
        # fill the 64 KB stderr pipe in ~60 s otherwise).
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    stderr_lines.append(line.rstrip())
                    if len(stderr_lines) > 200:
                        del stderr_lines[:100]
            except Exception:  # pragma: no cover -- defensive
                pass

        if proc.stderr is not None:
            threading.Thread(
                target=_drain_stderr, daemon=True,
                name="ffmpeg-stderr",
            ).start()

        # Parse stdout (the -progress block stream). Each block ends
        # with ``progress=continue`` or ``progress=end``. We update
        # the percentage on every ``out_time_us`` we see -- that's
        # the encoded-output timestamp in microseconds. Dividing by
        # the input duration gives us a real ratio that maps cleanly
        # to a UI progress bar.
        deadline = t0 + timeout_s
        last_pct = 0.0
        try:
            for line in proc.stdout or []:
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait(timeout=5)
                    mins = timeout_s // 60
                    return HookEncodeResult(
                        False, output_path, 0.0, 0,
                        f"ffmpeg เกินเวลา {mins} นาที — ลองตัดคลิปสั้นลง\n"
                        "(แนะนำคลิปขนาด ≤ 500 MB หรือสั้นกว่า 5 นาที)",
                    )
                line = line.strip()
                if not line:
                    continue
                if line.startswith("out_time_us=") and duration_s > 0:
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    pct = max(0.0, min(1.0, us / 1_000_000.0 / duration_s))
                    # Only fire the callback when the percentage
                    # actually moves -- saves Tk thread thrash on
                    # very fast encodes.
                    if abs(pct - last_pct) >= 0.005:
                        last_pct = pct
                        if progress_cb is not None:
                            progress_cb(
                                pct,
                                f"กำลัง encode… {int(pct * 100)}%",
                            )
                elif line == "progress=end":
                    if progress_cb is not None:
                        progress_cb(1.0, "Encode เสร็จ")
                    break
        except Exception:
            log.exception("ffmpeg progress reader crashed")
            # Don't return here -- still wait for the child so we can
            # report its actual exit code; a crashed reader doesn't
            # imply a crashed encoder.

        # Wait for the child to actually exit so we can read its
        # return code. Using a poll loop instead of a single
        # ``wait(timeout=...)`` gives us better timeout behaviour --
        # we already enforced the wall-clock cap above.
        try:
            remaining = max(1.0, deadline - time.monotonic())
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            mins = timeout_s // 60
            return HookEncodeResult(
                False, output_path, 0.0, 0,
                f"ffmpeg เกินเวลา {mins} นาที — ลองตัดคลิปสั้นลง",
            )

        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            tail = stderr_lines[-10:] if stderr_lines else ["ffmpeg failed (no stderr)"]
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
        progress_cb: ProgressCB = None,
        tiktok_pkg: str = TIKTOK_PACKAGE_DEFAULT,
    ) -> HookPushResult:
        """`adb push` the encoded MP4 to the phone.

        Ensures the parent directory exists first — when the phone has
        never opened TikTok, `Android/data/<pkg>/files/` may not exist
        yet and `adb push` would fail with `couldn't create directory`.

        ``progress_cb`` (optional) receives ``(0..1, status_text)``
        as the push proceeds. We sample the destination file's size
        on the phone via ``adb shell stat`` instead of parsing
        adb's progress output -- adb's stderr percentage is gated
        behind a TTY check that fails when we capture pipes, so the
        only reliable cross-platform signal is the growing target
        file. The poller runs in a worker thread; if the sampler
        crashes, the push itself still proceeds.

        ``tiktok_pkg`` MUST match the actual TikTok variant on the
        phone (``com.zhiliaoapp.musically`` for global, or
        ``com.ss.android.ugc.trill`` for Lite, etc.). It is used by
        the post-push force-reload broadcast: if we send the
        broadcast at the default package and the phone is running
        TikTok Lite, the running TikTok process never receives the
        signal and the customer keeps seeing the OLD clip even
        though the new file is byte-for-byte present on disk.
        Misrouting this broadcast was the cause of "เข้าแล้วไม่
        เห็นเปลี่ยน" reports up through v1.7.3.
        """
        adb = self._resolve_adb()
        if adb is None:
            return HookPushResult(
                False, 0, 0.0, target,
                "adb ไม่พบในระบบ\n"
                "ลองรัน: python3 tools/setup_macos_tools.py "
                "(หรือ setup_windows_tools.py)\n"
                "หรือลง Android Platform Tools เอง",
            )
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

        timeout_s = self._push_timeout(size)
        log.info(
            "Hook push: %s  (timeout=%ds, size=%.1fMB)",
            " ".join(cmd), timeout_s, size / (1024 ** 2),
        )
        if progress_cb is not None:
            progress_cb(0.0, "เริ่ม push…")
        t0 = time.monotonic()

        # Spawn the sampler thread BEFORE Popen so we don't miss the
        # very first bytes for tiny files. The thread polls
        # ``adb shell stat -c %s <target>`` once per second; we use
        # ``threading.Event`` to stop it cleanly the moment Popen
        # exits, avoiding a stale poll racing the next push.
        stop_evt = threading.Event()
        if progress_cb is not None and size > 0:
            self._spawn_push_sampler(
                adb=adb, serial=serial, target=target,
                total_bytes=size, progress_cb=progress_cb,
                stop_evt=stop_evt,
            )

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            stop_evt.set()
            mins = timeout_s // 60
            return HookPushResult(
                False, size, 0.0, target,
                f"adb push เกินเวลา {mins} นาที\n"
                "ตรวจสายเชื่อม / WiFi กับมือถือ และลองอีกครั้ง",
            )
        finally:
            stop_evt.set()
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return HookPushResult(False, size, elapsed, target, "\n".join(err))

        if progress_cb is not None:
            progress_cb(1.0, "Push เสร็จ")

        log.info(
            "Hook push OK: %.1fMB in %.1fs (%.1f MB/s) → %s",
            size / (1024 ** 2),
            elapsed,
            (size / (1024 ** 2)) / elapsed if elapsed > 0 else 0,
            target,
        )

        # Best-effort: poke the running TikTok process so it reloads
        # immediately instead of waiting for the watchdog's 2 s tick.
        # MIUI's scoped-storage layer often suppresses mtime updates
        # when overwriting an existing file with adjacent content, so
        # without this nudge the user sees the *old* clip even after
        # a successful push. (The watchdog now also checks file size,
        # so this broadcast is a fast-path optimisation, not a
        # correctness requirement.)
        #
        # IMPORTANT: ``tiktok_pkg`` MUST be the variant actually
        # installed on the phone. See docstring for the rationale —
        # passing the default here when the customer is on TikTok
        # Lite silently breaks live-update, which is exactly the
        # bug we fixed in this change.
        self._broadcast_force_reload(serial=serial, tiktok_pkg=tiktok_pkg)

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
        adb = self._resolve_adb() or self.cfg.adb_path
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
        adb = self._resolve_adb()
        if adb is None:
            return HookPushResult(
                False, 0, 0.0, "",
                "adb ไม่พบในระบบ\n"
                "ลองรัน: python3 tools/setup_macos_tools.py "
                "(หรือ setup_windows_tools.py)\n"
                "หรือลง Android Platform Tools เอง",
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
        adb = self._resolve_adb() or self.cfg.adb_path
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
        adb = self._resolve_adb() or self.cfg.adb_path
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
        adb = self._resolve_adb() or self.cfg.adb_path
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
        adb = self._resolve_adb() or self.cfg.adb_path
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
