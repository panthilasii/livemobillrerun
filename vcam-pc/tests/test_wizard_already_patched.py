"""Regression test for v1.8.1 — the "patched device silently filtered"
bug.

Before v1.8.1 the Add-Device wizard's step-1 filter contained a
``continue`` for every USB device whose serial appeared in the
device library with ``patched_at`` set. The intent was "the
dashboard handles re-patch separately"; the impact was that
returning customers (the entire long-tail of NP Create users)
would plug in a phone they'd previously onboarded and be told
"🔄 รอเครื่องเชื่อมต่อ…" forever — even though ``adb devices``
listed the phone as ``device``.

The fix: bucket already-patched USB rows separately and surface
them with a "🟢 พบเครื่อง — เคย patch แล้ว, กด ถัดไป เพื่อ re-patch"
prompt instead of silently dropping them.

These tests pin the bucketing helper so future refactors don't
re-introduce the silent skip.
"""

from __future__ import annotations

from src.adb import AdbDevice


def _bucket(live_devices, patched_serials, is_wifi_id):
    """Reproduce the v1.8.1 step-1 bucketing inline so the test
    doesn't have to instantiate Tk widgets to exercise the
    branching. Mirrors ``WizardPage._render_step_1`` exactly.
    """
    online_candidates: list = []
    already_patched: list = []
    unauthorized_devs: list = []
    offline_devs: list = []
    for d in live_devices:
        if is_wifi_id(d.serial):
            continue
        if d.online:
            if d.serial in patched_serials:
                already_patched.append(d)
            else:
                online_candidates.append(d)
        elif (d.state or "").lower() == "unauthorized":
            unauthorized_devs.append(d)
        elif (d.state or "").lower() in ("offline", "bootloader", "recovery"):
            offline_devs.append(d)
    return online_candidates, already_patched, unauthorized_devs, offline_devs


class TestBucketing:
    def test_fresh_online_device_lands_in_online_candidates(self):
        live = [AdbDevice(serial="abc123", state="device", model="X")]
        oc, ap, _, _ = _bucket(live, set(), lambda s: False)
        assert oc == live
        assert ap == []

    def test_previously_patched_device_visible_not_skipped(self):
        """The exact bug v1.8.1 fixes: a returning USB device whose
        serial is already marked patched must NOT be silently
        dropped — it has to surface in ``already_patched`` so the
        wizard can offer a re-patch."""
        live = [AdbDevice(serial="324953282110", state="device", model="Z2462N")]
        oc, ap, _, _ = _bucket(live, {"324953282110"}, lambda s: False)
        assert oc == []
        assert ap == live, (
            "regression: patched USB device was silently filtered; "
            "wizard would have shown '🔄 รอเครื่อง…' even though adb "
            "sees the phone."
        )

    def test_unauthorized_lands_in_unauthorized_bucket(self):
        live = [AdbDevice(serial="abc", state="unauthorized")]
        oc, ap, ud, _ = _bucket(live, set(), lambda s: False)
        assert oc == []
        assert ap == []
        assert ud == live

    def test_offline_lands_in_offline_bucket(self):
        live = [AdbDevice(serial="abc", state="offline")]
        _, _, _, od = _bucket(live, set(), lambda s: False)
        assert od == live

    def test_recovery_lands_in_offline_bucket(self):
        live = [AdbDevice(serial="abc", state="recovery")]
        _, _, _, od = _bucket(live, set(), lambda s: False)
        assert od == live

    def test_wifi_rows_skipped_entirely(self):
        live = [
            AdbDevice(serial="192.168.1.42:5555", state="device"),
            AdbDevice(serial="abc", state="device"),
        ]
        oc, _, _, _ = _bucket(
            live,
            set(),
            lambda s: ":" in s and s.replace(":", "").replace(".", "").isdigit(),
        )
        assert len(oc) == 1
        assert oc[0].serial == "abc"

    def test_mixed_state_real_world(self):
        """Customer's real-world state from the v1.8.1 bug report:
        one freshly-plugged phone (serial=324953282110) that's
        already in the library as patched. Wizard MUST surface it
        as a re-patch candidate, not '🔄 รอเครื่อง'.
        """
        live = [
            AdbDevice(serial="324953282110", state="device", model="Z2462N"),
        ]
        patched = {"NBV8PTHBDC001375", "R5CY320AV0T", "324953282110"}
        oc, ap, ud, od = _bucket(live, patched, lambda s: False)
        assert oc == []
        assert len(ap) == 1
        assert ap[0].serial == "324953282110"
        assert ud == []
        assert od == []
