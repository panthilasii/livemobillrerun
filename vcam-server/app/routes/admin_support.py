"""Admin endpoints for the support inbox.

Listing, downloading, and closing tickets uploaded via
``public_support.upload_support_log``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import auth, db
from ..auth import AdminUser
from ..config import SETTINGS

router = APIRouter(prefix="/api/admin/support", tags=["admin"])


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "license_id": (
            int(row["license_id"]) if row["license_id"] is not None else None
        ),
        "customer_name": row["customer_name"],
        "log_path": row["log_path"],
        "log_size_bytes": int(row["log_size_bytes"]),
        "message": row["message"],
        "status": row["status"],
        "submitted_at": row["submitted_at"],
        "last_admin_reply": row["last_admin_reply"],
        "last_admin_reply_at": row["last_admin_reply_at"],
    }


@router.get("")
def list_tickets(
    status: str = "",
    admin: AdminUser = Depends(auth.current_admin),
) -> list[dict[str, Any]]:
    """Newest-first ticket list, optionally filtered by status."""
    sql = (
        "SELECT id, license_id, customer_name, log_path, log_size_bytes, "
        "message, status, submitted_at, last_admin_reply, "
        "last_admin_reply_at FROM support_tickets"
    )
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status.strip(),)
    sql += " ORDER BY submitted_at DESC LIMIT 500"
    with db.connect() as cx:
        rows = cx.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/{tid}/download")
def download_ticket_log(
    tid: int,
    admin: AdminUser = Depends(auth.current_admin),
):
    """Stream the uploaded ZIP back to the admin's browser.

    We use FileResponse rather than reading-then-Response-bytes
    because some logs hit 50 MB and buffering them in memory
    starves the worker."""
    with db.connect() as cx:
        row = cx.execute(
            "SELECT log_path FROM support_tickets WHERE id = ?", (tid,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "ticket not found")
    full_path = SETTINGS.upload_dir / row["log_path"]
    # Defensive: prevent path traversal in case a ticket row was
    # corrupted or hand-edited. Realpath comparison.
    try:
        resolved = full_path.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(410, "log file missing on disk")
    if not str(resolved).startswith(str(SETTINGS.upload_dir.resolve())):
        raise HTTPException(403, "log_path escapes upload dir")
    return FileResponse(
        path=str(resolved),
        media_type="application/zip",
        filename=f"npc-support-{tid}.zip",
    )


@router.patch("/{tid}")
def update_ticket(
    tid: int,
    payload: dict[str, Any],
    admin: AdminUser = Depends(auth.current_admin),
) -> dict[str, Any]:
    """Update status and/or admin reply note. Used to close a
    ticket once it's resolved + record what was done."""
    sets: list[str] = []
    args: list[Any] = []
    if "status" in payload:
        s = str(payload["status"]).strip()
        if s not in ("open", "in_progress", "closed"):
            raise HTTPException(400, "status must be open|in_progress|closed")
        sets.append("status = ?")
        args.append(s)
    if "reply" in payload:
        sets.append("last_admin_reply = ?")
        args.append(str(payload["reply"]).strip()[:5000])
        sets.append("last_admin_reply_at = ?")
        args.append(db.now_iso())
    if not sets:
        raise HTTPException(400, "nothing to update")
    args.append(tid)

    with db.connect() as cx:
        cur = cx.execute(
            f"UPDATE support_tickets SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "ticket not found")
        row = cx.execute(
            "SELECT id, license_id, customer_name, log_path, log_size_bytes, "
            "message, status, submitted_at, last_admin_reply, "
            "last_admin_reply_at FROM support_tickets WHERE id = ?",
            (tid,),
        ).fetchone()

    auth.write_audit(
        admin, "support.update", "ticket", tid,
        ",".join(k for k in ("status", "reply") if k in payload),
    )
    return _row_to_dict(row)
