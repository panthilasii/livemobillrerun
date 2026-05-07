"""Live health probe for long-running streaming sessions.

Prints a one-line `[stat]` summary every N seconds combining:

- TCP server side (PC): client uptime, total bytes sent, bytes/sec,
  rough frames sent (NAL boundaries), connection state.
- Phone side (via adb): size + mtime of the YUV frame file written by
  vcam-app. If the size changes between probes we know the decoder is
  still consuming and writing frames; if it stalls we flag it.

The probe is best-effort — adb errors are suppressed (we don't want a
flaky USB cable to crash the streamer).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .adb import AdbController

if TYPE_CHECKING:
    from .tcp_server import TcpStreamServer

log = logging.getLogger(__name__)

# Path on the phone where vcam-app writes YUV frames (app-private fallback
# location — used when /data/local/tmp/vcam.yuv isn't writable, which is
# the case on unrooted devices).
DEFAULT_PHONE_YUV_PATHS = (
    "/data/data/com.livemobillrerun.vcam/files/vcam.yuv",
    "/data/local/tmp/vcam.yuv",
)


@dataclass
class HealthSnapshot:
    """Most recent reading we got back from a probe iteration."""

    pc_bytes_sent: int = 0
    pc_frames_sent: int = 0
    pc_uptime_s: float = 0.0
    pc_client_addr: str = "—"
    phone_yuv_size: int | None = None
    phone_yuv_mtime: int | None = None  # epoch seconds reported by `stat`
    phone_yuv_path: str | None = None
    phone_yuv_fresh_s: float | None = None  # how stale the file is, in s
    last_progress_at: float = field(default_factory=time.monotonic)
    stalled_for_s: float = 0.0


class HealthMonitor:
    """Background thread that prints `[stat]` lines every `interval_s`."""

    def __init__(
        self,
        server: "TcpStreamServer",
        adb: AdbController,
        interval_s: float = 5.0,
        phone_paths: tuple[str, ...] = DEFAULT_PHONE_YUV_PATHS,
        stall_warn_after_s: float = 8.0,
    ) -> None:
        self.server = server
        self.adb = adb
        self.interval_s = interval_s
        self.phone_paths = phone_paths
        self.stall_warn_after_s = stall_warn_after_s

        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = HealthSnapshot()
        self._last_bytes = 0
        self._resolved_phone_path: str | None = None
        self._adb_failures = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop, name="vcam-health", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2)

    def snapshot(self) -> HealthSnapshot:
        return self._snapshot

    # ── internals ──────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_evt.wait(self.interval_s):
            try:
                self._tick()
            except Exception:
                log.exception("health tick failed")

    def _tick(self) -> None:
        snap = self._snapshot

        bytes_now = self.server.bytes_sent
        bytes_per_sec = (bytes_now - self._last_bytes) / self.interval_s
        self._last_bytes = bytes_now

        snap.pc_bytes_sent = bytes_now
        snap.pc_frames_sent = self.server.frames_sent
        snap.pc_uptime_s = self.server.uptime_s
        snap.pc_client_addr = self.server.client_addr

        prev_mtime = snap.phone_yuv_mtime
        size, mtime, path, dev_now = self._probe_phone_yuv()
        snap.phone_yuv_size = size
        snap.phone_yuv_mtime = mtime
        snap.phone_yuv_path = path
        if mtime is not None and dev_now is not None:
            snap.phone_yuv_fresh_s = max(0, dev_now - mtime)
        else:
            snap.phone_yuv_fresh_s = None

        # The YUV file is rewritten in-place each frame, so size stays
        # constant — track mtime to detect "decoder is still consuming".
        mtime_advanced = (
            prev_mtime is not None
            and mtime is not None
            and mtime > prev_mtime
        )
        progressing = bytes_per_sec > 1024 or mtime_advanced
        if progressing:
            snap.last_progress_at = time.monotonic()
            snap.stalled_for_s = 0.0
        else:
            snap.stalled_for_s = time.monotonic() - snap.last_progress_at

        line = self._format_line(snap, bytes_per_sec)
        if snap.stalled_for_s >= self.stall_warn_after_s:
            log.warning("[stat] %s", line)
        else:
            log.info("[stat] %s", line)

    def _probe_phone_yuv(
        self,
    ) -> tuple[int | None, int | None, str | None, int | None]:
        """Returns (size_bytes, mtime_epoch, path, device_now_epoch)."""
        if not self.adb.is_available() or self._adb_failures > 6:
            return None, None, None, None
        if self._resolved_phone_path is None:
            for candidate in self.phone_paths:
                size, mtime = self._stat(candidate)
                if size is not None:
                    self._resolved_phone_path = candidate
                    return size, mtime, candidate, self._device_now()
            return None, None, None, None
        size, mtime = self._stat(self._resolved_phone_path)
        return size, mtime, self._resolved_phone_path, self._device_now()

    def _stat(self, path: str) -> tuple[int | None, int | None]:
        """Return (size_bytes, mtime_epoch) for `path`, or (None, None) if
        not present / readable. Uses `adb shell run-as` for app-private
        paths so it works on unrooted devices."""
        if path.startswith("/data/data/com.livemobillrerun.vcam/"):
            rel = path[len("/data/data/com.livemobillrerun.vcam/"):]
            cmd = f"run-as com.livemobillrerun.vcam stat -c '%s %Y' {rel}"
        else:
            cmd = f"stat -c '%s %Y' {path}"
        out = self.adb.shell(cmd, timeout=3).strip()
        if not out:
            self._adb_failures += 1
            return None, None
        parts = out.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None, None
        self._adb_failures = 0
        return int(parts[0]), int(parts[1])

    def _device_now(self) -> int | None:
        """Get the phone's current epoch time so we can compare against
        the YUV file's mtime."""
        out = self.adb.shell("date +%s", timeout=3).strip()
        return int(out) if out.isdigit() else None

    @staticmethod
    def _format_line(snap: HealthSnapshot, bytes_per_sec: float) -> str:
        mb = snap.pc_bytes_sent / (1024 * 1024)
        rate_kib = bytes_per_sec / 1024
        parts = [
            f"up={snap.pc_uptime_s:5.0f}s",
            f"pc={mb:6.2f}MB",
            f"rate={rate_kib:6.1f}KiB/s",
            f"frames~{snap.pc_frames_sent}",
            f"client={snap.pc_client_addr}",
        ]
        if snap.phone_yuv_size is not None:
            parts.append(f"phone_yuv={snap.phone_yuv_size // 1024}KiB")
            if snap.phone_yuv_fresh_s is not None:
                parts.append(f"age={snap.phone_yuv_fresh_s:.1f}s")
        else:
            parts.append("phone_yuv=?")
        if snap.stalled_for_s >= 1.0:
            parts.append(f"stall={snap.stalled_for_s:.0f}s")
        return "  ".join(parts)
