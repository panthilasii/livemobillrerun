"""Single-shot captcha solve: capture -> detect -> drag -> verify.

This is the brain that ties the pieces together for one attempt. The
per-device loop in :mod:`src.captcha.runner` calls :func:`solve_once`
on a cadence; everything here is stateless and Tk-free.

Detection strategy (the "hybrid, bring-your-own-key" decision):

1. Try the free Pillow CV detector first. If it finds a confident
   slide challenge, use it.
2. Otherwise, if a Gemini API key is configured, ask Gemini (covers
   rotate / image-select and is more robust at detection).
3. If neither produced a plan, report ``no_captcha`` — the normal
   steady state.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional

from . import capture, detect_cv, drag, gemini
from .gemini import DEFAULT_MODEL
from .models import SolveOutcome, SolvePlan

log = logging.getLogger(__name__)

LogFn = Optional[Callable[[str], None]]


def _detect(
    image,
    *,
    gemini_api_key: str,
    gemini_model: str,
) -> Optional[SolvePlan]:
    plan = detect_cv.detect(image)
    if plan is not None:
        return plan
    if gemini_api_key:
        return gemini.locate(
            image, api_key=gemini_api_key, model=gemini_model
        )
    return None


def solve_once(
    *,
    adb_path: str,
    adb_serial: str,
    gemini_api_key: str = "",
    gemini_model: str = DEFAULT_MODEL,
    capture_timeout: float = 10.0,
    verify_delay: float = 1.2,
    on_log: LogFn = None,
    rng: random.Random | None = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> SolveOutcome:
    """Run one detect-and-solve attempt against ``adb_serial``.

    Never raises; failures are reported via the returned
    :class:`SolveOutcome`.
    """
    def log_msg(m: str) -> None:
        if on_log is not None:
            try:
                on_log(m)
            except Exception:
                pass
        log.debug("captcha[%s]: %s", adb_serial, m)

    image = capture.grab_screen(adb_path, adb_serial, timeout=capture_timeout)
    if image is None:
        return SolveOutcome(status="error", detail="capture failed")

    plan = _detect(
        image, gemini_api_key=gemini_api_key, gemini_model=gemini_model
    )
    if plan is None:
        return SolveOutcome(status="no_captcha")

    log_msg(
        f"พบ captcha ({plan.captcha_type}/{plan.source}) — "
        f"ลากระยะ {plan.distance}px"
    )

    dragged = drag.perform_drag(
        adb_path, adb_serial, plan.src, plan.dst, rng=rng
    )
    if not dragged:
        return SolveOutcome(status="failed", detail="drag failed", plan=plan)

    # Give TikTok a moment to accept/reject before re-checking.
    if verify_delay > 0:
        _sleep(verify_delay)

    after = capture.grab_screen(adb_path, adb_serial, timeout=capture_timeout)
    if after is None:
        # We dragged but can't confirm; treat as best-effort solved so
        # the loop doesn't hammer a possibly-already-cleared screen.
        return SolveOutcome(status="solved", detail="unverified", plan=plan)

    still = _detect(
        after, gemini_api_key=gemini_api_key, gemini_model=gemini_model
    )
    if still is None:
        log_msg("แก้ captcha สำเร็จ")
        return SolveOutcome(status="solved", plan=plan)
    return SolveOutcome(status="failed", detail="still visible", plan=plan)


__all__ = ["solve_once", "SolveOutcome", "SolvePlan"]
