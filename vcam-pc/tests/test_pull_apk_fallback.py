"""Unit tests for the multi-strategy ``pull_tiktok`` APK fetch path.

Customers reported ``pull ล้มเหลว: [ 0%]`` — a misleading error
where the captured stderr was actually ``adb pull``'s in-progress
percentage line, not the real failure reason. The v1.8.14 fix:

1. Filter ``[ NN%]`` progress noise from captured output.
2. Three-step fallback ladder: ``adb pull`` → ``adb exec-out cat``
   → stage via ``/sdcard/Download/``.
3. Surface a Thai-friendly error listing every attempt for support.

These tests pin each rung of the ladder so future refactors can't
silently regress the recovery path that's keeping flaky-OEM
customers (Vivo / Oppo) able to patch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import subprocess

from src import lspatch_pipeline  # noqa: E402
from src.config import StreamConfig  # noqa: E402
from src.lspatch_pipeline import LSPatchPipeline  # noqa: E402


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _make_pipeline(tmp_path: Path) -> LSPatchPipeline:
    cfg = StreamConfig.load()
    cfg.adb_path = "/usr/bin/adb"
    p = LSPatchPipeline(cfg)
    p.pulled_dir = tmp_path / "pulled"
    p.pulled_dir.mkdir(parents=True, exist_ok=True)
    return p


def _is_get_state(cmd) -> bool:
    """Pre-flight ``adb -s SERIAL get-state`` invocation.

    The pull ladder now runs this probe before every pull (v1.8.x
    recurrence fix). Test fakes that don't recognise this rung
    would mistakenly see it as a pull invocation and break.
    """
    return "get-state" in cmd


# ── _clean_adb_progress ───────────────────────────────────────


def test_clean_adb_progress_strips_percentage_lines() -> None:
    """``[ 0%]``, ``[ 47%]``, ``[100%]`` etc. must be dropped so
    the actual error is what bubbles up to the customer."""
    raw = (
        "[  0%] /data/app/.../base.apk\n"
        "[ 47%] /data/app/.../base.apk\n"
        "[100%] /data/app/.../base.apk\n"
        "adb: error: failed to copy '...' permission denied\n"
    )
    out = LSPatchPipeline._clean_adb_progress(raw)
    assert "[" not in out
    assert "permission denied" in out


def test_clean_adb_progress_keeps_real_diagnostics() -> None:
    """Lines that don't match the progress shape must be preserved
    so a multi-line error message survives the filter."""
    raw = (
        "adb: error: cannot stat '/data/app/...': No such file\n"
        "adb: error: 1 file(s) failed to copy\n"
    )
    out = LSPatchPipeline._clean_adb_progress(raw)
    assert "No such file" in out
    assert "1 file(s)" in out


def test_clean_adb_progress_handles_empty_string() -> None:
    assert LSPatchPipeline._clean_adb_progress("") == ""
    assert LSPatchPipeline._clean_adb_progress("\n\n") == ""


# ── _pull_apk_with_fallback: happy path ───────────────────────


def test_pull_succeeds_first_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``adb pull`` works, no fallback is invoked. We assert
    on the pull call count so a future regression that always runs
    exec-out unnecessarily would fail this test."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kw):
        calls.append(list(cmd))
        if _is_get_state(cmd):
            return _completed(0, stdout="device\n")
        # Simulate the file being written.
        dst.write_bytes(b"PK\x03\x04" + b"\x00" * 1024)
        return _completed(0, stderr="[100%] /data/app/.../base.apk\n")

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk", dst, serial="DEV1",
        )

    assert ok is True
    assert err == ""
    pull_calls = [c for c in calls if "pull" in c]
    exec_out_calls = [c for c in calls if "exec-out" in c]
    assert len(pull_calls) == 1, "exactly one adb pull must run on happy path"
    assert exec_out_calls == [], "fallback must NOT run when pull succeeds"


def test_pull_falls_through_to_exec_out_on_permission_denied(
    tmp_path: Path,
) -> None:
    """Pull fails with permission denied → exec-out cat saves the
    day. With the v1.8.x pre-flight check we now also see one
    extra ``get-state`` call up front returning ``device``."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    counts = {"get_state": 0, "pull": 0, "exec_out": 0}

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            counts["get_state"] += 1
            return _completed(0, stdout="device\n")
        if "exec-out" in cmd:
            counts["exec_out"] += 1
            out = kw.get("stdout")
            if out is not None:
                out.write(b"PK\x03\x04" + b"\x00" * 2048)
            return _completed(0, stderr="")
        # adb pull → permission denied
        counts["pull"] += 1
        return _completed(
            1,
            stderr="[  0%] /data/app/.../base.apk\n"
                   "adb: error: failed to copy: permission denied\n",
        )

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk", dst, serial="DEV1",
        )

    assert ok is True
    assert err == ""
    assert counts["pull"] == 1
    assert counts["exec_out"] == 1
    assert dst.is_file() and dst.stat().st_size > 0


