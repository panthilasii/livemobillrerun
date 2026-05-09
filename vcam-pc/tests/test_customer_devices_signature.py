"""Per-device patched-signature baseline + auto-heal reconciliation.

These cover the bits added in 1.7.5 to make patched/unpatched
detection survive Android-version differences and devices.json
loss / cross-machine patching scenarios.
"""
from __future__ import annotations

import json

from src.customer_devices import DeviceLibrary


# ── mark_patched(signature=...) ───────────────────────────────────


def test_mark_patched_records_signature(tmp_path):
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched(
        "AB123",
        tiktok_version="39.5.4",
        signature="E0B8D3E5DEADBEEF",
    )
    e = lib.get("AB123")
    assert e is not None
    assert e.patched_signature == "e0b8d3e5deadbeef", (
        "signature should be normalised to lowercase"
    )


def test_mark_patched_legacy_no_signature_arg(tmp_path):
    """Older code paths still call without the signature kwarg —
    must keep working with empty string fallback."""
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched("AB123", tiktok_version="39.5.4")
    e = lib.get("AB123")
    assert e is not None
    assert e.patched_signature == ""


def test_signature_round_trip(tmp_path):
    path = tmp_path / "devices.json"
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched("AB123", signature="abcd1234")
    lib.save(path)

    reloaded = DeviceLibrary.load(path)
    assert reloaded.get("AB123").patched_signature == "abcd1234"


def test_legacy_json_loads_with_empty_signature(tmp_path):
    """Pre-1.7.5 devices.json files don't have the field — they
    must load clean with an empty default."""
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
    assert e.patched_signature == ""


# ── reconcile_observed_patched (auto-heal) ────────────────────────


def test_auto_heal_sets_patched_at_when_blank(tmp_path):
    """The phone IS patched (probe just confirmed it) but our
    entry has no patched_at. Reconcile must set the timestamp so
    the rest of the UI lights up correctly."""
    lib = DeviceLibrary()
    lib.upsert("AB123")
    e = lib.get("AB123")
    assert not e.patched_at  # precondition

    healed = lib.reconcile_observed_patched(
        "AB123",
        signature="e0b8d3e5deadbeef",
        tiktok_version="39.5.4",
    )

    assert healed is True
    e = lib.get("AB123")
    assert e.patched_at, "patched_at should now be set"
    assert e.patched_signature == "e0b8d3e5deadbeef"
    assert e.patched_tiktok_version == "39.5.4"


def test_auto_heal_no_op_when_already_patched(tmp_path):
    """If patched_at is already set, reconcile is a no-op — we
    don't overwrite a richer baseline with thinner observed data."""
    lib = DeviceLibrary()
    lib.upsert("AB123")
    lib.mark_patched(
        "AB123",
        tiktok_version="39.5.4",
        signature="e0b8d3e5original",
    )
    original_at = lib.get("AB123").patched_at

    healed = lib.reconcile_observed_patched(
        "AB123",
        signature="e0b8d3e5different",
        tiktok_version="39.6.0",
    )

    assert healed is False
    e = lib.get("AB123")
    assert e.patched_at == original_at, "must not overwrite"
    assert e.patched_signature == "e0b8d3e5original"


def test_auto_heal_records_only_blank_fields(tmp_path):
    """If patched_at is blank but signature/version were somehow
    set (unusual; manual file edit), reconcile sets timestamp but
    doesn't clobber the existing values."""
    lib = DeviceLibrary()
    lib.upsert("AB123")
    e = lib.get("AB123")
    e.patched_signature = "manually_set"
    e.patched_tiktok_version = "manual_ver"

    healed = lib.reconcile_observed_patched(
        "AB123",
        signature="observed_sig",
        tiktok_version="observed_ver",
    )

    assert healed is True
    e = lib.get("AB123")
    assert e.patched_at, "timestamp set"
    assert e.patched_signature == "manually_set", "kept original"
    assert e.patched_tiktok_version == "manual_ver", "kept original"


def test_auto_heal_unknown_serial_is_noop(tmp_path):
    """Race: probe completed for a phone the user just removed.
    Must not crash, must not auto-create the entry."""
    lib = DeviceLibrary()
    healed = lib.reconcile_observed_patched(
        "GHOST", signature="abc", tiktok_version="1.0",
    )
    assert healed is False
    assert lib.get("GHOST") is None


def test_auto_heal_with_no_observation_data(tmp_path):
    """Probe succeeded but couldn't extract version / fingerprint.
    Reconcile should still flip patched_at so the UI unblocks,
    even with empty arguments."""
    lib = DeviceLibrary()
    lib.upsert("AB123")
    healed = lib.reconcile_observed_patched("AB123")
    assert healed is True
    e = lib.get("AB123")
    assert e.patched_at
    assert e.patched_signature == ""
    assert e.patched_tiktok_version == ""
