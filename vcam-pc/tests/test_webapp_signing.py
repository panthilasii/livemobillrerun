"""TikTok Shop API request signing.

The signing algorithm has to match TikTok's Java reference SDK
EXACTLY -- a single byte off and every API call returns
``code=10005 sign error``. We pin the canonical-string formation
+ HMAC output here so future "let's clean up the signing helper"
refactors can't quietly break production.
"""
from __future__ import annotations

import hashlib
import hmac

from src.webapp import tiktok_shop


class TestCanonicalQuery:
    def test_keys_are_alpha_sorted(self):
        s = tiktok_shop._canonical_query({"b": "2", "a": "1", "c": "3"})
        assert s == "a1b2c3"

    def test_excludes_sign_and_access_token(self):
        # The signing input MUST NOT include either field --
        # including ``sign`` would create a chicken-and-egg problem,
        # and ``access_token`` is bearer auth, not signing input.
        s = tiktok_shop._canonical_query({
            "app_key": "ak",
            "access_token": "tok-must-not-appear",
            "sign": "old",
            "timestamp": 1234,
        })
        assert "tok-must-not-appear" not in s
        assert "old" not in s
        assert "ak" in s
        assert "1234" in s

    def test_object_value_serialised_as_compact_json(self):
        # Some TikTok endpoints accept JSON-shaped query params; the
        # reference SDK serialises them with no whitespace and sorted
        # keys at this layer. We don't sort *inner* keys (TikTok
        # doesn't), but we DO drop whitespace so the canonical
        # string is byte-stable.
        s = tiktok_shop._canonical_query({"filter": {"a": 1, "b": 2}})
        # No spaces.
        assert " " not in s
        # The value should be present.
        assert '{"a":1,"b":2}' in s


class TestSignRequest:
    def test_matches_reference_hmac(self):
        """Hand-computed reference: HMAC-SHA256(secret, payload)
        where payload = secret + path + canon + body + secret."""
        secret = "abc123"
        path = "/order/202309/orders/search"
        params = {
            "app_key": "ak",
            "timestamp": 1700000000,
            "page_size": 50,
        }
        # Compute expected by hand.
        canon = "app_keyakpage_size50timestamp1700000000"
        payload = secret + path + canon + "" + secret
        expected = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256,
        ).hexdigest()

        got = tiktok_shop.sign_request(
            app_secret=secret, path=path, params=params,
        )
        assert got == expected

    def test_body_changes_signature(self):
        """If a future endpoint uses POST + body, the body must be
        included in the canonical string -- otherwise an attacker
        could swap bodies under a stolen signature."""
        params = {"timestamp": 1}
        a = tiktok_shop.sign_request(
            app_secret="s", path="/x", params=params, body="",
        )
        b = tiktok_shop.sign_request(
            app_secret="s", path="/x", params=params, body="hello",
        )
        assert a != b


class TestAuthorizeURL:
    def test_contains_app_key_and_state(self):
        url = tiktok_shop.authorize_url(app_key="my_app", state="xyz")
        assert url.startswith(tiktok_shop.AUTHORIZE_URL)
        assert "app_key=my_app" in url
        assert "state=xyz" in url


class TestTokenSet:
    def test_expires_in_s_clamps_negative(self):
        ts = tiktok_shop.TokenSet(
            access_token="a", refresh_token="r",
            expires_at=0,
            shop_id="s", shop_name="S",
        )
        # expires_at in the past → 0, never negative.
        assert ts.expires_in_s == 0
