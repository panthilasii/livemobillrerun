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

    def test_macos_app_inside_customer_zip_finds_tools(self, tmp_path):
        """The 1.8.3 regression — macOS PyInstaller .app double-clicked
        from inside the customer ZIP must still find ``.tools/``.

        Layout the customer ZIP unpacks to::

            <bundle>/
              .tools/                                  ← the goal
              app/
                NP-Create.app/
                  Contents/
                    MacOS/                             ← PROJECT_ROOT
                      NP-Create

        The walk has to climb four directories (MacOS → Contents →
        NP-Create.app → app → bundle) to reach ``.tools/``. The 1.8.2
        bounded-3-level walk stopped at ``NP-Create.app/`` and
        returned None for find_adb / find_ffmpeg / find_lspatch_jar /
        find_vcam_apk — leaving the dashboard with every device
        permanently offline because the device-poller had no adb to
        call.

        v1.8.4 extended the walk to 5 levels; this test pins that
        contract so the bug can't re-surface silently when someone
        "tightens up" the resolver later.
        """
        bundle = tmp_path / "NP-Create-customer-macos-1.8.4"
        tools_dir = bundle / ".tools"
        tools_dir.mkdir(parents=True)
        macos_dir = bundle / "app" / "NP-Create.app" / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)

        with mock.patch.object(sys, "frozen", True, create=True):
            pt = _reload_platform_tools(macos_dir)
            assert pt._tools_root_base() == bundle, (
                "The walk must escape Contents/MacOS/ → Contents/ → "
                ".app/ → app/ → bundle/ and find .tools/ there. "
                "Stopping early = silent 'every device offline' bug "
                "shipped to customers in v1.8.3."
            )
            assert pt.LEGACY_TOOLS_ROOT == tools_dir.resolve()

    def test_walk_does_not_escape_to_user_home(self, tmp_path):
        """Bound the walk so a missing ``.tools/`` doesn't accidentally
        find one in a parent path the customer didn't intend (e.g.
        the dev's workspace, or someone else's NP Create install).
        """
        # Build a deep path with no .tools/ anywhere.
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g"
        deep.mkdir(parents=True)

        with mock.patch.object(sys, "frozen", True, create=True):
            pt = _reload_platform_tools(deep)
            # Should fall back to ``start`` (PROJECT_ROOT), not climb
            # all the way out into tmp_path or beyond.
            base = pt._tools_root_base()
            assert base == deep, (
                "When no .tools/ exists, walk must not wander — "
                f"it landed at {base!r} instead of staying at {deep!r}."
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


# ── find_adb_driver_dir (Windows only) ────────────────────────────


class TestFindAdbDriverDir:
    """v1.7.11 — bundled Google USB Driver. The in-app help dialog
    points the customer's Device Manager at this folder.
    """

    def _bundle_driver(self, root: Path) -> Path:
        inf = (
            root
            / ".tools"
            / "windows"
            / "adb-driver"
            / "usb_driver"
            / "android_winusb.inf"
        )
        inf.parent.mkdir(parents=True, exist_ok=True)
        inf.write_text("[Version]\nSignature=$WINDOWS NT$\n")
        return inf

    def test_returns_dir_on_windows_when_bundled(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        inf = self._bundle_driver(install)

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Windows"):
            pt = _reload_platform_tools(install)
            found = pt.find_adb_driver_dir()
            assert found is not None
            assert found == inf.parent.resolve()

    def test_returns_none_when_not_bundled(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Windows"):
            pt = _reload_platform_tools(install)
            assert pt.find_adb_driver_dir() is None

    def test_returns_none_on_macos(self, tmp_path):
        """macOS has native ADB-over-USB — no driver to install."""
        install = tmp_path / "install"
        install.mkdir()
        # Even if a driver dir exists (shouldn't happen on Mac
        # builds, but be defensive), the resolver must return None
        # so the help dialog doesn't pop on Mac.
        self._bundle_driver(install)

        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Darwin"):
            pt = _reload_platform_tools(install)
            assert pt.find_adb_driver_dir() is None


# ── find_mediamtx (v1.8.0) ────────────────────────────────────────


class TestFindMediaMTX:
    """The Mode B RTMP path needs the bundled MediaMTX binary.
    Resolver returns the platform-appropriate filename
    (``mediamtx.exe`` on Windows, ``mediamtx`` on macOS) under
    ``.tools/<os>/mediamtx/``."""

    def _bundle(self, root: Path, os_name: str) -> Path:
        suffix = ".exe" if os_name == "windows" else ""
        bin_path = (
            root
            / ".tools"
            / os_name
            / "mediamtx"
            / f"mediamtx{suffix}"
        )
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_bytes(b"\x7fELF" if os_name != "windows" else b"MZ\x90")
        return bin_path

    def test_resolves_on_windows(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        bin_path = self._bundle(install, "windows")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Windows"):
            pt = _reload_platform_tools(install)
            assert pt.find_mediamtx() == bin_path.resolve()

    def test_resolves_on_macos(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        bin_path = self._bundle(install, "macos")
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Darwin"):
            pt = _reload_platform_tools(install)
            assert pt.find_mediamtx() == bin_path.resolve()

    def test_returns_none_when_not_bundled(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch("platform.system", return_value="Windows"):
            pt = _reload_platform_tools(install)
            assert pt.find_mediamtx() is None


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
