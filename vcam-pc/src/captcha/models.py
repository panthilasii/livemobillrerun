"""Shared result types for the captcha subsystem.

Kept in their own module so ``detect_cv`` / ``gemini`` / ``solver``
can all reference them without import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

Point = Tuple[int, int]


@dataclass
class SolvePlan:
    """A concrete drag the solver should perform, in phone display
    pixels.

    * ``src`` — where to press down (the slider handle center).
    * ``dst`` — where to release (handle's final resting spot once
      the puzzle piece sits in the gap).
    * ``captcha_type`` — best-effort classification
      (``"slide"`` / ``"rotate"`` / ``"image_select"`` / ``"other"``).
    * ``source`` — which brain produced it (``"cv"`` / ``"gemini"``).
    * ``confidence`` — 0..1 heuristic strength (CV only; Gemini is 1).
    """

    src: Point
    dst: Point
    captcha_type: str = "slide"
    source: str = "cv"
    confidence: float = 0.0

    @property
    def distance(self) -> int:
        return int(self.dst[0] - self.src[0])


@dataclass
class SolveOutcome:
    """Result of a single ``solve_once`` attempt.

    ``status`` is one of:

    * ``"no_captcha"`` — nothing actionable on screen (the common,
      happy steady-state for a poll loop).
    * ``"solved"`` — we dragged and the challenge was gone on
      re-check.
    * ``"failed"`` — we dragged but the challenge is still up
      (caller may retry).
    * ``"error"`` — capture / decode / adb failure.
    """

    status: str
    detail: str = ""
    plan: Optional[SolvePlan] = None


__all__ = ["Point", "SolvePlan", "SolveOutcome"]
