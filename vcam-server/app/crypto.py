"""License key signing + revocation list signing.

This module is a thin shim over the vendored Ed25519 implementation.
It exists so:

* The signing seed is loaded once at import and cached. ``vcam-pc``
  does the same with a per-call read; we don't because the server
  signs many keys per minute under load and disk I/O adds up.

* All "license key string" formatting lives in one place. The
  format is **byte-for-byte identical** to ``vcam-pc/src/license_key.py``
  so a key issued here verifies on a customer build using the same
  embedded public key. If we diverge from that format, every shipped
  customer build instantly stops accepting our keys — so this file
  has tests that pin the encoding.

Format recap
------------

A license key looks like:

    888-AAAA-BBBB-CCCC-…

After stripping the prefix and hyphens, the body is base32 of:

    plen:2bytes_be || payload || sig64

Where ``payload`` is plain UTF-8 of:

    customer_name|max_devices|expiry_iso|nonce

Any change to that grammar breaks every shipped build.
"""
from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import Lock

from . import _ed25519 as ed
from .config import SETTINGS


# ── seed cache ──────────────────────────────────────────────────────


_seed_lock = Lock()
_cached_seed: bytes | None = None
_cached_pub: bytes | None = None


class CryptoError(Exception):
    """Raised on missing/malformed key material."""


def _load_seed() -> bytes:
    """Return the Ed25519 signing seed, lazily reading from disk.

    The seed is ``vcam-server/data/.private_key`` by default but
    can be relocated via the ``SIGNING_KEY_PATH`` env var (Docker
    deployments mount it at e.g. ``/run/secrets/signing_key`` so
    the rest of the data dir can be world-readable for backups
    while the seed stays ``chmod 600``)."""
    global _cached_seed, _cached_pub
    with _seed_lock:
        if _cached_seed is not None:
            return _cached_seed
        path: Path = SETTINGS.signing_key_path
        if not path.is_file():
            raise CryptoError(
                f"signing seed missing at {path} — run "
                "`python -m app.cli init-keys` once on this server"
            )
        text = path.read_text(encoding="utf-8").strip()
        if len(text) != 64:
            raise CryptoError(
                "malformed signing seed (expected 64 hex chars)"
            )
        try:
            seed = bytes.fromhex(text)
        except ValueError as exc:
            raise CryptoError("signing seed is not valid hex") from exc
        _cached_seed = seed
        # Derive + cache the matching public key. We never read the
        # public key from disk; the disk file is informational only
        # (we write it at init-keys time so the operator has a copy
        # to paste into vcam-pc/src/_pubkey.py).
        _cached_pub = ed.keypair_from_seed(seed)[1]
        return seed


def public_key() -> bytes:
    """Return the 32-byte Ed25519 public key derived from the seed.

    Cached after first ``_load_seed`` call. The hex form is what
    ships in ``vcam-pc/src/_pubkey.py`` so customer builds can
    verify keys we issue here.
    """
    if _cached_pub is None:
        _load_seed()
    assert _cached_pub is not None  # for type-checker
    return _cached_pub


def public_key_hex() -> str:
    return public_key().hex()


def init_new_keypair(force: bool = False) -> tuple[Path, str]:
    """Generate a fresh Ed25519 keypair and write both halves to
    disk under the configured paths. Returns ``(seed_path, pub_hex)``.

    ``force=True`` overwrites an existing seed file. The default
    refuses to clobber so a stray ``init-keys`` invocation can't
    accidentally invalidate every shipped key.
    """
    seed_path = SETTINGS.signing_key_path
    pub_path = SETTINGS.public_key_path

    if seed_path.is_file() and not force:
        raise CryptoError(
            f"signing seed already exists at {seed_path} — pass "
            "--force to overwrite (this invalidates every key "
            "previously issued under the old seed)"
        )

    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed = ed.random_seed()
    pub = ed.keypair_from_seed(seed)[1]

    seed_path.write_text(seed.hex() + "\n", encoding="utf-8")
    try:
        # 0o600 — readable by owner only. Best effort; some FS (FAT
        # / Windows) ignore mode bits.
        seed_path.chmod(0o600)
    except OSError:
        pass

    pub_path.write_text(pub.hex() + "\n", encoding="utf-8")

    # Force the cache to refresh from disk on next call.
    global _cached_seed, _cached_pub
    with _seed_lock:
        _cached_seed = None
        _cached_pub = None

    return seed_path, pub.hex()


# ── license key encoding ────────────────────────────────────────────


@dataclass(frozen=True)
class LicensePayload:
    """The 4-tuple we encode + sign as a license key.

    Mirrors ``vcam-pc/src/license_key.py:LicensePayload`` field-for-
    field. The customer's verify path expects this exact shape; do
    not add or reorder fields without bumping a format version
    everywhere.
    """

    customer: str
    max_devices: int
    expiry: date
    nonce: str

    def encode(self) -> str:
        return (
            f"{self.customer}|{self.max_devices}|"
            f"{self.expiry.isoformat()}|{self.nonce}"
        )


def _hyphenate(s: str, group: int = 4) -> str:
    return "-".join(s[i : i + group] for i in range(0, len(s), group))


def _b32_encode(b: bytes) -> str:
    return base64.b32encode(b).rstrip(b"=").decode("ascii")


def issue_key(
    customer: str,
    max_devices: int,
    expiry: date,
    nonce: str | None = None,
) -> tuple[str, LicensePayload]:
    """Sign a fresh license key. Returns ``(key, payload)``.

    The payload is returned alongside the key so the caller (the
    DB write path) can persist the structured fields without
    re-decoding the string. We deliberately don't insert into the
    DB here — this module is pure crypto; persistence is in
    ``routes/admin_licenses.py``.
    """
    if "|" in customer:
        raise CryptoError("customer name may not contain '|'")
    customer = customer.strip()
    if not customer:
        raise CryptoError("customer name may not be empty")
    if max_devices < 1 or max_devices > 100:
        raise CryptoError("max_devices must be 1..100")
    if nonce is None:
        nonce = secrets.token_hex(3)

    payload = LicensePayload(
        customer=customer,
        max_devices=int(max_devices),
        expiry=expiry,
        nonce=nonce,
    )

    seed = _load_seed()
    payload_bytes = payload.encode().encode("utf-8")
    sig = ed.sign(seed, payload_bytes)
    body = (
        len(payload_bytes).to_bytes(2, "big") + payload_bytes + sig
    )
    body_b32 = _b32_encode(body)
    key = f"{SETTINGS.license_prefix}-{_hyphenate(body_b32, 4)}"
    return key, payload


# ── revocation list signing ─────────────────────────────────────────


def sign_blob(payload: bytes) -> bytes:
    """Sign an arbitrary byte blob with the server's seed.

    Used for the revocation list and any future signed-manifest
    endpoint. Returns the 64-byte raw Ed25519 signature; callers
    are responsible for bundling the signature with the payload
    in whatever envelope format they need.
    """
    return ed.sign(_load_seed(), payload)


def verify_blob(payload: bytes, sig: bytes) -> bool:
    """Convenience wrapper used by tests."""
    return ed.verify(public_key(), payload, sig)
