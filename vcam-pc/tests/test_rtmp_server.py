"""Regression tests for v1.8.0's RTMP server wrapper.

The wrapper itself is small and mostly orchestrates a real
mediamtx subprocess, so these tests focus on the parts that
are pure-Python (config rendering, IP discovery branching,
URL formatting) plus a single end-to-end "spawn the bundled
binary" check that is automatically skipped if a developer
runs the suite without ``.tools/<os>/mediamtx`` populated.
"""

from __future__ import annotations

import socket
from unittest import mock

import pytest

from src import platform_tools, rtmp_server


# ── pure-Python helpers ────────────────────────────────────────────


class TestGenerateConfig:
    def test_only_rtmp_listener_enabled(self):
        cfg = rtmp_server._generate_config(1935)
        # Other protocols explicitly disabled — we don't want
        # mediamtx binding HLS/RTSP/WebRTC ports the customer
        # doesn't expect.
        for proto in ("rtsp", "hls", "webrtc", "srt"):
            assert f"{proto}: no" in cfg, f"missing 'no' for {proto}"
        assert "rtmp: yes" in cfg
        assert "rtmpAddress: :1935" in cfg

    def test_admin_surfaces_disabled(self):
        cfg = rtmp_server._generate_config(1935)
        for surface in ("api", "metrics", "pprof", "playback"):
            assert f"{surface}: no" in cfg

    def test_port_substituted(self):
        cfg = rtmp_server._generate_config(11935)
        assert "rtmpAddress: :11935" in cfg
        assert ":1935" not in cfg.replace(":11935", "")


# ── get_local_ip ───────────────────────────────────────────────────


class TestGetLocalIP:
    def test_returns_loopback_when_no_route(self):
        with mock.patch.object(socket, "socket", side_effect=OSError):
            assert rtmp_server.get_local_ip() == "127.0.0.1"

    def test_uses_socket_getsockname(self):
        fake_sock = mock.MagicMock()
        fake_sock.getsockname.return_value = ("192.168.1.42", 12345)
        with mock.patch.object(
            socket, "socket", return_value=fake_sock,
        ):
            assert rtmp_server.get_local_ip() == "192.168.1.42"


# ── RTMPServer URL helpers ─────────────────────────────────────────


class TestRTMPUrls:
    def test_loopback_default(self):
        s = rtmp_server.RTMPServer(port=1935)
        assert s.rtmp_url == "rtmp://127.0.0.1:1935/live"
        # Before start(), the LAN IP is uninitialised → loopback.
        assert s.rtmp_url_for_phone == "rtmp://127.0.0.1:1935/live"
        assert s.is_lan_routable is False

    def test_routable_after_start_with_lan_ip(self):
        s = rtmp_server.RTMPServer(port=1935)
        s._local_ip = "192.168.1.42"
        assert s.rtmp_url_for_phone == "rtmp://192.168.1.42:1935/live"
        assert s.is_lan_routable is True

    def test_zero_address_treated_as_unroutable(self):
        s = rtmp_server.RTMPServer(port=1935)
        s._local_ip = "0.0.0.0"
        assert s.is_lan_routable is False


# ── RTMPServer.start with no binary ────────────────────────────────


class TestStartNoBinary:
    def test_returns_false_and_logs_when_mediamtx_missing(self):
        s = rtmp_server.RTMPServer(port=1935)
        logs: list[str] = []
        s._log_cb = logs.append
        with mock.patch.object(
            platform_tools, "find_mediamtx", return_value=None,
        ):
            assert s.start() is False
        assert any("ไม่พบ mediamtx" in line for line in logs)
        assert s.is_running is False


# ── End-to-end: spawn the real bundled mediamtx ────────────────────


class TestRealSpawn:
    """End-to-end: actually spawn the bundled mediamtx. Skipped
    when the binary isn't on disk (e.g. fresh clone where
    ``tools/setup_ci_tools.py`` hasn't been run yet).

    NB: ``test_platform_tools_frozen.py`` reloads platform_tools
    with ``importlib.reload`` against a tmp_path so its
    ``LEGACY_TOOLS_ROOT`` is left dangling after that test
    file finishes. We force a fresh reload here so our
    resolver finds the real ``.tools/<os>/mediamtx``."""

    @pytest.fixture(autouse=True)
    def _refresh_platform_tools(self):
        import importlib

        importlib.reload(platform_tools)
        # rtmp_server holds a reference to platform_tools too;
        # reload it so it picks up the fresh resolver.
        importlib.reload(rtmp_server)

    def test_start_then_stop_listens_on_port(self):
        if platform_tools.find_mediamtx() is None:
            pytest.skip("mediamtx not bundled — run tools/setup_ci_tools.py first")

        # Non-default port to avoid clashing with anything the
        # dev might already have running on 1935.
        s = rtmp_server.RTMPServer(port=21935)
        try:
            assert s.start() is True
            assert s.is_running is True
            assert s.is_port_in_use() is True
        finally:
            s.stop()
        # Give the OS a beat to release the port.
        import time
        time.sleep(0.3)
        assert s.is_running is False
