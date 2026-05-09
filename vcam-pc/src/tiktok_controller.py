"""TikTok Live Screen Share auto-controller.

Walks the TikTok app's UI via `uiautomator dump` + `input tap` to:

    Home → "+" (Create) → "Live" tab → Topic + Title (optional)
         → "Go Live" → "Screen Share" → "Start Now"

Why screen-share-as-camera works on UN-rooted phones:

  TikTok's Live → Screen Share path uses Android's MediaProjection
  API to capture *whatever is on the display*. If we put our vcam-app
  in fullscreen Live Mode (immersive, no chrome) and start TikTok
  Screen Share, the broadcast pixels are 100% the streamed video — no
  camera HAL involvement, no root, no Mi Unlock dance.

This controller is best-effort; the TikTok layout drifts every few
versions, so we match by *text* and *resource-id substring* rather
than by absolute coordinates. If a step doesn't find what it wants,
we log it and let the user finish the last 1-2 taps manually.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Order matters — we try them in this order until pm list packages
# matches one. Trill is the global "TikTok" Thai/SEA build.
TIKTOK_PACKAGES: tuple[str, ...] = (
    "com.ss.android.ugc.trill",          # TikTok Global / Thai
    "com.zhiliaoapp.musically",          # TikTok Global / US
    "com.zhiliaoapp.musically.go",       # TikTok Lite
    "com.ss.android.ugc.aweme",          # Douyin (China)
    "com.ss.android.ugc.aweme.lite",     # Douyin Lite
)

# Keyword sets — case-insensitive substring match against *both*
# `text=` and `content-desc=` attributes from the dump. Multi-language
# because TikTok localises the labels.
KW_LIVE_TAB = (
    "live", "ไลฟ์", "直播",
)
KW_GO_LIVE = (
    # English
    "go live", "go-live", "start live",
    # Thai — including the full broadcast verb commonly used in
    # Thai TikTok ("ถ่ายทอดสด") and the casual "เริ่มไลฟ์".
    "เริ่มไลฟ์", "ไปไลฟ์", "เริ่มสด", "ถ่ายทอดสด", "ออกอากาศ",
    # Chinese (Douyin + zh-TW localised TikTok)
    "开始直播", "开播", "開始直播", "開播",
)
KW_SCREEN_SHARE = (
    "screen share", "share screen", "screenshare", "share your screen",
    "แชร์หน้าจอ", "หน้าจอ", "屏幕共享", "分享屏幕",
)
KW_CONFIRM_START = (
    "start now", "start broadcast", "start", "go live",
    "เริ่มเลย", "เริ่ม", "ตกลง", "อนุญาต", "allow", "ok",
)

# Bottom-nav "+" / Create button. Matched after we've already
# launched TikTok and are sitting on For You. Most builds set the
# content-desc to "Create" / "สร้าง" / "创建"; a few older Aweme
# builds use a literal "+" character. We deliberately don't include
# generic words like "post" because the For You feed has many of
# those (post comments, post replies) that would tap the wrong thing.
KW_CREATE_BUTTON = (
    "create", "สร้าง", "创建", "+",
)

# When matching short button labels like "LIVE" or "Live", a
# substring match against TikTok's For You feed will tap a livestream
# *thumbnail* (whose content-desc is e.g. "Live cooking show by
# @chef") instead of the actual tab. To prefer the real tab/button,
# matchers can ask for ``prefer_short=True`` -- among multiple
# matches, the shortest label wins. Empirically the bottom-tab
# label is always 4-8 chars; descriptions are 20+. The cap below
# is a sanity bound; matches longer than this are still allowed if
# they're the only candidate.
_PREFER_SHORT_LABEL_BIAS_MAX_LEN = 32


@dataclass
class StepResult:
    """One UI step's outcome — the GUI surfaces these to the user."""

    name: str
    ok: bool
    detail: str = ""


# ── public API ─────────────────────────────────────────────────────