def test_pull_falls_through_to_sdcard_when_pull_and_cat_fail(
    tmp_path: Path,
) -> None:
    """Pull and exec-out both fail → /sdcard staging recovers."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    sdcard_pulled = {"value": False}
    counts = {"native_pull": 0, "exec_out": 0}

    def _fake_shell(cmd, serial=None):
        # ``cp`` and ``rm`` shell calls — return success silently.
        return ""

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            return _completed(0, stdout="device\n")
        if "exec-out" in cmd:
            counts["exec_out"] += 1
            return _completed(1, stderr=b"cat: permission denied\n")
        # adb pull. The first pull is the native attempt; any
        # subsequent pull is the /sdcard staging variant.
        counts["native_pull"] += 1
        if counts["native_pull"] == 1:
            return _completed(1, stderr="adb: error: permission denied\n")
        out = kw.get("stdout")  # noqa: F841 — interface-compat
        sdcard_pulled["value"] = True
        dst.write_bytes(b"PK\x03\x04" + b"\x00" * 4096)
        return _completed(0, stderr="[100%] ...\n")

    with patch.object(pipe, "_adb_shell", side_effect=_fake_shell), \
         patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk", dst, serial="DEV1",
        )

    assert ok is True
    assert err == ""
    assert sdcard_pulled["value"] is True


def test_pull_returns_thai_error_when_all_three_strategies_fail(
    tmp_path: Path,
) -> None:
    """All ladders failed — error must list each attempt and
    surface a Thai-language fix list. Critically, the error
    must NOT contain a bare ``[ 0%]`` (the original bug)."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    def _fake_shell(cmd, serial=None):
        if cmd.startswith("cp "):
            return "Permission denied"
        return ""

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            return _completed(0, stdout="device\n")
        return _completed(
            1,
            stderr="[  0%] /data/app/...base.apk\n"
                   "adb: error: failed to copy: Permission denied\n",
        )

    with patch.object(pipe, "_adb_shell", side_effect=_fake_shell), \
         patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk", dst, serial="DEV1",
        )

    assert ok is False
    # Thai-language hint list present.
    assert "Vivo" in err or "Oppo" in err or "TikTok" in err
    # Lists 3 attempts.
    assert "1." in err and "2." in err and "3." in err
    # The original bug: must NOT just show "[ 0%]" with no diagnostic.
    assert "[  0%]" not in err and "[ 0%]" not in err
    # The actual error must surface.
    assert "Permission denied" in err
    # Zero-byte partial file must be cleaned up.
    assert not dst.exists() or dst.stat().st_size > 0


