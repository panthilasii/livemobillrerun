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

from src import _ed25519 as ed
from src.customer_devices import DeviceEntry, DeviceLibrary
from src.license_key import (
    LicenseError,
    generate_key,
    verify_key,
)


# ── license_key (Ed25519) ────────────────────────────────────────


class TestLicenseKey:
    # Deterministic keypair so test runs are stable.
    SEED = bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc4"
        "4449c5697b326919703bac031cae7f60"
    )

    @classmethod
    def setup_class(cls):
        cls.PRIV, cls.PUB = ed.keypair_from_seed(cls.SEED)
        cls.WRONG_PUB = ed.keypair_from_seed(b"x" * 32)[1]

    def _make(self, *args, **kw):
        kw.setdefault("private_seed", self.PRIV)
        return generate_key(*args, **kw)

    def _verify(self, key):
        return verify_key(key, public_key=self.PUB)

    def test_round_trip(self):
        key = self._make("Alice", max_devices=3, days=30)
        v = self._verify(key)
        assert v.customer == "Alice"
        assert v.max_devices == 3
        assert v.days_left in (29, 30)
        assert not v.is_expired

    def test_format_starts_with_888(self):
        key = self._make("X")
        assert key.startswith("888-")

    def test_tampered_key_rejected(self):
        """Flip a meaningful char inside the signature region. (Base32
        is case-insensitive *and* the last 1-2 chars only encode
        padding bits, so we pick a char near the middle of the string
        that's guaranteed to carry real signature data.)"""
        key = self._make("Bob")
        # Find a hyphenated group somewhere in the back half (signature
        # bytes live there) and bump its first char by one.
        chars = list(key)
        mid = len(chars) // 2 + 8  # well past the payload, into the sig
        # base32 alphabet is A-Z, 2-7. Pick something different.
        original = chars[mid].upper()
        chars[mid] = "Q" if original != "Q" else "M"
        bad = "".join(chars)
        with pytest.raises(LicenseError):
            self._verify(bad)

    def test_garbage_rejected(self):
        with pytest.raises(LicenseError):
            self._verify("not a real key")

    def test_wrong_pubkey_rejected(self):
        """Customer can't replace the bundled pubkey to forge new keys."""
        key = self._make("Carol")
        with pytest.raises(LicenseError):
            verify_key(key, public_key=self.WRONG_PUB)

    def test_explicit_expiry(self):
        future = date.today() + timedelta(days=100)
        key = self._make("Dave", max_devices=1, expiry=future)
        v = self._verify(key)
        assert v.expiry == future

    def test_expired_key_returns_but_flagged(self):
        past = date.today() - timedelta(days=2)
        key = self._make("Eve", max_devices=1, expiry=past)
        v = self._verify(key)
        assert v.is_expired
        assert v.days_left < 0

    def test_devices_clamped_to_one_minimum(self):
        key = self._make("Frank", max_devices=0)
        v = self._verify(key)
        assert v.max_devices == 1

    def test_default_tier_matches_brand(self):
        from src.branding import BRAND

        key = self._make("Default")
        v = self._verify(key)
        assert v.max_devices == BRAND.default_devices_per_key
        assert v.max_devices == 3

    def test_thai_customer_name(self):
        key = self._make("คุณสมชาย", max_devices=2)
        v = self._verify(key)
        assert v.customer == "คุณสมชาย"
        assert v.max_devices == 2

    def test_pipe_in_customer_rejected(self):
        with pytest.raises(ValueError):
            self._make("a|b")


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
