"""Per-device TikTok variant persistence.

We exercise the ``tiktok_package`` field added to ``DeviceEntry``
along with its load / save / upsert path. Coverage:

* New entries default to empty -- nothing is forced; the dashboard
  fills it in on the first hook-status probe.
* ``update_tiktok_package`` writes the value, and the next save +
  load round-trip preserves it.
* Legacy JSON files (without the field) load cleanly with a
  default empty string -- a migration without a migration script.
* Empty-string updates clear the value (covers the rare case where
  a customer uninstalls TikTok entirely).
"""
from __future__ import annotations

import json

import pytest

from src.customer_devices import DeviceLibrary


def test_default_is_empty(tmp_path):
    lib = DeviceLibrary()
    e = lib.upsert("AB123", model="Redmi 14C")
    assert e.tiktok_package == ""


def test_update_round_trip(tmp_path):
    path = tmp_path / "devices.json"
    lib = DeviceLibrary()
    lib.upsert("AB123", model="Redmi 14C")
    lib.update_tiktok_package("AB123", "com.zhiliaoapp.musically.go")
    lib.save(path)

    loaded = DeviceLibrary.load(path)
    e = loaded.get("AB123")
    assert e is not None
    assert e.tiktok_package == "com.zhiliaoapp.musically.go"


def test_legacy_json_without_field_loads_clean(tmp_path):
    """Existing customer installs from v1.4.x have no
    ``tiktok_package`` key in their devices.json. They must keep
    working -- empty default + auto-detection on first probe."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({
        "entries": {
            "OLDSERIAL": {
                "label": "Phone A",
                "model": "Redmi 13C",
                "rotation": 90,
                "patched_at": "2026-04-01T10:00:00",
            },
        },
    }), encoding="utf-8")

    lib = DeviceLibrary.load(path)
    e = lib.get("OLDSERIAL")
    assert e is not None
    assert e.tiktok_package == ""
    assert e.label == "Phone A"


def test_clear_with_empty_string(tmp_path):
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.update_tiktok_package("AB123", "com.ss.android.ugc.trill")
    assert lib.get("AB123").tiktok_package == "com.ss.android.ugc.trill"

    lib.update_tiktok_package("AB123", "")
    assert lib.get("AB123").tiktok_package == ""


def test_no_op_for_unknown_serial(tmp_path):
    """Updating a serial we've never seen must not crash and must
    not auto-create an entry. Tests the contract used by the
    dashboard's hook-status callback when the customer has
    unplugged the phone mid-probe."""
    lib = DeviceLibrary()
    # Should not raise.
    lib.update_tiktok_package("UNKNOWN", "com.x")
    assert lib.get("UNKNOWN") is None
