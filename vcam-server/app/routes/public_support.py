"""Customer-facing endpoint for "send my log to admin".

The customer hits ``📋 สร้าง / ส่ง Log ให้แอดมิน`` in the desktop
app. ``vcam-pc`` already builds a redacted ZIP via
``src/log_setup.collect_diagnostic_zip()``; this endpoint accepts
that ZIP and stores it for the admin to read in the panel.

Why we don't dump it into S3
----------------------------

Logs are small (median ~200 KB, p99 ~10 MB). Storing them on the
VPS disk under ``data/uploads/<ticket_id>.zip`` keeps the deploy
single-tier and means support workflow doesn't degrade if S3
credentials rotate. If the VPS disk runs out, the cleanup script
in ``app/cli.py`` deletes tickets older than 90 days.

Why we don't authenticate the upload
------------------------------------

The license key in the body is the auth: a customer who doesn't
have their key can't tag a log onto someone else's account. We
DO size-cap (default 50 MB) so a malicious customer can't fill
the disk with one giant upload.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from .. import db
from ..config import SETTINGS

router = APIRouter(prefix="/api/v1/support", tags=["public"])
log = logging.getLogger(__name__)


@router.post("/upload")
async def upload_support_log(
    request: Request,
    key: str = Form(""),
    message: str = Form(""),
    log_zip: UploadFile = File(...),
) -> dict[str, Any]:
    """Accept a diagnostic ZIP and create a support ticket.

    Multipart/form-data because the client (``vcam-pc``) builds the
    ZIP on disk and streams it; uvicorn's multipart parser handles
    the back-pressure.
    """
    key = key.strip()
    message = message.strip()[:5000]

    # Find the matching license. A blank/unknown key still creates
    # a ticket (so customers without keys can still report bugs)
    # but the admin panel surfaces "anonymous" so we triage them
    # last.
    license_id: int | None = None
    customer_name = ""
    if key:
        with db.connect() as cx:
            row = cx.execute(
                "SELECT id, customer_name FROM licenses WHERE key = ?",
                (key,),
            ).fetchone()
        if row is not None:
            license_id = int(row["id"])
            customer_name = row["customer_name"]

    # Stream the upload to disk, enforcing the size cap as we go.
    # We can't trust ``UploadFile.size`` because some clients lie
    # in the Content-Length header.
    bytes_written = 0
    chunk_size = 1 << 16
    cap = SETTINGS.upload_max_bytes

    SETTINGS.upload_dir.mkdir(parents=True, exist_ok=True)
    fname = f"support-{db.now_iso().replace(':', '')}-{secrets.token_hex(4)}.zip"
    target = SETTINGS.upload_dir / fname
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await log_zip.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > cap:
                    raise HTTPException(
                        413,
                        f"upload exceeds {cap // (1024 * 1024)} MB cap",
                    )
                fh.write(chunk)
    except HTTPException:
        # Tidy up the partial file so the upload dir doesn't pile
        # up with orphan halves on cap rejections.
        target.unlink(missing_ok=True)
        raise
    except Exception:
        target.unlink(missing_ok=True)
        log.exception("support upload write failed")
        raise HTTPException(500, "upload_write_failed")

    # Reject empty / micro uploads — almost always client bugs and
    # we don't want them as tickets.
    if bytes_written < 100:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "upload too small (<100 bytes)")

    with db.connect() as cx:
        cur = cx.execute(
            "INSERT INTO support_tickets (license_id, customer_name, "
            "log_path, log_size_bytes, message, status, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (
                license_id,
                customer_name,
                fname,  # store relative to UPLOAD_DIR
                bytes_written,
                message,
                db.now_iso(),
            ),
        )
        ticket_id = cur.lastrowid

    log.info(
        "support ticket #%s created (%d bytes, license_id=%s)",
        ticket_id, bytes_written, license_id,
    )
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "bytes": bytes_written,
        "message": "Log received — แอดมินจะติดต่อกลับใน 24 ชั่วโมง",
    }
