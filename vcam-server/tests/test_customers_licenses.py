"""End-to-end tests for the admin customer + license flow."""
from __future__ import annotations

from datetime import date, timedelta


def _create_customer(client, name="ลูกค้าทดสอบ", **kw):
    body = {"name": name, **kw}
    r = client.post("/api/admin/customers", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_and_list_customer(admin_client):
    c = _create_customer(admin_client, name="คุณเอ", line_id="@npa")
    assert c["id"] > 0
    assert c["name"] == "คุณเอ"
    assert c["line_id"] == "@npa"

    r = admin_client.get("/api/admin/customers")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "คุณเอ"


def test_search_customer(admin_client):
    _create_customer(admin_client, name="Alpha", line_id="@al")
    _create_customer(admin_client, name="Beta",  line_id="@bt")

    r = admin_client.get("/api/admin/customers?q=lpha")
    assert r.status_code == 200
    rows = r.json()
    assert {row["name"] for row in rows} == {"Alpha"}


def test_issue_license_for_customer(admin_client):
    c = _create_customer(admin_client)
    r = admin_client.post(
        f"/api/admin/customers/{c['id']}/licenses",
        json={"days": 60, "max_devices": 5, "note": "first key"},
    )
    assert r.status_code == 200, r.text
    lic = r.json()
    assert lic["customer_id"] == c["id"]
    assert lic["max_devices"] == 5
    assert lic["status"] == "active"
    assert lic["key"].startswith("888-")
    # Expiry should be roughly 60 days from today.
    expected = date.today() + timedelta(days=60)
    assert lic["expiry"] == expected.isoformat()


def test_revoke_license(admin_client):
    c = _create_customer(admin_client)
    r = admin_client.post(
        f"/api/admin/customers/{c['id']}/licenses",
        json={"days": 7},
    )
    lic = r.json()

    r2 = admin_client.post(
        f"/api/admin/licenses/{lic['id']}/revoke",
        json={"reason": "test revoke"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "revoked"

    # Idempotent — revoking again is a no-op success.
    r3 = admin_client.post(
        f"/api/admin/licenses/{lic['id']}/revoke", json={},
    )
    assert r3.status_code == 200


def test_extend_license_issues_new_key(admin_client):
    c = _create_customer(admin_client)
    r = admin_client.post(
        f"/api/admin/customers/{c['id']}/licenses",
        json={"days": 7},
    )
    old = r.json()

    r2 = admin_client.post(
        f"/api/admin/licenses/{old['id']}/extend",
        json={"days": 30},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["old_id"] == old["id"]
    assert body["new"]["status"] == "active"
    assert body["new"]["key"] != old["key"]

    # Old key should now be revoked.
    list_r = admin_client.get("/api/admin/licenses")
    by_id = {row["id"]: row for row in list_r.json()}
    assert by_id[old["id"]]["status"] == "revoked"
    assert by_id[body["new"]["id"]]["status"] == "active"


def test_customer_detail_includes_licenses(admin_client):
    c = _create_customer(admin_client)
    admin_client.post(
        f"/api/admin/customers/{c['id']}/licenses",
        json={"days": 30},
    )
    r = admin_client.get(f"/api/admin/customers/{c['id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["id"] == c["id"]
    assert len(detail["licenses"]) == 1
    assert detail["payments"] == []
    assert detail["activations"] == []
