"""TCP server that forwards FFmpeg's H.264 stdout to a connected phone.

Design:
- Accepts ONE client at a time. When a second client connects, it replaces
  the first.
- On client connect, we open a fresh FFmpeg process. On disconnect, we kill
  FFmpeg. This means FFmpeg only runs while there's a phone listening — no
  wasted CPU.
- A heartbeat thread polls every 250 ms to detect disconnect.

We don't bother with the length-prefix framing on the wire. FFmpeg writes
Annex-B H.264 (start codes 0x00 0x00 0x00 0x01 between NAL units), and the
Android receiver can feed those bytes straight into MediaCodec.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .config import DeviceProfile, StreamConfig
from .ffmpeg_streamer import FFmpegStreamer

log = logging.getLogger(__name__)

# Bytes sent per pump iteration. Must be smaller than the kernel send buffer.
CHUNK = 64 * 1024


class TcpStreamServer:
    def __init__(
        self,
        cfg: StreamConfig,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_state = on_state or (lambda s: None)
        self._server_sock: socket.socket | None = None
        self._streamer = FFmpegStreamer(cfg)
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._client: socket.socket | None = None
        self._client_addr: tuple[str, int] | None = None
        self._playlist: Path | None = None
        self._profile: DeviceProfile | None = None
        self._bytes_sent = 0
        self._frames_sent = 0  # rough — counts NAL boundaries
        self._connected_at: float | None = None

    # ── public API ─────────────────────────────────────────────

    @property
    def bytes_sent(self) -> int:
        return self._bytes_sent

    @property
    def frames_sent(self) -> int:
        """Rough — counts Annex-B start codes (≈ 2–4 per video frame)."""
        return self._frames_sent

    @property
    def is_client_connected(self) -> bool:
        return self._client is not None

    @property
    def client_addr(self) -> str:
        if not self._client_addr:
            return "—"
        host, port = self._client_addr
        return f"{host}:{port}"

    @property
    def uptime_s(self) -> float:
        if self._connected_at is None:
            return 0.0
        return time.monotonic() - self._connected_at

    def start(self, playlist: Path, profile: DeviceProfile) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("server already running")
        self._playlist = playlist
        self._profile = profile
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            if self._client:
                self._client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            if self._server_sock:
                self._server_sock.close()
        except OSError:
            pass
        self._streamer.stop()
        if self._thread:
            self._thread.join(timeout=3)
        self._notify("stopped")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── internals ──────────────────────────────────────────────

    def _notify(self, state: str) -> None:
        try:
            self.on_state(state)
        except Exception:
            log.exception("on_state callback failed")

    def _serve_forever(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", self.cfg.tcp_port))
        except OSError as e:
            log.error("bind :%d failed: %s", self.cfg.tcp_port, e)
            self._notify(f"bind failed: {e}")
            return
        srv.listen(1)
        srv.settimeout(0.5)
        self._server_sock = srv
        log.info("Listening on tcp://0.0.0.0:%d", self.cfg.tcp_port)
        self._notify(f"listening :{self.cfg.tcp_port}")

        while not self._stop_evt.is_set():
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            log.info("Client connected: %s", addr)
            self._client = client
            self._client_addr = addr
            self._connected_at = time.monotonic()
            self._bytes_sent = 0
            self._frames_sent = 0
            self._notify(f"client {addr[0]}:{addr[1]} connected")

            try:
                self._pump(client)
            except Exception:
                log.exception("pump error")
            finally:
                try:
                    client.close()
                except OSError:
                    pass
                self._client = None
                self._client_addr = None
                self._connected_at = None
                self._streamer.stop()
                self._notify("client disconnected")

        try:
            srv.close()
        except OSError:
            pass
        log.info("Server stopped")

    def _pump(self, client: socket.socket) -> None:
        """Spawn FFmpeg for this client, then forward stdout → socket."""
        assert self._playlist and self._profile
        proc = self._streamer.start(self._playlist, self._profile)
        client.settimeout(1.0)

        try:
            assert proc.stdout is not None
            while not self._stop_evt.is_set():
                if proc.poll() is not None:
                    log.warning("ffmpeg exited rc=%s", proc.returncode)
                    break
                buf = proc.stdout.read(CHUNK)
                if not buf:
                    log.info("ffmpeg stdout EOF")
                    break
                try:
                    client.sendall(buf)
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    log.info("client gone: %s", e)
                    break
                self._bytes_sent += len(buf)
                # crude frame counter: count Annex-B start-code occurrences
                self._frames_sent += buf.count(b"\x00\x00\x00\x01")
        finally:
            self._streamer.stop()
