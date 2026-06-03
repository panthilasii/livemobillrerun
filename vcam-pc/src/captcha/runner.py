"""Per-device auto-solve loops.

Mirrors the threading model used elsewhere in the app
(``EncodePushRegistry`` one-thread-per-device, ``AnnouncementPoller``
``threading.Event`` stop): each device that has auto-solve enabled
gets exactly one daemon thread that polls the screen and clears any
captcha it finds. Stopping is cooperative via an ``Event``.

The registry is the only object the UI / app touch:

    reg.start(serial, adb_path=..., adb_serial=..., gemini_api_key=...)
    reg.is_running(serial)
    reg.stop(serial)
    reg.stop_all()

It is Tk-free; status text is delivered through the ``on_status``
callback the caller supplies (the UI marshals it onto the Tk thread
with ``after(0, ...)``).
"""

from __future__ import annotations

import logging
import random
import threading
from typing import Callable, Dict, Optional

from . import solver
from .gemini import DEFAULT_MODEL

log = logging.getLogger(__name__)

StatusFn = Optional[Callable[[str, str], None]]  # (serial, message)


class _Worker:
    def __init__(self, thread: threading.Thread, stop: threading.Event) -> None:
        self.thread = thread
        self.stop = stop

    def is_alive(self) -> bool:
        return self.thread.is_alive()


class AutoSolveRegistry:
    """Tracks one auto-solve loop per device serial."""

    def __init__(self) -> None:
        self._workers: Dict[str, _Worker] = {}
        self._lock = threading.Lock()

    def is_running(self, serial: str) -> bool:
        with self._lock:
            w = self._workers.get(serial)
            return w is not None and w.is_alive()

    def running_serials(self) -> list[str]:
        with self._lock:
            return [s for s, w in self._workers.items() if w.is_alive()]

    def start(
        self,
        serial: str,
        *,
        adb_path: str,
        adb_serial: str,
        gemini_api_key: str = "",
        gemini_model: str = DEFAULT_MODEL,
        poll_s: float = 3.0,
        max_retries: int = 3,
        on_status: StatusFn = None,
    ) -> bool:
        """Start (or no-op if already running) the loop for ``serial``.

        Returns ``True`` if a new thread was spawned.
        """
        with self._lock:
            existing = self._workers.get(serial)
            if existing is not None and existing.is_alive():
                return False
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run,
                kwargs=dict(
                    serial=serial,
                    adb_path=adb_path,
                    adb_serial=adb_serial,
                    gemini_api_key=gemini_api_key,
                    gemini_model=gemini_model,
                    poll_s=max(1.0, float(poll_s)),
                    max_retries=max(1, int(max_retries)),
                    on_status=on_status,
                    stop=stop,
                ),
                name=f"captcha-solve-{serial}",
                daemon=True,
            )
            self._workers[serial] = _Worker(thread, stop)
            thread.start()
            return True

    def stop(self, serial: str) -> None:
        with self._lock:
            w = self._workers.pop(serial, None)
        if w is not None:
            w.stop.set()

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop.set()

    # ── loop body ───────────────────────────────────────────────

    def _run(
        self,
        *,
        serial: str,
        adb_path: str,
        adb_serial: str,
        gemini_api_key: str,
        gemini_model: str,
        poll_s: float,
        max_retries: int,
        on_status: StatusFn,
        stop: threading.Event,
    ) -> None:
        rng = random.Random()

        def status(msg: str) -> None:
            if on_status is not None:
                try:
                    on_status(serial, msg)
                except Exception:
                    log.debug("auto-solve on_status failed", exc_info=True)

        status("เฝ้าดู captcha อัตโนมัติ…")
        retries = 0
        while not stop.is_set():
            try:
                outcome = solver.solve_once(
                    adb_path=adb_path,
                    adb_serial=adb_serial,
                    gemini_api_key=gemini_api_key,
                    gemini_model=gemini_model,
                    on_log=lambda m: status(m),
                    rng=rng,
                )
            except Exception:
                log.exception("auto-solve iteration crashed")
                outcome = None

            if outcome is not None:
                if outcome.status == "solved":
                    retries = 0
                    status("แก้ captcha สำเร็จ ✓")
                elif outcome.status == "failed":
                    retries += 1
                    if retries >= max_retries:
                        retries = 0
                        status("ลองแก้ captcha หลายครั้งแล้วยังไม่ผ่าน")
                    else:
                        # Retry quickly without the full poll wait.
                        if stop.wait(0.6):
                            break
                        continue
                # ``no_captcha`` / ``error`` just fall through to the
                # normal poll interval below.

            # Cooperative sleep: wakes immediately on stop().
            if stop.wait(poll_s):
                break
        status("ปิดการแก้ captcha อัตโนมัติ")


__all__ = ["AutoSolveRegistry"]
