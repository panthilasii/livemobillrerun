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
    "go live", "go-live", "เริ่มไลฟ์", "ไปไลฟ์", "开始直播", "start live",
)
KW_SCREEN_SHARE = (
    "screen share", "share screen", "screenshare", "share your screen",
    "แชร์หน้าจอ", "หน้าจอ", "屏幕共享", "分享屏幕",
)
KW_CONFIRM_START = (
    "start now", "start broadcast", "start", "go live",
    "เริ่มเลย", "เริ่ม", "ตกลง", "อนุญาต", "allow", "ok",
)


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
        or None on failure."""
        ok, _ = self._adb(
            "shell", "uiautomator", "dump", self._dump_path_phone,
            timeout=6.0,
        )
        if not ok:
            return None
        ok, out = self._adb(
            "shell", "cat", self._dump_path_phone, timeout=4.0,
        )
        if not ok:
            return None
        return out

    @staticmethod
    def _find_node(xml: str, keywords: tuple[str, ...]) -> Optional[tuple[int, int]]:
        """Return the centre (x,y) of the first clickable node whose
        `text=` or `content-desc=` matches any keyword (case-insensitive
        substring). Returns None if no match."""
        # Each node is a single line in the dump; we walk them.
        # Use a fairly forgiving regex — TikTok's hierarchy can include
        # nodes without bounds (e.g. dividers).
        pattern = re.compile(
            r'(?:text|content-desc)="([^"]+)"[^>]*?'
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            re.IGNORECASE,
        )
        for label, x1, y1, x2, y2 in pattern.findall(xml):
            low = label.lower()
            if any(kw.lower() in low for kw in keywords):
                return (int(x1) + int(x2)) // 2, (int(y1) + int(y2)) // 2
        return None

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

        # Some TikTok builds open on a "For You" page; the bottom nav
        # has a [+] in the middle. We skip looking for [+] and rely
        # on the "Live" tab being reachable from the create screen.
        time.sleep(2.0)

        # 2. find "Live" tab ──────────────────────────────────
        # On most builds the bottom nav has 5 icons; the middle one is
        # a "+" that opens "Camera / Live / …" tab. To save a tap, we
        # send a deep-link if one matches the package.
        deep_link_attempts = (
            f"snssdk1180://live",       # trill
            f"snssdk1233://live",       # musically
        )
        for url in deep_link_attempts:
            ok, _ = self._adb(
                "shell", "am", "start", "-a", "android.intent.action.VIEW",
                "-d", url, timeout=4.0,
            )
            if ok:
                time.sleep(1.5)
                break

        live_xy = self._find_or_scroll(KW_LIVE_TAB, "Live tab")
        if live_xy is None:
            r = StepResult("live_tab", False, "Live tab not found — tap '+' in TikTok manually")
            results.append(r)
            self._emit("✗ Live tab not located; user-tap [+] then re-run")
            return results
        self._tap(*live_xy)
        results.append(StepResult("live_tab", True, f"@({live_xy[0]},{live_xy[1]})"))
        self._emit(f"→ tapped Live tab @{live_xy}")

        # 3. "Go Live" / "Start Live" button ─────────────────
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

        # 4. "Screen Share" mode ─────────────────────────────
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

        # 5. "Start Now" — only if user opted in ─────────────
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
