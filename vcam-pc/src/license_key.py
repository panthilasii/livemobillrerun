"""License key generation, encoding, and verification.

Format
------

A license key looks like::

    888-AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG

The leading ``888`` is the brand prefix; the remaining groups are a
URL-safe base64 of the payload concatenated with a 16-hex-character
HMAC-SHA256 truncation, hyphenated every four characters for human
readability.

The payload is plain text (UTF-8) of the form::

    customer_name|max_devices|expiry_iso|nonce

* ``customer_name`` — the customer's display name (no '|' allowed)
* ``max_devices``   — integer ≥ 1
* ``expiry_iso``    — YYYY-MM-DD (UTC)
* ``nonce``         — short random string so re-issued keys differ

Verification recomputes the HMAC with the embedded secret. The secret
lives in ``vcam-pc/.license_secret`` and is **never** committed to
git. The admin-side generator (``tools/gen_license.py``) and the
customer-side verifier (this module) read the same file.

Threat model
~~~~~~~~~~~~

This is *speed-bump* protection, not crypto-grade DRM. It stops
casual key-sharing and Discord screenshots. Sophisticated attackers
who can attach a debugger or patch the binary will defeat it; we
accept that and make money on the support, the auto-updates, and the
ease-of-use rather than on the lock itself.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .branding import BRAND
from .config import PROJECT_ROOT


SECRET_PATH = PROJECT_ROOT / ".license_secret"
ACTIVATION_PATH = Path.home() / ".livestudio" / "activation.json"

_DEFAULT_SECRET = b"livestudio-dev-secret-DO-NOT-USE-IN-PROD"


def _load_secret() -> bytes:
    """Return the HMAC secret. If the secret file is missing we fall
    back to a fixed dev-only value so unit tests keep working; ship
    builds must commit a real secret to ``.license_secret`` and bake
    it into the binary at packaging time."""
    if SECRET_PATH.is_file():
        data = SECRET_PATH.read_bytes().strip()
        if data:
            return data
    env = os.environ.get("LIVESTUDIO_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    return _DEFAULT_SECRET


# ── encoding ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LicensePayload:
    customer: str
    max_devices: int
    expiry: date
    nonce: str

    def encode_payload(self) -> str:
        return f"{self.customer}|{self.max_devices}|{self.expiry.isoformat()}|{self.nonce}"


def _hyphenate(s: str, group: int = 4) -> str:
    return "-".join(s[i : i + group] for i in range(0, len(s), group))


def _hmac_short(secret: bytes, payload: str) -> str:
    mac = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return mac[:16].upper()


def generate_key(
    customer: str,
    max_devices: int = 1,
    days: int = 30,
    expiry: date | None = None,
    nonce: str | None = None,
    secret: bytes | None = None,
) -> str:
    """Build a license key from the given fields.

    ``customer`` may not contain ``'|'`` (the field separator).
    ``max_devices`` is clamped to [1, 100]. ``days`` is used to derive
    ``expiry`` if no explicit ``expiry`` is given.
    """
    if "|" in customer:
        raise ValueError("customer name cannot contain '|'")
    max_devices = max(1, min(100, int(max_devices)))
    if expiry is None:
        from datetime import timedelta

        expiry = date.today() + timedelta(days=int(days))
    if nonce is None:
        nonce = secrets.token_hex(3)  # 6 chars, plenty of unique-ness
    payload = LicensePayload(
        customer=customer.strip(),
        max_devices=max_devices,
        expiry=expiry,
        nonce=nonce,
    ).encode_payload()
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")
    sig = _hmac_short(secret or _load_secret(), payload)
    body = f"{raw}{sig}"
    return f"{BRAND.license_prefix}-{_hyphenate(body, 4)}"


# ── decoding ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifiedLicense:
    customer: str
    max_devices: int
    expiry: date
    nonce: str
    raw_key: str

    @property
    def days_left(self) -> int:
        return (self.expiry - date.today()).days

    @property
    def is_expired(self) -> bool:
        return date.today() > self.expiry


class LicenseError(Exception):
    """Raised when a license key fails any validation step."""


def _strip_key(s: str) -> str:
    """Remove whitespace/dashes and the brand prefix."""
    s = "".join(ch for ch in s.strip() if not ch.isspace())
    s = s.replace("-", "")
    prefix = BRAND.license_prefix
    if s.startswith(prefix):
        s = s[len(prefix) :]
    return s


def verify_key(key: str, secret: bytes | None = None) -> VerifiedLicense:
    """Decode and verify a license key. Raises LicenseError on any
    problem (bad format, bad signature, malformed payload). Note: an
    *expired* key is returned successfully — callers must check
    ``is_expired`` themselves so the UI can show a "renew" message
    rather than a hard rejection.
    """
    body = _strip_key(key)
    if len(body) < 18:
        raise LicenseError("key too short")
    raw, sig = body[:-16], body[-16:]
    pad = "=" * (-len(raw) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(raw + pad)
        payload = payload_bytes.decode("utf-8")
    except Exception as e:
        raise LicenseError("malformed key encoding") from e
    expected = _hmac_short(secret or _load_secret(), payload)
    if not hmac.compare_digest(expected, sig.upper()):
        raise LicenseError("signature mismatch — key tampered or wrong build")
    parts = payload.split("|")
    if len(parts) != 4:
        raise LicenseError("malformed payload")
    customer, devs_s, exp_s, nonce = parts
    try:
        max_devices = int(devs_s)
        expiry = date.fromisoformat(exp_s)
    except ValueError as e:
        raise LicenseError("malformed payload fields") from e
    return VerifiedLicense(
        customer=customer,
        max_devices=max_devices,
        expiry=expiry,
        nonce=nonce,
        raw_key=key.strip(),
    )


# ── per-machine activation (offline) ─────────────────────────────


def _machine_id() -> str:
    """Stable-per-machine ID derived from hostname + first MAC. Not
    cryptographically secret — just enough to detect "this license
    was activated on a different PC". On a fresh install we don't
    fail; we let the UI prompt the user to confirm migration."""
    import socket
    import uuid

    host = socket.gethostname() or "unknown"
    mac = uuid.getnode()
    raw = f"{host}|{mac}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def save_activation(license_key: str) -> None:
    """Persist the verified license + machine-id binding to disk."""
    ACTIVATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVATION_PATH.write_text(
        json.dumps(
            {
                "license_key": license_key.strip(),
                "machine_id": _machine_id(),
                "activated_at": datetime.now().isoformat(timespec="seconds"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def load_activation() -> dict | None:
    if not ACTIVATION_PATH.is_file():
        return None
    try:
        return json.loads(ACTIVATION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_activation() -> None:
    """Drop the local activation file (used by Settings → Sign out)."""
    if ACTIVATION_PATH.is_file():
        try:
            ACTIVATION_PATH.unlink()
        except OSError:
            pass


def is_machine_bound(activation: dict) -> bool:
    """True iff the activation record was created on this machine."""
    return activation.get("machine_id") == _machine_id()
