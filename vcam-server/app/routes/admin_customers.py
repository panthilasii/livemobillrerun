"""Customer CRUD for the admin panel.

A "customer" is the human (or shop) we're selling to. They have
zero, one, or many licenses attached. Deleting a customer cascades
to their licenses (FK ``ON DELETE CASCADE``); this is intentional
because a customer leaving means their licenses are dead too — and
the audit trail is preserved separately in ``audit_log``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, db
from ..auth import AdminUser

router = APIRouter(prefix="/api/admin/customers", tags=["admin"])


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "line_id": row["line_id"],
        "phone": row["phone"],
        "email": row["email"],
        "notes": row["notes"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


@router.get("")
def list_customers(
    q: str = "",
    admin: AdminUser = Depends(auth.current_admin),
) -> list[dict[str, Any]]:
    """Return all customers, optionally filtered by free-text ``q``.

    The search is **substring across multiple columns** (name +
    line_id + phone + email + notes). It's deliberately not
    full-text-search — this is one admin operating on at most a
    few thousand rows for the lifetime of the product. ``LIKE``
    plus an index on ``name`` handles it instantly.
    """
    sql = (
        "SELECT id, name, line_id, phone, email, notes, status, created_at "
        "FROM customers"
    )
    args: tuple = ()
    if q.strip():
        like = f"%{q.strip()}%"
        sql += (
            " WHERE name LIKE ? OR line_id LIKE ? OR phone LIKE ? "
            "OR email LIKE ? OR notes LIKE ?"
        )
        args = (like, like, like, like, like)
    sql += " ORDER BY id DESC LIMIT 1000"
    with db.connect() as cx:
        rows = cx.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("")
def create_customer(
    payload: dict[str, Any],
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Create a new customer record. Required: ``name``. All
    other fields default to empty string / 'active'."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    if len(name) > 200:
        raise HTTPException(400, "name too long (max 200 chars)")

    fields = {
        "name": name,
        "line_id": (payload.get("line_id") or "").strip()[:100],
        "phone": (payload.get("phone") or "").strip()[:50],
        "email": (payload.get("email") or "").strip()[:200],
        "notes": (payload.get("notes") or "").strip()[:2000],
        "status": (payload.get("status") or "active").strip()[:50],
        "created_at": db.now_iso(),
    }
    with db.connect() as cx:
        cur = cx.execute(
            "INSERT INTO customers (name, line_id, phone, email, notes, "
            "status, created_at) VALUES (:name, :line_id, :phone, :email, "
            ":notes, :status, :created_at)",
            fields,
        )
        new_id = cur.lastrowid
    auth.write_audit(
        admin, "customer.create", "customer", new_id or 0, name,
    )
    return {"id": new_id, **fields}


@router.get("/{cid}")
def get_customer(
    cid: int,
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Return one customer + their licenses + payments + recent
    activations. The dashboard's customer-detail page makes a single
    request for everything to avoid N+1 spinners."""
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, name, line_id, phone, email, notes, status, "
            "created_at FROM customers WHERE id = ?",
            (cid,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "customer not found")
        result = _row_to_dict(row)

        result["licenses"] = [
            {
                "id": int(r["id"]),
                "key": r["key"],
                "max_devices": int(r["max_devices"]),
                "expiry": r["expiry"],
                "status": r["status"],
                "issued_at": r["issued_at"],
                "note": r["note"],
            }
            for r in cx.execute(
                "SELECT id, key, max_devices, expiry, status, issued_at, note "
                "FROM licenses WHERE customer_id = ? ORDER BY id DESC",
                (cid,),
            ).fetchall()
        ]

        result["payments"] = [
            {
                "id": int(r["id"]),
                "amount_satang": int(r["amount_satang"]),
                "method": r["method"],
                "reference": r["reference"],
                "status": r["status"],
                "received_at": r["received_at"],
                "note": r["note"],
                "license_id": (
                    int(r["license_id"]) if r["license_id"] is not None else None
                ),
            }
            for r in cx.execute(
                "SELECT id, amount_satang, method, reference, status, "
                "received_at, note, license_id FROM payments "
                "WHERE customer_id = ? ORDER BY id DESC LIMIT 200",
                (cid,),
            ).fetchall()
        ]

        result["activations"] = [
            {
                "id": int(r["id"]),
                "license_id": int(r["license_id"]),
                "machine_id": r["machine_id"],
                "machine_label": r["machine_label"],
                "last_ip": r["last_ip"],
                "app_version": r["app_version"],
                "activated_at": r["activated_at"],
                "last_seen_at": r["last_seen_at"],
                "status": r["status"],
            }
            for r in cx.execute(
                "SELECT a.id, a.license_id, a.machine_id, a.machine_label, "
                "a.last_ip, a.app_version, a.activated_at, a.last_seen_at, "
                "a.status FROM activations a JOIN licenses l "
                "ON l.id = a.license_id WHERE l.customer_id = ? "
                "ORDER BY a.last_seen_at DESC LIMIT 100",
                (cid,),
            ).fetchall()
        ]

    return result


@router.patch("/{cid}")
def update_customer(
    cid: int,
    payload: dict[str, Any],
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Partial update. Only the fields present in ``payload`` are
    written; missing fields keep their current value. This lets
    the front-end PATCH a single field (e.g. status change) without
    re-sending the whole record."""
    allowed = ("name", "line_id", "phone", "email", "notes", "status")
    sets: list[str] = []
    args: list[Any] = []
    for k in allowed:
        if k in payload:
            sets.append(f"{k} = ?")
            args.append(str(payload[k] or "").strip())
    if not sets:
        raise HTTPException(400, "no updatable fields in payload")
    args.append(cid)

    with db.connect() as cx:
        cur = cx.execute(
            f"UPDATE customers SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "customer not found")
    auth.write_audit(
        admin, "customer.update", "customer", cid,
        ",".join(k for k in allowed if k in payload),
    )
    return get_customer(cid, admin)  # type: ignore[arg-type]


@router.delete("/{cid}")
def delete_customer(
    cid: int,
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Hard delete. Cascades to licenses and (via licenses) to
    activations. Payments are kept (FK ``ON DELETE RESTRICT``) so
    bookkeeping records survive customer cleanup; if there are
    payments attached, the delete fails with 409 and the admin
    must archive them first."""
    with db.connect() as cx:
        # Pre-check payments — we want a friendly 409, not a raw
        # IntegrityError stack trace.
        n_payments = cx.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE customer_id = ?",
            (cid,),
        ).fetchone()["n"]
        if n_payments:
            raise HTTPException(
                409,
                f"customer has {n_payments} payment record(s) — "
                "delete or reassign payments first",
            )
        cur = cx.execute("DELETE FROM customers WHERE id = ?", (cid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "customer not found")
    auth.write_audit(admin, "customer.delete", "customer", cid)
    return {"ok": True, "id": cid}
