"""Smart WiFi reconnect — three-level fallback contract (v1.8.5).

The dashboard's ``Reconnect WiFi`` button used to be a single
``adb connect <saved_ip>`` shot — which silently fails ~80 % of the
time once the saved IP is more than a day old (DHCP renewals after
phone reboot / WiFi off-on / lease expiry routinely flip the address).

v1.8.5's ``StudioApp.reconnect_wifi`` adds a three-level recovery
ladder; this file pins down the contract for each rung so a future
"simplify" refactor can't silently regress to the old single-shot
behaviour.

Test matrix
~~~~~~~~~~~

1. Fast path     — saved IP works → success, fail counter cleared
2. USB fallback  — saved IP fails BUT phone is plugged in via USB →
                   re-tcpip recovers the fresh IP and reports it
3. USB partial   — re-tcpip captured fresh IP but couldn't connect
                   yet (cable still pinning adbd) → user-facing msg
                   from setup_wifi_after_patch is surfaced as-is,
                   fail counter NOT incremented (we made progress)
4. No-USB fail   — counter increments, message points to USB recovery
5. 3-fail clear  — after WIFI_RECONNECT_FAIL_LIMIT consecutive fails
                   without USB, ``wifi_ip`` is wiped from devices.json
                   so the next dashboard refresh shows "ตั้งค่า WiFi"
6. Success reset — a single success after partial fails clears the
                   counter (otherwise a transient outage poisons the
                   state forever)
"""
from __future__ import annotations

from unittest import mock

import pytest

from src.customer_devices import DeviceLibrary


class _FakeApp:
    """Minimal stand-in for ``StudioApp``.

    We avoid spinning up the real ``ctk.CTk`` window in unit tests
    because it blocks on a display server (and CI doesn't have one).
    Instead we mirror the exact attributes ``reconnect_wifi`` /
    ``setup_wifi_after_patch`` touch and import the real method off
    ``StudioApp`` so the production logic gets exercised verbatim.
    """

    WIFI_RECONNECT_FAIL_LIMIT = 3

    def __init__(self, lib: DeviceLibrary, transport: str = ""):
        self.devices_lib = lib
        self._wifi_reconnect_fails: dict[str, int] = {}
        self._transport = transport
        self._setup_wifi_returns = "✅ เปิด WiFi สำเร็จ — โทรศัพท์อยู่ที่ 1.2.3.4:5555"
        self._setup_wifi_calls = 0
        # ``reconnect_wifi`` reads cfg.adb_path; tests don't touch
        # subprocess so any string is fine.
        self.cfg = mock.Mock(adb_path="adb")

    def transport_of(self, serial: str) -> str:
        return self._transport

    def setup_wifi_after_patch(self, serial: str) -> str:
        self._setup_wifi_calls += 1
        return self._setup_wifi_returns

    def save_devices(self) -> None:
        pass


@pytest.fixture
def reconnect():
    """Bind the real ``StudioApp.reconnect_wifi`` to ``_FakeApp``."""
    from src.ui.studio_app import StudioApp
    return StudioApp.reconnect_wifi


@pytest.fixture
def lib_with_phone():
    lib = DeviceLibrary()
    lib.upsert("AA111", model="Test Phone")
    lib.update_wifi("AA111", "192.168.1.50", port=5555)
    return lib


# ── 1. Fast path ──────────────────────────────────────────────────


def test_saved_ip_succeeds_resets_counter(reconnect, lib_with_phone):
    app = _FakeApp(lib_with_phone, transport="")
    # Pretend a previous click had failed once already.
    app._wifi_reconnect_fails["AA111"] = 1

    with mock.patch("src.ui.studio_app.wifi_adb.adb_connect",
                    return_value=True):
        ok, msg = reconnect(app, "AA111")

    assert ok is True
    assert "192.168.1.50:5555" in msg
    assert "AA111" not in app._wifi_reconnect_fails, (
        "success must clear the per-serial fail counter so a "
        "transient outage doesn't poison the state forever"
    )
    assert app._setup_wifi_calls == 0, (
        "fast path must not run the USB-fallback re-tcpip flow"
    )


# ── 2. USB-assisted re-discovery ──────────────────────────────────


def test_stale_ip_with_usb_runs_re_tcpip(reconnect, lib_with_phone):
    """The 1.8.5 headline: when the cable is still plugged in, a
    failed ``adb connect`` triggers ``setup_wifi_after_patch`` and
    the customer gets reconnected on the fresh IP without leaving
    the dashboard."""
    app = _FakeApp(lib_with_phone, transport="usb")
    # First adb_connect (fast path) fails — saved IP is stale.
    app._setup_wifi_returns = (
        "✅ เปิด WiFi สำเร็จ — โทรศัพท์อยู่ที่ 192.168.1.99:5555\n"
        "ถอดสาย USB ได้เลย..."
    )

    # After re-tcpip, the library should reflect the new IP. The
    # fake setup_wifi_after_patch normally would update_wifi() —
    # mirror that side-effect here so reconnect_wifi can read it
    # back when it builds the success message.
    def _fake_setup(serial: str) -> str:
        app._setup_wifi_calls += 1
        lib_with_phone.update_wifi(serial, "192.168.1.99", port=5555)
        return app._setup_wifi_returns
    app.setup_wifi_after_patch = _fake_setup

    with mock.patch("src.ui.studio_app.wifi_adb.adb_connect",
                    return_value=False):
        ok, msg = reconnect(app, "AA111")

    assert ok is True, "USB-assisted re-discovery must report success"
    assert "192.168.1.99" in msg, (
        "success message must surface the FRESH IP, not the stale "
        f"one the user clicked from. Got: {msg!r}"
    )
    assert "192.168.1.50" in msg, (
        "message should also mention the stale IP that got replaced "
        "so the customer understands what changed"
    )
    assert app._setup_wifi_calls == 1
    assert "AA111" not in app._wifi_reconnect_fails