def test_pull_fallback_quotes_paths_with_special_chars(
    tmp_path: Path,
) -> None:
    """Android 10+ paths contain ``~~`` and ``==`` — the exec-out
    shell command must single-quote the path so the on-device
    shell doesn't try to interpret them."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kw):
        captured.append(list(cmd))
        if _is_get_state(cmd):
            return _completed(0, stdout="device\n")
        if "exec-out" in cmd:
            kw.get("stdout").write(b"PK\x03\x04" + b"\x00" * 1024)
            return _completed(0)
        return _completed(1, stderr="permission denied")

    remote = "/data/app/~~lSNgeY3Ke8RsibDLpEd4sg==/com.ss.android.ugc.trill-wQFoQU==/base.apk"
    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        pipe._pull_apk_with_fallback(remote, dst, serial="DEV1")

    exec_calls = [c for c in captured if "exec-out" in c]
    assert exec_calls, "exec-out fallback was not invoked"
    cat_arg = exec_calls[0][-1]
    assert cat_arg.startswith("cat '") and cat_arg.endswith("'")
    assert remote in cat_arg


# ── device-unreachable short-circuit (v1.8.x customer bug) ────


@pytest.mark.parametrize(
    "stderr",
    [
        "adb: error: device 'EIUOA6TSJ799EEGI' not found",
        "error: no devices/emulators found",
        "adb: error: device offline",
        "error: device unauthorized.",
        "adb: error: failed to get feature set: device 'XYZ' not found",
        "cannot connect to daemon at tcp:5037",
        "protocol fault (couldn't read status): connection reset",
    ],
)
def test_is_device_unreachable_recognises_known_disconnect_strings(
    stderr: str,
) -> None:
    """Every flavour of "the device is gone" that adb has been
    seen to emit must trip the short-circuit detector. New
    platform-tools versions occasionally tweak wording — pin the
    set we know about so a regression there can't silently revert
    the customer-facing fix."""
    assert LSPatchPipeline._is_device_unreachable(stderr) is True


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "adb: error: failed to copy: Permission denied",
        "adb: error: cannot stat: No such file or directory",
        "[  0%] /data/app/.../base.apk",
    ],
)
def test_is_device_unreachable_ignores_unrelated_errors(stderr: str) -> None:
    """A regular permission-denied / no-such-file error must NOT
    short-circuit — those still benefit from the exec-out and
    /sdcard fallbacks. False positives here would mean we abort
    customers who could've recovered."""
    assert LSPatchPipeline._is_device_unreachable(stderr) is False


def test_pre_flight_blocks_pull_when_device_is_no_device(
    tmp_path: Path,
) -> None:
    """Customer screenshot v1.8.x: pull fails with "device not
    found", then exec-out and /sdcard pull each restate "device
    not found". The dialog ended up showing three identical-cause
    errors plus the wrong Vivo/Oppo hint list.

    Pre-flight ``adb get-state`` now catches this *before* any
    pull rung runs — saves 10+ seconds of doomed retries and
    surfaces an actionable Thai hint immediately."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kw):
        calls.append(list(cmd))
        if _is_get_state(cmd):
            return _completed(
                1, stderr="error: device 'EIUOA6TSJ799EEGI' not found",
            )
        return _completed(
            1, stderr="adb: error: device 'EIUOA6TSJ799EEGI' not found\n",
        )

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk",
            dst, serial="EIUOA6TSJ799EEGI",
        )

    assert ok is False
    # Pre-flight must short-circuit BEFORE any pull attempt.
    pull_calls = [c for c in calls if "pull" in c]
    exec_out_calls = [c for c in calls if "exec-out" in c]
    assert pull_calls == [], (
        "pre-flight must skip pull when adb already says device is gone, "
        f"saw {len(pull_calls)} pull invocations"
    )
    assert exec_out_calls == [], "exec-out must not run on dead device"
    # New tailored message: must NOT show the obsolete Vivo/Oppo
    # SELinux hint that the old code spat out for this case.
    assert "Vivo" not in err and "Oppo" not in err
    # USB cable / Developer options remediation must be there.
    assert "สาย USB" in err or "Developer options" in err or "หลุดจาก adb" in err


def test_pre_flight_blocks_pull_when_device_unauthorized(
    tmp_path: Path,
) -> None:
    """When the device is plugged in but the USB-debugging dialog
    was dismissed, adb returns "unauthorized". The error must
    coach the customer through the Allow prompt instead of
    pointing them at irrelevant ROM / SELinux fixes."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kw):
        calls.append(list(cmd))
        if _is_get_state(cmd):
            return _completed(0, stdout="unauthorized\n")
        # Should never get here — pre-flight should bail first.
        return _completed(1, stderr="error: device unauthorized.\n")

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk",
            dst, serial="DEV1",
        )

    assert ok is False
    pull_calls = [c for c in calls if "pull" in c]
    assert pull_calls == [], "pull must not run when device is unauthorized"
    # The remediation must reference the on-phone Allow dialog —
    # that's the entire fix for this state.
    assert "Allow" in err or "อนุญาต" in err
    # And it must NOT push the customer to the wrong rabbit hole.
    assert "Vivo" not in err and "Oppo" not in err


