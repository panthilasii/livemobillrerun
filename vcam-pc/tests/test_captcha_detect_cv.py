"""``src.captcha.detect_cv`` — Pillow-lite slide detection."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from src.captcha import detect_cv  # noqa: E402


def _make_slide_captcha(gap_x: int = 250) -> Image.Image:
    """Synthesize a screenshot that resembles a TikTok slide captcha:
    a dim page with a bright popup card, a dark draggable piece on the
    left of the puzzle, and a dark gap notch at ``gap_x``."""
    w, h = 400, 800
    img = Image.new("RGB", (w, h), (28, 28, 32))  # dim overlay
    px = img.load()

    # Bright popup card.
    card = (40, 250, 360, 560)  # l, t, r, b
    for y in range(card[1], card[3]):
        for x in range(card[0], card[2]):
            px[x, y] = (205, 205, 210)

    # Puzzle area is the top ~72% of the card. Plant a dark gap notch.
    puzzle_top, puzzle_bottom = 260, 250 + int(310 * 0.72)
    for y in range(puzzle_top, puzzle_bottom):
        for x in range(gap_x, gap_x + 7):
            px[x, y] = (15, 15, 18)
        # Draggable piece on the far left of the puzzle.
        for x in range(58, 70):
            px[x, y] = (15, 15, 18)
    return img


def test_detects_slide_and_locates_gap():
    img = _make_slide_captcha(gap_x=250)
    plan = detect_cv.detect(img)
    assert plan is not None
    assert plan.captcha_type == "slide"
    assert plan.source == "cv"
    # Drag must move to the right (toward the gap) by a positive amount.
    assert plan.distance > 0
    # The destination x should land near the planted gap (within tolerance
    # of the downscale + piece-origin estimate).
    assert abs(plan.dst[0] - 250) < 60
    # src/dst sit on the same horizontal track row.
    assert plan.src[1] == plan.dst[1]


def test_blank_screen_returns_none():
    blank = Image.new("RGB", (400, 800), (30, 30, 34))
    assert detect_cv.detect(blank) is None


def test_uniform_bright_screen_returns_none():
    # No dim overlay / no popup band -> not a captcha.
    bright = Image.new("RGB", (400, 800), (210, 210, 210))
    assert detect_cv.detect(bright) is None


def test_tiny_image_returns_none():
    assert detect_cv.detect(Image.new("RGB", (10, 10))) is None
