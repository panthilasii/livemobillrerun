"""Best-effort slide/jigsaw captcha detection using Pillow only.

This is the *free* tier. It cannot match a vision model's accuracy
and only handles the common "drag the piece into the gap" slide
challenge, but it needs no API key and no network round-trip.

Algorithm (all reductions are C-level in Pillow, so we never iterate
over megapixels in Python):

1. Downscale the full screenshot to a small working width for speed.
2. Detect the captcha popup: TikTok dims the page behind the
   challenge, so the popup card is a bright contiguous band against a
   dark overlay. We find that band from per-row / per-column
   brightness profiles (``img.resize((W, 1))`` / ``((1, H))``).
3. Inside the popup, find the gap: a jigsaw gap leaves a strong
   vertical edge pair. We take the edge image's per-column profile and
   pick the strongest column to the right of the piece.
4. Estimate the slider handle (bottom-left of the popup) and turn the
   gap offset into a :class:`~src.captcha.models.SolvePlan` in
   full-resolution phone pixels.

If any step lacks a confident signal we return ``None`` — meaning
"no slide captcha detected", which the orchestrator treats as
``no_captcha`` (or hands off to Gemini when a key is configured).
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from PIL import Image, ImageFilter

from .models import SolvePlan

log = logging.getLogger(__name__)

# Working width the screenshot is downscaled to before analysis.
# 360 px keeps per-column lists tiny while preserving enough
# horizontal resolution to localise a gap to ~3 source px.
_WORK_W = 360

# A popup band must be at least this fraction brighter than the
# dimmed page around it to count as a captcha card.
_POPUP_BRIGHT_RATIO = 1.35

# The popup must occupy at least this fraction of screen height,
# otherwise a bright status-bar / notification is misread as a card.
_MIN_POPUP_H_FRAC = 0.18
_MAX_POPUP_H_FRAC = 0.85


def _column_profile(img: Image.Image) -> List[float]:
    """Average brightness of each column (length == img width)."""
    w = img.width
    row = img.resize((w, 1), Image.BILINEAR).convert("L")
    return [float(v) for v in row.tobytes()]


def _row_profile(img: Image.Image) -> List[float]:
    """Average brightness of each row (length == img height)."""
    h = img.height
    col = img.resize((1, h), Image.BILINEAR).convert("L")
    return [float(v) for v in col.tobytes()]


def _find_bright_band(profile: List[float]) -> Optional[Tuple[int, int]]:
    """Return ``(start, end)`` of the brightest contiguous run that
    sits clearly above the profile's baseline, or ``None``."""
    n = len(profile)
    if n == 0:
        return None
    lo = min(profile)
    hi = max(profile)
    if hi <= 0 or hi <= lo * _POPUP_BRIGHT_RATIO:
        return None
    thresh = lo + (hi - lo) * 0.5
    best: Optional[Tuple[int, int]] = None
    start = None
    for i, v in enumerate(profile):
        if v >= thresh:
            if start is None:
                start = i
        else:
            if start is not None:
                if best is None or (i - start) > (best[1] - best[0]):
                    best = (start, i)
                start = None
    if start is not None:
        if best is None or (n - start) > (best[1] - best[0]):
            best = (start, n)
    return best


def _bright_extent(profile: List[float]) -> Optional[Tuple[int, int]]:
    """Return ``(first, last+1)`` spanning ALL above-threshold samples.

    Unlike :func:`_find_bright_band` (largest contiguous run), this
    keeps the outer bounds even when internal dark stripes — the
    draggable piece and the gap notch — split the bright card into
    several runs. Used for the popup's horizontal extent so the gap
    isn't mistaken for the card's right edge.
    """
    n = len(profile)
    if n == 0:
        return None
    lo = min(profile)
    hi = max(profile)
    if hi <= 0 or hi <= lo * _POPUP_BRIGHT_RATIO:
        return None
    thresh = lo + (hi - lo) * 0.5
    first = None
    last = None
    for i, v in enumerate(profile):
        if v >= thresh:
            if first is None:
                first = i
            last = i
    if first is None:
        return None
    return (first, last + 1)


