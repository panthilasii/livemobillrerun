"""Manual payments ledger.

Why "manual"? Because the immediate need is "I got a Line slip,
record it before I forget" — not "automated webhook reconciliation
with 4 PSPs". When we wire PromptPay-Open or Omise webhooks later,
they'll write into the same table; the admin UI doesn't need to
change.

PromptPay QR generation
-----------------------

The ``GET /api/admin/payments/promptpay-qr`` endpoint composes a
QR payload following the standard EMV-format PromptPay (Thai
QR Code Standard for Payment, V3.0). We build the *string* here;
rendering it as a PNG/SVG happens client-side via the
``qrcode-js`` CDN library on the admin page (one less native
dep on the server).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, db
from ..auth import AdminUser

router = APIRouter(prefix="/api/admin/payments", tags=["admin"])


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "customer_id": int(row["customer_id"]),
        "license_id": (
            int(row["license_id"]) if row["license_id"] is not None else None
        ),
        "amount_satang": int(row["amount_satang"]),
        "amount_baht": round(int(row["amount_satang"]) / 100, 2),
        "method": row["method"],
        "reference": row["reference"],
        "status": row["status"],
        "note": row["note"],
        "received_at": row["received_at"],
    }


@router.get("")
def list_payments(
    customer_id: int = 0,
    status: str = "",
    admin: AdminUser = Depends(auth.current_admin),
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, customer_id, license_id, amount_satang, method, "
        "reference, status, note, received_at FROM payments"
    )
    where: list[str] = []
    args: list[Any] = []
    if customer_id:
        where.append("customer_id = ?")
        args.append(customer_id)
    if status:
        where.append("status = ?")
        args.append(status.strip())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY received_at DESC LIMIT 1000"
    with db.connect() as cx:
        rows = cx.execute(sql, tuple(args)).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("")
def record_payment(
    payload: dict[str, Any],
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Record a payment we've received (or are awaiting).

    Body::

        {
          "customer_id": 1,
          "license_id": 7,           # optional (link to issued key)
          "amount_baht": 990.00,     # OR amount_satang for satang precision
          "method": "promptpay",
          "reference": "Slip #44211",
          "status": "received",      # default
          "note": ""
        }
    """
    cid = int(payload.get("customer_id") or 0)
    if not cid:
        raise HTTPException(400, "customer_id required")
    if "amount_satang" in payload:
        sat = int(payload["amount_satang"])
    elif "amount_baht" in payload:
        sat = int(round(float(payload["amount_baht"]) * 100))
    else:
        raise HTTPException(400, "amount_baht or amount_satang required")
    if sat <= 0:
        raise HTTPException(400, "amount must be > 0")

    method = (payload.get("method") or "promptpay").strip()[:50]
    reference = (payload.get("reference") or "").strip()[:200]
    status = (payload.get("status") or "received").strip()
    if status not in ("pending", "received", "refunded"):
        raise HTTPException(400, "status must be pending|received|refunded")
    note = (payload.get("note") or "").strip()[:1000]
    license_id = payload.get("license_id")
    if license_id is not None:
        try:
            license_id = int(license_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "license_id must be int or null")

    with db.connect() as cx:
        if cx.execute(
            "SELECT 1 FROM customers WHERE id = ?", (cid,),
        ).fetchone() is None:
            raise HTTPException(404, "customer not found")
        if license_id and cx.execute(
            "SELECT 1 FROM licenses WHERE id = ? AND customer_id = ?",
            (license_id, cid),
        ).fetchone() is None:
            raise HTTPException(400, "license_id does not belong to customer")
        cur = cx.execute(
            "INSERT INTO payments (customer_id, license_id, amount_satang, "
            "method, reference, status, note, received_at, recorded_by_admin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid, license_id, sat, method, reference, status, note,
                db.now_iso(), admin.id,
            ),
        )
        new_id = cur.lastrowid
        row = cx.execute(
            "SELECT id, customer_id, license_id, amount_satang, method, "
            "reference, status, note, received_at FROM payments WHERE id = ?",
            (new_id,),
        ).fetchone()

    auth.write_audit(
        admin, "payment.create", "payment", new_id or 0,
        f"customer={cid} sat={sat} method={method}",
    )
    return _row_to_dict(row)


@router.patch("/{pid}")
def update_payment(
    pid: int,
    payload: dict[str, Any],
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Mostly used to flip status (received → refunded) or to
    correct a typo in the reference. We don't allow amount edits
    after creation — issue a refund + new payment instead, so the
    audit trail is clean."""
    sets: list[str] = []
    args: list[Any] = []
    for k in ("method", "reference", "status", "note"):
        if k in payload:
            v = str(payload[k] or "").strip()
            if k == "status" and v not in (
                "pending", "received", "refunded",
            ):
                raise HTTPException(400, "status invalid")
            sets.append(f"{k} = ?")
            args.append(v)
    if not sets:
        raise HTTPException(400, "nothing to update")
    args.append(pid)
    with db.connect() as cx:
        cur = cx.execute(
            f"UPDATE payments SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "payment not found")
        row = cx.execute(
            "SELECT id, customer_id, license_id, amount_satang, method, "
            "reference, status, note, received_at FROM payments WHERE id = ?",
            (pid,),
        ).fetchone()
    auth.write_audit(admin, "payment.update", "payment", pid)
    return _row_to_dict(row)


# ── PromptPay QR generation ────────────────────────────────────────


def _crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE — the checksum mode mandated by EMV QR.

    Polynomial 0x1021, init 0xFFFF, no reflection, no XOR-out.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def _emv_field(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def build_promptpay_payload(
    target: str, amount_baht: float | None = None,
) -> str:
    """Build an EMV-QR payload for Thai PromptPay.

    ``target`` is either a 13-digit Tax/National ID, a 10-digit
    mobile number (we'll pad it to 13 by prepending '0066'), or
    a 15-digit e-wallet ID. ``amount_baht`` is optional; if
    omitted the QR is "any-amount" (payer types it in).
    """
    target = "".join(ch for ch in target if ch.isdigit())
    if len(target) == 10:                  # mobile, e.g. 0812345678
        target_full = "0066" + target[1:]  # → 008812345678 ish
        sub_id, sub_val = "01", target_full
    elif len(target) == 13:                # tax / nat-id
        sub_id, sub_val = "02", target
    elif len(target) == 15:                # e-wallet
        sub_id, sub_val = "03", target
    else:
        raise ValueError("PromptPay target must be 10, 13, or 15 digits")

    aid = "A000000677010111"
    merchant_acct_info = (
        _emv_field("00", aid) + _emv_field(sub_id, sub_val)
    )

    parts = [
        _emv_field("00", "01"),                      # payload format indicator
        _emv_field("01", "12" if amount_baht else "11"),  # static / dynamic
        _emv_field("29", merchant_acct_info),        # merchant account
        _emv_field("53", "764"),                     # currency: THB
        _emv_field("58", "TH"),                      # country
    ]
    if amount_baht:
        parts.insert(4, _emv_field("54", f"{amount_baht:.2f}"))
    body = "".join(parts) + "6304"
    crc = _crc16_ccitt(body.encode("ascii"))
    return body + f"{crc:04X}"


@router.get("/promptpay-qr")
def promptpay_qr(
    target: str,
    amount_baht: float | None = None,
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Return the PromptPay payload string. The frontend renders
    it to QR with ``qrcode-js`` so we don't need a native PNG lib."""
    try:
        payload = build_promptpay_payload(target, amount_baht)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "payload": payload,
        "target": target,
        "amount_baht": amount_baht,
    }
