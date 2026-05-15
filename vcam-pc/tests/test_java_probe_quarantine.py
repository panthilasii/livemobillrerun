"""Unit tests for the quarantine-tolerant ``java -version`` probe.

macOS Gatekeeper makes the first ``java -version`` after extracting
the customer ZIP take 10-30 s while syspolicyd notarization-checks
every dylib in ``Contents/Home/lib/``. The 5 s timeout we shipped
in v1.8.12 false-alarmed customers as ``java probe failed`` even
though the bundled JDK was healthy. These tests pin the new probe
behaviour so we don't regress:

* a fast-success first call returns the version string
* a generic ``OSError`` is surfaced verbatim (no retry — xattr
  doesn't help with missing exec bit / wrong arch)
* a ``TimeoutExpired`` triggers ``xattr -dr com.apple.quarantine``
  on the bundled ``jdk-21/`` directory and retries exactly once
* the success after retry returns the second probe's version
* the failure after retry surfaces the Thai-language hint pointing
  at the most common cause (folder still in ``~/Downloads``)

We mock ``subprocess.run`` rather than spawning a real ``java`` so
the tests run on every CI host (Linux, Windows, macOS) without
needing a JDK installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import lspatch_pipeline  # noqa: E402
from src.lspatch_pipeline import (  # noqa: E402
    _jdk_root_from_java,
    _probe_java_version,
    _self_heal_jdk,
    _strip_motw_windows,
    _strip_quarantine_macos,
    detect_cloud_sync_folder,
    jdk_diagnostic,
    warm_up_java,
)


# ── _jdk_root_from_java ────────────────────────────────────────


def test_jdk_root_finds_macos_layout(tmp_path: Path) -> None:
    """Adoptium macOS bundles put ``java`` 4 levels under ``jdk-21/``.

    Walking parents must stop at the first ``jdk-21`` ancestor so
    the quarantine strip doesn't accidentally bleed up into the
    customer's whole ``.tools/macos/`` tree.
    """
    java = tmp_path / "jdk-21" / "Contents" / "Home" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()
    assert _jdk_root_from_java(java) == tmp_path / "jdk-21"


def test_jdk_root_finds_linux_layout(tmp_path: Path) -> None:
    java = tmp_path / "jdk-21" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()
    assert _jdk_root_from_java(java) == tmp_path / "jdk-21"


def test_jdk_root_returns_none_for_system_java(tmp_path: Path) -> None:
    """``/usr/bin/java`` lives outside our bundled layout — we must
    NOT try to ``xattr`` arbitrary system paths."""
    sysjava = tmp_path / "usr" / "bin" / "java"
    sysjava.parent.mkdir(parents=True)
    sysjava.touch()
    assert _jdk_root_from_java(sysjava) is None


# ── _probe_java_version: fast paths ────────────────────────────


def _completed(stderr: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout="", stderr=stderr)


def test_probe_returns_version_on_first_success() -> None:
    """Happy path — Adoptium prints the version on stderr line 1."""
    stderr = (
        'openjdk version "21.0.5" 2024-10-15 LTS\n'
        'OpenJDK Runtime Environment Temurin-21.0.5+11 (build 21.0.5+11-LTS)\n'
    )
    with patch.object(lspatch_pipeline.subprocess, "run",
                      return_value=_completed(stderr)):
        ok, vstr, err = _probe_java_version(Path("/fake/java"))
    assert ok is True
    assert "21.0.5" in vstr
    assert err == ""


def test_probe_surfaces_oserror_without_retry() -> None:
    """Missing exec bit / wrong arch produces ``OSError`` — xattr
    can't fix that, so we must NOT waste a second probe."""
    calls = []

    def _raise(*args, **kw):
        calls.append(args)
        raise OSError("Exec format error")

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_raise):
        ok, _, err = _probe_java_version(Path("/fake/java"))
    assert ok is False
    assert "Exec format error" in err
    assert len(calls) == 1, "OSError must NOT trigger a retry"


