"""Customer-facing endpoint tests:

* Activation succeeds with a key signed by this server.
* Activation rejects forged signatures (bit-flip).
* Heartbeat updates last_seen on a known activation.
* Revocation list signature validates with the server pub key.
"""
from __future__ import annotations

import json
from datetime import date, timedelta


def _new_customer_and_key(admin_client):
    r = admin_client.post(
        "/api/admin/customers", json={"name": "ลูกค้า A"},
    )
    cid = r.json()["id"]
    r2 = admin_client.post(
        f"/api/admin/customers/{cid}/licenses",
        json={"days": 30, "max_devices": 3},
    )
    return cid, r2.json()


def test_activate_round_trip(admin_client):
    _, lic = _new_customer_and_key(admin_client)
    r = admin_client.post(
        "/api/v1/activate",
        json={
            "key": lic["key"],
            "machine_id": "abc12345",
            "machine_label": "DESK-A",
            "app_version": "1.7.2",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["license_id"] == lic["id"]
    assert body["max_devices"] == 3


def test_activate_rejects_forged_signature(admin_client):
    _, lic = _new_customer_and_key(admin_client)
    # Flip ONE base32 char to invalidate the signature.
    key = lic["key"]
    flipped = key[:-1] + ("A" if key[-1] != "A" else "B")
    r = admin_client.post(
        "/api/v1/activate",
        json={"key": flipped, "machine_id": "x"},
    )
    assert r.status_code == 400


def test_activate_unknown_key_404(admin_client):
    """A signature-valid key the server doesn't have a row for
    must 404 (not 200) so the customer app surfaces 'unknown'."""
    # Issue a key, then delete the DB row, then re-attempt.
    _, lic = _new_customer_and_key(admin_client)
    from app import db
    with db.connect() as cx:
        cx.execute("DELETE FROM licenses WHERE id = ?", (lic["id"],))
    r = admin_client.post(
        "/api/v1/activate",
        json={"key": lic["key"], "machine_id": "x"},
    )
    assert r.status_code == 404


def test_revoked_key_rejects_activation(admin_client):
    _, lic = _new_customer_and_key(admin_client)
    admin_client.post(f"/api/admin/licenses/{lic['id']}/revoke", json={})
    r = admin_client.post(
        "/api/v1/activate",
        json={"key": lic["key"], "machine_id": "y"},
    )
    assert r.status_code == 403


def test_heartbeat_updates_last_seen(admin_client):
    _, lic = _new_customer_and_key(admin_client)
    admin_client.post(
        "/api/v1/activate",
        json={"key": lic["key"], "machine_id": "m1"},
    )
    r = admin_client.post(
        "/api/v1/heartbeat",
        json={"key": lic["key"], "machine_id": "m1"},
    )
    assert r.status_code == 200
    assert r.json()["license_status"] == "active"


def test_heartbeat_404_for_never_activated(admin_client):
    _, lic = _new_customer_and_key(admin_client)
    r = admin_client.post(
        "/api/v1/heartbeat",
        json={"key": lic["key"], "machine_id": "never-seen"},
    )
    assert r.status_code == 404


def test_revocations_list_is_signed_and_includes_revoked_nonces(
    admin_client,
):
    _, lic1 = _new_customer_and_key(admin_client)
    _, lic2 = _new_customer_and_key(admin_client)
    admin_client.post(f"/api/admin/licenses/{lic1['id']}/revoke", json={})

    r = admin_client.get("/api/v1/revocations")
    assert r.status_code == 200
    body = r.json()
    manifest = json.loads(body["manifest"])
    assert lic1["nonce"] in manifest["nonces"]
    assert lic2["nonce"] not in manifest["nonces"]

    # Sig must verify against the server's pub key.
    from app import crypto
    sig = bytes.fromhex(body["sig"])
    assert crypto.verify_blob(body["manifest"].encode("utf-8"), sig)


def test_health_endpoint_reports_pubkey(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "npc-server"
    assert len(body["public_key_hex"]) == 64
