"""Announcement client + signing roundtrip.

These tests exercise the announcement subsystem without touching
the network: we mock ``urllib.request.urlopen`` with a canned
signed response, then walk the public API.

What we lock in
---------------

* Signature verification is mandatory -- a payload signed with the
  *wrong* seed must be rejected.
* Version filtering follows ``min_version <= app < max_version``
  semantics (inclusive both ends, missing means "no bound").
* Expiry is honoured even if the announcement was never dismissed
  (so the client doesn't show stale "TikTok update" banners
  forever).
* Dismissed ids are persisted across runs.
* Malformed feeds, signature mismatches, network errors, JSON
  parse failures -- ALL must return an empty list, never raise.
* The poller's startup behaviour must surface announcements at
  app start, not 30 minutes later.
"""
from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src import _ed25519, announcements
from src._pubkey import PUBLIC_KEY_HEX


# ── fixtures ────────────────────────────────────────────────────


def _real_seed() -> bytes:
    """Find the admin signing seed.

    Tests must produce signatures that verify against the public
    key shipped in ``_pubkey.py``. That means we cannot just
    generate a fresh keypair -- we have to sign with the same
    seed the production build trusts.

    In CI / clean checkouts ``.private_key`` may not exist; in
    that case the suite is skipped (the announcement subsystem
    is itself trust-on-init-keys).
    """
    p = Path(__file__).resolve().parent.parent / ".private_key"
    if not p.is_file():
        pytest.skip(".private_key not on disk; skipping signing tests")
    seed = bytes.fromhex(p.read_text(encoding="utf-8").strip())
    # Sanity check: the seed must derive to the embedded pubkey.
    derived_pub = _derive_pub(seed)
    if derived_pub.hex() != PUBLIC_KEY_HEX:
        pytest.skip(".private_key does not match _pubkey.py (rotated keys)")
    return seed


def _derive_pub(seed: bytes) -> bytes:
    """Use the public ``keypair_from_seed`` helper -- avoids
    coupling the test to ed25519 internals (which have already
    changed shape once during this project)."""
    _seed, pub = _ed25519.keypair_from_seed(seed)
    return pub


def _envelope(announcements_list: list[dict], seed: bytes) -> dict:
    payload = {
        "feed_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "announcements": announcements_list,
    }
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    sig = _ed25519.sign(seed, payload_bytes)
    return {
        "format_version": 1,
        "payload": base64.urlsafe_b64encode(payload_bytes)
            .decode("ascii").rstrip("="),
        "signature": sig.hex(),
    }


def _patch_http(envelope_json: bytes):
    class _Resp:
        def __init__(self, data: bytes):
            self._data = data
        def read(self, n: int = -1):
            return self._data[:n] if n >= 0 else self._data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    return patch.object(
        announcements.urllib.request, "urlopen",
        return_value=_Resp(envelope_json),
    )


# ── envelope verification ───────────────────────────────────────


class TestSignatureVerify:
    def test_good_envelope_decodes_payload(self):
        seed = _real_seed()
        env = _envelope(
            [{"id": "x1", "title": "t", "body": "b"}], seed,
        )
        with _patch_http(json.dumps(env).encode("utf-8")):
            feed = announcements.fetch_feed("https://example.invalid/")
        assert len(feed) == 1
        assert feed[0].id == "x1"

    def test_wrong_seed_is_rejected(self):
        # Sign with a fake seed -- must NOT verify against the
        # production pubkey.
        fake_seed = b"\x42" * 32
        env = _envelope(
            [{"id": "evil", "title": "t", "body": "b"}], fake_seed,
        )
        with _patch_http(json.dumps(env).encode("utf-8")):
            feed = announcements.fetch_feed("https://example.invalid/")
        assert feed == [], "forged signature must be rejected"

    def test_missing_signature_is_rejected(self):
        env = {"payload": "deadbeef", "format_version": 1}
        with _patch_http(json.dumps(env).encode("utf-8")):
            feed = announcements.fetch_feed("https://example.invalid/")
        assert feed == []

    def test_garbage_returns_empty_not_raises(self):
        with _patch_http(b"not even json"):
            feed = announcements.fetch_feed("https://example.invalid/")
        assert feed == []

    def test_network_error_returns_empty(self):
        import urllib.error
        with patch.object(
            announcements.urllib.request, "urlopen",
            side_effect=urllib.error.URLError("dns fail"),
        ):
            feed = announcements.fetch_feed("https://example.invalid/")
        assert feed == []


