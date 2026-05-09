"""License key generation, encoding, and verification (Ed25519).

Format
------

A license key looks like::

    888-AAAA-BBBB-CCCC-…

The leading ``888`` is the brand prefix; the rest is base32 (RFC
4648, alphabet ``A–Z 2–7``, no padding) of::

    plen:2bytes_be || payload || sig64

…where ``payload`` is the same plain-text record we used in the
HMAC era and ``sig64`` is a 64-byte Ed25519 signature over the
payload. The whole body is hyphenated every 4 characters for
human readability.

The payload is plain UTF-8 of the form::

    customer_name|max_devices|expiry_iso|nonce

* ``customer_name`` — the customer's display name (no '|' allowed)
* ``max_devices``   — integer ≥ 1
* ``expiry_iso``    — YYYY-MM-DD (UTC)
* ``nonce``         — short random string so re-issued keys differ

Threat model
~~~~~~~~~~~~

Asymmetric crypto. The signing private key lives on the admin's
machine only (``vcam-pc/.private_key``) — it never ships. The
public verify key is baked into ``src/_pubkey.py`` and travels
with every customer build. This means:

* A customer who reverse-engineers the binary can verify keys but
  cannot forge new ones.
* Rotating the keypair (``tools/init_keys.py --force``) revokes
  every license shipped against the old public key — useful if a
  customer's bundle is leaked widely.

Backwards compatibility
~~~~~~~~~~~~~~~~~~~~~~~

The format is *not* compatible with the previous HMAC scheme; old
keys must be reissued. This is acceptable because we have not yet
sold any keys.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import _ed25519 as ed
from .branding import BRAND
from .config import PROJECT_ROOT


PRIVATE_KEY_PATH = PROJECT_ROOT / ".private_key"
ACTIVATION_PATH = Path.home() / ".npcreate" / "activation.json"


# ── key material ─────────────────────────────────────────────────


def _load_public_key() -> bytes:
    """Read the embedded public verify-key.

    We import lazily so unit tests that use ``ed.keypair_from_seed``
    with a deterministic seed don't need a real ``_pubkey.py`` on
    disk — they pass ``public_key=`` directly to ``verify_key``.
    """
    try:
        from . import _pubkey  # type: ignore
    except ImportError as e:
        raise LicenseError(
            "public key not initialised — run "
            "`python tools/init_keys.py` once on the admin machine"
        ) from e
    hex_str = getattr(_pubkey, "PUBLIC_KEY_HEX", "").strip()
    if len(hex_str) != 64:
        raise LicenseError("malformed public key constant")
    return bytes.fromhex(hex_str)


def _load_private_seed() -> bytes:
    """Read the admin's signing seed. Admin-only path."""
    if not PRIVATE_KEY_PATH.is_file():
        raise LicenseError(
            f"private key not found at {PRIVATE_KEY_PATH} — "
            f"run `python tools/init_keys.py` first"
        )
    text = PRIVATE_KEY_PATH.read_text(encoding="utf-8").strip()
    env = os.environ.get("LIVESTUDIO_PRIVATE_HEX", "").strip()
    if env:
        text = env
    if len(text) != 64:
        raise LicenseError("malformed private key file (expected 64 hex chars)")
    return bytes.fromhex(text)


# ── encoding ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LicensePayload:
    customer: str
    max_devices: int
    expiry: date
    nonce: str

    def encode(self) -> str:
        return f"{self.customer}|{self.max_devices}|{self.expiry.isoformat()}|{self.nonce}"


def _hyphenate(s: str, group: int = 4) -> str:
    return "-".join(s[i : i + group] for i in range(0, len(s), group))


def _b32_encode(b: bytes) -> str:
    """RFC 4648 base32 (alphabet ``A–Z 2–7``).

    Chosen over base64url because base32 contains no ``-`` characters,
    so we can use ``-`` purely as a visual group separator without
    collisions (e.g. ``888-AAAA-BBBB-…``). Also case-insensitive when
    a customer types it back to us over Line.
    """
    return base64.b32encode(b).rstrip(b"=").decode("ascii")


def _b32_decode(s: str) -> bytes:
    s = s.upper()
    pad = "=" * (-len(s) % 8)
    return base64.b32decode(s + pad)


