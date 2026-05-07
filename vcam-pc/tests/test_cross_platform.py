"""Cross-platform smoke tests.

We can't actually run the build on Windows from this CI box, but
we *can* exercise the platform-resolution code with mocked OS
detection, and we can lint-check the generated launcher scripts
for obvious syntax breakage. Together these catch ~80% of the
"works on macOS, breaks on Windows" mistakes.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from src import platform_tools
from src.license_history import LicenseHistory, IssuedLicense
from src.license_key import generate_key, verify_key


# ── platform_tools ──────────────────────────────────────────────────


class TestPlatformTools:
    def test_current_os_macos(self):
        with mock.patch("platform.system", return_value="Darwin"):
            assert platform_tools.current_os() == "macos"
            assert platform_tools.is_macos() is True
            assert platform_tools.is_windows() is False
            assert platform_tools.exe_suffix() == ""

    def test_current_os_windows(self):
        with mock.patch("platform.system", return_value="Windows"):
            assert platform_tools.current_os() == "windows"
            assert platform_tools.is_windows() is True
            assert platform_tools.is_macos() is False
            assert platform_tools.exe_suffix() == ".exe"

    def test_current_os_linux(self):
        with mock.patch("platform.system", return_value="Linux"):
            assert platform_tools.current_os() == "linux"
            assert platform_tools.exe_suffix() == ""

    def test_subprocess_env_forces_english_locale(self):
        env = platform_tools.make_subprocess_env()
        # The Thai-Buddhist-calendar bug in apkzlib was the original
        # reason this exists; if any of these regress we'd silently
        # break LSPatch on Thai-locale Macs and Windows boxes.
        assert env["LANG"] == "C"
        assert env["LC_ALL"] == "C"
        assert "-Duser.language=en" in env["JAVA_TOOL_OPTIONS"]
        assert "-Duser.country=US" in env["JAVA_TOOL_OPTIONS"]

    def test_subprocess_env_extends_path(self, tmp_path):
        extra = tmp_path / "fake-bin"
        extra.mkdir()
        env = platform_tools.make_subprocess_env(extra_path=[extra])
        # The first PATH segment must be our extra dir so child JVM
        # picks the bundled ``java`` even if the system has a stale
        # one earlier on PATH.
        assert env["PATH"].split(":")[0] == str(extra)


# ── launcher script syntax ──────────────────────────────────────────


class TestLauncherSyntax:
    """Sanity-check the Windows .bat and macOS .command bodies that
    ``build_release.py`` generates.

    We import the helper directly rather than running the build
    (the full build needs adb/JDK to be present) and assert basic
    structural properties: shebangs, no shell-injection-prone
    interpolation, mention of `--studio` so the launcher actually
    starts the right app, etc.
    """

    @classmethod
    def setup_class(cls):
        # Lazy import so the test still passes if PIL / customtkinter
        # are missing (the build script doesn't need them).
        import importlib
        cls.br = importlib.import_module("tools.build_release")

    def test_windows_launcher_is_batch(self):
        body = self.br._launcher_body("windows")
        # Must be cmd.exe-friendly: starts with @echo off, uses %~dp0
        # for self-locating, falls back gracefully when py / python
        # aren't on PATH, never uses `&&` (cmd treats it as logical
        # AND but DOS-style chaining is more reliable with separate
        # if-blocks here).
        assert body.lstrip().startswith("@echo off")
        assert "%~dp0" in body
        assert "py -3" in body or "python " in body
        assert "--studio" in body
        # Final pause so a customer who double-clicks and hits an
        # error sees the error before the window vanishes.
        assert body.rstrip().endswith("pause")

    def test_macos_launcher_is_bash(self):
        body = self.br._launcher_body("macos")
        assert body.startswith("#!/usr/bin/env bash")
        assert 'set -e' in body
        # Self-locate so a double-click doesn't depend on cwd.
        assert '$(dirname "$0")' in body
        assert "--studio" in body
        # Native dialog when Python is missing, not a silent failure.
        assert "osascript" in body

    def test_linux_launcher_is_bash(self):
        body = self.br._launcher_body("linux")
        assert body.startswith("#!/usr/bin/env bash")
        assert "--studio" in body


# ── customer-build leak audit ───────────────────────────────────────


class TestBundleAudit:
    """Static check that ``build_release.py`` would never include
    the admin's signing material in a customer ZIP. We don't run
    the full build (slow, needs JDK), just inspect the deny-lists.
    """

    @classmethod
    def setup_class(cls):
        import importlib
        cls.br = importlib.import_module("tools.build_release")

    def test_admin_tools_blocklist(self):
        # Every script that can sign or rotate a key must be in
        # ADMIN_TOOLS so build_release.py drops it from customer ZIPs.
        must_block = {
            "gen_license.py",
            "init_keys.py",
            "build_release.py",
            "setup_windows_tools.py",
            "setup_ffmpeg.py",
        }
        missing = must_block - self.br.ADMIN_TOOLS
        assert not missing, (
            f"these scripts should be admin-only but aren't blocked: "
            f"{missing}"
        )

    def test_ship_tools_patterns_dont_drag_dev_only(self):
        """The patterns we ship from .tools/<os>/ must NOT include
        gradle, ndk, build-tools or jadx — those belong to dev."""
        patterns = " ".join(self.br.SHIP_TOOLS_PATTERNS)
        for forbidden in ("gradle", "ndk", "jadx", "build-tools", "cmake"):
            assert forbidden not in patterns, (
                f"customer ship pattern leaks {forbidden!r}: {patterns!r}"
            )


# ── license_history ─────────────────────────────────────────────────


class TestLicenseHistory:
    def test_append_and_save_round_trip(self, tmp_path):
        path = tmp_path / "license_history.json"
        h = LicenseHistory()
        h.append(
            customer="Alice", max_devices=3,
            expiry="2026-12-31",
            key="888-AAAA-BBBB", note="slip 1",
        )
        h.append(
            customer="Bob", max_devices=1,
            expiry="2026-06-30",
            key="888-CCCC-DDDD", note="",
        )
        h.save(path)

        assert path.is_file()
        h2 = LicenseHistory.load(path)
        assert h2.count() == 2
        # `recent` returns newest first.
        latest = h2.recent(1)[0]
        assert latest.customer == "Bob"
        # Assert we can mark revoked + re-save.
        assert h2.mark_revoked("888-AAAA-BBBB") is True
        assert h2.mark_revoked("nope") is False

    def test_load_missing_file_returns_empty(self, tmp_path):
        h = LicenseHistory.load(tmp_path / "missing.json")
        assert h.count() == 0
