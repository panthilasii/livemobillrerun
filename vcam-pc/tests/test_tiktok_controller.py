"""``src.tiktok_controller`` -- the UI driving primitives.

The controller is the brittle part of the live-stream automation:
TikTok ships layout changes every 2-4 weeks and the customer sees
the breakage (≠ unit tests). To buy ourselves room, we cover the
*shape* of the matching logic explicitly so a regression in the
matcher manifests as a failed test rather than a customer ticket.

Key invariants that this module locks in
----------------------------------------

1. **Short labels beat long labels.** When the dump contains both a
   "LIVE" tab (4 chars) and "Live cooking show by @chef" (28 chars),
   a search for the LIVE-tab keywords MUST tap the tab. Without this,
   pressing "เริ่มไลฟ์" on the dashboard taps a random feed thumbnail
   and TikTok stays on For You — which is exactly the bug a customer
   filed before this fix landed.

2. **Empty / whitespace labels are ignored.** Some TikTok builds emit
   ``content-desc=""`` placeholders for spacer views. We must not
   match against the empty string.

3. **wm-size parsing tolerates ‘Override size’ lines.** Emulators and
   developer-options-resized devices produce two lines; the override
   is the active resolution and must win.

4. **The compressed dump is preferred.** Animations on the For You
   feed make the non-compressed dump fail with "could not get idle
   state". The fallback to plain dump exists for old Android, but
   the *first* attempt must use ``--compressed``.

We don't mock subprocess inside _find_node tests (that helper is
pure-XML); for ``_dump_ui`` and ``_screen_size`` we patch ``_adb``
directly so each test is hermetic and runs in <1 ms.
"""
from __future__ import annotations

from typing import Iterator
from unittest.mock import patch

import pytest

from src.tiktok_controller import TikTokAutoController


# ── XML fixtures ────────────────────────────────────────────────


def _node(text: str, x1=0, y1=0, x2=100, y2=100, *, kind: str = "text") -> str:
    """Generate one <node …/> line as ``uiautomator dump`` would.

    ``kind`` lets us toggle between ``text="..."`` and
    ``content-desc="..."`` so tests can pin one vs. the other.
    """
    attr = f'{kind}="{text}"'
    return (
        f'<node index="0" {attr} resource-id="" class="x" '
        f'package="com.tt" checkable="false" checked="false" '
        f'clickable="true" enabled="true" focusable="false" focused="false" '
        f'scrollable="false" long-clickable="false" password="false" '
        f'selected="false" bounds="[{x1},{y1}][{x2},{y2}]" />'
    )


def _xml(*nodes: str) -> str:
    """Wrap node lines in a minimal hierarchy/window envelope."""
    inner = "\n".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
        '<hierarchy rotation="0">'
        '<node index="0" text="" class="root" '
        'package="com.tt" bounds="[0,0][1080,2400]">'
        f'{inner}'
        '</node></hierarchy>'
    )


# ── _find_node ──────────────────────────────────────────────────