def test_pre_flight_blocks_pull_when_device_offline(
    tmp_path: Path,
) -> None:
    """``device offline`` is a different remediation than
    ``no-device`` — typically a sleep/wake-cycle transport bug
    fixed by toggling USB debugging or restarting the adb server."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kw):
        calls.append(list(cmd))
        if _is_get_state(cmd):
            return _completed(0, stdout="offline\n")
        return _completed(1, stderr="adb: error: device offline\n")

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk",
            dst, serial="DEV1",
        )

    assert ok is False
    pull_calls = [c for c in calls if "pull" in c]
    assert pull_calls == [], "pull must not run when device is offline"
    assert "kill-server" in err or "USB debugging" in err


def test_pull_does_not_short_circuit_on_plain_permission_denied(
    tmp_path: Path,
) -> None:
    """Regression guard for the original v1.8.14 ladder: a regular
    permission-denied error must still try exec-out and /sdcard —
    they are the entire point of having a ladder."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    counts = {"pull": 0, "exec_out": 0}

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            return _completed(0, stdout="device\n")
        if "exec-out" in cmd:
            counts["exec_out"] += 1
            kw.get("stdout").write(b"PK\x03\x04" + b"\x00" * 2048)
            return _completed(0)
        counts["pull"] += 1
        return _completed(
            1, stderr="adb: error: failed to copy: Permission denied\n",
        )

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk",
            dst, serial="DEV1",
        )

    assert ok is True
    assert err == ""
    assert counts["pull"] == 1
    assert counts["exec_out"] == 1, "exec-out fallback was skipped — ladder broken"


# ── auto-recover layer (v1.8.x customer recurrence fix) ───────


def test_pull_recovers_on_retry_when_device_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Most customer "device not found" reports were transient USB
    flaps lasting 1-3 s — exactly the case the auto-recover layer
    targets. Verify the second pull attempt fires after the device
    state recovers, and the customer never sees an error dialog."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"

    # Speed up the polling loop in tests — production uses 0.5 s.
    monkeypatch.setattr(lspatch_pipeline.time, "sleep", lambda _s: None)

    state = {
        "get_state_calls": 0,
        "pull_calls": 0,
    }

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            state["get_state_calls"] += 1
            # Pre-flight: device looks fine. First poll after the
            # disconnect: still gone. Second poll: back online.
            if state["get_state_calls"] == 1:
                return _completed(0, stdout="device\n")
            if state["get_state_calls"] == 2:
                return _completed(1, stderr="error: device 'DEV1' not found")
            return _completed(0, stdout="device\n")
        state["pull_calls"] += 1
        if state["pull_calls"] == 1:
            # First pull drops mid-transfer.
            return _completed(
                1, stderr="adb: error: device 'DEV1' not found\n",
            )
        # Retry succeeds.
        dst.write_bytes(b"PK\x03\x04" + b"\x00" * 4096)
        return _completed(0, stderr="[100%] /data/app/.../base.apk\n")

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk",
            dst, serial="DEV1",
        )

    assert ok is True, f"auto-recover must turn a flap into a success, err={err!r}"
    assert err == ""
    # Exactly two pull invocations: original + retry.
    assert state["pull_calls"] == 2
    assert dst.is_file() and dst.stat().st_size > 0


