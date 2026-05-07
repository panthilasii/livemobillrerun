"""Unit tests for license_key + customer_devices modules.

Run with::

    pytest tests/test_studio_core.py
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.customer_devices import DeviceEntry, DeviceLibrary
from src.license_key import (
    LicenseError,
    generate_key,
    verify_key,
)


# ── license_key ─────────────────────────────────────────────────────


class TestLicenseKey:
    SECRET = b"unit-test-secret-do-not-ship"

    def test_round_trip(self):
        key = generate_key(
            "Alice", max_devices=3, days=30, secret=self.SECRET,
        )
        v = verify_key(key, secret=self.SECRET)
        assert v.customer == "Alice"
        assert v.max_devices == 3
        assert v.days_left in (29, 30)  # tolerate clock-rounding
        assert not v.is_expired

    def test_format_starts_with_888(self):
        key = generate_key("X", secret=self.SECRET)
        assert key.startswith("888-")

    def test_tampered_key_rejected(self):
        key = generate_key("Bob", secret=self.SECRET)
        # Flip the last char.
        bad = key[:-1] + ("A" if key[-1] != "A" else "B")
        with pytest.raises(LicenseError):
            verify_key(bad, secret=self.SECRET)

    def test_garbage_rejected(self):
        with pytest.raises(LicenseError):
            verify_key("not a real key", secret=self.SECRET)

    def test_wrong_secret_rejected(self):
        key = generate_key("Carol", secret=self.SECRET)
        with pytest.raises(LicenseError):
            verify_key(key, secret=b"different-secret")

    def test_explicit_expiry(self):
        future = date.today() + timedelta(days=100)
        key = generate_key(
            "Dave", max_devices=1, expiry=future, secret=self.SECRET,
        )
        v = verify_key(key, secret=self.SECRET)
        assert v.expiry == future

    def test_expired_key_returns_but_flagged(self):
        past = date.today() - timedelta(days=2)
        key = generate_key(
            "Eve", max_devices=1, expiry=past, secret=self.SECRET,
        )
        v = verify_key(key, secret=self.SECRET)
        assert v.is_expired
        assert v.days_left < 0

    def test_devices_clamped_to_one_minimum(self):
        key = generate_key("Frank", max_devices=0, secret=self.SECRET)
        v = verify_key(key, secret=self.SECRET)
        assert v.max_devices == 1

    def test_default_tier_matches_brand(self):
        """1 license = 3 phones is the default selling tier (BRAND)."""
        from src.branding import BRAND

        key = generate_key("Default", secret=self.SECRET)
        v = verify_key(key, secret=self.SECRET)
        assert v.max_devices == BRAND.default_devices_per_key
        assert v.max_devices == 3  # explicit tripwire

    def test_thai_customer_name(self):
        key = generate_key("คุณสมชาย", max_devices=2, secret=self.SECRET)
        v = verify_key(key, secret=self.SECRET)
        assert v.customer == "คุณสมชาย"
        assert v.max_devices == 2

    def test_pipe_in_customer_rejected(self):
        with pytest.raises(ValueError):
            generate_key("a|b", secret=self.SECRET)


# ── customer_devices ────────────────────────────────────────────────


class TestDeviceLibrary:
    def test_upsert_and_get(self):
        lib = DeviceLibrary()
        e = lib.upsert("S1", model="Redmi 14C", label="A")
        assert e.serial == "S1"
        assert e.model == "Redmi 14C"
        assert e.label == "A"
        assert e.added_at  # set on first insert
        assert lib.count() == 1
        assert lib.get("S1") is e

    def test_upsert_idempotent(self):
        lib = DeviceLibrary()
        a1 = lib.upsert("S1", model="Redmi 14C")
        a2 = lib.upsert("S1", label="A")
        assert a1 is a2  # same entry, mutated in place
        assert a2.label == "A"
        assert a2.model == "Redmi 14C"  # preserved

    def test_remove(self):
        lib = DeviceLibrary()
        lib.upsert("S1")
        assert lib.remove("S1") is True
        assert lib.count() == 0
        assert lib.remove("S1") is False  # already gone

    def test_update_video(self):
        lib = DeviceLibrary()
        lib.upsert("S1")
        lib.update_video("S1", "/tmp/clip.mp4")
        assert lib.get("S1").last_video == "/tmp/clip.mp4"

    def test_update_transform(self):
        lib = DeviceLibrary()
        lib.upsert("S1")
        lib.update_transform(
            "S1", rotation=270, mirror_h=True, mirror_v=False,
        )
        e = lib.get("S1")
        assert e.rotation == 270
        assert e.mirror_h is True
        assert e.mirror_v is False

    def test_rotation_normalised(self):
        lib = DeviceLibrary()
        lib.upsert("S1")
        lib.update_transform("S1", rotation=450)
        assert lib.get("S1").rotation == 90  # 450 % 360

    def test_mark_patched(self):
        lib = DeviceLibrary()
        lib.upsert("S1")
        assert not lib.get("S1").is_patched()
        lib.mark_patched("S1")
        assert lib.get("S1").is_patched()

    def test_can_add_more(self):
        lib = DeviceLibrary()
        lib.upsert("S1")
        lib.upsert("S2")
        assert lib.can_add_more(3) is True
        assert lib.can_add_more(2) is False  # at cap
        assert lib.can_add_more(1) is False

    def test_save_load_round_trip(self, tmp_path):
        path = tmp_path / "devices.json"
        lib = DeviceLibrary()
        lib.upsert("S1", model="Redmi", label="A")
        lib.update_transform("S1", rotation=180)
        lib.update_video("S1", "/abs/clip.mp4")
        lib.save(path)

        # The JSON should be human-readable and round-trip cleanly.
        data = json.loads(path.read_text())
        assert "entries" in data
        assert "S1" in data["entries"]
        assert data["entries"]["S1"]["rotation"] == 180

        lib2 = DeviceLibrary.load(path)
        assert lib2.count() == 1
        e = lib2.get("S1")
        assert e.model == "Redmi"
        assert e.label == "A"
        assert e.rotation == 180
        assert e.last_video == "/abs/clip.mp4"

    def test_load_missing_file_returns_empty(self, tmp_path):
        lib = DeviceLibrary.load(tmp_path / "nope.json")
        assert lib.count() == 0

    def test_display_name_fallback(self):
        e = DeviceEntry(serial="VERYLONGSERIAL")
        assert e.display_name() == "VERYLONGSERIAL"
        e.model = "Redmi"
        assert e.display_name() == "Redmi"
        e.label = "บัญชี A"
        assert e.display_name() == "บัญชี A"
