"""Optional high-accuracy captcha localisation via Google Gemini.

Bring-your-own-key: the API key comes from
``StreamConfig.gemini_api_key`` and is never shipped with the app.
We call the public ``generativelanguage.googleapis.com`` REST
endpoint with ``httpx`` (already a project dependency) so this adds
no new packages and no native code.

The prompt mirrors the approach proven by the reference TikTokSolver
build: send the screenshot and ask for the slider handle start point
and the target end point as *percentages* of the image, plus a coarse
captcha-type classification. Percentages are resolution-independent,
so we map them onto the phone's real pixels on return.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Optional

from PIL import Image

from .models import SolvePlan

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.0-flash"
_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
_TIMEOUT_S = 30.0

_PROMPT = (
    "You are looking at a phone screenshot. Determine if a TikTok "
    "slide / jigsaw / rotate verification CAPTCHA popup is visible "
    "(a draggable puzzle piece that must be moved into a matching "
    "gap, with a slider/drag bar along the bottom).\n\n"
    "Reply with ONLY a compact JSON object, no markdown, with keys:\n"
    '  "captcha": true|false — whether such a CAPTCHA is visible.\n'
    '  "captcha_type": "slide"|"rotate"|"image_select"|"other".\n'
    '  "slider_src": [x_pct, y_pct] — CENTER of the slider handle/'
    "button on the bottom bar (the thing you press to start dragging). "
    "MUST be on the handle itself, not elsewhere on the track.\n"
    '  "slider_dst": [x_pct, y_pct] — CENTER position on that SAME '
    "bottom track where the handle should rest once the puzzle is "
    "solved.\n"
    "All percentages are 0-100 of image width (x) / height (y). "
    'If no CAPTCHA is visible return {"captcha": false}.'
)


def locate(
    image: Image.Image,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = _TIMEOUT_S,
) -> Optional[SolvePlan]:
    """Ask Gemini to detect + locate the captcha drag.

    Returns a :class:`SolvePlan` in full-resolution phone pixels, or
    ``None`` when there is no captcha / the call failed / the response
    couldn't be parsed. Never raises.
    """
    if not api_key:
        return None
    try:
        return _locate_inner(image, api_key=api_key, model=model, timeout=timeout)
    except Exception:
        log.debug("gemini locate crashed", exc_info=True)
        return None


def _locate_inner(
    image: Image.Image,
    *,
    api_key: str,
    model: str,
    timeout: float,
) -> Optional[SolvePlan]:
    import httpx

    buf = io.BytesIO()
    # JPEG keeps the upload small (captcha frames are photographic);
    # quality 85 is plenty for the model to read a slider.
    image.convert("RGB").save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": _PROMPT},
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.0, "response_mime_type": "application/json"},
    }
    url = _ENDPOINT.format(model=model)
    resp = httpx.post(
        url,
        params={"key": api_key},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        log.debug("gemini HTTP %s: %s", resp.status_code, resp.text[:200])
        return None

    text = _extract_text(resp.json())
    if not text:
        return None
    data = _parse_json_loose(text)
    if not isinstance(data, dict) or not data.get("captcha"):
        return None

    src_pct = data.get("slider_src")
    dst_pct = data.get("slider_dst")
    if not (_is_pair(src_pct) and _is_pair(dst_pct)):
        return None

    w, h = image.width, image.height
    src = (_pct_to_px(src_pct[0], w), _pct_to_px(src_pct[1], h))
    dst = (_pct_to_px(dst_pct[0], w), _pct_to_px(dst_pct[1], h))
    return SolvePlan(
        src=src,
        dst=dst,
        captcha_type=str(data.get("captcha_type", "slide")),
        source="gemini",
        confidence=1.0,
    )


def _extract_text(body: dict) -> str:
    try:
        parts = body["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _parse_json_loose(text: str):
    """Parse JSON that may be wrapped in ```json fences or have stray
    prose around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _is_pair(v) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) == 2
        and all(isinstance(n, (int, float)) for n in v)
    )


def _pct_to_px(pct: float, dimension: int) -> int:
    return int(round(max(0.0, min(100.0, float(pct))) / 100.0 * dimension))


__all__ = ["locate", "DEFAULT_MODEL"]
