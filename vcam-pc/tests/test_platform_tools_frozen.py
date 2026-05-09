"""Regression tests for the v1.7.9 ``platform_tools._tools_root_base``
fix.

Background
----------

Pre-1.7.9, ``LEGACY_TOOLS_ROOT`` was always
``PROJECT_ROOT.parent / ".tools"``. That worked for dev /
portable-ZIP launches (where ``PROJECT_ROOT`` points at
``vcam-pc/`` so ``.parent`` lands on the workspace root containing
``.tools/``), but **silently misrouted** the lookup on PyInstaller
frozen builds installed via Inno Setup or .dmg, because those put
``.tools/<os>/`` *next to the executable*, not one level up.

Net effect on Windows .exe: ``find_adb()`` / ``find_java()`` /
``find_lspatch_jar()`` / ``find_vcam_apk()`` all returned ``None``
even though Inno had laid the binaries down correctly. The wizard
froze on "🔄 รอเครื่องเชื่อมต่อ…" forever because the device
poller had no working adb to call.

These tests pin the layout convention by mode so the bug can't
silently regress again.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


# ── helpers ────────────────────────────────────────────────────────


def _reload_platform_tools(project_root: Path):
    """Reload ``src.platform_tools`` with PROJECT_ROOT mocked.

    LEGACY_TOOLS_ROOT is computed at import time, so we have to
    re-import the module after patching its dependency. We use
    ``importlib.reload`` instead of just monkey-patching the
    attribute so the new value flows through any helpers that
    closed over it (e.g. ``tools_root_for``).
    """
    from src import config

    with mock.patch.object(config, "PROJECT_ROOT", project_root):
        # platform_tools imports PROJECT_ROOT into its own namespace
        # at module import; importlib.reload re-runs that import.
        from src import platform_tools as pt
        # We have to also patch the PROJECT_ROOT name in the
        # already-imported pt module since reload re-binds it from
        # config (which we've patched).
        importlib.reload(pt)
        return pt


# ── _tools_root_base ───────────────────────────────────────────────


class TestToolsRootBase:
    """Pin the directory that ``.tools/`` is anchored on per launch
    mode. Wrong = silent customer breakage.
    """

    def test_dev_mode_uses_workspace_root(self, tmp_path):
        """Dev / portable: PROJECT_ROOT = vcam-pc/, .tools/ at parent."""
        workspace = tmp_path / "ws"
        project = workspace / "vcam-pc"
        project.mkdir(parents=True)

        with mock.patch.object(sys, "frozen", False, create=True):
            pt = _reload_platform_tools(project)
            assert pt._tools_root_base() == workspace
            assert pt.LEGACY_TOOLS_ROOT == (workspace / ".tools").resolve()

    def test_frozen_mode_uses_project_root(self, tmp_path):
        """Inno / .dmg: PROJECT_ROOT IS the install dir, .tools/ next to .exe."""
        install_dir = tmp_path / "NPCreate-install"
        install_dir.mkdir()

        with mock.patch.object(sys, "frozen", True, create=True):
            pt = _reload_platform_tools(install_dir)
            assert pt._tools_root_base() == install_dir
            assert pt.LEGACY_TOOLS_ROOT == (install_dir / ".tools").resolve()

    def test_frozen_mode_does_NOT_walk_up(self, tmp_path):
        """The 1.7.8 regression — explicitly forbid ``.parent`` walk."""
        install_dir = tmp_path / "anywhere" / "NP Create"
        install_dir.mkdir(parents=True)

        with mock.patch.object(sys, "frozen", True, create=True):
            pt = _reload_platform_tools(install_dir)
            wrong_path = install_dir.parent / ".tools"
            assert pt.LEGACY_TOOLS_ROOT != wrong_path.resolve(), (
                "PROJECT_ROOT.parent walks one level too high in "
                "frozen mode — Inno Setup lays .tools/ next to the "
                ".exe, not above it. This was the v1.7.8 break."
            )


# ── find_adb fallback chain ───────────────────────────────────────


class TestFindAdbFallbacks:
    """The actual symptom in v1.7.8 was ``find_adb()`` returning
    None even though Inno had bundled scrcpy's adb. Pin both the
    canonical platform-tools path AND the scrcpy fallback.
    """

    def _layout(self, root: Path, os_name: str, *, where: str) -> Path:
        """Create one of three valid adb locations and return the path.

        ``where`` ∈ {"platform-tools", "android-sdk", "scrcpy"}.
        """
        sfx = ".exe" if os_name == "windows" else ""
        if where == "platform-tools":
            adb = root / ".tools" / os_name / "platform-tools" / f"adb{sfx}"
        elif where == "android-sdk":
            adb = root / ".tools" / os_name / "android-sdk" / "platform-tools" / f"adb{sfx}"
        elif where == "scrcpy":
            adb = root / ".tools" / os_name / "scrcpy" / f"adb{sfx}"
        else:
            raise ValueError(where)
        adb.parent.mkdir(parents=True, exist_ok=True)
        adb.write_bytes(b"#!/bin/sh\n")
        adb.chmod(0o755)
        return adb

    @pytest.mark.parametrize("os_name", ["windows", "macos"])
    def test_platform_tools_wins(self, tmp_path, os_name):
        install = tmp_path / "install"
        install.mkdir()
        # Create all three; canonical platform-tools should win.
        canonical = self._layout(install, os_name, where="platform-tools")
        self._layout(install, os_name, where="scrcpy")

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system",
                        return_value="Windows" if os_name == "windows" else "Darwin"):
            pt = _reload_platform_tools(install)
            found = pt.find_adb()
            assert found is not None
            assert found == canonical.resolve()

    @pytest.mark.parametrize("os_name", ["windows", "macos"])
    def test_scrcpy_adb_used_when_platform_tools_missing(
        self, tmp_path, os_name
    ):
        """The 1.7.8 regression scenario: only scrcpy/adb is present
        (because CI only ran setup_scrcpy.py). find_adb() must
        find that fallback or the wizard never sees the device.
        """
        install = tmp_path / "install"
        install.mkdir()
        scrcpy_adb = self._layout(install, os_name, where="scrcpy")

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system",
                        return_value="Windows" if os_name == "windows" else "Darwin"), \
             mock.patch("shutil.which", return_value=None):
            pt = _reload_platform_tools(install)
            found = pt.find_adb()
            assert found is not None, (
                "scrcpy bundles adb in .tools/<os>/scrcpy/. Falling "
                "back to None here = the v1.7.8 customer breakage."
            )
            assert found == scrcpy_adb.resolve()

    def test_returns_none_when_nothing_bundled_and_no_system_adb(
        self, tmp_path
    ):
        install = tmp_path / "install"
        install.mkdir()

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Windows"), \
             mock.patch("shutil.which", return_value=None):
            pt = _reload_platform_tools(install)
            assert pt.find_adb() is None


# ── find_vcam_apk uses the same base ──────────────────────────────


class TestFindVcamApkFrozen:
    def test_resolves_apk_next_to_exe_in_frozen_mode(self, tmp_path):
        """Mirror of the .tools/ fix: ``apk/`` is shipped next to the
        .exe by installer.iss, not one level up.
        """
        install = tmp_path / "install"
        install.mkdir()
        apk = install / "apk" / "vcam-app-release.apk"
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b"PK\x03\x04fake-apk")

        with mock.patch.object(sys, "frozen", True, create=True):
            pt = _reload_platform_tools(install)
            found = pt.find_vcam_apk()
            assert found is not None
            assert found == apk.resolve()


# ── env override ───────────────────────────────────────────────────


class TestEnvOverride:
    """``NPCREATE_TOOLS_ROOT`` short-circuits canonical resolution
    so power users / shared toolchains don't have to fork the code.
    """

    def test_env_var_supersedes_bundled_layout(self, tmp_path):
        # Bundled layout lives in install/.tools/...
        install = tmp_path / "install"
        install.mkdir()
        bundled = install / ".tools" / "windows" / "platform-tools" / "adb.exe"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"bundled")

        # Override layout lives at /shared-tools/windows/...
        override = tmp_path / "shared"
        ov_adb = override / "windows" / "platform-tools" / "adb.exe"
        ov_adb.parent.mkdir(parents=True)
        ov_adb.write_bytes(b"override")

        env = {"NPCREATE_TOOLS_ROOT": str(override)}
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Windows"), \
             mock.patch.dict(os.environ, env, clear=False):
            pt = _reload_platform_tools(install)
            found = pt.find_adb()
            assert found is not None
            assert found == ov_adb.resolve(), (
                "NPCREATE_TOOLS_ROOT must beat the bundled layout"
            )
