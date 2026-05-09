"""Admin password hashing + signed-cookie session.

Why bcrypt
----------

bcrypt is the boring-correct choice for password hashing in 2026:
slow on purpose (cost factor configurable), salt baked into the
hash string, no separate salt column, no key-derivation lib needed.

We use the raw ``bcrypt`` package, not passlib, because the latter
emits a deprecation warning on every import in modern Python and
the API surface we need is two functions.

Why itsdangerous (signed cookies) instead of JWT
------------------------------------------------

JWT is overkill for a 1-server admin panel. We don't need
distributed session validation; we need "is this cookie one we
issued?". A 32-byte HMAC over the user-id is exactly that, and
``itsdangerous`` ships it with rotation built in.

Cookie shape::

    {admin_id: 42, exp: 1740000000}

Signed with ``SETTINGS.session_secret``. If the secret rotates
(deploy bug, key compromise) every existing cookie becomes
invalid — which is exactly the desired behaviour. We log this so
ops can see a wave of re-logins instead of a wave of mysterious
500s.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import db
from .config import SETTINGS

log = logging.getLogger(__name__)


# ── password hashing ────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Return a bcrypt hash suitable for the ``admins.password_hash``
    column. Cost factor 12 ≈ 250 ms on a modest VPS — slow enough
    to make brute-force impractical, fast enough that login feels
    instant from the operator's POV."""
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=12),
    ).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time check of plaintext against bcrypt hash."""
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), hashed.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # Malformed hash in DB. Treat as wrong-password rather than
        # 500 — an invalid stored hash is an integrity issue, not a
        # usability one for the requester.
        log.exception("bcrypt verify raised on stored hash")
        return False


# ── signed-cookie session ───────────────────────────────────────────


_signer = TimestampSigner(SETTINGS.session_secret, salt="admin-session-v1")


def issue_session_cookie(response: Response, admin_id: int) -> None:
    """Write the session cookie for ``admin_id`` onto ``response``.

    The cookie value is the signed bytes ``b"<admin_id>"`` — we
    don't need anything more in the payload because admin metadata
    is fetched fresh from the DB on every request (so a
    just-deactivated admin can't use a still-valid cookie)."""
    token = _signer.sign(str(admin_id).encode("utf-8")).decode("utf-8")
    response.set_cookie(
        key=SETTINGS.cookie_name,
        value=token,
        max_age=SETTINGS.cookie_max_age,
        httponly=True,
        secure=SETTINGS.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SETTINGS.cookie_name, path="/",
        secure=SETTINGS.cookie_secure,
    )


def _decode_session_cookie(raw: str) -> Optional[int]:
    """Validate + decode the cookie. Returns admin_id or None."""
    try:
        unsigned = _signer.unsign(
            raw.encode("utf-8"),
            max_age=SETTINGS.cookie_max_age,
        )
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    try:
        return int(unsigned.decode("utf-8"))
    except ValueError:
        return None


# ── DB-backed admin lookup ─────────────────────────────────────────


@dataclass
class AdminUser:
    id: int
    email: str
    display_name: str
    is_active: bool


def _fetch_admin(admin_id: int) -> Optional[AdminUser]:
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, email, display_name, is_active FROM admins WHERE id = ?",
            (admin_id,),
        ).fetchone()
    if row is None:
        return None
    return AdminUser(
        id=int(row["id"]),
        email=row["email"],
        display_name=row["display_name"] or row["email"],
        is_active=bool(row["is_active"]),
    )


def authenticate(email: str, password: str) -> Optional[AdminUser]:
    """Verify email/password against ``admins`` table. Returns the
    user on success, ``None`` on any failure (wrong email, wrong
    password, deactivated account). The same return value for
    every failure mode keeps timing + responses uniform — handlers
    above ``authenticate`` always show "อีเมล/รหัสผ่านไม่ถูกต้อง"."""
    if not email or not password:
        return None
    with db.connect() as cx:
        row = cx.execute(
            "SELECT id, email, display_name, password_hash, is_active "
            "FROM admins WHERE email = ?",
            (email.strip().lower(),),
        ).fetchone()
    if row is None:
        # Even on missing user, run bcrypt against a dummy hash so
        # response time is uniform — defends against username-
        # enumeration through timing.
        bcrypt.checkpw(
            b"dummy",
            b"$2b$12$c2DkJjY5kGxKZf5OHj3CCO6pX6Q3h.YxQcK7N7t2LpE0E5L7xUnLW",
        )
        return None
    if not row["is_active"]:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    # Refresh last_login_at — best-effort; failure here doesn't block login.
    try:
        with db.connect() as cx:
            cx.execute(
                "UPDATE admins SET last_login_at = ? WHERE id = ?",
                (db.now_iso(), int(row["id"])),
            )
    except Exception:
        log.exception("update last_login_at failed")
    return AdminUser(
        id=int(row["id"]),
        email=row["email"],
        display_name=row["display_name"] or row["email"],
        is_active=True,
    )


# ── FastAPI dependency ──────────────────────────────────────────────


def current_admin(request: Request) -> AdminUser:
    """Dependency that guards admin routes.

    Reads the cookie, decodes the signed session, looks up the
    admin in the DB, and raises 401 if anything is off. We
    re-fetch on every request so a freshly deactivated admin
    can't sneak in with an old-but-still-valid cookie.
    """
    raw = request.cookies.get(SETTINGS.cookie_name)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not_authenticated",
            headers={"WWW-Authenticate": "Cookie"},
        )
    admin_id = _decode_session_cookie(raw)
    if admin_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_or_expired_session",
            headers={"WWW-Authenticate": "Cookie"},
        )
    admin = _fetch_admin(admin_id)
    if admin is None or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin_not_found_or_inactive",
        )
    return admin


def maybe_current_admin(request: Request) -> Optional[AdminUser]:
    """Same as ``current_admin`` but returns ``None`` instead of
    raising. Used for the login page so an already-authenticated
    user is redirected to the dashboard."""
    try:
        return current_admin(request)
    except HTTPException:
        return None


# ── audit log helper ────────────────────────────────────────────────


def write_audit(
    admin: Optional[AdminUser],
    action: str,
    target_kind: str = "",
    target_id: int = 0,
    details: str = "",
) -> None:
    """Append a row to ``audit_log``. Best-effort; logging failure
    must NEVER block the actual action — we'd rather lose an
    audit row than break license issuance."""
    try:
        with db.connect() as cx:
            cx.execute(
                "INSERT INTO audit_log (admin_id, action, target_kind, "
                "target_id, details, at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    admin.id if admin else None,
                    action,
                    target_kind,
                    int(target_id),
                    details,
                    db.now_iso(),
                ),
            )
    except Exception:
        log.exception("audit_log write failed for action=%s", action)
