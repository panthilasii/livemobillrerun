"""``DeviceLibrary.remove`` — backing API for the Dashboard's
trash button (v1.8.3).

The UI calls ``app.devices_lib.remove(serial)`` followed by
``app.save_devices()``; this file pins down the in-memory and
on-disk contract so a future refactor that "cleans up" the
library can't silently break the delete-device flow.
"""
from __future__ import annotations

import json

from src.customer_devices import DeviceLibrary


def test_remove_drops_entry_from_in_memory_list():
    lib = DeviceLibrary()
    lib.upsert("AAA111", label="alpha")
    lib.upsert("BBB222", label="beta")

    ok = lib.remove("AAA111")

    assert ok is True
    assert lib.get("AAA111") is None
    assert [e.serial for e in lib.list()] == ["BBB222"]


def test_remove_unknown_serial_is_idempotent():
    lib = DeviceLibrary()
    lib.upsert("AAA111")

    # Removing the wrong serial must not raise and must not
    # touch other entries.
    assert lib.remove("DOES_NOT_EXIST") is False
    assert lib.get("AAA111") is not None


def test_remove_persists_through_save_load(tmp_path):
    lib_path = tmp_path / "devices.json"
    lib = DeviceLibrary()
    lib.upsert("AAA111", label="alpha")
    lib.upsert("BBB222", label="beta")
    lib.save(path=lib_path)

    # Sanity check: both entries on disk before delete.
    raw = json.loads(lib_path.read_text("utf-8"))
    assert set(raw["entries"].keys()) == {"AAA111", "BBB222"}

    lib.remove("BBB222")
    lib.save(path=lib_path)

    raw_after = json.loads(lib_path.read_text("utf-8"))
    assert list(raw_after["entries"].keys()) == ["AAA111"], (
        "remove() then save() must drop the entry from the JSON file "
        "— otherwise the deleted device reappears on next launch"
    )

    # And reloading from disk must match in-memory state.
    reloaded = DeviceLibrary.load(path=lib_path)
    assert reloaded.get("BBB222") is None
    assert reloaded.get("AAA111") is not None


def test_remove_then_reupsert_starts_fresh():
    """Re-adding a previously deleted phone must not inherit the
    old entry's label / patch state. The Dashboard relies on this
    so a customer who deletes "บัญชี A" and re-pairs the same
    physical phone gets a clean entry, not the stale nickname."""
    lib = DeviceLibrary()
    lib.upsert("AAA111", label="บัญชี A")
    lib.mark_patched("AAA111", tiktok_version="39.5.4")
    assert lib.get("AAA111").is_patched()

    lib.remove("AAA111")
    lib.upsert("AAA111", model="Redmi Note 12")

    fresh = lib.get("AAA111")
    assert fresh is not None
    assert fresh.label == "", "label must reset after remove()"
    assert fresh.is_patched() is False, (
        "re-added entry must not carry forward the old patch state"
    )
    assert fresh.model == "Redmi Note 12"