def test_pull_recovers_after_silent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original v1.8.x bug had stderr EMPTY after stripping
    progress lines — ``adb pull`` dropped mid-transfer before
    writing any error message. Verify we treat empty-stderr as a
    likely-disconnect signal and try the recovery path too."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"
    monkeypatch.setattr(lspatch_pipeline.time, "sleep", lambda _s: None)

    state = {"pulls": 0, "states": 0}

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            state["states"] += 1
            return _completed(0, stdout="device\n")
        state["pulls"] += 1
        if state["pulls"] == 1:
            # Mid-transfer drop: only progress lines, no real error.
            return _completed(
                1,
                stderr="[  0%] /data/app/.../base.apk\n"
                       "[ 47%] /data/app/.../base.apk\n",
            )
        dst.write_bytes(b"PK\x03\x04" + b"\x00" * 4096)
        return _completed(0, stderr="[100%] /data/app/.../base.apk\n")

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk",
            dst, serial="DEV1",
        )

    assert ok is True
    assert state["pulls"] == 2, (
        "silent stderr is the classic mid-pull disconnect signature — "
        "must trigger the auto-recover path"
    )


def test_pull_gives_up_when_device_does_not_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the device never comes back within the recovery window,
    surface the device-disconnect Thai error — don't retry forever
    and don't fall through to exec-out (which would also fail with
    the same noise)."""
    pipe = _make_pipeline(tmp_path)
    dst = pipe.pulled_dir / "base.apk"
    monkeypatch.setattr(lspatch_pipeline.time, "sleep", lambda _s: None)
    # Make the recovery wait loop bail immediately by advancing
    # the monotonic clock past its deadline.
    clock = {"t": 1000.0}

    def _fake_monotonic():
        clock["t"] += 5.0
        return clock["t"]

    monkeypatch.setattr(lspatch_pipeline.time, "monotonic", _fake_monotonic)

    counts = {"pulls": 0, "states": 0}

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            counts["states"] += 1
            # Pre-flight passes; later polls all say "not found".
            if counts["states"] == 1:
                return _completed(0, stdout="device\n")
            return _completed(1, stderr="error: device 'DEV1' not found")
        counts["pulls"] += 1
        return _completed(1, stderr="adb: error: device 'DEV1' not found\n")

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, err = pipe._pull_apk_with_fallback(
            "/data/app/~~abc==/com.tiktok-xyz==/base.apk",
            dst, serial="DEV1",
        )

    assert ok is False
    # Exactly one pull — recovery polling found nothing, no retry.
    assert counts["pulls"] == 1, (
        f"expected exactly 1 pull (no retry), got {counts['pulls']}"
    )
    assert "หลุดจาก adb" in err


# ── keep-awake layer (v1.8.x customer recurrence fix) ─────────


def test_pull_tiktok_wraps_flow_with_keep_awake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pull pipeline must wake the screen + force
    ``stayon usb`` BEFORE any APK pull (so OEM battery managers
    can't suspend ``adbd`` mid-transfer) and restore stayon to
    ``false`` AFTER, even when the pull fails.

    This is a single-test pin on the whole pipeline-level wrap,
    not on the helper internals — that way the test stays valid
    if we ever change the exact shell commands but keep the
    contract."""
    pipe = _make_pipeline(tmp_path)
    # Skip the package-detection round-trip by passing a package
    # explicitly. The flow still has to do ``pm path`` and
    # ``dumpsys``, then enter the pull loop where things fail.
    shell_log: list[str] = []

    def _fake_shell(cmd, serial=None):
        shell_log.append(cmd)
        if cmd.startswith("pm path"):
            return "package:/data/app/~~abc==/com.tiktok-xyz==/base.apk"
        if cmd.startswith("dumpsys package"):
            return "    versionName=99.9.9"
        return ""

    def _fake_run(cmd, *args, **kw):
        if _is_get_state(cmd):
            return _completed(0, stdout="device\n")
        # Pull always fails so we hit the error path — keep-awake
        # cleanup still must run.
        return _completed(1, stderr="adb: error: failed to copy: denied\n")

    with patch.object(pipe, "_adb_shell", side_effect=_fake_shell), \
         patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        result = pipe.pull_tiktok(package="com.ss.android.ugc.trill", serial="DEV1")

    assert result.ok is False
    # Both wake events must fire before we touch the APK.
    wake_idx = next(
        (i for i, c in enumerate(shell_log) if "KEYCODE_WAKEUP" in c), -1,
    )
    stayon_on_idx = next(
        (i for i, c in enumerate(shell_log) if c == "svc power stayon usb"), -1,
    )
    pm_path_idx = next(
        (i for i, c in enumerate(shell_log) if c.startswith("pm path")), -1,
    )
    assert 0 <= wake_idx < pm_path_idx, (
        "screen must be woken BEFORE the first APK-discovery shell call"
    )
    assert 0 <= stayon_on_idx < pm_path_idx, (
        "stayon must be set BEFORE the first APK-discovery shell call"
    )
    # And cleanup must run even though the pull failed.
    assert "svc power stayon false" in shell_log, (
        "stayon must be restored to false even when the pull errors out"
    )
    # The restore must come AFTER the pull attempt, not before.
    stayon_off_idx = shell_log.index("svc power stayon false")
    assert stayon_off_idx > pm_path_idx