def detect(image: Image.Image) -> Optional[SolvePlan]:
    """Detect a slide captcha in a full-resolution screenshot.

    Returns a :class:`SolvePlan` in *full-resolution* phone pixels, or
    ``None`` when no confident slide challenge is found. Never raises.
    """
    try:
        return _detect_inner(image)
    except Exception:
        log.debug("cv detect crashed", exc_info=True)
        return None


def _detect_inner(image: Image.Image) -> Optional[SolvePlan]:
    full_w, full_h = image.width, image.height
    if full_w < 64 or full_h < 64:
        return None

    scale = full_w / float(_WORK_W)
    work_h = max(1, int(round(full_h / scale)))
    gray = image.convert("L").resize((_WORK_W, work_h), Image.BILINEAR)

    # ── 1. Locate the popup card by brightness band (vertical) ──
    rows = _row_profile(gray)
    band = _find_bright_band(rows)
    if band is None:
        return None
    top, bottom = band
    band_h = bottom - top
    if not (_MIN_POPUP_H_FRAC <= band_h / work_h <= _MAX_POPUP_H_FRAC):
        return None

    popup = gray.crop((0, top, _WORK_W, bottom))

    # Horizontal extent of the card within that vertical band. Use the
    # OUTER extent (not the largest run) so the piece + gap stripes,
    # which dip below threshold, don't truncate the card boundary.
    cols = _column_profile(popup)
    hband = _bright_extent(cols)
    if hband is None:
        left, right = 0, _WORK_W
    else:
        left, right = hband
    if right - left < _WORK_W * 0.3:
        # Card too narrow to be a captcha image — likely a toast.
        return None

    # ── 2. Gap detection inside the puzzle image ──
    # The puzzle image occupies the upper ~70% of the card; the
    # slider track is the bottom strip. Analyse the puzzle region.
    puzzle_bottom = int(popup.height * 0.72)
    puzzle = popup.crop((left, 0, right, max(1, puzzle_bottom)))
    edges = puzzle.filter(ImageFilter.FIND_EDGES)
    edge_cols = _column_profile(edges)
    if not edge_cols:
        return None

    # The draggable piece lives at the far left; ignore the leftmost
    # ~15% so we don't lock onto the piece's own border. Also ignore
    # the rightmost ~8% so the card's own right border (a strong
    # bright->dark vertical edge sitting at the crop boundary) isn't
    # mistaken for the gap. Search what's left for the strongest
    # vertical edge (the gap).
    n_cols = len(edge_cols)
    piece_guard = int(n_cols * 0.15)
    right_guard = max(piece_guard + 1, int(n_cols * 0.92))
    search = edge_cols[piece_guard:right_guard]
    if not search:
        return None
    peak_local = max(range(len(search)), key=lambda i: search[i])
    peak_strength = search[peak_local]
    mean_strength = sum(edge_cols) / len(edge_cols)
    # Require the gap edge to clearly stand out from the noise floor,
    # otherwise we're just chasing JPEG artefacts on a normal screen.
    if peak_strength < mean_strength * 2.2 or peak_strength < 12:
        return None

    gap_col_in_puzzle = piece_guard + peak_local  # within ``puzzle`` x
    piece_col_in_puzzle = int(len(edge_cols) * 0.06)

    # ── 3. Map back to full-resolution phone pixels ──
    # All x below are relative to the popup-card left edge; add it in.
    gap_x_work = left + gap_col_in_puzzle
    piece_x_work = left + piece_col_in_puzzle
    offset_work = gap_x_work - piece_x_work
    if offset_work <= 4:
        return None

    # Slider handle: bottom track, near the card's left edge.
    track_y_work = top + int(popup.height * 0.88)
    handle_x_work = left + int((right - left) * 0.08)

    src = (int(round(handle_x_work * scale)), int(round(track_y_work * scale)))
    dst = (
        int(round((handle_x_work + offset_work) * scale)),
        int(round(track_y_work * scale)),
    )

    # Clamp inside the screen.
    src = (max(0, min(full_w - 1, src[0])), max(0, min(full_h - 1, src[1])))
    dst = (max(0, min(full_w - 1, dst[0])), max(0, min(full_h - 1, dst[1])))

    confidence = min(1.0, peak_strength / (mean_strength * 5.0 + 1e-6))
    return SolvePlan(
        src=src,
        dst=dst,
        captcha_type="slide",
        source="cv",
        confidence=round(confidence, 3),
    )


__all__ = ["detect"]
