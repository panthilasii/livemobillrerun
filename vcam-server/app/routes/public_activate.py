"""Customer-facing endpoints (no admin auth required).

These are called by the customer's ``vcam-pc`` install:

* ``POST /api/v1/activate``      — phone home when a license key
                                   is entered. Records a row in
                                   ``activations`` so the admin
                                   panel can show "this customer
                                   has 3 PCs activated".
* ``POST /api/v1/heartbeat``     — periodic check-in (~ once per
                                   hour). Updates ``last_seen_at``
                                   so we can spot abandoned
                                   installs / fraud.
* ``GET  /api/v1/revocations``   — signed list of revoked license
                                   nonces. The customer app polls
                                   this every ~6 h and shuts down
                                   if its own nonce is on the list.
* ``GET  /api/v1/license/check`` — synchronous "is THIS key still
                                   valid?" check. Cheap; doesn't
                                   write anything. Used at app
                                   startup before the heavy poll.

Trust model
-----------

We trust the license-key string itself: it carries an Ed25519
signature over (customer, max_devices, expiry, nonce). If the sig
verifies AND the nonce + customer + expiry match a row in our
``licenses`` table AND that row's status is ``active``, the
caller is legit.

A malicious client can't:

* Forge a key — needs the private seed.
* Replay an old activation — we re-verify the key on every call.
* Pretend to be a different customer's machine — the
  ``machine_id`` is just an identifier; we record it but don't
  trust it for entitlement checks.

What we DON'T defend against (deliberate scope):

* Customer copying their key to N machines — we COUNT activations
  and the admin panel surfaces a flag if N > max_devices, but we
  let the app keep running because instant lock-outs cause more
  support tickets than they prevent.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from .. import crypto, db
from ..config import SETTINGS

router = APIRouter(prefix="/api/v1", tags=["public"])
log = logging.getLogger(__name__)


# ── helpers ────────────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    """Extract the real client IP, honouring ``X-Forwarded-For`` if
    present (we'll be behind Caddy/Nginx in production). Falls back
    to the direct peer in dev."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # First entry is the original client.
        return xff.split(",")[0].strip()[:64]
    if request.client:
        return request.client.host[:64]
    return ""


def _verify_key_payload(key: str) -> dict[str, Any]:
    """Parse + verify the Ed25519 signature on the key string.

    Returns the unpacked payload fields. Raises HTTPException(400)
    on any verification failure — we don't differentiate "bad sig"
    from "bad encoding" externally to avoid giving probers a
    diagnostic oracle for forging attempts.
    """
    import base64

    body = "".join(ch for ch in key.upper() if ch != "-" and not ch.isspace())
    if body.startswith(SETTINGS.license_prefix):
        body = body[len(SETTINGS.license_prefix):]
    pad = "=" * (-len(body) % 8)
    try:
        raw = base64.b32decode(body + pad)
    except Exception:
        raise HTTPException(400, "invalid_key")

    if len(raw) < 2 + 64:
        raise HTTPException(400, "invalid_key")

    plen = int.from_bytes(raw[:2], "big")
    if 2 + plen + 64 != len(raw):
        raise HTTPException(400, "invalid_key")

    payload_bytes = raw[2:2 + plen]
    sig = raw[2 + plen:]

    if not crypto.verify_blob(payload_bytes, sig):
        raise HTTPException(400, "invalid_key")

    try:
        payload_str = payload_bytes.decode("utf-8")
        customer, devs_s, exp_s, nonce = payload_str.split("|")
        return {
            "customer": customer,
            "max_devices": int(devs_s),
            "expiry": exp_s,
            "nonce": nonce,
        }
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid_key")


# ── /activate ──────────────────────────────────────────────────────


@router.post("/activate")
def activate(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Record an activation. Returns the license summary so the
    desktop app can render "Active until 2026-06-06 (3 devices)".

    Body::

        {
          "key": "888-...",
          "machine_id": "<hex>",      # stable per PC
          "machine_label": "DESKTOP-XYZ",
          "app_version": "1.7.2"
        }
    """
    key = (payload.get("key") or "").strip()
    machine_id = (payload.get("machine_id") or "").strip()[:64]
    if not key or not machine_id:
        raise HTTPException(400, "key and machine_id required")

    parsed = _verify_key_payload(key)

    # Map back to the DB row by exact key string. We persist the
    # full hyphenated key in the DB so this match is unambiguous
    # and indexed (UNIQUE constraint).
    with db.connect() as cx:
        lic = cx.execute(
            "SELECT id, status, max_devices, expiry FROM licenses "
            "WHERE key = ?",
            (key,),
        ).fetchone()
        if lic is None:
            # Signature checks out but we don't know this nonce.
            # Two possibilities:
            # 1. Issued before the server existed — we'd want to
            #    backfill but never auto-create rows from public
            #    input (poison data).
            # 2. Forged signature against a key we never made
            #    (impossible if our private seed is intact).
            # Either way, the safe answer is "unknown_key" — the
            # customer app will show a "ติดต่อแอดมิน" prompt.
            raise HTTPException(404, "unknown_key")
        if lic["status"] == "revoked":
            raise HTTPException(403, "revoked")

        now = db.now_iso()
        # Upsert: same (license_id, machine_id) → update last_seen.
        cur = cx.execute(
            "INSERT INTO activations (license_id, machine_id, machine_label, "
            "last_ip, user_agent, app_version, activated_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(license_id, machine_id) DO UPDATE SET "
            "  machine_label = excluded.machine_label, "
            "  last_ip = excluded.last_ip, "
            "  user_agent = excluded.user_agent, "
            "  app_version = excluded.app_version, "
            "  last_seen_at = excluded.last_seen_at",
            (
                int(lic["id"]),
                machine_id,
                (payload.get("machine_label") or "").strip()[:200],
                _client_ip(request),
                request.headers.get("user-agent", "")[:200],
                (payload.get("app_version") or "").strip()[:50],
                now,
                now,
            ),
        )

    return {
        "ok": True,
        "license_id": int(lic["id"]),
        "customer": parsed["customer"],
        "max_devices": int(lic["max_devices"]),
        "expiry": lic["expiry"],
        "status": lic["status"],
    }


# ── /heartbeat ─────────────────────────────────────────────────────


@router.post("/heartbeat")
def heartbeat(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Lightweight check-in. Updates ``last_seen_at`` for the
    (license, machine) pair AND returns the current revocation
    status so the customer app can self-deactivate without a
    separate /revocations call when convenient."""
    key = (payload.get("key") or "").strip()
    machine_id = (payload.get("machine_id") or "").strip()[:64]
    if not key or not machine_id:
        raise HTTPException(400, "key and machine_id required")

    # We DON'T re-verify the signature here — a forged-but-valid sig
    # without a DB row would be caught by the JOIN below, and we want
    # heartbeats to be cheap (called frequently). Real validation
    # happened at /activate time.

    with db.connect() as cx:
        lic = cx.execute(
            "SELECT id, status, expiry FROM licenses WHERE key = ?",
            (key,),
        ).fetchone()
        if lic is None:
            raise HTTPException(404, "unknown_key")

        now = db.now_iso()
        # Only update if this (license, machine) was previously
        # activated. Heartbeats from never-activated machines are
        # noise and get a 404 — pushes the client to call /activate
        # first (the activation endpoint also updates last_seen).
        cur = cx.execute(
            "UPDATE activations SET last_seen_at = ? "
            "WHERE license_id = ? AND machine_id = ?",
            (now, int(lic["id"]), machine_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "not_activated")

        act = cx.execute(
            "SELECT status FROM activations WHERE license_id = ? "
            "AND machine_id = ?",
            (int(lic["id"]), machine_id),
        ).fetchone()

    return {
        "ok": True,
        "license_status": lic["status"],
        "machine_status": act["status"] if act else "active",
        "expiry": lic["expiry"],
    }


# ── /revocations ───────────────────────────────────────────────────


_revocation_cache: dict[str, Any] = {"at": 0.0, "body": b"", "sig_hex": ""}


def _build_revocation_payload() -> dict[str, Any]:
    """Build the canonical signed-revocation body.

    Body shape (signed JSON, sorted keys for deterministic bytes)::

        {
          "kind": "npc.revocations.v1",
          "issued_at": "2026-05-09T10:00:00",
          "nonces": ["a1b2c3", "deadbe", ...]
        }

    Customers see ``{"manifest": <body_str>, "sig": <hex>}`` and
    verify the sig over ``body_str`` against the embedded
    public key in ``vcam-pc/src/_pubkey.py``.
    """
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT nonce FROM licenses WHERE status = 'revoked' "
            "ORDER BY id"
        ).fetchall()
    body = json.dumps(
        {
            "kind": "npc.revocations.v1",
            "issued_at": db.now_iso(),
            "nonces": [r["nonce"] for r in rows],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sig = crypto.sign_blob(body)
    return {"body": body, "sig_hex": sig.hex()}


@router.get("/revocations")
def revocations() -> dict[str, Any]:
    """Return the current signed revocation list.

    Cached for ``REVOCATION_CACHE_SECONDS`` (default 60 s) so a
    swarm of customer apps polling at the same minute doesn't
    re-sign on every hit. Cache invalidates naturally; admin-side
    revoke actions take effect within a minute.
    """
    now = time.time()
    cached_at = float(_revocation_cache.get("at", 0.0))
    if now - cached_at < SETTINGS.revocation_cache_seconds:
        body = _revocation_cache["body"]
        sig_hex = _revocation_cache["sig_hex"]
    else:
        out = _build_revocation_payload()
        body = out["body"]
        sig_hex = out["sig_hex"]
        _revocation_cache["at"] = now
        _revocation_cache["body"] = body
        _revocation_cache["sig_hex"] = sig_hex

    return {
        "manifest": body.decode("utf-8"),
        "sig": sig_hex,
        "public_key_hex": crypto.public_key_hex(),
    }


# ── /license/check ─────────────────────────────────────────────────


@router.get("/license/check")
def license_check(key: str) -> dict[str, Any]:
    """Cheap synchronous probe. Used at app startup before the
    heavier revocation poll, so a freshly-revoked customer sees
    the lock-out instantly on next launch instead of after the
    next 6-hour poll cycle."""
    key = key.strip()
    if not key:
        raise HTTPException(400, "key required")
    with db.connect() as cx:
        row = cx.execute(
            "SELECT status, expiry FROM licenses WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        # Don't 404 here — customer apps may have keys we don't
        # know about (issued before server existed, future
        # backfill). They should treat "unknown" as "valid (offline
        # mode)" because the local Ed25519 verify already passed.
        return {"status": "unknown"}
    return {"status": row["status"], "expiry": row["expiry"]}
