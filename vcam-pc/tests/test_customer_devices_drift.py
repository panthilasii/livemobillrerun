"""TikTok update-drift tracking on ``DeviceEntry``.

The Studio remembers which TikTok ``versionName`` was current at
the moment we Patched a phone (``patched_tiktok_version``) so the
hook-status probe can flag silent in-app updates that wipe out the
LSPatch overlay. This file pins down the contract of:

* ``mark_patched(serial, tiktok_version=...)`` — both the new
  two-arg form and the legacy single-arg form.
* ``mark_tiktok_drift_warned`` — sets a rate-limit timestamp.
* JSON round-trip — the new fields persist and legacy files load
  with sensible defaults.
* Re-patch resets the warning rate-limit so a *future* drift can
  still surface a fresh dialog.
"""
from __future__ import annotations

import json

from src.customer_devices import DeviceLibrary


def test_mark_patched_records_version(tmp_path):
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched("AB123", tiktok_version="39.5.4")
    e = lib.get("AB123")
    assert e is not None
    assert e.patched_at, "patched_at should be set"
    assert e.patched_tiktok_version == "39.5.4"
    assert e.tiktok_drift_warned_at == "", (
        "fresh patch must clear stale drift warnings"
    )


def test_mark_patched_legacy_single_arg(tmp_path):
    """Older code paths still call mark_patched(serial) without the
    version. That must keep working — we just don't enable drift
    detection for that entry until the next full patch records a
    real value."""
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched("AB123")
    e = lib.get("AB123")
    assert e is not None
    assert e.patched_at
    assert e.patched_tiktok_version == ""


def test_mark_drift_warned_stamps_timestamp(tmp_path):
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_tiktok_drift_warned("AB123")
    e = lib.get("AB123")
    assert e is not None
    assert e.tiktok_drift_warned_at, "should be ISO timestamp string"


def test_round_trip_preserves_drift_fields(tmp_path):
    path = tmp_path / "devices.json"
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched("AB123", tiktok_version="39.5.4")
    lib.mark_tiktok_drift_warned("AB123")
    lib.save(path)

    reloaded = DeviceLibrary.load(path)
    e = reloaded.get("AB123")
    assert e is not None
    assert e.patched_tiktok_version == "39.5.4"
    assert e.tiktok_drift_warned_at != ""


def test_legacy_json_loads_with_defaults(tmp_path):
    """Pre-1.7.4 devices.json files don't have the drift fields.
    They must load clean with empty defaults so the customer's
    update path is zero-touch."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({
        "entries": {
            "OLDSERIAL": {
                "label": "Phone A",
                "patched_at": "2026-04-01T10:00:00",
            },
        },
    }), encoding="utf-8")
    lib = DeviceLibrary.load(path)
    e = lib.get("OLDSERIAL")
    assert e is not None
    assert e.patched_tiktok_version == ""
    assert e.tiktok_drift_warned_at == ""


def test_repatch_clears_drift_warning(tmp_path):
    """After the customer Re-Patches, the per-session drift warning
    flag must reset so a *future* TikTok update will warn again."""
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched("AB123", tiktok_version="39.5.4")
    lib.mark_tiktok_drift_warned("AB123")
    assert lib.get("AB123").tiktok_drift_warned_at

    lib.mark_patched("AB123", tiktok_version="39.6.0")
    e = lib.get("AB123")
    assert e is not None
    assert e.patched_tiktok_version == "39.6.0"
    assert e.tiktok_drift_warned_at == "", (
        "re-patch must clear the rate-limit so future drift warns again"
    )


def test_mark_patched_unknown_serial_is_noop(tmp_path):
    """No crash when the device entry doesn't exist (probe race
    condition: phone unplugged mid-patch finalize)."""
    lib = DeviceLibrary()
    lib.mark_patched("GHOST", tiktok_version="39.5.4")
    lib.mark_tiktok_drift_warned("GHOST")
    assert lib.get("GHOST") is None
