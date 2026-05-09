"""Pure-Python Ed25519 (RFC 8032) — vendored, no external deps.

We deliberately avoid pulling in ``cryptography`` or ``pynacl`` so the
customer build has zero compiled native dependencies (which Windows
users frequently fail to install). Performance is fine for our use
case: license verification runs once per app start.

Adapted from the public-domain reference in RFC 8032 §6 with light
clean-up. Do not use this for high-throughput crypto; for our offline
licence-verification path it's plenty fast.

Public API
~~~~~~~~~~

* ``keypair_from_seed(seed: bytes) -> (priv32, pub32)``
* ``sign(priv32, msg) -> sig64``
* ``verify(pub32, msg, sig64) -> bool``
* ``random_seed() -> bytes`` (32 random bytes)

All keys/sigs are raw bytes; format-level encoding (PEM, base64) is
handled by callers.
"""

from __future__ import annotations

import hashlib
import os
from typing import Tuple

# Curve parameters (Ed25519 / Curve25519).
_p = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_d = (-121665 * pow(121666, _p - 2, _p)) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _to_int_le(b: bytes) -> int:
    return int.from_bytes(b, "little")


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


def _y_from_basepoint() -> int:
    return (4 * pow(5, _p - 2, _p)) % _p


_BY = _y_from_basepoint()
_BX = _x_recover(_BY)
_B = (_BX % _p, _BY % _p)


def _edwards_add(P: Tuple[int, int], Q: Tuple[int, int]) -> Tuple[int, int]:
    x1, y1 = P
    x2, y2 = Q
    den = _d * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * pow(1 + den, _p - 2, _p)
    y3 = (y1 * y2 + x1 * x2) * pow(1 - den, _p - 2, _p)
    return (x3 % _p, y3 % _p)


def _scalar_mult(P: Tuple[int, int], n: int) -> Tuple[int, int]:
    if n == 0:
        return (0, 1)
    Q = _scalar_mult(P, n // 2)
    Q = _edwards_add(Q, Q)
    if n & 1:
        Q = _edwards_add(Q, P)
    return Q


def _encode_point(P: Tuple[int, int]) -> bytes:
    x, y = P
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    out = bytearray(32)
    for i in range(32):
        b = 0
        for j in range(8):
            b |= bits[8 * i + j] << j
        out[i] = b
    return bytes(out)


def _decode_point(s: bytes) -> Tuple[int, int]:
    if len(s) != 32:
        raise ValueError("expected 32-byte point")
    bits = []
    for byte in s:
        for j in range(8):
            bits.append((byte >> j) & 1)
    y = sum(bits[i] << i for i in range(255))
    x = _x_recover(y)
    if (x & 1) != bits[-1]:
        x = _p - x
    P = (x, y)
    if not _on_curve(P):
        raise ValueError("point not on curve")
    return P


def _on_curve(P: Tuple[int, int]) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _p == 0


def _hash_to_scalar(h: bytes) -> int:
    a = 1 << 254
    for i in range(3, 254):
        a += (1 << i) * ((h[i // 8] >> (i % 8)) & 1)
    return a


# ── public API ──────────────────────────────────────────────────────


def random_seed() -> bytes:
    return os.urandom(32)


def keypair_from_seed(seed: bytes) -> Tuple[bytes, bytes]:
    """Return ``(private_seed_32, public_key_32)``.

    The "private key" returned here is the 32-byte seed, *not* the
    expanded 64-byte form some libraries use. Pass the same seed back
    into ``sign()`` to produce signatures.
    """
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed)
    a = _hash_to_scalar(h)
    A = _scalar_mult(_B, a)
    return seed, _encode_point(A)


def sign(priv_seed: bytes, msg: bytes) -> bytes:
    if len(priv_seed) != 32:
        raise ValueError("priv_seed must be 32 bytes")
    h = _sha512(priv_seed)
    a = _hash_to_scalar(h)
    A = _encode_point(_scalar_mult(_B, a))
    r = _to_int_le(_sha512(h[32:64] + msg))
    R = _scalar_mult(_B, r)
    Renc = _encode_point(R)
    k = _to_int_le(_sha512(Renc + A + msg))
    s = (r + k * a) % _L
    return Renc + s.to_bytes(32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(sig) != 64 or len(pub) != 32:
        return False
    try:
        R = _decode_point(sig[:32])
        A = _decode_point(pub)
    except ValueError:
        return False
    s = _to_int_le(sig[32:64])
    if s >= _L:
        return False
    k = _to_int_le(_sha512(sig[:32] + pub + msg))
    lhs = _scalar_mult(_B, s)
    rhs = _edwards_add(R, _scalar_mult(A, k))
    return lhs == rhs
