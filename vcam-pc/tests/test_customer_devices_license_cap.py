"""``DeviceLibrary.try_admit_new`` — license-cap gate (v1.8.20).

These tests pin down the contract for the cap-enforced new-device
admission path. The bug we're guarding against:

    Pre-v1.8.20 the auto-discovery loop in
    ``studio_app._on_devices_polled`` called ``upsert()`` for
    every USB row, which silently bypassed the license cap.
    Customers paid for "3 seats" but ended up running 6-8 phones
    from one PC because the sidebar's "+ เพิ่มเครื่อง" button
    was the *only* place the cap was checked.

The new ``try_admit_new`` method must:

* Refuse new serials when ``len(entries) >= max_devices`` (return
  None, don't mutate state).
* Always allow updates to existing entries (label edit / model
  refresh) — those are paid seats, regardless of cap.
* Be a strict superset of ``upsert`` for the existing-entry case
  (same return value, same side-effects on the entry's model /
  label fields).
"""
from __future__ import annotations

from src.customer_devices import DeviceLibrary


def test_try_admit_new_under_cap_admits_serial():
    lib = DeviceLibrary()

    admitted = lib.try_admit_new(
        "SERIAL_A", model="Redmi 13C", label="ช่อง 1", max_devices=3,
    )

    assert admitted is not None
    assert admitted.serial == "SERIAL_A"
    assert admitted.model == "Redmi 13C"
    assert admitted.label == "ช่อง 1"
    assert lib.count() == 1


def test_try_admit_new_refuses_when_cap_reached():
    """Once the library hits cap, new serials are silently refused.

    Returning ``None`` (rather than raising) keeps the auto-poll
    caller simple — the poller doesn't have to guard every call
    with try/except, it just checks the return value.
    """
    lib = DeviceLibrary()
    lib.try_admit_new("SEAT_1", max_devices=3)
    lib.try_admit_new("SEAT_2", max_devices=3)
    lib.try_admit_new("SEAT_3", max_devices=3)
    assert lib.count() == 3

    refused = lib.try_admit_new("SEAT_4", model="OPPO", max_devices=3)

    assert refused is None
    assert lib.count() == 3, "library must NOT grow past cap"
    assert lib.get("SEAT_4") is None, (
        "refused serial must not leak into entries"
    )


def test_try_admit_new_allows_updates_to_existing_seats_past_cap():
    """A paid seat can keep editing its label/model even when the
    library is at cap. The cap is for *new* admissions only —
    refusing label edits on existing devices would break the
    Dashboard's rename-on-double-click flow with no upside."""
    lib = DeviceLibrary()
    lib.try_admit_new("SEAT_1", label="old label", max_devices=2)
    lib.try_admit_new("SEAT_2", max_devices=2)
    assert lib.count() == 2

    # Library is now at cap, but SEAT_1 already exists.
    updated = lib.try_admit_new(
        "SEAT_1", label="new label", model="updated model", max_devices=2,
    )

    assert updated is not None
    assert updated.label == "new label"
    assert updated.model == "updated model"
    assert lib.count() == 2


def test_try_admit_new_cap_of_one_admits_first_only():
    """Defensive: customers on a 1-seat license still need to be
    able to add their *first* device (no off-by-one)."""
    lib = DeviceLibrary()

    first = lib.try_admit_new("ONLY_ONE", max_devices=1)
    second = lib.try_admit_new("BLOCKED", max_devices=1)

    assert first is not None
    assert second is None
    assert lib.count() == 1


def test_try_admit_new_cap_zero_is_clamped_to_one():
    """A corrupt license that hands us ``max_devices=0`` should not
    completely lock the customer out — the library clamps the
    floor to 1 so at least their daily-driver phone stays
    operable while support resolves the bad key."""
    lib = DeviceLibrary()

    admitted = lib.try_admit_new("ANY", max_devices=0)

    assert admitted is not None, "cap=0 must clamp to cap=1"
    assert lib.count() == 1

    refused = lib.try_admit_new("SECOND", max_devices=0)
    assert refused is None


def test_try_admit_new_does_not_mutate_on_refusal():
    """If admission is refused, the library state — including the
    other entries' model/label — must be identical to before the
    call. Auto-poll spams this method every 2 s; a partial
    mutation would slowly corrupt the customer's labels."""
    lib = DeviceLibrary()
    lib.try_admit_new("SEAT_1", model="orig model", label="orig label", max_devices=1)

    snapshot = (
        lib.get("SEAT_1").model,
        lib.get("SEAT_1").label,
        lib.count(),
    )

    refused = lib.try_admit_new(
        "OVERFLOW", model="NOPE", label="should not appear",
        max_devices=1,
    )

    assert refused is None
    assert lib.get("SEAT_1").model == snapshot[0]
    assert lib.get("SEAT_1").label == snapshot[1]
    assert lib.count() == snapshot[2]


def test_try_admit_new_sets_added_at():
    """Admitted entries get a fresh ``added_at`` timestamp so the
    Settings page's "ออนไลน์ครั้งล่าสุด" column can sort by it.

    We don't pin the exact value (it's wall-clock dependent);
    just assert it's a non-empty ISO-ish string."""
    lib = DeviceLibrary()

    admitted = lib.try_admit_new("FRESH", max_devices=5)

    assert admitted is not None
    assert isinstance(admitted.added_at, str)
    assert len(admitted.added_at) >= 10, (
        f"added_at should be ISO-ish, got {admitted.added_at!r}"
    )


def test_can_add_more_and_try_admit_new_agree():
    """Both APIs read off the same internal counter; they must never
    disagree about whether a new device fits. The Dashboard
    sidebar uses ``can_add_more`` for button gating; auto-poll
    uses ``try_admit_new``. A disagreement would mean a customer
    sees an enabled "+ เพิ่ม" button that then silently refuses
    — bad UX."""
    lib = DeviceLibrary()

    for i, serial in enumerate(("A", "B", "C")):
        before = lib.can_add_more(3)
        admitted = lib.try_admit_new(serial, max_devices=3)
        if before:
            assert admitted is not None, (
                f"can_add_more said OK but try_admit_new refused on iter {i}"
            )
        else:
            assert admitted is None, (
                f"can_add_more said FULL but try_admit_new accepted on iter {i}"
            )

    # Library should be exactly at cap now.
    assert lib.can_add_more(3) is False
    assert lib.try_admit_new("D", max_devices=3) is None