# ── _probe_java_version: timeout → quarantine strip → retry ────


def test_probe_strips_quarantine_and_retries_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First probe times out → strip quarantine → second probe wins."""
    java = tmp_path / "jdk-21" / "Contents" / "Home" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()

    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    monkeypatch.setattr(
        lspatch_pipeline.shutil, "which",
        lambda name: "/usr/bin/xattr" if name == "xattr" else None,
    )

    success = _completed(
        'openjdk version "21.0.5" 2024-10-15 LTS\n'
    )

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kw):
        calls.append(list(cmd))
        if "xattr" in cmd[0]:
            return _completed("", returncode=0)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        return success

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, vstr, err = _probe_java_version(java)

    assert ok is True
    assert "21.0.5" in vstr
    assert err == ""

    assert len(calls) == 3, "expected probe → xattr → probe"
    assert "xattr" in calls[1][0] and "com.apple.quarantine" in calls[1]
    assert str(tmp_path / "jdk-21") in calls[1]


def test_probe_thai_hint_when_retry_also_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both probes time out — surface the Downloads/Applications hint
    so the customer knows what to do without reading source."""
    java = tmp_path / "jdk-21" / "Contents" / "Home" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()

    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    monkeypatch.setattr(
        lspatch_pipeline.shutil, "which",
        lambda name: "/usr/bin/xattr" if name == "xattr" else None,
    )

    def _fake_run(cmd, *args, **kw):
        if "xattr" in cmd[0]:
            return _completed("", returncode=0)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, _, err = _probe_java_version(java)

    assert ok is False
    assert "timed out" in err.lower()
    assert "Downloads" in err
    assert "Applications" in err or "Documents" in err


# ── _strip_quarantine_macos ────────────────────────────────────


def test_strip_quarantine_noop_on_non_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "linux",
    )
    assert _strip_quarantine_macos(tmp_path) is False


def test_strip_quarantine_noop_when_xattr_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    monkeypatch.setattr(lspatch_pipeline.shutil, "which", lambda _: None)
    assert _strip_quarantine_macos(tmp_path) is False


def test_strip_quarantine_returns_true_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    monkeypatch.setattr(
        lspatch_pipeline.shutil, "which",
        lambda name: "/usr/bin/xattr" if name == "xattr" else None,
    )

    with patch.object(lspatch_pipeline.subprocess, "run",
                      return_value=_completed("", returncode=0)):
        assert _strip_quarantine_macos(tmp_path) is True


# ── _strip_motw_windows ────────────────────────────────────────


def test_strip_motw_noop_on_non_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    assert _strip_motw_windows(tmp_path) is False


def test_strip_motw_noop_when_powershell_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )
    monkeypatch.setattr(lspatch_pipeline.shutil, "which", lambda _: None)
    assert _strip_motw_windows(tmp_path) is False