def test_usb_fallback_partial_success_does_not_increment_counter(
    reconnect, lib_with_phone,
):
    """``setup_wifi_after_patch`` returns a 📶-prefixed string when
    it managed to grab a fresh IP but couldn't verify the connect
    yet (USB cable still holding adbd in USB mode for a beat).

    That's *progress* — the persisted IP is now correct — so the
    fail counter must NOT advance, otherwise three consecutive
    near-misses would wipe out a perfectly valid IP we just learned.
    """
    app = _FakeApp(lib_with_phone, transport="usb")
    app._setup_wifi_returns = (
        "📶 บันทึก WiFi 192.168.1.99:5555 ไว้แล้ว — แต่ยังเชื่อมไม่ติดตอนนี้\n"
        "ลองถอดสาย USB แล้วกด..."
    )

    with mock.patch("src.ui.studio_app.wifi_adb.adb_connect",
                    return_value=False):
        ok, msg = reconnect(app, "AA111")

    assert ok is False
    assert "192.168.1.99" in msg
    assert "AA111" not in app._wifi_reconnect_fails, (
        "partial success must not push us toward the 3-fail "
        "clear-IP cliff — that would discard the fresh IP we "
        "literally just discovered."
    )


# ── 3. No-USB failure path ────────────────────────────────────────


def test_no_usb_failure_increments_counter_with_helpful_msg(
    reconnect, lib_with_phone,
):
    app = _FakeApp(lib_with_phone, transport="")  # no USB

    with mock.patch("src.ui.studio_app.wifi_adb.adb_connect",
                    return_value=False):
        ok, msg = reconnect(app, "AA111")

    assert ok is False
    assert app._wifi_reconnect_fails["AA111"] == 1
    assert "เสียบสาย USB" in msg, (
        "error message must point the customer at the USB recovery "
        "path so they know what action to take next"
    )
    assert app._setup_wifi_calls == 0, (
        "must not call setup_wifi_after_patch without USB — "
        "tcpip needs a USB-connected device"
    )


# ── 4. Three-strike clear ─────────────────────────────────────────


def test_third_failure_clears_saved_ip(reconnect, lib_with_phone):
    """After WIFI_RECONNECT_FAIL_LIMIT (3) consecutive no-USB
    failures, the saved IP is wiped and the counter reset, so the
    UI flips to "📶 ตั้งค่า WiFi" and stops the customer from
    re-trying a dead IP forever."""
    app = _FakeApp(lib_with_phone, transport="")

    with mock.patch("src.ui.studio_app.wifi_adb.adb_connect",
                    return_value=False):
        ok1, _ = reconnect(app, "AA111")
        ok2, _ = reconnect(app, "AA111")
        ok3, msg3 = reconnect(app, "AA111")

    assert (ok1, ok2, ok3) == (False, False, False)
    e = lib_with_phone.get("AA111")
    assert e is not None
    assert e.wifi_ip == "", (
        "third consecutive failure must clear wifi_ip so the "
        "dashboard surfaces 'ตั้งค่า WiFi' instead of letting the "
        "customer mash a dead 'เชื่อม WiFi อีกครั้ง' button"
    )
    assert "AA111" not in app._wifi_reconnect_fails, (
        "counter must reset after clearing — re-pairing later "
        "should start from a clean slate"
    )
    assert "ลบ IP เก่า" in msg3
    assert "ตั้งค่า WiFi" in msg3


def test_first_two_failures_keep_saved_ip(reconnect, lib_with_phone):
    """One or two failures alone don't justify wiping the IP — the
    customer might just have rebooted the router."""
    app = _FakeApp(lib_with_phone, transport="")

    with mock.patch("src.ui.studio_app.wifi_adb.adb_connect",
                    return_value=False):
        for expected_count in (1, 2):
            ok, _ = reconnect(app, "AA111")
            assert ok is False
            assert app._wifi_reconnect_fails["AA111"] == expected_count
            assert lib_with_phone.get("AA111").wifi_ip == "192.168.1.50"


# ── 5. Edge cases ─────────────────────────────────────────────────


def test_no_wifi_saved_returns_helpful_msg(reconnect):
    lib = DeviceLibrary()
    lib.upsert("BB222")
    app = _FakeApp(lib)

    ok, msg = reconnect(app, "BB222")

    assert ok is False
    assert "ตั้งค่า WiFi" in msg


def test_unknown_serial_returns_failure(reconnect):
    lib = DeviceLibrary()
    app = _FakeApp(lib)

    ok, _ = reconnect(app, "DOES_NOT_EXIST")
    assert ok is False
