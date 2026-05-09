"""License lifecycle endpoints for the admin panel.

Operations
----------

* ``GET    /api/admin/licenses``               — list, filter
* ``POST   /api/admin/customers/{id}/licenses``— issue new
* ``POST   /api/admin/licenses/{id}/revoke``   — flip to 'revoked'
* ``POST   /api/admin/licenses/{id}/extend``   — push expiry by N days
* ``DELETE /api/admin/licenses/{id}``          — hard delete (rare)

License keys, once revoked, **stay in the DB** so:

1. The revocation list endpoint (``/api/v1/revocations``) can
   include them so customer apps shut themselves off.
2. The audit trail keeps "this customer once had this key" intact
   for refund disputes.

Reissue (because customer lost the key) is just "issue a new one";
we don't try to recover the old key bytes (they include a fresh
nonce that we don't keep around).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, crypto, db
from ..auth import AdminUser
from ..config import SETTINGS

# This router uses TWO prefixes (the issue endpoint is nested under
# /customers/{id}, the rest under /licenses). We declare it without a
# prefix and let main.py mount it at root + the per-resource paths.
router = APIRouter(tags=["admin"])


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "customer_id": int(row["customer_id"]),
        "customer_name": row["customer_name"],
        "key": row["key"],
        "nonce": row["nonce"],
        "max_devices": int(row["max_devices"]),
        "expiry": row["expiry"],
        "issued_at": row["issued_at"],
        "issued_by_admin": (
            int(row["issued_by_admin"])
            if row["issued_by_admin"] is not None else None
        ),
        "note": row["note"],
        "status": row["status"],
    }


@router.get("/api/admin/licenses")
def list_licenses(
    status: str = "",
    admin: AdminUser = Depends(auth.current_admin),
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, customer_id, customer_name, key, nonce, max_devices, "
        "expiry, issued_at, issued_by_admin, note, status FROM licenses"
    )
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status.strip(),)
    sql += " ORDER BY id DESC LIMIT 1000"
    with db.connect() as cx:
        rows = cx.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/api/admin/customers/{cid}/licenses")
def issue_license_for_customer(
    cid: int,
    payload: dict[str, Any],
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Sign a fresh license, attach it to ``cid``, persist + audit.

    Body fields (all optional except ``customer_name`` falls back
    to the customer row):
    * ``max_devices`` — default ``LICENSE_DEFAULT_DEVICES`` (3).
    * ``days``        — default ``LICENSE_DEFAULT_DAYS`` (30).
    * ``expiry``      — explicit yyyy-mm-dd, overrides ``days``.
    * ``customer_name`` — name embedded in the SIGNED payload.
                          Defaults to the customer row's ``name``;
                          override if the customer wants their
                          shop name on the key instead of personal.
    * ``note``        — admin-only annotation (e.g. "renewal #2").
    """
    with db.connect() as cx:
        cust = cx.execute(
            "SELECT id, name FROM customers WHERE id = ?", (cid,),
        ).fetchone()
        if cust is None:
            raise HTTPException(404, "customer not found")
        default_name = cust["name"]

    customer_name = (
        (payload.get("customer_name") or default_name).strip()
    )
    max_devices = int(
        payload.get("max_devices") or SETTINGS.license_default_devices
    )
    if "expiry" in payload and payload["expiry"]:
        try:
            expiry = date.fromisoformat(str(payload["expiry"]))
        except ValueError:
            raise HTTPException(400, "expiry must be yyyy-mm-dd")
    else:
        days = int(payload.get("days") or SETTINGS.license_default_days)
        if days < 1 or days > 3650:
            raise HTTPException(400, "days must be 1..3650")
        expiry = date.today() + timedelta(days=days)

    note = (payload.get("note") or "").strip()[:500]

    try:
        key, payload_obj = crypto.issue_key(
            customer=customer_name,
            max_devices=max_devices,
            expiry=expiry,
        )
    except crypto.CryptoError as exc:
        raise HTTPException(400, str(exc))

    with db.connect() as cx:
        cur = cx.execute(
            "INSERT INTO licenses (customer_id, key, nonce, customer_name, "
            "max_devices, expiry, issued_at, issued_by_admin, note, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                cid,
                key,
                payload_obj.nonce,
                customer_name,
                max_devices,
                expiry.isoformat(),
                db.now_iso(),
                admin.id,
                note,
            ),
        )
        new_id = cur.lastrowid

    auth.write_audit(
        admin, "license.issue", "license", new_id or 0,
        f"customer={cid} max_devices={max_devices} expiry={expiry}",
    )
    return {
        "id": new_id,
        "customer_id": cid,
        "key": key,
        "nonce": payload_obj.nonce,
        "customer_name": customer_name,
        "max_devices": max_devices,
        "expiry": expiry.isoformat(),
        "status": "active",
        "issued_at": db.now_iso(),
        "note": note,
    }