def test_strip_motw_runs_powershell_unblock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Verify the PowerShell command structure — it's brittle copy
    the customer would paste from support, so the surface matters."""
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )
    monkeypatch.setattr(
        lspatch_pipeline.shutil, "which",
        lambda name: r"C:\Windows\System32\powershell.exe"
        if name in ("powershell", "powershell.exe") else None,
    )

    captured: list[list[str]] = []

    def _fake_run(cmd, *args, **kw):
        captured.append(list(cmd))
        return _completed("", returncode=0)

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        assert _strip_motw_windows(tmp_path) is True

    assert captured, "Unblock-File command was never invoked"
    cmd = captured[0]
    assert "powershell" in cmd[0].lower()
    assert "-NoProfile" in cmd
    assert "Bypass" in cmd
    joined = " ".join(cmd)
    assert "Unblock-File" in joined
    assert str(tmp_path) in joined


# ── _self_heal_jdk dispatch ────────────────────────────────────


def test_self_heal_dispatches_to_macos_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    called = {"mac": False, "win": False}

    def _mac(target):
        called["mac"] = True
        return True

    def _win(target):
        called["win"] = True
        return True

    monkeypatch.setattr(lspatch_pipeline, "_strip_quarantine_macos", _mac)
    monkeypatch.setattr(lspatch_pipeline, "_strip_motw_windows", _win)
    assert _self_heal_jdk(tmp_path) is True
    assert called == {"mac": True, "win": False}


def test_self_heal_dispatches_to_windows_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )
    called = {"mac": False, "win": False}
    monkeypatch.setattr(
        lspatch_pipeline, "_strip_quarantine_macos",
        lambda t: called.__setitem__("mac", True) or True,
    )
    monkeypatch.setattr(
        lspatch_pipeline, "_strip_motw_windows",
        lambda t: called.__setitem__("win", True) or True,
    )
    assert _self_heal_jdk(tmp_path) is True
    assert called == {"mac": False, "win": True}


def test_self_heal_returns_false_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Linux has no equivalent — bundled JDK already executes
    without per-file signature checks. Skip the heuristic."""
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "linux",
    )
    assert _self_heal_jdk(tmp_path) is False


# ── detect_cloud_sync_folder ──────────────────────────────────


def test_cloud_sync_detects_onedrive() -> None:
    p = Path(r"C:\Users\Bob\OneDrive\NP-Create\.tools\windows\jdk-21\bin\java.exe")
    assert "OneDrive" in detect_cloud_sync_folder(p)


def test_cloud_sync_detects_icloud() -> None:
    p = Path("/Users/me/Library/Mobile Documents/com~apple~CloudDocs/NP/jdk-21/bin/java")
    assert "iCloud Drive" in detect_cloud_sync_folder(p)


def test_cloud_sync_detects_dropbox() -> None:
    p = Path("/Users/me/Dropbox/NP-Create/jdk-21/bin/java")
    assert "Dropbox" in detect_cloud_sync_folder(p)


def test_cloud_sync_detects_google_drive() -> None:
    p = Path("/Users/me/Library/CloudStorage/GoogleDrive-foo/jdk-21/bin/java")
    assert "Google Drive" in detect_cloud_sync_folder(p)


def test_cloud_sync_clean_for_local_disk() -> None:
    assert detect_cloud_sync_folder(Path("/Applications/NP-Create/jdk-21/bin/java")) == ""
    assert detect_cloud_sync_folder(Path(r"C:\NP-Create\jdk-21\bin\java.exe")) == ""


def test_cloud_sync_dedupes_multiple_hints() -> None:
    """``CloudDocs`` and ``Mobile Documents`` both map to iCloud
    Drive — the result must list it once, not twice."""
    p = Path("/Users/me/Library/Mobile Documents/com~apple~CloudDocs/x/jdk-21/bin/java")
    out = detect_cloud_sync_folder(p)
    assert out.count("iCloud Drive") == 1


# ── _probe_java_version: cloud-sync hint surfaces in error ────


def test_probe_timeout_message_mentions_cloud_when_path_in_cloud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the JDK is under OneDrive/iCloud, the timeout message
    must call that out so the customer knows to move it off cloud
    storage first — otherwise they'd just retry from the same
    placeholder file."""
    onedrive = tmp_path / "OneDrive" / "NP-Create" / ".tools" / "windows" / "jdk-21"
    java = onedrive / "bin" / "java.exe"
    java.parent.mkdir(parents=True)
    java.touch()

    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )
    monkeypatch.setattr(lspatch_pipeline.shutil, "which", lambda _: None)

    def _fake_run(cmd, *args, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    with patch.object(lspatch_pipeline.subprocess, "run", side_effect=_fake_run):
        ok, _, err = _probe_java_version(java)

    assert ok is False
    assert "OneDrive" in err
    assert "Unblock-File" in err  # Windows copy-paste hint
    assert "PowerShell" in err


def test_probe_oserror_message_lists_jdk_repair_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError (missing exec bit, wrong arch) should explain the
    fix in Thai with concrete actions."""
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    with patch.object(lspatch_pipeline.subprocess, "run",
                      side_effect=OSError("Bad CPU type")):
        ok, _, err = _probe_java_version(Path("/fake/java"))
    assert ok is False
    assert "Bad CPU type" in err
    # Must point at the most likely fixes.
    assert "Rosetta" in err or "ARM" in err.upper()
    assert "antivirus" in err or "ZIP" in err


