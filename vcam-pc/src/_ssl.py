"""Shared SSL context builder for urllib-based fetches.

Why this exists
---------------

Every customer-side urllib HTTPS call (announcements feed,
auto-update manifest, scrcpy auto-installer, license-server
phone-home) hits the same recurring failure on macOS:

    SSL: CERTIFICATE_VERIFY_FAILED — unable to get local issuer
    certificate

The python.org macOS installer ships *without* a populated CA
trust store. Apple's Keychain isn't linked. The user is expected
to manually run ``Install Certificates.command`` from the Python
install dir, which a meaningful fraction of our non-technical
customer base never does. Shipping ``certifi`` (already a
transitive dep of pip) and routing every TLS call through its
Mozilla CA bundle makes those calls "just work" without any
customer intervention.

Use this everywhere you would otherwise call ``urllib.request.
urlopen(req)`` — pass the returned context as the ``context=``
kwarg.

The context is cached because building it parses ~250 KB of PEM
on each call; for a long-running app that polls feeds every
few minutes the savings add up.
"""

from __future__ import annotations

import logging
import ssl

log = logging.getLogger(__name__)

_cached_ctx: ssl.SSLContext | None = None


def default_context() -> ssl.SSLContext:
    """Return a process-wide ``ssl.SSLContext`` whose trust store
    layers ``certifi``'s Mozilla bundle on top of the OS default.

    Falls back to the OS-default context (no certifi layer) if
    ``certifi`` cannot be imported — better to keep a half-broken
    feature working on Linux/Windows than to crash the app at
    startup on customer machines without certifi.
    """
    global _cached_ctx
    if _cached_ctx is not None:
        return _cached_ctx
    try:
        import certifi
        _cached_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        log.warning(
            "certifi unavailable; falling back to OS default trust "
            "store (TLS verification may fail on python.org macOS)"
        )
        _cached_ctx = ssl.create_default_context()
    return _cached_ctx
