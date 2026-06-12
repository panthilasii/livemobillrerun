"""Bundled-tool self-heal (``platform_tools.heal_bundled_tools``).

The customer-facing bug: a macOS .dmg / Safari ZIP stamps
``com.apple.quarantine`` on the bundled ``adb`` and drops the +x bit on
some unzip paths, so Gatekeeper SIGKILLs adb on spawn and the wizard
hangs on "รอเครื่อง…". These pins guard the two behaviours that matter:

* the heal is a no-op on Windows (the flag/problem are macOS-only),
* on a simulated macOS it chmod +x's every resolved binary AND fires
  ``xattr -d com.apple.quarantine`` on each of them.

We can't mutate real Gatekeeper state from CI (mac/linux runners with no
.dmg), so we monkeypatch the OS detector, the resolvers, ``os.chmod`` and
``subprocess.run`` and assert the calls we'd make.
"""
from __future__ import annotations

import stat
from pathlib import Path

import src.platform_tools as pt


def _make_tool(tmp_path: Path, name: str, *, executable: bool) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x7fELF stub")
    mode = 0o644
    if executable:
        mode = 0o755
    p.chmod(mode)
    return p


def test_heal_is_noop_on_windows(monkeypatch, tmp_path):
    adb = _make_tool(tmp_path, "adb.exe", executable=False)
    monkeypatch.setattr(pt, "current_os", lambda: "windows")
    monkeypatch.setattr(pt, "find_adb", lambda: adb)
    monkeypatch.setattr(pt, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(pt, "find_java", lambda: None)
    monkeypatch.setattr(pt, "find_scrcpy", lambda: None)
    monkeypatch.setattr(pt, "find_mediamtx", lambda: None)

    ran: list[list[str]] = []
    monkeypatch.setattr(
        pt.subprocess, "run", lambda *a, **k: ran.append(a[0]) or None
    )

    pt.heal_bundled_tools()

    # Untouched: no chmod, no xattr on Windows.
    assert ran == []
    assert not (adb.stat().st_mode & stat.S_IXUSR)


def test_heal_chmods_and_dequarantines_on_macos(monkeypatch, tmp_path):
    adb = _make_tool(tmp_path, "adb", executable=False)
    ffmpeg = _make_tool(tmp_path, "ffmpeg", executable=False)
    # ffprobe sibling of ffmpeg should be picked up automatically.
    ffprobe = _make_tool(tmp_path, "ffprobe", executable=False)

    monkeypatch.setattr(pt, "current_os", lambda: "macos")
    monkeypatch.setattr(pt, "find_adb", lambda: adb)
    monkeypatch.setattr(pt, "find_ffmpeg", lambda: ffmpeg)
    monkeypatch.setattr(pt, "find_java", lambda: None)
    monkeypatch.setattr(pt, "find_scrcpy", lambda: None)
    monkeypatch.setattr(pt, "find_mediamtx", lambda: None)
    monkeypatch.setattr(pt.shutil, "which", lambda _name: "/usr/bin/xattr")

    xattr_targets: list[str] = []

    def _fake_run(args, *a, **k):
        # args: [xattr, "-d"|"-dr", "com.apple.quarantine", target]
        if args and args[0] == "/usr/bin/xattr":
            xattr_targets.append(args[-1])
        return None

    monkeypatch.setattr(pt.subprocess, "run", _fake_run)
    # Don't actually spawn the background recursive sweep thread.
    monkeypatch.setattr(
        pt.threading, "Thread", lambda *a, **k: type(
            "T", (), {"start": lambda self: None}
        )()
    )

    pt.heal_bundled_tools()

    # Every resolved binary got the +x bit.
    for p in (adb, ffmpeg, ffprobe):
        assert p.stat().st_mode & stat.S_IXUSR, f"{p.name} not executable"

    # And each was de-quarantined (targeted, non-recursive).
    assert str(adb) in xattr_targets
    assert str(ffmpeg) in xattr_targets
    assert str(ffprobe) in xattr_targets


def test_heal_skips_unresolved_tools(monkeypatch, tmp_path):
    # Only adb is bundled; the rest resolve to None and must be skipped
    # without blowing up.
    adb = _make_tool(tmp_path, "adb", executable=False)
    monkeypatch.setattr(pt, "current_os", lambda: "macos")
    monkeypatch.setattr(pt, "find_adb", lambda: adb)
    monkeypatch.setattr(pt, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(pt, "find_java", lambda: None)
    monkeypatch.setattr(pt, "find_scrcpy", lambda: None)
    monkeypatch.setattr(pt, "find_mediamtx", lambda: None)
    monkeypatch.setattr(pt.shutil, "which", lambda _name: "/usr/bin/xattr")
    monkeypatch.setattr(pt.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        pt.threading, "Thread", lambda *a, **k: type(
            "T", (), {"start": lambda self: None}
        )()
    )

    pt.heal_bundled_tools()  # must not raise

    assert adb.stat().st_mode & stat.S_IXUSR