# ── warm_up_java (dispatch only — no real subprocess) ─────────


def test_warm_up_java_noop_on_none() -> None:
    """``warm_up_java(None)`` must NOT spawn a thread or raise —
    happens on machines without a bundled JDK at all."""
    warm_up_java(None)  # smoke test: no crash, no thread leak


def test_warm_up_java_spawns_daemon_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Warm-up must run in the background so it can't block
    the Tk window from opening — the whole point is to hide the
    10-30 s cold-start cost from the customer."""
    java = tmp_path / "jdk-21" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()

    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "linux",
    )
    monkeypatch.setattr(
        lspatch_pipeline, "_run_java_version",
        lambda j, t: (True, "openjdk 21", ""),
    )

    warm_up_java(java)

    import threading
    import time
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        names = {t.name for t in threading.enumerate()}
        if "java-warm-up" in names or all(
            "java-warm-up" not in n for n in names
        ):
            break
        time.sleep(0.01)


# ── jdk_diagnostic ────────────────────────────────────────────


def test_jdk_diagnostic_handles_missing_java() -> None:
    info = jdk_diagnostic(None)
    assert info["java_path"] == ""
    assert info["java_exists"] is False
    assert info["jvm_size"] == 0


def test_jdk_diagnostic_reports_jvm_size_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Adoptium macOS bundles ship libjvm.dylib at
    ``Contents/Home/lib/server/`` — the diagnostic must read that
    exact path so support can spot AV-truncated installs (where
    the file shrinks from ~17 MB to a few KB)."""
    jdk = tmp_path / "jdk-21"
    java = jdk / "Contents" / "Home" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()
    libjvm = jdk / "Contents" / "Home" / "lib" / "server" / "libjvm.dylib"
    libjvm.parent.mkdir(parents=True)
    libjvm.write_bytes(b"\x00" * 17_000_000)

    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "macos",
    )
    info = jdk_diagnostic(java)
    assert info["java_exists"] is True
    assert info["jdk_root"] == str(jdk)
    assert info["jvm_size"] == 17_000_000


def test_jdk_diagnostic_surfaces_cloud_sync_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    onedrive = tmp_path / "OneDrive" / "jdk-21" / "bin" / "java.exe"
    onedrive.parent.mkdir(parents=True)
    onedrive.touch()
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )
    info = jdk_diagnostic(onedrive)
    assert "OneDrive" in info["cloud_sync"]


# ── "could not find java.dll" detection (the v1.8.13 bug) ─────


