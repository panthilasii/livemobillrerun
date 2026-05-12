"""Startup diagnostic — plain-text snapshot of every runtime path
the app resolves, written to ``<logs>/startup-diagnostic.txt`` on
every launch.

Why this module exists
======================

v1.7.8 / v1.7.9 shipped the Inno + PyInstaller installer
(`Setup.exe`). Customers reported "the wizard never finds my
phone" — symptom: the Step 2 page is stuck on
"🔄 รอเครื่องเชื่อมต่อ…" and no "Allow USB Debugging" popup
appears on the device.

That symptom is opaque because there are *three* path-resolution
layers between an app launch and an actual ``adb devices`` call:

1. ``config.PROJECT_ROOT`` — frozen vs source mode anchor.
2. ``platform_tools._tools_root_base()`` / ``LEGACY_TOOLS_ROOT``
   — where ``find_adb()`` looks for the bundled binary.
3. ``adb.AdbController._resolve()`` — final binary path the
   subprocess actually invokes.

A wrong answer in any one of those silently degrades the wizard
to "no devices ever". The customer can't tell us which layer
failed, and we can't reproduce on dev because dev mode uses a
different anchor.

This module writes the answer for ALL three layers, plus a live
``adb version`` probe, to a single human-readable file. When a
customer hits the "no popup" symptom the admin can ask them for
*one* file and see the failure mode at a glance.

The diagnostic is *non-blocking*: any exception inside is
caught and swallowed. We must never let a diagnostic crash
prevent the app from launching.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

DIAGNOSTIC_FILENAME = "startup-diagnostic.txt"


def write_diagnostic(log_dir: Path | None = None) -> Path | None:
    """Write the diagnostic snapshot. Returns the file path, or None
    on failure. Never raises.

    ``log_dir`` defaults to ``<PROJECT_ROOT>/logs`` — same place
    ``log_setup.configure_logging`` writes ``npcreate.log``, so a
    single ZIP from "Settings → Send Logs" picks up both files.
    """
    try:
        # Imports are inside the function so a broken import (e.g.
        # missing customtkinter on a stripped-down build) doesn't
        # take down the diagnostic itself. We deliberately don't
        # diagnostic-via-print here — the customer might not have a
        # console at all (PyInstaller --noconsole).
        return _write_unsafe(log_dir)
    except Exception:
        # Never let the diagnostic block startup.
        return None


def _write_unsafe(log_dir: Path | None) -> Path | None:
    from . import platform_tools
    from .branding import BRAND
    from .config import DATA_ROOT, PROJECT_ROOT

    target_dir = log_dir or (DATA_ROOT / "logs")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / DIAGNOSTIC_FILENAME

    lines: list[str] = []

    def w(line: str = "") -> None:
        lines.append(line)

    def kv(k: str, v) -> None:
        lines.append(f"  {k:32s} = {v}")

    w(f"NP Create startup diagnostic — v{BRAND.version}")
    w("=" * 70)
    import datetime as _dt
    w(f"written: {_dt.datetime.now().isoformat(timespec='seconds')}")
    w()

    # ── runtime ──────────────────────────────────────────────────
    w("Python / runtime")
    w("-" * 40)
    kv("python.version", sys.version.replace("\n", " "))
    kv("platform", platform.platform())
    kv("sys.frozen", getattr(sys, "frozen", False))
    kv("sys.executable", sys.executable)
    kv("sys.argv", sys.argv)
    kv("sys._MEIPASS", getattr(sys, "_MEIPASS", "(not set)"))
    kv("os.getcwd()", os.getcwd())
    w()

    # ── project paths ────────────────────────────────────────────
    w("Project paths")
    w("-" * 40)
    kv("DATA_ROOT", DATA_ROOT)
    kv("  exists", DATA_ROOT.is_dir())
    kv("PROJECT_ROOT", PROJECT_ROOT)
    kv("  exists", PROJECT_ROOT.is_dir())
    try:
        # _tools_root_base is the v1.7.9 fix point — show the value
        # so the admin can verify the resolver picked the right
        # anchor for whichever distribution method the customer used.
        base = platform_tools._tools_root_base()
        kv("_tools_root_base()", base)
        kv("  exists", base.is_dir())
    except Exception as exc:
        kv("_tools_root_base()", f"<error: {exc!r}>")
    kv("LEGACY_TOOLS_ROOT", platform_tools.LEGACY_TOOLS_ROOT)
    kv("  exists", platform_tools.LEGACY_TOOLS_ROOT.is_dir())
    try:
        per_os = platform_tools.tools_root_for()
        kv("tools_root_for(os)", per_os)
        kv("  exists", per_os.is_dir())
        if per_os.is_dir():
            # List immediate children so the admin can see whether
            # the install layout matches expectations (platform-tools/
            # jdk-21/ lspatch/ scrcpy/ ffmpeg.exe).
            try:
                children = sorted(p.name for p in per_os.iterdir())
                kv("  contents", ", ".join(children) or "(empty)")
            except Exception as exc:
                kv("  contents", f"<error: {exc!r}>")
    except Exception as exc:
        kv("tools_root_for()", f"<error: {exc!r}>")
    w()

    # ── tool resolution ──────────────────────────────────────────
    w("Tool resolution")
    w("-" * 40)
    for name, fn in [
        ("find_adb", platform_tools.find_adb),
        ("find_ffmpeg", platform_tools.find_ffmpeg),
        ("find_java", platform_tools.find_java),
        ("find_lspatch_jar", platform_tools.find_lspatch_jar),
        ("find_scrcpy", platform_tools.find_scrcpy),
        ("find_vcam_apk", platform_tools.find_vcam_apk),
    ]:
        try:
            r = fn()
        except Exception as exc:
            r = f"<error: {exc!r}>"
        kv(name, r)
    w()

    # ── adb live test ────────────────────────────────────────────
    w("ADB liveness test")
    w("-" * 40)
    try:
        adb = platform_tools.find_adb()
        if adb is None:
            w("  find_adb() returned None — no bundled adb on disk.")
            sys_adb = shutil.which("adb")
            kv("shutil.which('adb')", sys_adb or "(not on PATH)")
        else:
            try:
                r = subprocess.run(
                    [str(adb), "version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                kv("adb version returncode", r.returncode)
                kv("adb version stdout", (r.stdout or "").strip()[:300])
                if r.stderr:
                    kv("adb version stderr", r.stderr.strip()[:300])
            except Exception as exc:
                kv("adb version error", repr(exc))

            # adb devices output — the most direct evidence of
            # whether the bundled binary can actually talk to USB.
            try:
                r = subprocess.run(
                    [str(adb), "devices", "-l"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                kv("adb devices returncode", r.returncode)
                kv("adb devices stdout", (r.stdout or "").strip()[:500])
                if r.stderr:
                    kv("adb devices stderr", r.stderr.strip()[:300])
            except Exception as exc:
                kv("adb devices error", repr(exc))
    except Exception as exc:
        kv("liveness probe error", repr(exc))
    w()

    # ── env overrides ───────────────────────────────────────────
    w("Environment overrides")
    w("-" * 40)
    for k in (
        "NPCREATE_TOOLS_ROOT",
        "ADB_PATH",
        "ANDROID_HOME",
        "JAVA_HOME",
        "PATH",
    ):
        v = os.environ.get(k, "(unset)")
        # PATH on Windows can be huge — truncate to keep the
        # diagnostic readable.
        if k == "PATH" and len(v) > 500:
            v = v[:497] + "..."
        kv(k, v)
    w()

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