class TestFindNodePreferShort:
    """``_find_node(prefer_short=True)`` returns the centre of the
    SHORTEST matching label. This is the core fix for the
    "tap a feed thumbnail instead of the LIVE tab" bug."""

    def test_short_tab_label_beats_long_description(self):
        # Reproduces the customer-facing bug: For You feed is showing
        # a livestream thumbnail with a verbose content-desc, AND the
        # bottom create-screen has a "LIVE" tab. Without prefer_short,
        # the document-order match would tap the thumbnail.
        xml = _xml(
            _node("Live cooking show by @chef", 100, 100, 800, 600, kind="content-desc"),
            _node("LIVE", 900, 2200, 1080, 2300),
        )
        xy = TikTokAutoController._find_node(xml, ("live",))
        # The tab is at (900..1080, 2200..2300) → centre (990, 2250).
        assert xy == (990, 2250)

    def test_no_match_returns_none(self):
        xml = _xml(_node("Photo", 0, 0, 100, 100))
        assert TikTokAutoController._find_node(xml, ("live",)) is None

    def test_empty_label_is_skipped(self):
        # An invisible spacer with content-desc="" must not be
        # treated as the shortest match (length 0).
        xml = _xml(
            _node("", 0, 0, 50, 50, kind="content-desc"),
            _node("LIVE", 900, 2200, 1080, 2300),
        )
        xy = TikTokAutoController._find_node(xml, ("live",))
        assert xy == (990, 2250)

    def test_thai_keywords(self):
        # The Thai TikTok build labels the tab "ไลฟ์".
        xml = _xml(
            _node("ดูไลฟ์ของ @chef ที่กำลังขายของ", 100, 100, 900, 200, kind="content-desc"),
            _node("ไลฟ์", 950, 2200, 1050, 2300),
        )
        xy = TikTokAutoController._find_node(xml, ("ไลฟ์",))
        assert xy == (1000, 2250)

    def test_thai_broadcast_verb_for_go_live(self):
        # Thai TikTok labels the Go-Live button with the formal
        # broadcast verb "ถ่ายทอดสด" and not the casual
        # "เริ่มไลฟ์" we initially shipped. This test pins the
        # expanded keyword set so the regression doesn't sneak
        # back in.
        from src.tiktok_controller import KW_GO_LIVE
        assert "ถ่ายทอดสด" in KW_GO_LIVE
        assert "เริ่มไลฟ์" in KW_GO_LIVE
        # And Douyin's "开播" (Chinese short form) which used to
        # silently fall through:
        assert "开播" in KW_GO_LIVE

    def test_legacy_first_hit_when_prefer_short_false(self):
        # When customers explicitly opt out of the heuristic we keep
        # the original document-order behaviour for backwards
        # compatibility (used by tests that need deterministic order).
        xml = _xml(
            _node("Live cooking show", 100, 100, 800, 600, kind="content-desc"),
            _node("LIVE", 900, 2200, 1080, 2300),
        )
        xy = TikTokAutoController._find_node(xml, ("live",), prefer_short=False)
        # First hit (the long content-desc) wins.
        assert xy == ((100 + 800) // 2, (100 + 600) // 2)


# ── _dump_ui ────────────────────────────────────────────────────


class _FakeAdb:
    """Stand-in for ``TikTokAutoController._adb`` that lets tests
    decide which adb commands succeed and what they return.

    Records every call so assertions can verify *which* dump path
    was tried first."""

    def __init__(
        self,
        *,
        compressed_ok: bool = True,
        plain_ok: bool = True,
        cat_ok: bool = True,
        cat_out: str = "<hierarchy/>",
    ) -> None:
        self.compressed_ok = compressed_ok
        self.plain_ok = plain_ok
        self.cat_ok = cat_ok
        self.cat_out = cat_out
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, timeout: float = 8.0) -> tuple[bool, str]:
        self.calls.append(args)
        if args[:2] == ("shell", "uiautomator") and args[2] == "dump":
            if "--compressed" in args:
                return self.compressed_ok, "" if self.compressed_ok else "ERROR"
            return self.plain_ok, "" if self.plain_ok else "ERROR"
        if args[:2] == ("shell", "cat"):
            return self.cat_ok, (self.cat_out if self.cat_ok else "")
        if args[:2] == ("shell", "wm") and args[2] == "size":
            # Default to a real-world phone resolution.
            return True, "Physical size: 1080x2400"
        return True, ""


class TestDumpUi:
    def test_compressed_path_is_tried_first(self):
        ctrl = TikTokAutoController(adb_path="adb")
        fake = _FakeAdb()
        ctrl._adb = fake  # type: ignore[assignment]

        out = ctrl._dump_ui()

        assert out == "<hierarchy/>"
        # First adb call must be the --compressed dump. If this
        # regresses, customers on TikTok For You feed will go back
        # to seeing "[Live tab] couldn't dump UI" spam.
        first = fake.calls[0]
        assert "--compressed" in first

    def test_falls_back_to_plain_dump_when_compressed_unsupported(self):
        ctrl = TikTokAutoController(adb_path="adb")
        # Old Android: --compressed isn't a flag yet.
        fake = _FakeAdb(compressed_ok=False, plain_ok=True)
        ctrl._adb = fake  # type: ignore[assignment]

        out = ctrl._dump_ui()

        assert out == "<hierarchy/>"
        # Both dumps were tried; cat happened after.
        assert any("--compressed" in c for c in fake.calls)
        assert any(c[:3] == ("shell", "uiautomator", "dump") and "--compressed" not in c for c in fake.calls)
        assert any(c[:2] == ("shell", "cat") for c in fake.calls)

    def test_returns_none_if_both_dumps_fail(self):
        ctrl = TikTokAutoController(adb_path="adb")
        fake = _FakeAdb(compressed_ok=False, plain_ok=False)
        ctrl._adb = fake  # type: ignore[assignment]
        assert ctrl._dump_ui() is None

    def test_returns_none_if_cat_fails(self):
        ctrl = TikTokAutoController(adb_path="adb")
        fake = _FakeAdb(cat_ok=False)
        ctrl._adb = fake  # type: ignore[assignment]
        assert ctrl._dump_ui() is None


