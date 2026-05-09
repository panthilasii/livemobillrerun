"""Support upload + payments ledger tests."""
from __future__ import annotations

import io


def _customer(admin_client, name="ลูกค้า"):
    return admin_client.post(
        "/api/admin/customers", json={"name": name},
    ).json()


def test_payment_create_and_list(admin_client):
    c = _customer(admin_client)
    r = admin_client.post(
        "/api/admin/payments",
        json={
            "customer_id": c["id"],
            "amount_baht": 990.0,
            "method": "promptpay",
            "reference": "Slip #1234",
        },
    )
    assert r.status_code == 200, r.text
    pay = r.json()
    assert pay["amount_satang"] == 99000
    assert pay["amount_baht"] == 990.0

    r2 = admin_client.get("/api/admin/payments")
    assert r2.status_code == 200
    assert any(p["id"] == pay["id"] for p in r2.json())


def test_payment_invalid_amount_rejected(admin_client):
    c = _customer(admin_client)
    r = admin_client.post(
        "/api/admin/payments",
        json={"customer_id": c["id"], "amount_baht": 0},
    )
    assert r.status_code == 400


def test_promptpay_qr_payload_structure(admin_client):
    """The PromptPay payload should start with the EMV format
    indicator '000201' and end with a 4-char CRC."""
    r = admin_client.get(
        "/api/admin/payments/promptpay-qr"
        "?target=0812345678&amount_baht=990.00",
    )
    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload.startswith("000201")
    # Last 4 = CRC hex.
    assert all(c in "0123456789ABCDEFabcdef" for c in payload[-4:])
    # Currency tag 5303764 must be present (THB).
    assert "5303764" in payload


def test_support_upload_creates_ticket(admin_client):
    c = _customer(admin_client)
    r = admin_client.post(
        f"/api/admin/customers/{c['id']}/licenses", json={"days": 30},
    )
    lic = r.json()

    fake_zip = b"PK\x03\x04" + b"\x00" * 200  # ≥ 100 bytes
    r2 = admin_client.post(
        "/api/v1/support/upload",
        data={"key": lic["key"], "message": "ขอความช่วยเหลือ"},
        files={"log_zip": ("log.zip", io.BytesIO(fake_zip), "application/zip")},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"]
    ticket_id = r2.json()["ticket_id"]

    # Admin sees it.
    r3 = admin_client.get("/api/admin/support")
    assert r3.status_code == 200
    rows = r3.json()
    assert any(t["id"] == ticket_id for t in rows)


def test_support_upload_too_small_rejected(admin_client):
    fake = io.BytesIO(b"tiny")
    r = admin_client.post(
        "/api/v1/support/upload",
        data={"key": ""},
        files={"log_zip": ("log.zip", fake, "application/zip")},
    )
    assert r.status_code == 400


def test_support_upload_anonymous_when_no_key(admin_client):
    """Customer without a license key can still send a log; we
    record it with empty customer_name so the admin can triage."""
    fake = io.BytesIO(b"PK" + b"\x00" * 200)
    r = admin_client.post(
        "/api/v1/support/upload",
        data={"key": "", "message": "anonymous"},
        files={"log_zip": ("log.zip", fake, "application/zip")},
    )
    assert r.status_code == 200
    tid = r.json()["ticket_id"]
    rows = admin_client.get("/api/admin/support").json()
    row = next(t for t in rows if t["id"] == tid)
    assert row["customer_name"] == ""
    assert row["license_id"] is None