@router.post("/api/admin/licenses/{lid}/revoke")
def revoke_license(
    lid: int,
    payload: dict[str, Any] | None = None,
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Flip status → 'revoked'. The revocation list endpoint then
    advertises this nonce so phone-home customer apps see it on
    the next poll (≤ ``REVOCATION_CACHE_SECONDS`` later) and shut
    themselves down."""
    reason = ((payload or {}).get("reason") or "").strip()[:500]
    with db.connect() as cx:
        cur = cx.execute(
            "UPDATE licenses SET status = 'revoked' WHERE id = ? "
            "AND status != 'revoked'",
            (lid,),
        )
        if cur.rowcount == 0:
            # Either non-existent or already revoked — make the
            # response distinguishable so the UI can react.
            row = cx.execute(
                "SELECT status FROM licenses WHERE id = ?", (lid,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "license not found")
            return {"ok": True, "id": lid, "status": row["status"], "noop": True}
    auth.write_audit(
        admin, "license.revoke", "license", lid, reason or "(no reason)",
    )
    return {"ok": True, "id": lid, "status": "revoked"}


@router.post("/api/admin/licenses/{lid}/extend")
def extend_license(
    lid: int,
    payload: dict[str, Any],
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Issue a NEW key with a later expiry, and revoke the old one.

    We don't mutate the old expiry in-place because the signed
    license-key payload includes the expiry — changing it on the
    server side would not change what the customer's app verifies.
    Re-issue is the only correct path.

    Body: ``{"days": 30}`` OR ``{"expiry": "yyyy-mm-dd"}``.
    """
    with db.connect() as cx:
        old = cx.execute(
            "SELECT id, customer_id, customer_name, max_devices, "
            "expiry, status FROM licenses WHERE id = ?",
            (lid,),
        ).fetchone()
    if old is None:
        raise HTTPException(404, "license not found")

    if "expiry" in payload and payload["expiry"]:
        try:
            new_expiry = date.fromisoformat(str(payload["expiry"]))
        except ValueError:
            raise HTTPException(400, "expiry must be yyyy-mm-dd")
    else:
        days = int(payload.get("days") or 0)
        if days <= 0:
            raise HTTPException(400, "either 'days' (>0) or 'expiry' required")
        old_expiry = date.fromisoformat(old["expiry"])
        # Anchor the new expiry on whichever is later: today or the
        # current expiry. That way "extend by 30 days" on a still-
        # active license adds to the remaining time, but on an
        # expired one it adds from today.
        anchor = max(date.today(), old_expiry)
        new_expiry = anchor + timedelta(days=days)

    # Issue replacement.
    new_key_resp = issue_license_for_customer(
        cid=int(old["customer_id"]),
        payload={
            "max_devices": int(old["max_devices"]),
            "customer_name": old["customer_name"],
            "expiry": new_expiry.isoformat(),
            "note": f"extension of license #{lid}",
        },
        admin=admin,
    )
    # Revoke old.
    revoke_license(
        lid=lid, payload={"reason": f"extended → license #{new_key_resp['id']}"},
        admin=admin,
    )
    auth.write_audit(
        admin, "license.extend", "license", lid,
        f"new_id={new_key_resp['id']} expiry={new_expiry}",
    )
    return {"old_id": lid, "new": new_key_resp}


@router.delete("/api/admin/licenses/{lid}")
def delete_license(
    lid: int,
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Hard delete. Loses the audit trail of the key's existence
    in the licenses table — only the audit_log row remains. Use
    revoke instead unless you have a privacy reason (e.g. test
    keys cluttering up production)."""
    with db.connect() as cx:
        cur = cx.execute("DELETE FROM licenses WHERE id = ?", (lid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "license not found")
    auth.write_audit(admin, "license.delete", "license", lid)
    return {"ok": True, "id": lid}