def test_probe_detects_could_not_find_java_dll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The exact error customers reported on Windows with Thai
    usernames: ``java.exe`` runs (so timeout doesn't fire and
    OSError doesn't fire) but emits ``Error: could not find
    java.dll`` on stderr. Our pre-fix code parsed that string
    as the version, ran it through the regex, found no major
    digit, and reported "Java 0 is too old" — masking the real
    cause from the customer."""
    java = tmp_path / "ปอร์เช่" / "jdk-21" / "bin" / "java.exe"
    java.parent.mkdir(parents=True)
    java.touch()

    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )

    err_out = "Error: could not find java.dll\nError: Could not find Java SE Runtime Environment.\n"
    with patch.object(
        lspatch_pipeline.subprocess, "run",
        return_value=_completed(err_out, returncode=1),
    ):
        ok, vstr, err = lspatch_pipeline._probe_java_version(java)

    assert ok is False, "Error output must NOT be reported as success"
    assert "java.dll" in err
    # Must surface the non-ASCII fix because the path contains Thai.
    assert "ภาษาอังกฤษ" in err or "ASCII" in err.upper()


def test_probe_skips_picked_up_java_options_banner() -> None:
    """If the customer set ``_JAVA_OPTIONS`` for some other tool,
    Java prints a banner BEFORE the version. We must skip it,
    not parse "Picked up _JAVA_OPTIONS" as the version."""
    out = (
        "Picked up _JAVA_OPTIONS: -Dfile.encoding=UTF-8\n"
        'openjdk version "21.0.5" 2024-10-15 LTS\n'
    )
    with patch.object(
        lspatch_pipeline.subprocess, "run", return_value=_completed(out),
    ):
        ok, vstr, err = lspatch_pipeline._probe_java_version(Path("/fake/java"))
    assert ok is True
    assert "21.0.5" in vstr


def test_probe_rejects_bare_error_line() -> None:
    """Any first line starting with ``Error:`` must fail probe."""
    out = "Error: opening registry key 'Software\\JavaSoft\\...'\n"
    with patch.object(
        lspatch_pipeline.subprocess, "run", return_value=_completed(out),
    ):
        ok, _, err = lspatch_pipeline._probe_java_version(Path("/fake/java"))
    assert ok is False
    assert "unexpected" in err.lower() or "Error" in err


def test_probe_rejects_garbage_output() -> None:
    """A binary that's not actually Java but happens to print
    something on stderr must not be parsed as a version."""
    out = "this is not a java -version output at all\n"
    with patch.object(
        lspatch_pipeline.subprocess, "run", return_value=_completed(out),
    ):
        ok, _, err = lspatch_pipeline._probe_java_version(Path("/fake/java"))
    assert ok is False
    assert "unexpected" in err.lower()


# ── path utilities ────────────────────────────────────────────


def test_path_has_non_ascii_detects_thai() -> None:
    """Thai characters in user folder are the v1.8.13 root cause."""
    p = Path(r"C:\Users\ปอร์เช่\Downloads\NP-Create\jdk-21\bin\java.exe")
    assert lspatch_pipeline._path_has_non_ascii(p) is True


def test_path_has_non_ascii_clean_for_ascii_path() -> None:
    p = Path(r"C:\Users\bob\NP-Create\jdk-21\bin\java.exe")
    assert lspatch_pipeline._path_has_non_ascii(p) is False


def test_jdk_diagnostic_flags_non_ascii(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Diagnostic must flag the non-ASCII path so support can spot
    the issue before the customer even hits Patch."""
    java = tmp_path / "บอบ" / "jdk-21" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "linux",
    )
    info = jdk_diagnostic(java)
    assert info["non_ascii_path"] is True


def test_jdk_diagnostic_reports_java_dll_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Windows: when ``bin/java.dll`` is gone (AV quarantine /
    incomplete extract), ``java_dll_present`` must be False so
    support sees the smoking gun in the diagnostic."""
    jdk = tmp_path / "jdk-21"
    java = jdk / "bin" / "java.exe"
    java.parent.mkdir(parents=True)
    java.touch()
    # Note: we deliberately do NOT create java.dll.
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )
    info = jdk_diagnostic(java)
    assert info["java_dll_present"] is False


def test_jdk_diagnostic_reports_java_dll_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    jdk = tmp_path / "jdk-21"
    bin_dir = jdk / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "java.exe").touch()
    (bin_dir / "java.dll").write_bytes(b"\x00" * 1024)
    monkeypatch.setattr(
        lspatch_pipeline.platform_tools, "current_os", lambda: "windows",
    )
    info = jdk_diagnostic(bin_dir / "java.exe")
    assert info["java_dll_present"] is True
