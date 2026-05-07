"""Integration test for TcpStreamServer using a fake FFmpeg process.

We don't actually run FFmpeg here. Instead we monkey-patch the streamer
so it returns a fake subprocess whose stdout yields a fixed byte payload,
then we connect a real TCP client and verify the bytes are forwarded.
"""

from __future__ import annotations

import io
import socket
import time
from pathlib import Path

import pytest

from src.config import DeviceProfile, StreamConfig
from src.tcp_server import TcpStreamServer


class _FakeProc:
    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(b"")
        self._alive = True
        self.pid = 12345
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if not self._alive:
            return self.returncode
        return None

    def terminate(self) -> None:
        self._alive = False
        self.returncode = 0

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        self.returncode = 0
        return 0


class _FakeStreamer:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.proc: _FakeProc | None = None
        self.start_calls = 0
        self.stop_calls = 0

    def start(self, _playlist: Path, _profile: DeviceProfile) -> _FakeProc:
        self.start_calls += 1
        self.proc = _FakeProc(self.payload)
        return self.proc

    def stop(self) -> None:
        self.stop_calls += 1
        if self.proc:
            self.proc.terminate()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def streamer_payload() -> bytes:
    # Realistic-ish: 32 NAL boundaries × 1KB each.
    nal = b"\x00\x00\x00\x01" + (b"X" * 1020)
    return nal * 32


def test_server_forwards_ffmpeg_stdout_to_client(streamer_payload: bytes) -> None:
    cfg = StreamConfig(tcp_port=_free_port(), auto_adb_reverse=False)
    fake = _FakeStreamer(streamer_payload)
    states: list[str] = []

    server = TcpStreamServer(cfg, on_state=states.append)
    server._streamer = fake  # type: ignore[assignment]
    server.start(Path("/tmp/dummy.txt"), DeviceProfile(name="t"))

    deadline = time.monotonic() + 2.0
    while "listening" not in " ".join(states) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert any("listening" in s for s in states), states

    received = bytearray()
    with socket.create_connection(("127.0.0.1", cfg.tcp_port), timeout=2.0) as sock:
        sock.settimeout(2.0)
        try:
            while True:
                buf = sock.recv(4096)
                if not buf:
                    break
                received.extend(buf)
        except socket.timeout:
            pass

    server.stop()

    assert fake.start_calls == 1
    assert bytes(received) == streamer_payload
    assert server.bytes_sent == len(streamer_payload)


def test_server_handles_client_disconnect(streamer_payload: bytes) -> None:
    cfg = StreamConfig(tcp_port=_free_port(), auto_adb_reverse=False)
    fake = _FakeStreamer(streamer_payload * 1000)  # plenty of bytes
    server = TcpStreamServer(cfg)
    server._streamer = fake  # type: ignore[assignment]
    server.start(Path("/tmp/dummy.txt"), DeviceProfile(name="t"))

    deadline = time.monotonic() + 2.0
    while not server.is_running() and time.monotonic() < deadline:
        time.sleep(0.05)

    sock = socket.create_connection(("127.0.0.1", cfg.tcp_port), timeout=2.0)
    sock.recv(64)
    sock.close()

    deadline = time.monotonic() + 2.0
    while fake.stop_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert fake.stop_calls >= 1
    server.stop()


def test_server_reports_bind_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reserve a free port BEFORE we monkey-patch bind.
    cfg = StreamConfig(tcp_port=_free_port(), auto_adb_reverse=False)

    def boom_bind(self: socket.socket, _addr: tuple[str, int]) -> None:
        raise OSError(98, "address already in use (simulated)")

    monkeypatch.setattr(socket.socket, "bind", boom_bind, raising=True)

    states: list[str] = []
    server = TcpStreamServer(cfg, on_state=states.append)
    server._streamer = _FakeStreamer(b"")  # type: ignore[assignment]
    server.start(Path("/tmp/dummy.txt"), DeviceProfile(name="t"))

    deadline = time.monotonic() + 1.5
    while not any("bind failed" in s for s in states) and time.monotonic() < deadline:
        time.sleep(0.05)
    server.stop()

    joined = " ".join(states)
    assert "bind failed" in joined, states
