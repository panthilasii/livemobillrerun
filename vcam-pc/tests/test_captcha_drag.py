"""``src.captcha.drag`` — trajectory shape + adb swipe argv."""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.captcha import drag  # noqa: E402


def _completed(rc: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=b"")


def test_trajectory_overshoots_then_settles():
    rng = random.Random(0)
    segs = drag.build_trajectory((100, 2000), (400, 2000), rng=rng)
    assert len(segs) == 2
    (s0_src, s0_dst, d0), (s1_src, s1_dst, d1) = segs
    # Main swipe overshoots past the true target (x > 400, moving right).
    assert s0_dst[0] > 400
    # Settle swipe ends exactly on the target x.
    assert s1_dst[0] == 400
    # Main swipe is slower (longer duration) than the corrective settle.
    assert d0 > d1


def test_trajectory_left_direction_overshoots_left():
    segs = drag.build_trajectory((500, 1000), (200, 1000), rng=random.Random(1))
    main = segs[0]
    # Overshoot should be to the LEFT of the target (smaller x).
    assert main[1][0] < 200


def test_trajectory_tiny_distance_single_segment():
    segs = drag.build_trajectory((100, 100), (103, 100))
    assert len(segs) == 1


def test_perform_drag_issues_swipe_per_segment():
    calls = []

    def _fake_run(args, **kw):
        calls.append(args)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        ok = drag.perform_drag(
            "/usr/bin/adb", "SER1", (100, 2000), (400, 2000),
            rng=random.Random(3), sleep=False,
        )
    assert ok is True
    assert len(calls) == 2
    first = calls[0]
    assert first[:5] == ["/usr/bin/adb", "-s", "SER1", "shell", "input"]
    assert first[5] == "swipe"
    # 4 coordinate args + duration after "swipe".
    assert len(first[6:]) == 5


def test_perform_drag_returns_false_on_adb_failure():
    with patch("subprocess.run", return_value=_completed(1)):
        ok = drag.perform_drag(
            "/usr/bin/adb", "SER1", (100, 2000), (400, 2000), sleep=False
        )
    assert ok is False