def test_keep_awake_helpers_are_best_effort(
    tmp_path: Path,
) -> None:
    """The keep-awake helpers must never raise — a customer with
    a flaky USB connection might lose adbd between the shell
    invocations. Wrap-failure must not bubble into the pull
    pipeline's exception handler and trash the user's session."""
    pipe = _make_pipeline(tmp_path)

    def _boom(cmd, serial=None):
        raise RuntimeError("adb shell exploded")

    with patch.object(pipe, "_adb_shell", side_effect=_boom):
        # Both must complete without raising.
        pipe._keep_device_awake(serial="DEV1")
        pipe._release_keep_awake(serial="DEV1")


# ── pre-flight check helper ───────────────────────────────────


def test_pre_pull_check_passes_when_state_is_device(
    tmp_path: Path,
) -> None:
    pipe = _make_pipeline(tmp_path)
    with patch.object(pipe, "_device_state", return_value="device"):
        ok, err = pipe._pre_pull_check(serial="DEV1")
    assert ok is True and err == ""


def test_pre_pull_check_allows_pipeline_to_proceed_on_probe_failure(
    tmp_path: Path,
) -> None:
    """If ``adb get-state`` itself fails to give us an answer
    (empty string), we don't know the state — let the ladder try
    and surface whatever real error comes back. Blocking on a
    flaky probe would cost customers more than it saves."""
    pipe = _make_pipeline(tmp_path)
    with patch.object(pipe, "_device_state", return_value=""):
        ok, err = pipe._pre_pull_check(serial="DEV1")
    assert ok is True and err == ""


@pytest.mark.parametrize(
    "state,must_contain",
    [
        ("unauthorized", "Allow"),
        ("offline", "kill-server"),
        ("no-device", "สาย USB"),
    ],
)
def test_pre_pull_check_blocks_on_bad_state_with_specific_hint(
    tmp_path: Path, state: str, must_contain: str,
) -> None:
    """Each bad state must have its own remediation. Lumping them
    into a generic message was the bug we were trying to fix."""
    pipe = _make_pipeline(tmp_path)
    with patch.object(pipe, "_device_state", return_value=state):
        ok, err = pipe._pre_pull_check(serial="DEV1")
    assert ok is False
    assert must_contain in err
