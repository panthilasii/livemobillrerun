"""NP Create announcement / news feed -- client side.

Architecture
------------

Customer app pulls a signed JSON manifest from a fixed URL every
``POLL_INTERVAL_S`` minutes. Each announcement carries:

* a unique ``id`` (used to track dismiss state locally)
* a Thai-language title + body
* an optional ``min_version`` / ``max_version`` filter so we can
  target patches at specific releases (e.g. "TikTok update broke
  audio in 1.4.3, please upgrade to 1.5.0")
* an ``expires_at`` ISO timestamp -- once past, the client hides
  the message regardless of whether the customer dismissed it
* an optional ``severity`` (info / warning / critical)

The whole document is **signed with the admin Ed25519 private
key**. We reuse the same keypair as the licensing system, since:

* admin already manages it via ``tools/init_keys.py``
* the customer build already ships ``_pubkey.py``
* an attacker who can hijack DNS / inject HTTP responses still
  cannot forge announcements without the signing seed

Why pull, not push
------------------

WebSocket / SSE drops on NAT timeout, weak WiFi, hotel networks
and mobile tethers -- the customer base. HTTP poll on a 30-minute
interval costs a fraction of a request per customer per day and
survives every network we've seen in the field. When we do stand
up the dashboard webapp later, we can mount the same JSON at the
same URL and nothing on the desktop side has to change.

Failure modes (all silent)
--------------------------

The announcement subsystem is **best effort**. Anything from
"DNS down" to "JSON malformed" to "signature wrong" results in a
log line and a no-op for the UI -- the program must NEVER refuse
to launch because announcements failed. Customers running offline
or behind firewalls just don't see news; the live-streaming code
path is unaffected.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import _ed25519
from ._pubkey import PUBLIC_KEY_HEX

log = logging.getLogger(__name__)

# Where the signed JSON lives. Override via ``NP_ANNOUNCEMENT_URL``
# environment variable for staging tests; admin will swap this to
# the real CDN URL once the dashboard server is provisioned.
DEFAULT_URL = "https://npcreate.github.io/announcements/feed.json"

# How often the background thread polls. 30 min hits the sweet spot
# of "customer sees emergency news within an hour of publish" and
# "we don't hammer the CDN". Cheaper than one request per customer
# per active session.
POLL_INTERVAL_S = 30 * 60

# Hard cap on download size -- malicious feeds can't OOM us.
MAX_FEED_BYTES = 1 * 1024 * 1024   # 1 MB

# Where to remember which announcements the user dismissed. Lives
# next to the customer's ``customer_devices.json`` so it survives
# upgrades but is wiped on uninstall via the [UninstallDelete]
# stanza in installer.iss.
_DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "announcements_state.json"
)


SEVERITY_LEVELS = ("info", "warning", "critical")


@dataclass(frozen=True)
class Announcement:
    """One announcement after parsing + validation."""

    id: str
    title: str
    body: str
    severity: str = "info"
    min_version: str | None = None
    max_version: str | None = None
    expires_at: str | None = None  # ISO 8601, optional
    action_label: str | None = None
    action_url: str | None = None
    published_at: str | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except ValueError:
            log.warning("bad expires_at on %s: %r", self.id, self.expires_at)
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        ref = now or datetime.now(timezone.utc)
        return ref >= exp

    def applies_to_version(self, version: str) -> bool:
        """``version`` is the running app's BRAND.version (e.g.
        ``"1.4.6"``). We compare as tuple of ints; anything that
        won't parse just means "show it" (don't gate on a parse
        bug)."""
        if self.min_version and not _ge(version, self.min_version):
            return False
        if self.max_version and not _le(version, self.max_version):
            return False
        return True


def _parse(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            return ()
    return tuple(parts)


def _ge(a: str, b: str) -> bool:
    pa, pb = _parse(a), _parse(b)
    if not pa or not pb:
        return True
    return pa >= pb


def _le(a: str, b: str) -> bool:
    pa, pb = _parse(a), _parse(b)
    if not pa or not pb:
        return True
    return pa <= pb


# ── feed download + verify ───────────────────────────────────────


class FeedError(Exception):
    """Raised when the feed download or verification fails."""


def _http_get(url: str, timeout: float = 8.0) -> bytes:
    """Fetch a URL with a short timeout and a size cap. Raises
    ``FeedError`` on every failure mode -- callers never need to
    distinguish socket errors from HTTP errors."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NP-Create-Client/1.0",
            "Accept": "application/json",
        },
    )
    try:
        # Always pass an explicit certifi-backed SSL context so
        # python.org macOS installs (no Keychain hookup) don't
        # fail with "unable to get local issuer certificate".
        # See src/_ssl.py for the full rationale.
        from . import _ssl as _ssl_helper
        ctx = _ssl_helper.default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read(MAX_FEED_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise FeedError(f"fetch failed: {exc}") from exc
    if len(data) > MAX_FEED_BYTES:
        raise FeedError(f"feed too large: > {MAX_FEED_BYTES} bytes")
    return data


def _verify_envelope(envelope: dict) -> dict:
    """Verify the Ed25519 signature wrapping the announcement list.

    Schema::

        {
          "payload": "<base64url-encoded JSON of announcements>",
          "signature": "<hex-encoded Ed25519 signature>",
          "format_version": 1
        }

    Returns the *parsed* payload dict on success; raises
    ``FeedError`` otherwise.
    """
    sig_hex = envelope.get("signature")
    payload_b64 = envelope.get("payload")
    if not sig_hex or not payload_b64:
        raise FeedError("missing signature or payload")

    import base64
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        sig = bytes.fromhex(sig_hex)
    except (ValueError, TypeError) as exc:
        raise FeedError(f"bad encoding: {exc}") from exc

    try:
        pub = bytes.fromhex(PUBLIC_KEY_HEX)
    except ValueError as exc:
        raise FeedError(f"bad pubkey: {exc}") from exc

    if not _ed25519.verify(pub, payload_bytes, sig):
        raise FeedError("signature verification failed")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedError(f"payload not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FeedError("payload not a JSON object")
    return payload


def fetch_feed(url: str = DEFAULT_URL) -> list[Announcement]:
    """Download, verify, and parse the announcement feed.

    Returns an empty list on any failure (logged, never raised) so
    the caller can use this directly without a try/except. The
    network I/O happens on the *calling* thread; pair it with
    ``AnnouncementPoller`` for background polling.
    """
    try:
        raw = _http_get(url)
        envelope = json.loads(raw.decode("utf-8"))
    except (FeedError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.info("announcements: feed unavailable (%s)", exc)
        return []

    try:
        payload = _verify_envelope(envelope)
    except FeedError as exc:
        log.warning("announcements: verify failed: %s", exc)
        return []

    items = payload.get("announcements") or []
    out: list[Announcement] = []
    for raw_item in items:
        try:
            ann = Announcement(
                id=str(raw_item["id"]),
                title=str(raw_item["title"]),
                body=str(raw_item["body"]),
                severity=str(raw_item.get("severity", "info")),
                min_version=raw_item.get("min_version"),
                max_version=raw_item.get("max_version"),
                expires_at=raw_item.get("expires_at"),
                action_label=raw_item.get("action_label"),
                action_url=raw_item.get("action_url"),
                published_at=raw_item.get("published_at"),
            )
        except (KeyError, TypeError) as exc:
            log.warning("announcements: bad item shape: %s", exc)
            continue
        if ann.severity not in SEVERITY_LEVELS:
            ann = Announcement(**{**ann.__dict__, "severity": "info"})
        out.append(ann)
    return out


# ── dismiss / read state ─────────────────────────────────────────


@dataclass
class _State:
    dismissed: set[str] = field(default_factory=set)


def _load_state(path: Path) -> _State:
    if not path.is_file():
        return _State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _State(dismissed=set(data.get("dismissed", [])))
    except (OSError, json.JSONDecodeError):
        log.info("announcements: state file unreadable, resetting")
        return _State()


def _save_state(path: Path, state: _State) -> None:
    try:
        path.write_text(
            json.dumps({"dismissed": sorted(state.dismissed)}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("announcements: cannot persist state: %s", exc)


def filter_visible(
    feed: list[Announcement],
    *,
    app_version: str,
    state_path: Path = _DEFAULT_STATE_PATH,
    now: datetime | None = None,
) -> list[Announcement]:
    """Filter the feed down to announcements the customer should
    actually see right now: not expired, version-applicable, and
    not previously dismissed.
    """
    state = _load_state(state_path)
    out: list[Announcement] = []
    for ann in feed:
        if ann.id in state.dismissed:
            continue
        if ann.is_expired(now):
            continue
        if not ann.applies_to_version(app_version):
            continue
        out.append(ann)
    return out


def dismiss(announcement_id: str, *, state_path: Path = _DEFAULT_STATE_PATH) -> None:
    state = _load_state(state_path)
    state.dismissed.add(announcement_id)
    _save_state(state_path, state)


# ── background poller ────────────────────────────────────────────


class AnnouncementPoller:
    """Background thread that refreshes the feed at a fixed cadence
    and invokes ``on_update`` whenever the *visible* set changes.

    Failure handling: any exception (network, JSON, sig, etc.)
    surfaces only as a log line. The customer never sees a stack
    trace because of an announcement bug.
    """

    def __init__(
        self,
        app_version: str,
        on_update: Callable[[list[Announcement]], None],
        *,
        url: str = DEFAULT_URL,
        interval_s: int = POLL_INTERVAL_S,
        state_path: Path = _DEFAULT_STATE_PATH,
    ) -> None:
        self.app_version = app_version
        self.on_update = on_update
        self.url = url
        self.interval_s = max(60, int(interval_s))
        self.state_path = state_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_visible_ids: tuple[str, ...] = ()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="np-announcements", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def refresh_now(self) -> None:
        """Force an immediate poll. Useful right after the user
        dismisses one announcement -- we can re-check whether the
        next one in queue should pop up."""
        if self._thread and self._thread.is_alive():
            # Wake the sleep loop early via the stop event used as
            # a "tick" semaphore. The flag is cleared by _run on
            # wakeup, see below.
            self._stop.set()
            self._stop.clear()

    def _run(self) -> None:
        # First poll happens immediately so the customer sees any
        # pending announcements at startup, not 30 min later.
        delay = 0.0
        while not self._stop.is_set():
            if delay > 0 and self._stop.wait(timeout=delay):
                break
            try:
                feed = fetch_feed(self.url)
                visible = filter_visible(
                    feed,
                    app_version=self.app_version,
                    state_path=self.state_path,
                )
                ids = tuple(a.id for a in visible)
                if ids != self._last_visible_ids:
                    self._last_visible_ids = ids
                    try:
                        self.on_update(visible)
                    except Exception:
                        log.exception("announcement on_update callback")
            except Exception:
                log.exception("announcement poll iteration")
            # Slight jitter on the interval avoids the thundering-herd
            # problem when many customers launch at the top of the hour.
            delay = self.interval_s + (time.monotonic() % 30)


__all__ = [
    "Announcement",
    "AnnouncementPoller",
    "DEFAULT_URL",
    "FeedError",
    "fetch_feed",
    "filter_visible",
    "dismiss",
]
