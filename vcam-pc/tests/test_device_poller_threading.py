"""Regression test for the v1.8.2 device-poll vs wifi-reconnect
starvation bug.

Symptom (customer report, May 2026): "ระบบไม่ซิงค์ที่จอเลย /
macbook ผม ตอนนี้ไม่อ่านค่าการเชื่อมต่อเลย" — phone plugged
in via USB, ``adb devices`` showed it as ``device``, but every
device card on the dashboard sat on "offline" indefinitely.

Root cause
----------
``_DevicePoller.run()`` ran *both* device polling AND WiFi
reconnection inline. With 4–6 saved WiFi targets — all on a
LAN the customer was no longer on — each poll cycle paid
``timeout × len(targets)`` seconds blocking on dead
``adb connect`` calls before getting around to the
``adb devices`` query that the dashboard depended on. Result:
``live_devices`` could be 20+ s stale, the freshly-plugged
phone never made it into the UI.

Fix
---
Split into two threads:
* ``_DevicePoller`` — only polls ``adb devices`` on
  ``INTERVAL_S`` cadence.
* ``_WifiReconnector`` — calls ``adb connect`` for saved
  targets in a sibling thread, with adaptive backoff for
  unreachable IPs.

This test pins the interval guarantee: even with N
unreachable WiFi targets in the library, the device poll
keeps firing at ~``INTERVAL_S``-second intervals.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from src.adb import AdbDevice
from src.ui.studio_app import _DevicePoller, _WifiReconnector


class _FakeAdb:
    """Minimal AdbController stand-in for the poller tests.

    ``devices()`` returns instantly with a single fake USB row.
    ``adb_path`` is a sentinel so wifi_adb.adb_connect (mocked
    via the ``slow_connect`` fixture below) can short-circuit.
    """

    def __init__(self) -> None:
        self.adb_path = "/fake/adb"

    def is_available(self) -> bool:
        return True

    def devices(self):
        return [AdbDevice(serial="USB123", state="device", model="TestPhone")]


@pytest.fixture
def slow_wifi_connect(monkeypatch):
    """Patch wifi_adb.adb_connect so every ``adb connect`` call
    blocks for 3 seconds before returning False — simulating an
    unreachable WiFi target on a different LAN."""
    from src.ui import studio_app as sa

    def _slow(adb_path, ip, port, timeout=3.0):
        time.sleep(timeout)
        return False

    monkeypatch.setattr(sa.wifi_adb, "adb_connect", _slow)
    return _slow


class TestDevicePollNotStarvedByWifi:
    """The v1.8.2 fix: device poll cadence is independent of
    WiFi reconnect health."""

    def test_device_poll_fires_at_interval_with_dead_wifi_targets(
        self, slow_wifi_connect
    ):
        """6 unreachable WiFi targets must not delay device poll
        cadence beyond ``INTERVAL_S + small_jitter``."""
        adb = _FakeAdb()

        # Every callback timestamp goes here.
        captured: list[tuple[float, list[AdbDevice]]] = []

        def on_devices(devs):
            captured.append((time.monotonic(), devs))

        def tk_after(_ms, fn):
            # Run synchronously in a worker so this test doesn't
            # require an event loop.
            threading.Thread(target=fn, daemon=True).start()

        def get_targets():
            return [(f"203.0.113.{i}", 5555) for i in range(1, 7)]

        # Tighten _WifiReconnector's interval so the test's 4 s
        # observation window catches the second wifi tick too —
        # but the assertion below is on _DevicePoller's cadence,
        # not the wifi reconnector's.
        _WifiReconnector.INTERVAL_S = 1.0

        poller = _DevicePoller(
            adb,
            on_devices=on_devices,
            tk_after=tk_after,
            get_wifi_targets=get_targets,
        )
        poller.start()
        try:
            time.sleep(4.5)  # Observe ~3 device-poll cycles.
        finally:
            poller.stop()
            # Give worker threads a beat to wind down.
            time.sleep(0.3)

        assert len(captured) >= 2, (
            "device poll never produced a callback — likely starved "
            "by inline wifi reconnect (the v1.8.2 bug)"
        )
        # Every callback must include the USB device — the inverse
        # symptom would be empty device lists during the wifi stall.
        for _, devs in captured:
            assert any(d.serial == "USB123" for d in devs), (
                "device poll returned empty list during wifi-thread blocking"
            )
        # Cadence: each callback should be within
        # INTERVAL_S + 0.5 s of the previous one. If wifi
        # blocking starves the poll loop, gaps balloon to 4+ s.
        max_gap = max(
            b[0] - a[0] for a, b in zip(captured[:-1], captured[1:])
        ) if len(captured) > 1 else 0.0
        assert max_gap < _DevicePoller.INTERVAL_S + 1.0, (
            f"max gap between device polls was {max_gap:.2f}s — "
            f"wifi reconnect is starving the loop again"
        )


class TestWifiReconnectorBackoff:
    """Adaptive backoff: a target that fails ``FAIL_THRESHOLD``
    times in a row gets de-prioritised so the rest of the
    reconnect tick isn't dominated by it."""

    def test_unreachable_target_eventually_throttles(
        self, slow_wifi_connect, monkeypatch
    ):
        adb = _FakeAdb()
        stop = threading.Event()
        targets = [("203.0.113.99", 5555)]

        attempt_count = 0

        def _fail_fast(adb_path, ip, port, timeout=3.0):
            nonlocal attempt_count
            attempt_count += 1
            return False  # always fail

        from src.ui import studio_app as sa
        monkeypatch.setattr(sa.wifi_adb, "adb_connect", _fail_fast)

        # Tight interval so the test runs quickly; FAIL_THRESHOLD
        # left at default (3) so we expect 3 attempts then a back-
        # off pause longer than our observation window.
        _WifiReconnector.INTERVAL_S = 0.05
        recon = _WifiReconnector(adb, lambda: targets, stop)
        recon.start()
        try:
            time.sleep(0.6)  # ~12 ticks if no backoff
        finally:
            stop.set()
            recon.join(timeout=2.0)

        # Without backoff we'd see ~12 attempts; with backoff it
        # should taper. Assert: at most 6 attempts (FAIL_THRESHOLD
        # + a little slack for timing jitter).
        assert attempt_count <= 6, (
            f"backoff didn't kick in — got {attempt_count} attempts in 0.6 s"
        )
        # Sanity: at least the threshold count must have run, otherwise
        # the test isn't actually exercising the backoff path.
        assert attempt_count >= _WifiReconnector.FAIL_THRESHOLD


class TestPollerCleanShutdown:
    """``stop()`` must terminate both threads promptly even when
    the wifi reconnector is mid-attempt."""

    def test_stop_terminates_both_threads(self, slow_wifi_connect):
        adb = _FakeAdb()
        captured: list = []

        poller = _DevicePoller(
            adb,
            on_devices=captured.append,
            tk_after=lambda *a, **k: None,
            get_wifi_targets=lambda: [("203.0.113.42", 5555)],
        )
        poller.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        poller.stop()
        # Wait up to 5 s — 3 s wifi connect + small slack.
        poller.join(timeout=5.0)
        poller._wifi.join(timeout=5.0)
        elapsed = time.monotonic() - t0

        assert not poller.is_alive(), "device poll thread didn't stop"
        assert not poller._wifi.is_alive(), "wifi reconnector didn't stop"
        # The wifi thread can't be killed mid-adb-connect, so the
        # join can take up to CONNECT_TIMEOUT_S (3 s). Accept that;
        # what matters is that we *eventually* shut down cleanly.
        assert elapsed < 5.0, (
            f"shutdown took {elapsed:.2f} s — should be under 5 s"
        )
