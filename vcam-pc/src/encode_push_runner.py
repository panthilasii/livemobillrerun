"""Pure-Python encode + push kernel (v1.8.6).

The dashboard ``_on_encode_push`` handler used to inline 90 lines
of state-machine logic (write playlist → encode → push → progress
splits → success/failure UI). Splitting it out here gives us:

* **Per-device parallelism.** UI spawns ``run_encode_push`` on a
  daemon thread per click; multiple threads run independently
  because every dependency (ffmpeg child, adb child, output path,
  playlist tempfile) is per-call.
* **Testability.** No Tk imports in this module — tests call the
  function directly with a stub ``HookModePipeline`` and assert
  on the mutated ``EncodePushTask``.
* **Single source of truth.** The two-phase progress split (0..0.5
  encode, 0.5..1.0 push) lives in one place; the UI never
  recomputes it.

Concurrency contract
~~~~~~~~~~~~~~~~~~~~

Every input to ``run_encode_push`` must be safe to use concurrently
across multiple threads:

* ``HookModePipeline`` is stateless w.r.t. encode/push — it spawns
  fresh subprocesses each call and reads only ``self.cfg``.
* ``write_playlist`` writes to a unique ``tempfile.mkstemp`` path
  per call.
* ``task.output`` is per-serial (``device_local_mp4``) so two
  ffmpeg children can't clobber each other's bytes.
* ``profile`` is read-only.

The single piece of cross-task state is the customer's ``devices.json``
library — but the runner doesn't touch it. Persistence of any per-
device side-effect (e.g. last_encoded_at) is left to the UI thread
on completion, where the existing ``app.save_devices()`` happens
on the Tk thread and serialises naturally.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from .config import DeviceProfile, StreamConfig
from .encode_push_tasks import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_ENCODING,
    STATE_ERROR,
    STATE_PUSHING,
    EncodePushTask,
    TaskUpdateCB,
    mark_state,
)
from .hook_mode import (
    TARGET_PATH_TEMPLATE,
    HookModePipeline,
    target_for_package,
)
from .playlist import write_playlist as _default_write_playlist

log = logging.getLogger(__name__)


# ``write_playlist`` is injectable so tests can short-circuit the
# disk write. The default is the production helper that uses
# ``tempfile.mkstemp`` per call (parallel-safe).
WritePlaylistFn = Callable[[list[Path], bool], Path]


def run_encode_push(
    *,
    pipeline: HookModePipeline,
    cfg: StreamConfig,
    profile: DeviceProfile,
    task: EncodePushTask,
    on_update: TaskUpdateCB = None,
    write_playlist_fn: Optional[WritePlaylistFn] = None,
) -> EncodePushTask:
    """Encode ``task.source`` → push to phone identified by ``task.adb_id``.

    Mutates ``task`` in place through every state transition and
    fires ``on_update(task)`` on each transition so the UI can
    repaint. Returns the task on completion (state = ``done`` or
    ``error``) for the caller's convenience; the same object is
    available via the registry by ``serial``.

    Failure modes — none of these raise; we always return a
    terminal-state task so the UI can render a single "ล้มเหลว:
    <reason>" diagnostic without try/except plumbing on the Tk
    side:

    * playlist write fails (disk full, permission) → STATE_ERROR
    * source clip vanished between click and run → STATE_ERROR
    * ffmpeg returns non-zero / times out → STATE_ERROR with
      log_tail surfaced
    * adb push fails (USB unplugged mid-run, phone full) →
      STATE_ERROR with adb stderr surfaced
    * any unexpected exception → STATE_ERROR with the repr
    * ``task.cancel_event`` was tripped (typically by app close
      via ``EncodePushRegistry.cancel_all_running``) → STATE_CANCELLED.
      The pipeline kills its ffmpeg / adb subprocess as part of
      the same transition so we don't leak orphan children when
      the parent exits.
    """
    if write_playlist_fn is None:
        write_playlist_fn = _default_write_playlist

    # ── 0a. Cancel was requested before we even started? Bail.
    # ``cancel_all_running`` from app close fires the event for
    # every queued task; the runner thread can land here on first
    # tick and short-circuit without spawning ffmpeg at all.
    if task.is_cancel_requested():
        mark_state(
            task, STATE_CANCELLED,
            message="ยกเลิกก่อนเริ่ม encode",
            on_update=on_update,
        )
        return task

    # ── 0b. Sanity: source still on disk?
    src = Path(task.source)
    if not src.is_file():
        mark_state(
            task, STATE_ERROR,
            error=f"ไม่พบไฟล์คลิป: {src}",
            message="ไฟล์คลิปหาย — เลือกใหม่",
            on_update=on_update,
        )
        return task

    # ── 1. Build single-file playlist (tempfile, parallel-safe).
    try:
        pl = write_playlist_fn([src], cfg.loop_playlist)
    except Exception as ex:
        log.exception("playlist write failed for %s", task.serial)
        mark_state(
            task, STATE_ERROR,
            error=f"playlist write failed: {ex}",
            message="เตรียม playlist ไม่สำเร็จ",
            on_update=on_update,
        )
        return task

    try:
        # ── 2. Encode (0..0.5 of progress).
        mark_state(
            task, STATE_ENCODING,
            progress=0.0,
            message="เตรียม encode…",
            on_update=on_update,
        )

        def _on_encode_progress(pct: float, msg: str) -> None:
            # Half the bar = encode phase. Carry through whatever
            # Thai message the pipeline produced so the customer
            # sees the same wording the v1.8.0 single-task flow
            # used (we're consciously preserving that copy).
            mark_state(
                task, STATE_ENCODING,
                progress=pct * 0.5,
                message=msg,
                on_update=on_update,
            )

        t0 = time.monotonic()
        try:
            r = pipeline.encode_playlist(
                playlist_file=pl,
                profile=profile,
                output_path=task.output,
                progress_cb=_on_encode_progress,
                cancel_event=task.cancel_event,
            )
        except Exception as ex:
            log.exception("encode crashed for %s", task.serial)
            mark_state(
                task, STATE_ERROR,
                error=f"encode crashed: {ex}",
                message="encode ล้มเหลว",
                on_update=on_update,
            )
            return task

        # ``encode_playlist`` reports ok=False on cancel just like
        # any other failure mode, but we want to surface a distinct
        # STATE_CANCELLED so the dashboard renders it as "ยกเลิก"
        # rather than a generic "encode/push ล้มเหลว". Detect via
        # the event we passed in — that event is the single source
        # of truth for "did the customer ask us to stop?".
        if not r.ok and task.is_cancel_requested():
            mark_state(
                task, STATE_CANCELLED,
                message="ยกเลิกระหว่าง encode",
                on_update=on_update,
            )
            return task

        if not r.ok:
            mark_state(
                task, STATE_ERROR,
                error=r.log_tail or "ffmpeg failed",
                message=f"Encode ล้มเหลว: {r.log_tail or 'ffmpeg failed'}",
                on_update=on_update,
            )
            return task

        task.encoded_bytes = int(r.bytes)
        task.elapsed_encode_s = time.monotonic() - t0

        # Cancel may have arrived AFTER encode finished and BEFORE
        # we start push. Catch that gap too — otherwise we'd kick
        # off an adb push the customer just asked us to stop.
        if task.is_cancel_requested():
            mark_state(
                task, STATE_CANCELLED,
                message="ยกเลิกหลัง encode เสร็จ (ยังไม่ push)",
                on_update=on_update,
            )
            return task

        # ── 3. Push (0.5..1.0 of progress).
        mark_state(
            task, STATE_PUSHING,
            progress=0.5,
            message=f"Encode สำเร็จ ({_human_bytes(r.bytes)}). กำลัง push…",
            on_update=on_update,
        )

        def _on_push_progress(pct: float, msg: str) -> None:
            mark_state(
                task, STATE_PUSHING,
                progress=0.5 + pct * 0.5,
                message=msg,
                on_update=on_update,
            )

        target = target_for_package(task.tiktok_pkg)
        try:
            push = pipeline.push_to_phone(
                task.output,
                serial=task.adb_id,
                target=target,
                progress_cb=_on_push_progress,
                tiktok_pkg=task.tiktok_pkg,
                cancel_event=task.cancel_event,
            )
        except Exception as ex:
            log.exception("push crashed for %s", task.serial)
            mark_state(
                task, STATE_ERROR,
                error=f"push crashed: {ex}",
                message="push ล้มเหลว",
                on_update=on_update,
            )
            return task

        # Same cancel-vs-error split as for encode — surface a
        # clean STATE_CANCELLED instead of a misleading "Push
        # ล้มเหลว: ยกเลิกระหว่าง push" string in the error column.
        if not push.ok and task.is_cancel_requested():
            mark_state(
                task, STATE_CANCELLED,
                message="ยกเลิกระหว่าง push",
                on_update=on_update,
            )
            return task

        if not push.ok:
            mark_state(
                task, STATE_ERROR,
                error=push.error,
                message=f"Push ล้มเหลว: {push.error}",
                on_update=on_update,
            )
            return task

        task.bytes_pushed = int(push.bytes)
        task.elapsed_push_s = float(push.elapsed_s)

        mark_state(
            task, STATE_DONE,
            progress=1.0,
            message=(
                f"สำเร็จ ({_human_bytes(push.bytes)} ใน "
                f"{push.elapsed_s:.1f} วิ)"
            ),
            on_update=on_update,
        )
        return task
    finally:
        # Always clean up the playlist tempfile, even on early
        # return. Leaking these adds up — one per click — and
        # they live in the OS temp dir so customers never see
        # the residue accumulating until disk pressure shows up.
        try:
            Path(pl).unlink(missing_ok=True)
        except Exception:
            log.debug("could not clean up playlist tempfile", exc_info=True)


def _human_bytes(n: int) -> str:
    """Local copy of ``hook_mode.human_bytes`` to avoid an import
    cycle in the test harness — keeps this module's top-level
    imports minimal."""
    if n < 1024:
        return f"{n} B"
    f = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        f /= 1024.0
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
    return f"{n} B"


# Re-export for callers that want to address the on-phone target
# directly (e.g. the diagnostic page that shows "what path will
# we push to for this device").
__all__ = [
    "run_encode_push",
    "TARGET_PATH_TEMPLATE",
]
