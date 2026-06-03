"""Perform a human-ish slider drag over ADB.

Phase 1 uses ``adb shell input swipe`` because it needs no change to
the Android app and works on every patched phone today. A single
``input swipe`` is a perfectly linear interpolation, which TikTok's
slide verification can flag as robotic ("mouse position" rejection).
We soften that with the trick proven by the reference solver:

* overshoot the target by a few pixels on the main swipe, then
* issue a short corrective swipe back to the true target, and
* randomise duration + add a couple of px of vertical jitter

so the press-release isn't a textbook straight line at constant
speed. This is *mitigation*, not a guarantee — the truly human path
is in-process ``MotionEvent`` injection through the hook, which is
deferred to Phase 2 (it requires Android-side changes + a re-patch).

``build_trajectory`` is factored out and pure so it can be unit
tested without a device; ``perform_drag`` is the thin adb driver.
"""

from __future__ import annotations

import logging
import random
import subprocess
import time
from typing import List, Tuple

log = logging.getLogger(__name__)

Point = Tuple[int, int]


def build_trajectory(
    src: Point,
    dst: Point,
    *,
    overshoot_px: int = 8,
    jitter_px: int = 2,
    rng: random.Random | None = None,
) -> List[Tuple[Point, Point, int]]:
    """Return a list of ``(seg_src, seg_dst, duration_ms)`` swipe
    segments approximating a human drag from ``src`` to ``dst``.

    Two segments: a main swipe that overshoots the target slightly,
    then a quick settle back onto it. The handle only moves
    horizontally on the track, so ``y`` is kept on the source row
    apart from a pixel or two of jitter to break the perfectly
    straight line.
    """
    r = rng or random.Random()
    distance = abs(dst[0] - src[0])

    # No meaningful distance — a single tiny swipe is enough.
    if distance < 6:
        return [(src, dst, 200)]

    direction = 1 if dst[0] >= src[0] else -1
    over_x = dst[0] + direction * max(0, int(overshoot_px))
    jy = lambda: r.randint(-jitter_px, jitter_px) if jitter_px > 0 else 0

    over_point = (over_x, src[1] + jy())
    settle_point = (dst[0], dst[1] + jy())

    # Longer main swipe = slower, more deliberate human pull. Scale
    # loosely with distance so a long drag isn't a 200 ms flick.
    main_ms = int(min(1200, max(450, distance * r.uniform(1.6, 2.6))))
    settle_ms = r.randint(120, 240)

    return [
        (src, over_point, main_ms),
        (over_point, settle_point, settle_ms),
    ]


def perform_drag(
    adb_path: str,
    adb_serial: str,
    src: Point,
    dst: Point,
    *,
    overshoot_px: int = 8,
    jitter_px: int = 2,
    timeout: float = 8.0,
    rng: random.Random | None = None,
    sleep: bool = True,
) -> bool:
    """Drag the slider from ``src`` to ``dst`` via ``input swipe``.

    Returns ``True`` if every segment's adb command exited 0.
    """
    segments = build_trajectory(
        src, dst, overshoot_px=overshoot_px, jitter_px=jitter_px, rng=rng
    )
    ok = True
    for i, (seg_src, seg_dst, dur_ms) in enumerate(segments):
        if not _run_swipe(
            adb_path, adb_serial, seg_src, seg_dst, dur_ms, timeout=timeout
        ):
            ok = False
            break
        # Brief pause between the main pull and the settle so the two
        # swipes don't merge into one suspiciously continuous motion.
        if sleep and i < len(segments) - 1:
            time.sleep((rng or random).uniform(0.05, 0.18) if rng else 0.1)
    return ok


def _run_swipe(
    adb_path: str,
    adb_serial: str,
    src: Point,
    dst: Point,
    duration_ms: int,
    *,
    timeout: float,
) -> bool:
    args = [adb_path]
    if adb_serial:
        args += ["-s", adb_serial]
    args += [
        "shell",
        "input",
        "swipe",
        str(int(src[0])),
        str(int(src[1])),
        str(int(dst[0])),
        str(int(dst[1])),
        str(int(duration_ms)),
    ]
    try:
        r = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        log.debug("input swipe failed", exc_info=True)
        return False
    return r.returncode == 0


__all__ = ["build_trajectory", "perform_drag"]
