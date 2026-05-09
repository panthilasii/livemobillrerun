"""Crypto layer tests.

Pin the *byte format* of license keys: any change here breaks
verification on every shipped customer build.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest


def test_init_new_keypair_writes_both_files(client):
    from app import crypto
    from app.config import SETTINGS
    assert SETTINGS.signing_key_path.is_file()
    assert SETTINGS.public_key_path.is_file()
    seed_hex = SETTINGS.signing_key_path.read_text().strip()
    pub_hex = SETTINGS.public_key_path.read_text().strip()
    assert len(seed_hex) == 64
    assert len(pub_hex) == 64
    # Public key derived from seed must match what's on disk.
    from app import _ed25519 as ed
    derived_pub = ed.keypair_from_seed(bytes.fromhex(seed_hex))[1]
    assert derived_pub.hex() == pub_hex


def test_init_keypair_refuses_overwrite(client):
    from app import crypto
    with pytest.raises(crypto.CryptoError):
        crypto.init_new_keypair(force=False)


def test_issue_key_round_trip(client):
    """A key signed by the server must verify against the same
    server's public key, and decode back into the original payload.
    This is THE invariant — break it and every customer's
    activation rejects."""
    from app import crypto
    expiry = date.today() + timedelta(days=30)
    key, payload = crypto.issue_key("คุณสมชาย", 3, expiry)

    # Format: '888-XXXX-XXXX-...' — prefix + base32-grouped body.
    assert key.startswith("888-")
    assert payload.customer == "คุณสมชาย"
    assert payload.max_devices == 3
    assert payload.expiry == expiry

    # Verify via the same path the public-facing endpoint uses.
    from app.routes.public_activate import _verify_key_payload
    parsed = _verify_key_payload(key)
    assert parsed["customer"] == "คุณสมชาย"
    assert parsed["max_devices"] == 3
    assert parsed["expiry"] == expiry.isoformat()
    assert parsed["nonce"] == payload.nonce


def test_issue_key_rejects_pipe_in_customer_name(client):
    from app import crypto
    with pytest.raises(crypto.CryptoError):
        crypto.issue_key(
            "name|with|pipes", 3, date.today() + timedelta(days=30),
        )


def test_issue_key_rejects_invalid_device_count(client):
    from app import crypto
    expiry = date.today() + timedelta(days=30)
    with pytest.raises(crypto.CryptoError):
        crypto.issue_key("ok", 0, expiry)
    with pytest.raises(crypto.CryptoError):
        crypto.issue_key("ok", 101, expiry)


def test_sign_and_verify_blob_round_trip(client):
    from app import crypto
    blob = b"hello-revocations"
    sig = crypto.sign_blob(blob)
    assert len(sig) == 64
    assert crypto.verify_blob(blob, sig) is True
    # Tamper detection.
    assert crypto.verify_blob(blob + b"x", sig) is False
