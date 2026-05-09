"""NP Create -- start / stop a TikTok Live broadcast from the PC.

Why this module exists
----------------------

Customers running 5 phones do not want to walk between handsets
tapping "Go Live" on each one and then walking back at the end of
the session to tap "End live". They want a row of buttons on the
PC dashboard:

    [🔴 เริ่มไลฟ์]   ←→   [⏹ จบไลฟ์ • 00:25:43]

…that drives each phone independently. This module is the ADB +
uiautomator orchestrator that those buttons call into.

How starting works
------------------

We delegate to the existing ``tiktok_controller.TikTokAutoController``
which already walks Home → "+" → Live tab → "Go Live" → "Screen
Share" → "Start Now" via uiautomator dumps + tap simulation. The
key difference vs. the existing wizard step:

* We pass ``confirm_start=True`` so the broadcast actually goes
  live (the wizard stops one tap short to let the user double-
  check).
* We let the caller (``DeviceLibrary.start_live``) record the
  start timestamp BEFORE we start tapping, so a partial failure
  still leaves the customer with a usable timer they can stop
  manually.

How stopping works
------------------

There is no public Android intent for "end TikTok live". We
escalate through three strategies in order of grace:

1. **BACK key + dialog tap** -- send ``KEYCODE_BACK``, dump UI,
   look for the "End live?" / "จบไลฟ์?" confirmation dialog and
   tap "End" / "ตกลง". This matches the user's manual flow and
   gives a clean "broadcast ended" notification to the audience.

2. **Foreground stop** -- ``input keyevent KEYCODE_BACK`` once
   more. Some TikTok builds put the stop confirmation in a
   bottom-sheet rather than a dialog; the second back closes
   it.

3. **Force-stop** -- ``am force-stop <pkg>``. Brutal: TikTok
   loses session state and the audience disconnects abruptly,
   but it always works. Used as a last resort after step 1+2
   produced no observable state change.

The returned ``StopLiveResult`` reports which strategy succeeded
so the UI can warn the customer ("ปิดด้วย Force-Stop -- ผู้ชม
หลุดทันที") on the noisy path.

Threading
---------

All calls in this module are blocking subprocess calls; total
wall-time for ``start_live`` is up to ~25 s (TikTok inflate +
five tap+settle cycles) and ``stop_live`` is up to ~10 s. UI
code MUST run them in a worker thread, never on the Tk loop.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)


# Keyword sets for the End-broadcast confirmation BUTTON inside
# the dialog that pops up after we send KEYCODE_BACK during a
# live. We deliberately keep this narrow -- TikTok's dialog
# typically renders the question as a title ("End live?" /
# "จบไลฟ์ใช่ไหม?") and the action as a separate button
# ("End now" / "ยืนยัน"). A keyword like "end live" alone would
# match the TITLE first and we'd tap a non-clickable label,
# leaving the dialog open. Stick to button-only verbs.
_KW_END_CONFIRM = (
    "end now", "end broadcast", "confirm",
    "จบเลย", "ยืนยัน", "ตกลง",
    "结束直播", "确定",
)


# ── result types ───────────────────────────────────────────────


@dataclass
class StartLiveResult:
    """Outcome of ``start_live``. The customer-friendly summary
    is in ``summary`` (Thai); ``steps`` is the trace from the
    underlying ``TikTokAutoController`` for diagnostics."""

    ok: bool
    summary: str = ""
    steps: list = field(default_factory=list)


@dataclass
class StopLiveResult:
    """Outcome of ``stop_live``. ``strategy`` is one of
    ``"back+confirm" | "back" | "force_stop" | "best_effort"``.
    ``ok`` reflects "did we send a signal" -- not "did the
    audience see a clean end" -- because we can't reliably
    detect the latter from outside the app."""

    ok: bool
    strategy: str = "best_effort"
    summary: str = ""


# ── public API ─────────────────────────────────────────────────


LogCallback = Optional[Callable[[str], None]]


def start_live(
    adb_path: str,
    serial: Optional[str],
    *,
    log_cb: LogCallback = None,
    tap_settle_s: float = 1.5,
) -> StartLiveResult:
    """Drive the selected phone all the way to a live broadcast.

    Returns a ``StartLiveResult`` with ``ok=True`` iff every step
    of the TikTok flow ("Live tab → Go Live → Screen Share →
    Start Now") reported success. On partial success we still
    return ``ok=False`` BUT include the ``steps`` so the UI can
    hint at where the customer needs to tap manually -- the most
    common failure ("Go Live not found") means the account simply
    doesn't have Live access, which is unrecoverable without
    intervention from TikTok itself.
    """
    # Imported locally so this module can be unit-tested with a
    # mocked controller without dragging in the heavy uiautomator
    # XML helpers at import time.
    from .tiktok_controller import TikTokAutoController

    ctrl = TikTokAutoController(
        adb_path=adb_path,
        log_callback=log_cb,
        tap_settle_s=tap_settle_s,
    )

    # Bind ``serial`` for the controller's adb wrapper. The
    # existing controller doesn't take a serial parameter; rather
    # than refactoring it, we monkey-patch its ``_adb`` to inject
    # the ``-s`` flag locally.
    if serial:
        original_adb = ctrl._adb

        def _adb_with_serial(*args, timeout: float = 8.0):
            new_args = ("-s", serial, *args)
            return original_adb(*new_args, timeout=timeout)

        ctrl._adb = _adb_with_serial   # type: ignore[assignment]

    try:
        steps = ctrl.run_to_screen_share(confirm_start=True)
    except Exception as exc:
        log.exception("TikTokAutoController.run_to_screen_share crashed")
        return StartLiveResult(
            ok=False,
            summary=f"❌ เริ่มไลฟ์ไม่สำเร็จ: {exc}",
            steps=[],
        )

    # Success = the LAST step was ``start_now`` and it was ok.
    final = steps[-1] if steps else None
    if final is not None and final.name == "start_now" and final.ok:
        return StartLiveResult(
            ok=True,
            summary="✅ เริ่มไลฟ์เรียบร้อย",
            steps=steps,
        )

    # Build a Thai-language hint pointing at the first failed step
    # so the customer knows whether they need to tap something
    # themselves or whether their account is simply ineligible.
    failed = next((s for s in steps if not s.ok), None)
    if failed is None:
        summary = "❌ ไม่ทราบสถานะการเริ่มไลฟ์"
    elif failed.name == "find_package":
        summary = "❌ ไม่พบ TikTok บนเครื่อง — ติดตั้งก่อน"
    elif failed.name == "live_tab":
        summary = "❌ หาแท็บ Live ไม่เจอ — เปิด TikTok แล้วกด '+' เอง"
    elif failed.name == "go_live":
        summary = (
            "❌ บัญชีนี้ยังไลฟ์ไม่ได้ "
            "(ต้องผ่าน follower ขั้นต่ำ + ยืนยันอายุ)"
        )
    elif failed.name == "screen_share":
        summary = "❌ Screen Share ไม่พร้อม — บางภูมิภาคปิดฟีเจอร์"
    else:
        summary = f"❌ ขั้นตอน '{failed.name}' ล้มเหลว: {failed.detail}"

    return StartLiveResult(ok=False, summary=summary, steps=steps)


def stop_live(
    adb_path: str,
    serial: Optional[str],
    package: str,
    *,
    log_cb: LogCallback = None,
    settle_s: float = 1.0,
    timeout_s: float = 6.0,
) -> StopLiveResult:
    """Stop a TikTok live broadcast on ``serial``.

    Strategy escalation
    -------------------

    1. ``BACK`` key, then look for the "End live?" confirmation
       dialog and tap "End"/"ตกลง". Graceful path -- audience
       sees a normal "host has ended the live" message.
    2. Second ``BACK`` to clear any bottom-sheet variant.
    3. ``am force-stop <pkg>`` as a last resort. Audience drops
       abruptly. We still report ``ok=True`` because the goal
       (broadcast stopped) was achieved.

    The function ALWAYS returns a result; we never raise into
    the UI -- a stuck phone shouldn't prevent the customer from
    clicking Start on the next session.
    """
    base = [str(adb_path)]
    if serial:
        base += ["-s", str(serial)]

    def _emit(msg: str) -> None:
        log.info(msg)
        if log_cb is not None:
            try:
                log_cb(msg)
            except Exception:
                log.debug("stop_live log_cb failed", exc_info=True)

    # ── strategy 1: BACK + confirm dialog ───────────────────────
    _adb(base, ["shell", "input", "keyevent", "KEYCODE_BACK"], timeout_s)
    time.sleep(settle_s)

    xy = _find_confirm_button(base, timeout_s)
    if xy is not None:
        _adb(base, ["shell", "input", "tap", str(xy[0]), str(xy[1])], timeout_s)
        time.sleep(settle_s)
        _emit(f"→ tapped End-confirmation @{xy}")
        return StopLiveResult(
            ok=True, strategy="back+confirm",
            summary="✅ จบไลฟ์เรียบร้อย",
        )

    # ── strategy 2: extra BACK ─────────────────────────────────
    _adb(base, ["shell", "input", "keyevent", "KEYCODE_BACK"], timeout_s)
    time.sleep(settle_s)
    xy = _find_confirm_button(base, timeout_s)
    if xy is not None:
        _adb(base, ["shell", "input", "tap", str(xy[0]), str(xy[1])], timeout_s)
        time.sleep(settle_s)
        _emit(f"→ tapped End-confirmation @{xy} (after 2nd back)")
        return StopLiveResult(
            ok=True, strategy="back",
            summary="✅ จบไลฟ์เรียบร้อย (ใช้ปุ่มย้อน 2 ครั้ง)",
        )

    # ── strategy 3: force-stop ─────────────────────────────────
    rc, _ = _adb(base, ["shell", "am", "force-stop", package], timeout_s)
    if rc == 0:
        _emit(f"→ am force-stop {package}")
        return StopLiveResult(
            ok=True, strategy="force_stop",
            summary=(
                "⚠️ จบไลฟ์ด้วยการบังคับปิดแอป — ผู้ชมจะหลุดทันที "
                "(หาปุ่ม End ในแอปไม่เจอ)"
            ),
        )

    return StopLiveResult(
        ok=False, strategy="best_effort",
        summary="❌ ปิดไลฟ์ไม่สำเร็จ — ลองกดปุ่มจบในมือถือเอง",
    )


# ── plumbing ───────────────────────────────────────────────────


def _adb(
    base: list[str], args: list[str], timeout: float,
) -> tuple[Optional[int], str]:
    """Run an adb command with the given base prefix and return
    ``(return_code, combined_output)``. Timeout / spawn failure
    yields ``(None, error_text)`` so callers don't conflate
    "command failed" with "we couldn't run it"."""
    try:
        r = subprocess.run(
            base + args, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, ValueError) as exc:
        return None, str(exc)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _find_confirm_button(
    base: list[str], timeout: float,
) -> Optional[tuple[int, int]]:
    """Dump the foreground UI and look for an End / ยืนยัน
    button. Returns ``(x, y)`` of its centre, or ``None`` if no
    such button is visible right now."""
    dump_path = "/sdcard/vcam_stop_uidump.xml"

    rc, _ = _adb(
        base,
        ["shell", "uiautomator", "dump", dump_path],
        timeout,
    )
    if rc != 0:
        return None
    rc, xml = _adb(base, ["shell", "cat", dump_path], timeout)
    if rc != 0 or not xml:
        return None

    pattern = re.compile(
        r'(?:text|content-desc)="([^"]+)"[^>]*?'
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        re.IGNORECASE,
    )
    for label, x1, y1, x2, y2 in pattern.findall(xml):
        low = label.lower()
        if any(kw.lower() in low for kw in _KW_END_CONFIRM):
            cx = (int(x1) + int(x2)) // 2
            cy = (int(y1) + int(y2)) // 2
            return cx, cy
    return None


# ── formatting helpers (used by UI) ────────────────────────────


def format_elapsed(seconds: int) -> str:
    """``HH:MM:SS`` if ≥1h, else ``MM:SS``. Matches the visual
    convention of the YouTube / TikTok live-stream timer customers
    are already used to."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


__all__ = [
    "StartLiveResult",
    "StopLiveResult",
    "start_live",
    "stop_live",
    "format_elapsed",
]
