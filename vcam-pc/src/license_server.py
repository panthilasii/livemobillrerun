"""Optional client for the central NP Create admin server.

When ``BRAND.license_server_url`` is empty (the default for legacy
builds and air-gapped customers), every function in this module is
a no-op that returns ``None``. The desktop app keeps working in
fully offline mode — license verification is always performed
locally via Ed25519, with or without server contact.

When the URL is set, this module:

* Notifies the server on first activation so the admin panel knows
  about the customer install ("phone home").
* Periodically polls the **signed** revocation list and shuts the
  app down if the customer's nonce appears on it.
* Provides a one-call upload helper for the "ส่ง Log ให้แอดมิน"
  button — sends the diagnostic ZIP straight to the admin inbox
  instead of asking the customer to email/Line it manually.

Failure mode: every call is **fail-open**. A server outage,
firewall block, captive portal, or DNS miss must NEVER stop the
customer from using the app — they paid for offline-capable
software. Failures are logged + swallowed; the calling code
treats ``None`` / ``False`` as "skip, try again later".
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import _ed25519 as ed
from .branding import BRAND
from .license_key import _machine_id

log = logging.getLogger(__name__)

# Network timeout for any single call. We default short — a stuck
# server must not block the UI thread for more than a couple of
# seconds. Heavy operations (support upload) override this.
DEFAULT_TIMEOUT_SEC = 5.0


def is_enabled() -> bool:
    """``True`` iff the customer build has a server URL configured.

    Used by callers to skip work entirely (no thread spawn, no log
    line) on builds without a server."""
    return bool((BRAND.license_server_url or "").strip())


def _server_url(path: str) -> Optional[str]:
    base = (BRAND.license_server_url or "").rstrip("/")
    if not base:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _post_json(
    path: str, body: dict, timeout: float = DEFAULT_TIMEOUT_SEC,
) -> Optional[dict]:
    url = _server_url(path)
    if url is None:
        return None
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "content-type": "application/json",
            "user-agent": f"NPCreate/{BRAND.version}",
        },
    )
    try:
        # certifi-backed context — see src/_ssl.py for rationale.
        from . import _ssl as _ssl_helper
        ctx = _ssl_helper.default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.info(
            "license_server POST %s → HTTP %s (fail-open)", path, e.code,
        )
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        log.info("license_server POST %s → network error: %s", path, e)
    except (ValueError, OSError) as e:
        log.info("license_server POST %s → parse/io error: %s", path, e)
    return None


def _get_json(
    path: str, timeout: float = DEFAULT_TIMEOUT_SEC,
) -> Optional[dict]:
    url = _server_url(path)
    if url is None:
        return None
    req = urllib.request.Request(
        url, method="GET",
        headers={"user-agent": f"NPCreate/{BRAND.version}"},
    )
    try:
        from . import _ssl as _ssl_helper
        ctx = _ssl_helper.default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.info(
            "license_server GET %s → HTTP %s (fail-open)", path, e.code,
        )
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        log.info("license_server GET %s → network error: %s", path, e)
    except (ValueError, OSError) as e:
        log.info("license_server GET %s → parse/io error: %s", path, e)
    return None


# ── activation ──────────────────────────────────────────────────────


@dataclass
class ActivationResult:
    ok: bool
    license_id: Optional[int] = None
    customer: str = ""
    max_devices: Optional[int] = None
    expiry: str = ""
    status: str = ""           # 'active' | 'revoked' | ...
    error: str = ""            # populated on ok=False


def activate(license_key: str, machine_label: str = "") -> ActivationResult:
    """Notify the server of a fresh activation.

    Called from ``license_key.save_activation`` after the local
    write succeeds. Best-effort: returns ``ok=False`` on any
    failure but the local activation is already persisted, so
    the customer can keep using the app offline.
    """
    if not is_enabled():
        return ActivationResult(ok=False, error="server_url_not_configured")

    body = {
        "key": license_key.strip(),
        "machine_id": _machine_id(),
        "machine_label": machine_label or _hostname(),
        "app_version": BRAND.version,
    }
    resp = _post_json("/api/v1/activate", body)
    if resp is None:
        return ActivationResult(ok=False, error="network")
    if not resp.get("ok"):
        return ActivationResult(ok=False, error=str(resp))
    return ActivationResult(
        ok=True,
        license_id=resp.get("license_id"),
        customer=resp.get("customer", ""),
        max_devices=resp.get("max_devices"),
        expiry=resp.get("expiry", ""),
        status=resp.get("status", "active"),
    )


def heartbeat(license_key: str) -> Optional[dict]:
    """Periodic check-in. Returns the server's view of the
    license so the caller can react to a freshly-pushed
    revocation without waiting for the full revocation poll.

    A typical scheduler runs this every 30–60 minutes.
    """
    if not is_enabled():
        return None
    body = {"key": license_key.strip(), "machine_id": _machine_id()}
    return _post_json("/api/v1/heartbeat", body)


# ── revocation list ────────────────────────────────────────────────


def fetch_revocations() -> Optional[set[str]]:
    """Pull the signed revocation list from the server.

    Returns the set of revoked nonces on success, ``None`` on any
    failure. The signature is verified against the **embedded**
    public key (``_pubkey.PUBLIC_KEY_HEX``) — an attacker who
    spoofs the server but doesn't have the signing seed cannot
    forge a malicious revocation list to lock customers out.

    Run on a 6-hour timer; cache the result so we don't rehit the
    network on every license check.
    """
    if not is_enabled():
        return None
    resp = _get_json("/api/v1/revocations")
    if resp is None:
        return None
    manifest = resp.get("manifest", "")
    sig_hex = resp.get("sig", "")
    if not manifest or not sig_hex:
        return None
    try:
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        log.warning("revocations: bad sig hex from server, ignoring")
        return None

    try:
        from . import _pubkey  # type: ignore
        pub = bytes.fromhex(_pubkey.PUBLIC_KEY_HEX.strip())
    except (ImportError, AttributeError, ValueError):
        log.warning("revocations: no embedded public key — refusing list")
        return None

    if not ed.verify(pub, manifest.encode("utf-8"), sig):
        log.warning("revocations: signature mismatch — REJECTING")
        return None

    try:
        body = json.loads(manifest)
    except ValueError:
        log.warning("revocations: manifest not JSON")
        return None
    if body.get("kind") != "npc.revocations.v1":
        log.warning("revocations: unknown manifest kind %r", body.get("kind"))
        return None
    nonces = body.get("nonces", [])
    if not isinstance(nonces, list):
        return None
    return {str(n) for n in nonces}


# ── support log upload ─────────────────────────────────────────────


def upload_support_log(
    zip_path: str,
    license_key: str = "",
    message: str = "",
    timeout: float = 60.0,
) -> Optional[dict]:
    """POST a diagnostic ZIP to ``/api/v1/support/upload``.

    Used by the "📋 ส่ง Log ให้แอดมิน" button. Uses
    multipart/form-data (urllib has no built-in helper, so we hand-
    roll a minimal encoder — keeps ``vcam-pc`` deps zero).
    """
    if not is_enabled():
        return None
    url = _server_url("/api/v1/support/upload")
    if url is None:
        return None

    import os
    import secrets

    boundary = "----NPCBoundary" + secrets.token_hex(8)

    try:
        with open(zip_path, "rb") as fh:
            zip_bytes = fh.read()
    except OSError as e:
        log.warning("upload_support_log: cannot read zip %s: %s", zip_path, e)
        return None

    fname = os.path.basename(zip_path) or "log.zip"

    # Build multipart body manually.
    parts: list[bytes] = []
    for k, v in (("key", license_key.strip()), ("message", message.strip())):
        parts.append(f"--{boundary}\r\n".encode("ascii"))
        parts.append(
            f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8")
        )
        parts.append(v.encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode("ascii"))
    parts.append(
        f'Content-Disposition: form-data; name="log_zip"; '
        f'filename="{fname}"\r\n'.encode("utf-8")
    )
    parts.append(b"Content-Type: application/zip\r\n\r\n")
    parts.append(zip_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    body = b"".join(parts)

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "content-type": f"multipart/form-data; boundary={boundary}",
            "user-agent": f"NPCreate/{BRAND.version}",
        },
    )
    try:
        from . import _ssl as _ssl_helper
        ctx = _ssl_helper.default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            socket.timeout, ConnectionError) as e:
        log.info("upload_support_log: failed: %s", e)
        return None


# ── helpers ────────────────────────────────────────────────────────


def _hostname() -> str:
    """Best-effort hostname for the activation label.

    Used purely as a friendly tag in the admin panel ("DESKTOP-FOO");
    we already have a stable machine_id for entitlement tracking.
    """
    try:
        return socket.gethostname()[:200]
    except Exception:
        return ""
