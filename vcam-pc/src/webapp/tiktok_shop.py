"""TikTok Shop Open Platform API client.

Reference
---------

* Partner portal:   https://partner.tiktokshop.com
* API root (TH):    https://open-api.tiktokglobalshop.com
* OAuth landing:    https://services.tiktokshop.com/open/authorize
* Token exchange:   https://auth.tiktok-shops.com/api/v2/token/get

What this module covers
-----------------------

1. **OAuth landing URL** -- ``authorize_url(state)`` builds the URL
   the seller clicks to authorize our app on their shop.
2. **Token exchange** -- ``exchange_code(code)`` swaps the OAuth
   ``code`` returned in the callback for an ``access_token`` +
   ``refresh_token`` pair.
3. **Refresh** -- ``refresh_access_token(refresh_token)`` rotates
   tokens before they expire (default lifetime: 7 days).
4. **Signed request** -- ``signed_get(path, params, access_token)``
   issues an HMAC-SHA256-signed call to any v202309 API endpoint.
5. **Order list** -- ``list_orders(...)`` is the one endpoint we
   actually consume in v0; everything else can be added when the
   dashboard grows.

Signing algorithm (v202309)
---------------------------

TikTok Shop signs each call with HMAC-SHA256 over the canonical
request string::

    {app_secret}
    {path}
    {sorted_query_string_without_sign_and_access_token}
    {body_or_empty}
    {app_secret}

The result goes into the ``sign`` query parameter. The
``access_token`` and ``sign`` parameters themselves are excluded
from the canonical string -- including them would create a
chicken-and-egg dependency.

Why hand-roll it
----------------

There's no first-party Python SDK for TikTok Shop. The unofficial
ones on PyPI are abandoned or partial. A 200-line stdlib + httpx
implementation is easier to audit + ship inside our installer
than a flaky third-party wheel.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Region-specific API hosts. TikTok Shop is sharded by region so a
# Thai shop's tokens won't authenticate against the SG endpoint and
# vice versa. We default to TH because the entire NP Create user
# base is Thai, but expose ``region`` so re-selling abroad doesn't
# require a code change.
_REGION_HOSTS = {
    "TH": "https://open-api.tiktokglobalshop.com",
    "ID": "https://open-api.tiktokglobalshop.com",
    "MY": "https://open-api.tiktokglobalshop.com",
    "PH": "https://open-api.tiktokglobalshop.com",
    "SG": "https://open-api.tiktokglobalshop.com",
    "VN": "https://open-api.tiktokglobalshop.com",
    "US": "https://open-api.tiktokshop.com",
    "GB": "https://open-api.tiktokshop.com",
}

AUTHORIZE_URL = "https://services.tiktokshop.com/open/authorize"
TOKEN_HOST = "https://auth.tiktok-shops.com"


# ── data classes ────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenSet:
    """Result of a successful token exchange / refresh."""

    access_token: str
    refresh_token: str
    expires_at: int          # epoch seconds (UTC)
    shop_id: str
    shop_name: str
    region: str = "TH"

    @property
    def expires_in_s(self) -> int:
        return max(0, self.expires_at - int(time.time()))


# ── signing ─────────────────────────────────────────────────────


def _canonical_query(params: dict[str, Any]) -> str:
    """Build the sorted ``key1value1key2value2…`` string used by the
    TikTok Shop signing algorithm. Empty values are kept; that
    matches what the reference Java SDK emits, even though the docs
    are vague about it.

    ``access_token`` and ``sign`` are excluded -- including them
    creates the chicken-and-egg dependency mentioned in the module
    docstring.
    """
    keys = sorted(k for k in params.keys() if k not in ("access_token", "sign"))
    out: list[str] = []
    for k in keys:
        v = params[k]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        out.append(f"{k}{v}")
    return "".join(out)


def sign_request(
    *,
    app_secret: str,
    path: str,
    params: dict[str, Any],
    body: str | None = None,
) -> str:
    """HMAC-SHA256 signature for a TikTok Shop v202309 call.

    The format is::

        HMAC_SHA256(app_secret,
            app_secret + path + canonical_query + body + app_secret)

    where ``body`` is the empty string for GETs. Returns the
    signature as a lowercase hex string -- TikTok's gateway is
    case-sensitive on the comparison.
    """
    canon = _canonical_query(params)
    payload = app_secret + path + canon + (body or "") + app_secret
    sig = hmac.new(
        app_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return sig


# ── OAuth ───────────────────────────────────────────────────────


def authorize_url(*, app_key: str, state: str) -> str:
    """Build the URL the seller clicks to grant our app access to
    their TikTok Shop.

    ``state`` should be a random string the caller verifies on the
    callback to defeat CSRF. We don't generate it here -- letting
    the caller decide makes it easy to put state in the desktop
    app's session and check on the redirect handler.
    """
    qs = urllib.parse.urlencode({"app_key": app_key, "state": state})
    return f"{AUTHORIZE_URL}?{qs}"


def exchange_code(
    *,
    app_key: str,
    app_secret: str,
    code: str,
    region: str = "TH",
    timeout: float = 15.0,
) -> TokenSet:
    """Swap the OAuth ``code`` for access + refresh tokens.

    Raises ``httpx.HTTPError`` (network) or ``ValueError`` (TikTok
    returned an error envelope). The caller is responsible for
    persisting the result via ``db.upsert_shop``.
    """
    url = f"{TOKEN_HOST}/api/v2/token/get"
    params = {
        "app_key": app_key,
        "app_secret": app_secret,
        "auth_code": code,
        "grant_type": "authorized_code",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, params=params)
    return _parse_token_envelope(resp, region=region)


def refresh_access_token(
    *,
    app_key: str,
    app_secret: str,
    refresh_token: str,
    region: str = "TH",
    timeout: float = 15.0,
) -> TokenSet:
    """Rotate an expiring access token. Refresh tokens themselves
    last ~30 days and are also rotated -- always persist the new
    refresh_token from the response, never reuse the old one."""
    url = f"{TOKEN_HOST}/api/v2/token/refresh"
    params = {
        "app_key": app_key,
        "app_secret": app_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, params=params)
    return _parse_token_envelope(resp, region=region)


def _parse_token_envelope(resp: httpx.Response, *, region: str) -> TokenSet:
    """Decode the TikTok token envelope, normalising both v1 and
    v2 response shapes.

    TikTok ships two slightly different shapes depending on which
    region the developer registered in. Handling both means we
    don't have to ship region-specific clients.
    """
    try:
        env = resp.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"non-JSON token response: {resp.text[:200]}") from exc

    if env.get("code") not in (0, "0"):
        raise ValueError(
            f"TikTok token error code={env.get('code')} "
            f"msg={env.get('message')}"
        )

    data = env.get("data") or {}
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = int(data.get("access_token_expire_in")
                     or data.get("expire_in") or 0)
    shop_id = (data.get("shop_id_list") or data.get("seller_name")
               or data.get("open_id") or "")
    if isinstance(shop_id, list):
        shop_id = shop_id[0] if shop_id else ""
    shop_name = data.get("seller_name") or data.get("shop_name") or shop_id

    if not (access_token and refresh_token):
        raise ValueError(f"missing tokens in response: {env}")

    return TokenSet(
        access_token=str(access_token),
        refresh_token=str(refresh_token),
        expires_at=int(time.time()) + expires_in - 60,  # safety margin
        shop_id=str(shop_id),
        shop_name=str(shop_name),
        region=region,
    )


# ── high-level client ───────────────────────────────────────────


@dataclass
class TikTokShopClient:
    app_key: str
    app_secret: str
    region: str = "TH"
    timeout: float = 20.0

    @property
    def host(self) -> str:
        return _REGION_HOSTS.get(self.region, _REGION_HOSTS["TH"])

    def signed_get(
        self,
        path: str,
        params: dict[str, Any],
        access_token: str,
        shop_cipher: str | None = None,
    ) -> dict:
        """Issue a signed GET. Adds ``app_key``, ``timestamp``,
        ``sign`` to the query automatically; the caller only
        provides domain-specific params (``page_size``, filters …).
        """
        params = dict(params or {})
        params["app_key"] = self.app_key
        params["timestamp"] = int(time.time())
        if shop_cipher:
            params["shop_cipher"] = shop_cipher
        sig = sign_request(
            app_secret=self.app_secret, path=path, params=params,
        )
        params["sign"] = sig
        params["access_token"] = access_token

        url = f"{self.host}{path}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, params=params)
        try:
            env = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"non-JSON response: {resp.text[:200]}") from exc
        if env.get("code") not in (0, "0"):
            raise ValueError(
                f"TikTok API error code={env.get('code')} "
                f"path={path} msg={env.get('message')}"
            )
        return env.get("data") or {}

    def list_orders(
        self,
        *,
        access_token: str,
        shop_cipher: str | None,
        start_time: int,
        end_time: int,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> dict:
        """Pull a page of orders within ``[start_time, end_time]``
        (epoch seconds). Returns the raw ``data`` dict from the
        envelope so the sync layer can iterate ``next_page_token``
        until exhaustion.
        """
        path = "/order/202309/orders/search"
        params: dict[str, Any] = {
            "page_size": page_size,
            "create_time_ge": start_time,
            "create_time_le": end_time,
        }
        if page_token:
            params["page_token"] = page_token
        return self.signed_get(
            path, params,
            access_token=access_token,
            shop_cipher=shop_cipher,
        )