def generate_key(
    customer: str,
    max_devices: int | None = None,
    days: int | None = None,
    expiry: date | None = None,
    nonce: str | None = None,
    private_seed: bytes | None = None,
) -> str:
    """Build a license key (admin only — needs the private seed).

    Tests inject ``private_seed=`` directly; production code reads
    it from ``.private_key`` on disk.
    """
    if "|" in customer:
        raise ValueError("customer name cannot contain '|'")
    if max_devices is None:
        max_devices = BRAND.default_devices_per_key
    if days is None:
        days = BRAND.default_license_days
    max_devices = max(1, min(100, int(max_devices)))
    if expiry is None:
        from datetime import timedelta

        expiry = date.today() + timedelta(days=int(days))
    if nonce is None:
        import secrets

        nonce = secrets.token_hex(3)

    payload = LicensePayload(
        customer=customer.strip(),
        max_devices=max_devices,
        expiry=expiry,
        nonce=nonce,
    ).encode()

    seed = private_seed if private_seed is not None else _load_private_seed()
    payload_bytes = payload.encode("utf-8")
    sig = ed.sign(seed, payload_bytes)

    body_bytes = (
        len(payload_bytes).to_bytes(2, "big") + payload_bytes + sig
    )
    body_enc = _b32_encode(body_bytes)
    return f"{BRAND.license_prefix}-{_hyphenate(body_enc, 4)}"


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
    s = "".join(ch for ch in s.strip() if not ch.isspace())
    s = s.replace("-", "")
    prefix = BRAND.license_prefix
    if s.startswith(prefix):
        s = s[len(prefix) :]
    return s


def verify_key(key: str, public_key: bytes | None = None) -> VerifiedLicense:
    """Decode and verify a license key. Raises LicenseError on any
    problem (bad format, bad signature, malformed payload).

    An *expired* key is returned successfully — callers must check
    ``is_expired`` themselves so the UI can show a "renew" message
    rather than a hard rejection.
    """
    body = _strip_key(key)
    try:
        body_bytes = _b32_decode(body)
    except Exception as e:
        raise LicenseError("malformed key encoding") from e
    if len(body_bytes) < 2 + 64:
        raise LicenseError("key too short")
    plen = int.from_bytes(body_bytes[:2], "big")
    if 2 + plen + 64 != len(body_bytes):
        raise LicenseError("malformed key length header")
    payload_bytes = body_bytes[2 : 2 + plen]
    sig = body_bytes[2 + plen :]

    pub = public_key if public_key is not None else _load_public_key()
    if not ed.verify(pub, payload_bytes, sig):
        raise LicenseError("signature mismatch — key tampered or wrong build")

    try:
        payload = payload_bytes.decode("utf-8")
    except Exception as e:
        raise LicenseError("malformed payload encoding") from e
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
    import hashlib
    import socket
    import uuid

    host = socket.gethostname() or "unknown"
    mac = uuid.getnode()
    raw = f"{host}|{mac}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def save_activation(license_key: str) -> None:
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

    # Fire-and-forget phone-home to the central admin server so the
    # admin panel can show "X PCs activated" without us having to
    # ask customers in Line. Local activation is already persisted
    # above — this network call is best-effort and runs on a
    # background thread so a slow DNS / firewall / captive portal
    # never blocks the activation modal closing.
    try:
        from . import license_server as _ls
        if _ls.is_enabled():
            import threading
            threading.Thread(
                target=_ls.activate,
                args=(license_key,),
                kwargs={"machine_label": _ls._hostname()},
                daemon=True,
                name="license-phone-home",
            ).start()
    except Exception:
        # Never let a server-client bug propagate into the
        # critical "save activation" path.
        pass


def load_activation() -> dict | None:
    if not ACTIVATION_PATH.is_file():
        return None
    try:
        return json.loads(ACTIVATION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_activation() -> None:
    if ACTIVATION_PATH.is_file():
        try:
            ACTIVATION_PATH.unlink()
        except OSError:
            pass


def is_machine_bound(activation: dict) -> bool:
    return activation.get("machine_id") == _machine_id()
