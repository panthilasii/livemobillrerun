"""Tests for the optional central-server client.

The module is **fail-open**, so most tests just confirm that
errors don't propagate and the no-op path returns ``None`` /
``False`` cleanly. We mock urlopen rather than running a real
local server because the server lives in a separate package
(``vcam-server/``) and pulling in FastAPI here would balloon the
customer-side test runtime.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from src import license_server


# ── disabled mode (no URL configured) ──────────────────────────────


def test_is_enabled_default_false():
    assert license_server.is_enabled() is False


def test_activate_noop_when_disabled():
    r = license_server.activate("888-AAAA-BBBB")
    assert r.ok is False
    assert r.error == "server_url_not_configured"


def test_heartbeat_noop_when_disabled():
    assert license_server.heartbeat("888-AAAA-BBBB") is None


def test_fetch_revocations_noop_when_disabled():
    assert license_server.fetch_revocations() is None


def test_upload_support_log_noop_when_disabled(tmp_path):
    z = tmp_path / "log.zip"
    z.write_bytes(b"PK" + b"\x00" * 200)
    assert license_server.upload_support_log(str(z)) is None


# ── enabled mode (URL set, mocked HTTP) ────────────────────────────


@pytest.fixture
def enabled(monkeypatch):
    """Patch BRAND.license_server_url for the duration of the test."""
    from src.branding import Brand
    from dataclasses import replace
    from src import branding, license_server as ls

    fake_brand = replace(branding.BRAND, license_server_url="https://test.invalid")
    monkeypatch.setattr(branding, "BRAND", fake_brand)
    monkeypatch.setattr(ls, "BRAND", fake_brand)
    yield


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_activate_success(enabled):
    fake = json.dumps({
        "ok": True, "license_id": 7, "customer": "X",
        "max_devices": 3, "expiry": "2026-12-31", "status": "active",
    }).encode()
    with patch.object(
        license_server.urllib.request, "urlopen",
        return_value=_FakeResp(fake),
    ):
        r = license_server.activate("888-AAAA-BBBB")
    assert r.ok is True
    assert r.license_id == 7
    assert r.customer == "X"


def test_activate_swallows_network_error(enabled):
    """A network error must NOT raise — fail-open guarantee."""
    with patch.object(
        license_server.urllib.request, "urlopen",
        side_effect=urllib.error.URLError("offline"),
    ):
        r = license_server.activate("888-AAAA-BBBB")
    assert r.ok is False
    assert r.error == "network"


def test_activate_swallows_http_error(enabled):
    err = urllib.error.HTTPError(
        "https://test.invalid/api/v1/activate", 503, "Service Down",
        {}, io.BytesIO(b""),
    )
    with patch.object(
        license_server.urllib.request, "urlopen", side_effect=err,
    ):
        r = license_server.activate("888-AAAA-BBBB")
    assert r.ok is False
    assert r.error == "network"


def test_fetch_revocations_rejects_unsigned_manifest(enabled):
    """Even when enabled, a missing/invalid signature must reject
    the list (could be an attacker forging a lock-out)."""
    fake = json.dumps({
        "manifest": json.dumps({"kind": "npc.revocations.v1", "nonces": ["bad"]}),
        "sig": "00" * 64,        # not a valid signature
    }).encode()
    with patch.object(
        license_server.urllib.request, "urlopen",
        return_value=_FakeResp(fake),
    ):
        result = license_server.fetch_revocations()
    # Without a real public key + matching sig we can't accept this;
    # depending on whether _pubkey is on disk we get None either
    # way. Either is correct (fail-closed for the security check).
    assert result is None