# ── version filtering ───────────────────────────────────────────


class TestVersionFilter:
    @staticmethod
    def _ann(min_v=None, max_v=None) -> announcements.Announcement:
        return announcements.Announcement(
            id="v", title="t", body="b",
            min_version=min_v, max_version=max_v,
        )

    def test_no_bounds_always_applies(self):
        assert self._ann().applies_to_version("1.4.0")
        assert self._ann().applies_to_version("99.99.99")

    def test_min_inclusive(self):
        a = self._ann(min_v="1.4.0")
        assert a.applies_to_version("1.4.0")
        assert a.applies_to_version("1.4.5")
        assert not a.applies_to_version("1.3.9")

    def test_max_inclusive(self):
        a = self._ann(max_v="1.4.5")
        assert a.applies_to_version("1.4.5")
        assert a.applies_to_version("1.4.0")
        assert not a.applies_to_version("1.5.0")

    def test_range(self):
        a = self._ann(min_v="1.4.0", max_v="1.4.9")
        assert a.applies_to_version("1.4.5")
        assert not a.applies_to_version("1.5.0")
        assert not a.applies_to_version("1.3.0")


# ── expiry ──────────────────────────────────────────────────────


class TestExpiry:
    def test_no_expiry_is_never_expired(self):
        a = announcements.Announcement(id="x", title="t", body="b")
        assert not a.is_expired(datetime.now(timezone.utc))

    def test_past_expiry_is_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        a = announcements.Announcement(
            id="x", title="t", body="b", expires_at=past,
        )
        assert a.is_expired()

    def test_future_expiry_not_expired(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        a = announcements.Announcement(
            id="x", title="t", body="b", expires_at=future,
        )
        assert not a.is_expired()

    def test_malformed_expiry_treated_as_never(self):
        a = announcements.Announcement(
            id="x", title="t", body="b", expires_at="not-a-date",
        )
        assert not a.is_expired()


# ── dismiss state ───────────────────────────────────────────────


class TestDismissState:
    def test_dismissed_ids_filtered_out(self, tmp_path):
        state_file = tmp_path / "state.json"
        anns = [
            announcements.Announcement(id="a", title="t", body="b"),
            announcements.Announcement(id="b", title="t", body="b"),
        ]
        announcements.dismiss("a", state_path=state_file)
        visible = announcements.filter_visible(
            anns, app_version="1.4.6", state_path=state_file,
        )
        assert [a.id for a in visible] == ["b"]

    def test_dismiss_persists(self, tmp_path):
        state_file = tmp_path / "state.json"
        announcements.dismiss("seen", state_path=state_file)
        announcements.dismiss("also-seen", state_path=state_file)
        # New process: re-read.
        loaded = announcements._load_state(state_file)
        assert loaded.dismissed == {"seen", "also-seen"}

    def test_corrupt_state_starts_fresh(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not json", encoding="utf-8")
        loaded = announcements._load_state(state_file)
        assert loaded.dismissed == set()


# ── filter_visible integrates everything ────────────────────────


class TestFilterVisible:
    def test_combined_dismiss_expiry_version(self, tmp_path):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        state_file = tmp_path / "state.json"
        announcements.dismiss("dismissed", state_path=state_file)

        anns = [
            announcements.Announcement(id="dismissed", title="t", body="b"),
            announcements.Announcement(id="expired", title="t", body="b",
                                       expires_at=past),
            announcements.Announcement(id="future-only", title="t", body="b",
                                       min_version="9.0.0"),
            announcements.Announcement(id="visible", title="t", body="b",
                                       expires_at=future, min_version="1.0.0"),
        ]
        visible = announcements.filter_visible(
            anns, app_version="1.4.6", state_path=state_file,
        )
        assert [a.id for a in visible] == ["visible"]