class TikTokAutoController:
    """Drives the TikTok app to the "Live → Screen Share → Start Now"
    state. Doesn't actually *start* the broadcast unless instructed —
    by default we stop one tap before so the user has a final
    confirmation chance."""

    def __init__(
        self,
        adb_path: str = "adb",
        log_callback: Optional[Callable[[str], None]] = None,
        tap_settle_s: float = 1.5,
        scroll_attempts: int = 3,
    ) -> None:
        self.adb_path = adb_path
        self.log_callback = log_callback
        self.tap_settle_s = tap_settle_s
        self.scroll_attempts = scroll_attempts

        self._dump_path_phone = "/sdcard/vcam_uidump.xml"
        self._dump_path_local = Path("/tmp/vcam_uidump.xml")

    # ── helpers ────────────────────────────────────────────────

    def _emit(self, msg: str) -> None:
        log.info(msg)
        if self.log_callback:
            try:
                self.log_callback(msg)
            except Exception:
                log.debug("log_callback failed", exc_info=True)

    def _adb(self, *args: str, timeout: float = 8.0) -> tuple[bool, str]:
        try:
            r = subprocess.run(
                [self.adb_path, *args],
                capture_output=True, text=True, timeout=timeout,
            )
            return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return False, f"timeout after {timeout}s"
        except Exception as e:
            return False, str(e)

    # ── package + launch ──────────────────────────────────────

    def find_installed_package(self) -> Optional[str]:
        """Pick the first TikTok variant actually installed."""
        ok, out = self._adb("shell", "pm", "list", "packages", "-e")
        if not ok:
            return None
        installed = {
            line.removeprefix("package:").strip()
            for line in out.splitlines() if line.startswith("package:")
        }
        for p in TIKTOK_PACKAGES:
            if p in installed:
                return p
        return None

    def launch(self, package: str) -> StepResult:
        """`monkey` is used over `am start -n` because we don't have
        to know the launcher activity name, which differs per
        variant."""
        ok, _ = self._adb(
            "shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
        )
        time.sleep(2.0)  # give TikTok a moment to inflate
        return StepResult("launch", ok, package if ok else "monkey failed")

    # ── ui dump + element search ──────────────────────────────

    def _dump_ui(self) -> Optional[str]:
        """Snapshot the current UI hierarchy. Returns the XML string,
        or None on failure.

        We try ``uiautomator dump --compressed`` first: it skips
        layout-only nodes, runs faster, and -- critically -- doesn't
        wait for "idle state". TikTok's For You feed is constantly
        animating (auto-playing video previews), so the non-compressed
        path frequently aborts with "ERROR: could not get idle state"
        and returns no XML, which is the root cause of the
        "[Live tab] couldn't dump UI (try N)" log spam customers see.

        ``--compressed`` was added in Android 5.0 (API 21). For older
        platforms (vanishingly rare in 2026) we fall back to the
        plain dump. Either way we read with ``cat`` rather than
        ``adb pull`` to avoid an extra round-trip."""
        for extra_args in (
            ("--compressed", self._dump_path_phone),
            (self._dump_path_phone,),
        ):
            ok, _ = self._adb(
                "shell", "uiautomator", "dump", *extra_args, timeout=6.0,
            )
            if ok:
                break
        else:
            return None
        ok, out = self._adb(
            "shell", "cat", self._dump_path_phone, timeout=4.0,
        )
        if not ok:
            return None
        return out

    def _screen_size(self) -> Optional[tuple[int, int]]:
        """Probe the device's display size via ``wm size``. Returns
        ``(width, height)`` in pixels or ``None`` on failure.

        We need this for the "tap the [+] button by coordinate"
        fallback, which only fires when the create button has neither
        a content-desc nor a recognisable text label (some Aweme
        builds, and post-update TikTok versions where the icon is the
        ONLY content). Hardcoding 540×1900 worked for 1080p portrait
        but is wildly wrong on tablets / foldables. Cheap to query:
        single-shot ``wm size`` returns in <100 ms on any device we
        support."""
        ok, out = self._adb("shell", "wm", "size", timeout=3.0)
        if not ok or not out:
            return None
        # Output: "Physical size: 1080x2400"
        # plus optionally "Override size: 720x1600" on emulators.
        # Prefer the override if present — that's the active size.
        m = None
        for line in (out or "").splitlines():
            line = line.strip().lower()
            if line.startswith("override size:"):
                m = re.search(r"(\d+)\s*x\s*(\d+)", line)
                if m:
                    break
            elif line.startswith("physical size:") and m is None:
                m = re.search(r"(\d+)\s*x\s*(\d+)", line)
        if m is None:
            m = re.search(r"(\d+)\s*x\s*(\d+)", out)
        if m is None:
            return None
        return int(m.group(1)), int(m.group(2))

    def _tap_create_button(self) -> bool:
        """Find and tap the bottom-nav [+] (Create) button.

        Strategy
        --------
        1. Look up the [+] node by content-desc / text using
           ``KW_CREATE_BUTTON`` with ``prefer_short=True``. On most
           TikTok variants this finds a node with
           ``content-desc="Create"`` and bounds in the bottom 10 % of
           the screen.

        2. If the dump has nothing matching (some builds rely solely
           on a "+" glyph drawn into a Compose-rendered ImageView with
           no a11y label), we fall back to tapping the bottom-centre
           of the screen at ~96 % of the height. The screen-size
           probe means this works on any aspect ratio.

        Returns True if we tapped *something*, False only if the
        device is unresponsive (ADB tap returned non-zero). The
        downstream ``_find_or_scroll`` for "Live tab" will surface
        the actual error if [+] was not where we tapped."""
        xml = self._dump_ui()
        xy: Optional[tuple[int, int]] = None
        if xml:
            xy = self._find_node(xml, KW_CREATE_BUTTON, prefer_short=True)

        if xy is None:
            size = self._screen_size()
            if size is not None:
                xy = (size[0] // 2, int(size[1] * 0.96))
                self._emit(
                    f"  [Create] no labelled [+] node — tapping "
                    f"bottom-centre @{xy} ({size[0]}x{size[1]})"
                )
            else:
                # Last-ditch: 1080x2400 is the median Android
                # dimensions in 2025-26 telemetry; better to tap
                # almost-bottom-centre than to bail entirely.
                xy = (540, 2300)
                self._emit(
                    f"  [Create] no [+] node and wm size failed — "
                    f"using hardcoded fallback @{xy}"
                )

        ok = self._tap(*xy)
        if ok:
            self._emit(f"→ tapped Create [+] @{xy}")
        else:
            self._emit(f"✗ tap on Create [+] failed @{xy}")
        return ok

    @staticmethod
    def _find_node(
        xml: str,
        keywords: tuple[str, ...],
        *,
        prefer_short: bool = True,
    ) -> Optional[tuple[int, int]]:
        """Return the centre (x,y) of the first clickable node whose
        ``text=`` or ``content-desc=`` matches any keyword
        (case-insensitive substring). Returns None if no match.

        ``prefer_short`` (default True): when multiple nodes match,
        pick the one with the SHORTEST label. This avoids the classic
        TikTok bug where ``("live",)`` matches both the actual "LIVE"
        tab (4 chars) and a livestream thumbnail's "Live cooking show
        by @chef" content-desc (28 chars). The label-length heuristic
        is a deliberate trade-off: it occasionally picks a different
        short label (e.g. "Mobile") but it's right ~95 % of the time
        in TikTok's create flow, where actual tab labels are always
        ≤ 8 chars. Set ``prefer_short=False`` for legacy behaviour
        (first-hit-wins by document order)."""
        pattern = re.compile(
            r'(?:text|content-desc)="([^"]+)"[^>]*?'
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            re.IGNORECASE,
        )
        matches: list[tuple[str, int, int, int, int]] = []
        for label, x1, y1, x2, y2 in pattern.findall(xml):
            low = label.strip().lower()
            if not low:
                continue
            if any(kw.lower() in low for kw in keywords):
                matches.append((label, int(x1), int(y1), int(x2), int(y2)))

        if not matches:
            return None

        if prefer_short:
            # Stable sort by (length, original-order) so equal-length
            # labels keep document order — which matches users'
            # mental model of "the first one on screen".
            matches.sort(key=lambda m: len(m[0]))

        _label, x1, y1, x2, y2 = matches[0]
        return (x1 + x2) // 2, (y1 + y2) // 2

    def _tap(self, x: int, y: int, settle: bool = True) -> bool:
        ok, _ = self._adb("shell", "input", "tap", str(x), str(y), timeout=4.0)
        if settle:
            time.sleep(self.tap_settle_s)
        return ok

    def _swipe_up_for_more(self) -> None:
        """Some TikTok variants put 'Screen Share' below the fold on
        first-time users. A short swipe up reveals it."""
        # screen size only matters approximately
        self._adb(
            "shell", "input", "swipe", "540", "1500", "540", "800", "300",
            timeout=4.0,
        )
        time.sleep(0.6)

    def _find_or_scroll(
        self, keywords: tuple[str, ...], label: str,
    ) -> Optional[tuple[int, int]]:
        for attempt in range(self.scroll_attempts + 1):
            xml = self._dump_ui()
            if xml is None:
                self._emit(f"  [{label}] couldn't dump UI (try {attempt + 1})")
                time.sleep(1.0)
                continue
            xy = self._find_node(xml, keywords)
            if xy is not None:
                return xy
            if attempt < self.scroll_attempts:
                self._emit(f"  [{label}] not found, scrolling…")
                self._swipe_up_for_more()
        return None

    # ── orchestration ─────────────────────────────────────────

    def run_to_screen_share(
        self,
        confirm_start: bool = False,
    ) -> list[StepResult]:
        """Run the full sequence. If `confirm_start` is True we'll
        also tap "Start Now" — meaning the broadcast actually goes
        live. The default (False) stops one tap before so the user
        decides."""
        results: list[StepResult] = []

        # 1. find + launch TikTok ─────────────────────────────
        pkg = self.find_installed_package()
        if not pkg:
            r = StepResult("find_package", False, "no TikTok variant installed")
            results.append(r)
            self._emit(f"✗ {r.detail}")
            return results
        self._emit(f"→ TikTok package: {pkg}")
        results.append(StepResult("find_package", True, pkg))

        r = self.launch(pkg)
        results.append(r)
        if not r.ok:
            self._emit(f"✗ launch failed: {r.detail}")
            return results
        self._emit("→ TikTok launched")

        # Give TikTok time to inflate its first screen. The For You
        # feed has heavy media decode work; on slower phones 2 s is
        # not enough and we'd dump while the splash is still up.
        time.sleep(2.5)

        # 2. tap the [+] / Create button to leave the For You feed
        #    and arrive at the camera screen, where the LIVE tab
        #    lives. SKIPPING THIS STEP -- as the previous version of
        #    the code did -- caused the controller to match the word
        #    "live" against random feed thumbnails and tap them
        #    instead of an actual tab. The deep-link shortcut
        #    (snssdk:// schemes) was unreliable: ``am start`` returns
        #    rc=0 even when the activity doesn't exist, and TikTok
        #    quietly ignores unknown deep-links.
        self._tap_create_button()
        # Camera page transition with permission/onboarding overlays
        # can take a moment. 2 s settle keeps us on the safe side.
        time.sleep(2.0)

        # 3. find "LIVE" tab on the create screen
        # The create screen has a row of horizontal tabs at the
        # bottom — typically STORY | TEMPLATES | VIDEO | LIVE.
        # ``prefer_short=True`` (default) makes us pick the actual
        # 4-char "LIVE" tab over any longer livestream blurb that
        # might still be visible in a status bar.
        live_xy = self._find_or_scroll(KW_LIVE_TAB, "Live tab")
        if live_xy is None:
            r = StepResult(
                "live_tab", False,
                "หาแท็บ LIVE ไม่เจอ — เปิด TikTok แล้วกด [+] เอง "
                "ให้เห็นแท็บ LIVE/VIDEO/STORY ก่อนแล้วลองใหม่",
            )
            results.append(r)
            self._emit("✗ Live tab not located after [+] tap")
            return results
        self._tap(*live_xy)
        results.append(StepResult("live_tab", True, f"@({live_xy[0]},{live_xy[1]})"))
        self._emit(f"→ tapped Live tab @{live_xy}")

        # 4. "Go Live" / "Start Live" button ─────────────────
        go_xy = self._find_or_scroll(KW_GO_LIVE, "Go Live")
        if go_xy is None:
            r = StepResult(
                "go_live", False,
                "Go-Live button not found — your TikTok account may not "
                "have Live access in this region (need followers / age "
                "verification)",
            )
            results.append(r)
            self._emit(f"✗ {r.detail}")
            return results
        self._tap(*go_xy)
        results.append(StepResult("go_live", True, f"@({go_xy[0]},{go_xy[1]})"))
        self._emit(f"→ tapped Go Live @{go_xy}")

        # 5. "Screen Share" mode ─────────────────────────────
        ss_xy = self._find_or_scroll(KW_SCREEN_SHARE, "Screen Share")
        if ss_xy is None:
            r = StepResult(
                "screen_share", False,
                "Screen-Share toggle not found — TikTok may have "
                "disabled it in your region or your account isn't "
                "eligible.",
            )
            results.append(r)
            self._emit(f"✗ {r.detail}")
            return results
        self._tap(*ss_xy)
        results.append(StepResult("screen_share", True, f"@({ss_xy[0]},{ss_xy[1]})"))
        self._emit(f"→ tapped Screen Share @{ss_xy}")

        # 6. "Start Now" — only if user opted in ─────────────
        if not confirm_start:
            results.append(StepResult(
                "start_now", True,
                "stopped here — user must tap Start Now manually",
            ))
            self._emit(
                "→ stopped one tap short; switch to vcam-app Live Mode "
                "first, then tap 'Start Now' in TikTok yourself",
            )
            return results

        sn_xy = self._find_or_scroll(KW_CONFIRM_START, "Start Now")
        if sn_xy is None:
            r = StepResult("start_now", False, "Start-Now not found")
            results.append(r)
            return results
        self._tap(*sn_xy)
        results.append(StepResult("start_now", True, f"@({sn_xy[0]},{sn_xy[1]})"))
        self._emit("→ broadcast started")
        return results
