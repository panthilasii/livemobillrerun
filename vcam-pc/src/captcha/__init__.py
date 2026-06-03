"""Auto captcha-solver subsystem (Phase 1: PC-only via ADB).

When TikTok pops a slide / jigsaw verification challenge on the
*phone* during a live broadcast, this package detects it from a
screen capture and drags the slider to clear it — so the customer
doesn't have to babysit the phone.

Design constraints (see vcam-pc/requirements.txt "zero native deps")
--------------------------------------------------------------------
* No ``opencv`` / ``numpy``. Image work uses ``Pillow`` (already a
  dependency) and the few column/row reductions we need are done
  with PIL's C-level ``resize`` so we never loop over megapixels in
  pure Python.
* The optional high-accuracy path calls Google Gemini over REST
  using ``httpx`` (also already a dependency). The API key is
  *bring-your-own* — read from ``StreamConfig.gemini_api_key`` — so
  we ship no key and incur no per-solve cost ourselves.

Capture + input both run through the same ADB transport the rest of
the app uses, and operate in the phone's display-pixel coordinate
space (``screencap`` pixels == ``input swipe`` pixels), so no
scaling is required.

The modules are deliberately Tk-free and accept primitive arguments
(``adb_path``, ``adb_serial``, …) so the whole pipeline can be unit
tested by mocking ``subprocess.run`` — exactly like the existing
``tests/test_pull_apk_fallback.py`` / ``tests/test_live_control.py``.
"""

from __future__ import annotations

from .models import SolveOutcome, SolvePlan
from .solver import solve_once
from .runner import AutoSolveRegistry

__all__ = [
    "SolveOutcome",
    "SolvePlan",
    "solve_once",
    "AutoSolveRegistry",
]
