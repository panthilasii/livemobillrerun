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
        # The .bat hands off to ``src._winlauncher`` (see that module
        # for the rationale — cmd.exe parses .bat with the OEM
        # codepage so any Thai byte in the file blows up parsing).
        # The actual ``--studio`` arg lives inside the Python
        # launcher, so we verify the handoff path here instead.
        assert "src._winlauncher" in body, (
            "Windows .bat must hand off to src._winlauncher so all "
            "Thai user messaging happens in Python (where UTF-8 is "
            "explicit). Putting Thai bytes in a .bat triggers cmd.exe "
            "parser errors like 'xxx is not recognized as an internal "
            "or external command'."
        )
        # Pause must appear when Python is missing so the customer
        # actually sees the install instructions before the cmd.exe
        # window auto-closes.
        assert "pause" in body
        assert "chcp 65001" in body, "must set UTF-8 codepage for Thai output"
        # The .bat itself MUST be ASCII-only, otherwise cmd.exe parses
        # it under the OEM codepage and crashes with "'xxx' is not
        # recognized as an internal or external command".
        try:
            body.encode("ascii")
        except UnicodeEncodeError as exc:  # pragma: no cover
            raise AssertionError(
                "run.bat must be pure ASCII — non-ASCII bytes break "
                f"cmd.exe parsing on customer machines: {exc}"
            )

    def test_macos_launcher_is_bash(self):
        body = self.br._launcher_body("macos")
        assert body.startswith("#!/usr/bin/env bash")
        # Either `set -e` (exit-on-error) or `set -u` (unset-var-error)
        # is acceptable — both make typos blow up loudly during the
        # 30-second first-run install instead of silently doing
        # nothing. We deliberately do *not* require both because
        # `set -e` interferes with our explicit error dialog flow.
        assert ("set -e" in body) or ("set -u" in body), (
            "macOS launcher should opt in to a strict bash mode"
        )
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
        # Every script that can sign keys, rotate the keypair, build
        # bundles, fetch external binaries, or otherwise expose the
        # admin's workflow must be in ADMIN_TOOLS so build_release.py
        # drops it from customer ZIPs.
        must_block = {
            "gen_license.py",
            "init_keys.py",
            "build_release.py",
            "build_pyinstaller.py",
            "_pyinstaller_entry.py",
            "_download_helper.py",
            "setup_windows_tools.py",
            "setup_macos_tools.py",
            "setup_ffmpeg.py",
        }
        missing = must_block - self.br.ADMIN_TOOLS
        assert not missing, (
            f"these scripts should be admin-only but aren't blocked: "
            f"{missing}"
        )

    def test_admin_tools_set_covers_every_script_in_tools_dir(self):
        """Catch the easy mistake of adding a new tools/ helper and
        forgetting to add it to ADMIN_TOOLS *or* explicitly mark it
        as customer-safe. Default = block (safer).

        We scan Python, shell, batch, Inno Setup, and adjacent text
        files because every one of those formats can leak admin
        workflow / dev-only assumptions / EULA-source-of-truth into
        the customer ZIP.
        """
        from pathlib import Path

        tools_dir = (
            Path(__file__).resolve().parent.parent / "tools"
        )
        # Files deliberately shipped to customers. ``__init__.py`` is
        # always fine — package marker, no executable code we care
        # about. Add a new entry here only after a deliberate review.
        CUSTOMER_SAFE: set[str] = {"__init__.py"}

        # Glob every executable + text format we currently keep under
        # tools/. Add new patterns when introducing new file types.
        candidates: list[Path] = []
        for pat in ("*.py", "*.sh", "*.bat", "*.iss", "*.txt"):
            candidates.extend(tools_dir.glob(pat))

        for f in candidates:
            name = f.name
            if name in CUSTOMER_SAFE:
                continue
            assert name in self.br.ADMIN_TOOLS, (
                f"{name} sits in tools/ but is neither in "
                f"ADMIN_TOOLS nor declared customer-safe — "
                f"this would leak into customer bundles. "
                f"Add it to ADMIN_TOOLS in build_release.py."
            )

    def test_apk_ship_name_resolvable_at_runtime(self):
        """``build_release.py`` writes the vcam-app APK under one
        specific filename inside the customer ZIP, and
        ``platform_tools.find_vcam_apk()`` searches a hard-coded
        list of names at runtime. If those two drift, customers see
        "patch failed" with no useful error -- the runtime can't
        locate the Xposed module APK shipped right next to it.

        This regression actually reached the field in 1.4.5
        (Windows customer reported "render ผ่าน adb ไม่ได้").
        Pin the alignment so it can't slip again.
        """
        import inspect
        from src import platform_tools

        src = inspect.getsource(platform_tools.find_vcam_apk)
        # The shipped name from build_release.py:
        ship_name = "vcam-app-release.apk"
        assert ship_name in src, (
            f"build_release.py writes apk/{ship_name} but "
            f"find_vcam_apk() does not search for that name. "
            f"Customers will see 'patch failed' on Windows."
        )

        # Also assert the matching constant is in build_release.py
        # itself -- read it as a string so it survives black/ruff
        # reformatting.
        from pathlib import Path as _Path
        br_src = _Path(self.br.__file__).read_text(encoding="utf-8")
        assert f'"{{prefix}}/apk/{ship_name}"' in br_src, (
            f"build_release.py must write apk/{ship_name} so the "
            f"runtime resolver can find it."
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