# ── _screen_size ────────────────────────────────────────────────


class TestScreenSize:
    @pytest.mark.parametrize("output,expected", [
        ("Physical size: 1080x2400\n", (1080, 2400)),
        ("Physical size: 720x1600\n", (720, 1600)),
        ("Physical size: 1080x1920\n", (1080, 1920)),
        # Override line wins (active developer-options size).
        (
            "Physical size: 1080x2400\nOverride size: 720x1600\n",
            (720, 1600),
        ),
    ])
    def test_parses_wm_size_output(self, output, expected):
        ctrl = TikTokAutoController(adb_path="adb")

        def fake_adb(*args, timeout=8.0):
            return True, output

        ctrl._adb = fake_adb  # type: ignore[assignment]
        assert ctrl._screen_size() == expected

    def test_returns_none_on_adb_failure(self):
        ctrl = TikTokAutoController(adb_path="adb")

        def fake_adb(*args, timeout=8.0):
            return False, ""

        ctrl._adb = fake_adb  # type: ignore[assignment]
        assert ctrl._screen_size() is None

    def test_returns_none_on_garbage_output(self):
        ctrl = TikTokAutoController(adb_path="adb")

        def fake_adb(*args, timeout=8.0):
            return True, "??? unparseable ???"

        ctrl._adb = fake_adb  # type: ignore[assignment]
        assert ctrl._screen_size() is None


# ── _tap_create_button ──────────────────────────────────────────


class TestTapCreateButton:
    """Two-strategy logic: prefer-labelled-node, fall back to
    bottom-centre-by-coords. The fallback is what makes the controller
    survive on stripped-down Aweme builds where the [+] icon has no
    a11y label at all."""

    def _harness(self, *, dump_xml: str | None, screen=(1080, 2400)):
        ctrl = TikTokAutoController(adb_path="adb")
        # Capture every adb call so tests can identify the tap.
        calls: list[tuple[str, ...]] = []

        def fake_adb(*args, timeout=8.0):
            calls.append(args)
            if args[:3] == ("shell", "uiautomator", "dump"):
                return (dump_xml is not None), ""
            if args[:2] == ("shell", "cat"):
                return (dump_xml is not None), (dump_xml or "")
            if args[:2] == ("shell", "wm"):
                return True, f"Physical size: {screen[0]}x{screen[1]}\n"
            if args[:3] == ("shell", "input", "tap"):
                return True, ""
            return True, ""

        ctrl._adb = fake_adb  # type: ignore[assignment]
        return ctrl, calls

    def test_taps_create_node_when_labelled(self):
        # Bottom-nav row with [+] at content-desc="Create".
        xml = _xml(
            _node("Home", 0, 2300, 200, 2400, kind="content-desc"),
            _node("Create", 440, 2300, 640, 2400, kind="content-desc"),
            _node("Profile", 880, 2300, 1080, 2400, kind="content-desc"),
        )
        ctrl, calls = self._harness(dump_xml=xml)

        assert ctrl._tap_create_button() is True

        tap_calls = [c for c in calls if c[:3] == ("shell", "input", "tap")]
        assert len(tap_calls) == 1
        # Centre of (440,2300)-(640,2400) is (540, 2350).
        assert tap_calls[0] == ("shell", "input", "tap", "540", "2350")

    def test_falls_back_to_screen_centre_when_unlabelled(self):
        # Old Aweme build: nothing in the dump matches Create
        # keywords, so we have to tap by coordinate. We tap at
        # ~96 % of the screen height.
        xml = _xml(_node("Home", 0, 2300, 200, 2400, kind="content-desc"))
        ctrl, calls = self._harness(dump_xml=xml, screen=(1080, 2400))

        assert ctrl._tap_create_button() is True

        tap_calls = [c for c in calls if c[:3] == ("shell", "input", "tap")]
        assert len(tap_calls) == 1
        # 1080 / 2 = 540; 2400 * 0.96 = 2304.
        assert tap_calls[0] == ("shell", "input", "tap", "540", "2304")

    def test_dump_failure_still_taps_via_wm_fallback(self):
        # If the dump itself fails, _screen_size still works (it's a
        # different adb command) so we get a coordinate fallback.
        ctrl, calls = self._harness(dump_xml=None, screen=(720, 1600))
        assert ctrl._tap_create_button() is True

        tap_calls = [c for c in calls if c[:3] == ("shell", "input", "tap")]
        assert len(tap_calls) == 1
        # 720 / 2 = 360; 1600 * 0.96 = 1536.
        assert tap_calls[0] == ("shell", "input", "tap", "360", "1536")
