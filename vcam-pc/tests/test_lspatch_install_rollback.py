"""Unit tests for the LSPatch install-rollback safety net.

The rollback path is what guarantees that, if the patched install
fails *after* we've uninstalled the customer's original TikTok, the
phone doesn't end up TikTok-less. These tests pin that behaviour so
future refactors can't silently regress it.

What we *don't* test here:

* ``adb`` itself — we mock ``subprocess.run`` to drive each branch
  deterministically. End-to-end testing happens on real hardware.
* The ``patch`` step — that's already covered by separate tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make ``src.*`` importable when running ``pytest tests/`` from the
# vcam-pc root, the same way other test modules do.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import StreamConfig  # noqa: E402
from src.lspatch_pipeline import (  # noqa: E402
    InstallResult,
    LSPatchPipeline,
    TIKTOK_PACKAGES,
    _TIKTOK_PKG_PATTERNS,
)


# ── helpers ────────────────────────────────────────────────────


class _CompletedProcess:
    """Stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "",
                 stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_pipeline(tmp_path: Path) -> LSPatchPipeline:
    """Construct a pipeline whose cache directory is the test tmpdir
    so we don't pollute the real ``.cache/lspatch`` between runs."""
    cfg = StreamConfig()
    cfg.adb_path = "adb"
    pipeline = LSPatchPipeline(cfg)
    pipeline.cache_dir = tmp_path / "lspatch"
    pipeline.cache_dir.mkdir(parents=True, exist_ok=True)
    pipeline.pulled_dir = pipeline.cache_dir / "pulled"
    pipeline.patched_dir = pipeline.cache_dir / "patched"
    return pipeline


def _touch(p: Path, content: bytes = b"fake-apk") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


# ── TIKTOK_PACKAGES re-export ──────────────────────────────────


def test_tiktok_packages_exposes_lite_and_douyin() -> None:
    """The pipeline must use the *full* package list from ``hook_status``
    so it doesn't fail to detect TikTok Lite / Douyin Lite installs."""
    assert "com.zhiliaoapp.musically.go" in TIKTOK_PACKAGES
    assert "com.ss.android.ugc.aweme.lite" in TIKTOK_PACKAGES


# ── _TIKTOK_PKG_PATTERNS discovery regex ──────────────────────


@pytest.mark.parametrize("pkg", [
    "com.ss.android.ugc.trill",
    "com.zhiliaoapp.musically",
    "com.zhiliaoapp.musically.preload",
    "com.ss.android.ugc.aweme",
    "com.ss.android.ugc.aweme.lite",
    "com.ss.android.ugc.tiktok",
    "com.tiktok.now",
])
def test_tiktok_pattern_matches_known_variants(pkg: str) -> None:
    assert _TIKTOK_PKG_PATTERNS.match(pkg), f"should match {pkg!r}"


@pytest.mark.parametrize("pkg", [
    "com.facebook.katana",
    "com.google.android.youtube",
    "com.foo.bar",
    "com.line.android",
])
def test_tiktok_pattern_rejects_unrelated(pkg: str) -> None:
    assert not _TIKTOK_PKG_PATTERNS.match(pkg), f"should NOT match {pkg!r}"


# ── detect_tiktok discovery path ──────────────────────────────


def test_detect_tiktok_falls_back_to_pm_list_for_unknown_variant(
    tmp_path: Path,
) -> None:
    """If none of TIKTOK_PACKAGES respond to ``pm path`` but ``pm list
    packages`` shows a TikTok-shaped name, ``detect_tiktok`` should
    return that name instead of giving up."""
    pipeline = _make_pipeline(tmp_path)

    canonical = list(TIKTOK_PACKAGES)
    odd = "com.zhiliaoapp.musically.preload"

    def fake_shell(cmd: str, serial=None) -> str:
        if cmd.startswith("pm path "):
            return ""  # no canonical variant installed
        if cmd == "pm list packages":
            return "\n".join([
                "package:com.android.settings",
                f"package:{odd}",
                "package:com.facebook.katana",
            ])
        return ""

    with patch.object(pipeline, "_adb_shell", side_effect=fake_shell):
        assert pipeline.detect_tiktok() == odd
        assert canonical  # sanity: regression marker


def test_detect_tiktok_returns_empty_when_truly_absent(
    tmp_path: Path,
) -> None:
    pipeline = _make_pipeline(tmp_path)

    def fake_shell(cmd: str, serial=None) -> str:
        if cmd.startswith("pm path "):
            return ""
        if cmd == "pm list packages":
            return (
                "package:com.android.settings\n"
                "package:com.facebook.katana\n"
            )
        return ""

    with patch.object(pipeline, "_adb_shell", side_effect=fake_shell):
        assert pipeline.detect_tiktok() == ""


# ── install rollback safety ────────────────────────────────────


def test_install_rollback_succeeds_after_install_failure(
    tmp_path: Path,
) -> None:
    """If ``install-multiple`` of patched APKs fails, the pipeline
    must re-install the original APKs and report ``rollback_ok``."""
    pipeline = _make_pipeline(tmp_path)

    original = [_touch(tmp_path / "orig" / "base.apk")]
    patched = [_touch(tmp_path / "patch" / "base-patched.apk")]

    call_log: list[list[str]] = []

    def fake_run(cmd, **kw):
        call_log.append(list(cmd))
        # cmd[1] is "uninstall" / "install-multiple"
        if "uninstall" in cmd:
            return _CompletedProcess(0, "Success\n")
        if "install-multiple" in cmd:
            # Fail on the patched bundle, succeed on the rollback. We
            # detect which one we're in by checking if the path
            # contains "patch" (patched_apks live under patched/).
            paths = [a for a in cmd if a.endswith(".apk")]
            patched_in_call = any("patch" in p for p in paths)
            if patched_in_call:
                return _CompletedProcess(
                    1, "", "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]\n",
                )
            return _CompletedProcess(0, "Success\n")
        return _CompletedProcess(0, "Success\n")

    with patch("src.lspatch_pipeline.subprocess.run", side_effect=fake_run):
        result = pipeline.install(
            package="com.ss.android.ugc.trill",
            patched_apks=patched,
            original_apks=original,
        )

    assert isinstance(result, InstallResult)
    assert result.ok is False
    assert result.rollback_attempted is True
    assert result.rollback_ok is True
    assert "INSTALL_FAILED_VERSION_DOWNGRADE" in result.error
    # We expect: uninstall, install-multiple (patched, fails),
    # install-multiple (originals, rollback succeeds).
    assert len(call_log) == 3, call_log
    assert "uninstall" in call_log[0]
    assert "install-multiple" in call_log[1]
    assert "install-multiple" in call_log[2]


def test_install_rollback_skipped_when_no_originals(tmp_path: Path) -> None:
    """If the caller didn't pass ``original_apks=`` we cannot roll back —
    the result must say so explicitly so the GUI can warn the customer."""
    pipeline = _make_pipeline(tmp_path)
    patched = [_touch(tmp_path / "patch" / "base.apk")]

    def fake_run(cmd, **kw):
        if "uninstall" in cmd:
            return _CompletedProcess(0, "Success\n")
        return _CompletedProcess(1, "", "INSTALL_FAILED\n")

    with patch("src.lspatch_pipeline.subprocess.run", side_effect=fake_run):
        result = pipeline.install(
            package="com.ss.android.ugc.trill",
            patched_apks=patched,
            original_apks=None,
        )

    assert result.ok is False
    assert result.rollback_attempted is False
    assert result.rollback_ok is False


def test_install_rollback_handles_disappeared_apks(tmp_path: Path) -> None:
    """If the pulled APKs were deleted between pull and install, the
    rollback can't proceed — report it as a non-attempt rather than a
    silent failure."""
    pipeline = _make_pipeline(tmp_path)
    patched = [_touch(tmp_path / "patch" / "base.apk")]
    # Originals point to nonexistent files (e.g. ``.cache`` was wiped).
    ghosts = [tmp_path / "ghost" / "base.apk"]

    def fake_run(cmd, **kw):
        if "uninstall" in cmd:
            return _CompletedProcess(0, "Success\n")
        return _CompletedProcess(1, "", "INSTALL_FAILED\n")

    with patch("src.lspatch_pipeline.subprocess.run", side_effect=fake_run):
        result = pipeline.install(
            package="com.ss.android.ugc.trill",
            patched_apks=patched,
            original_apks=ghosts,
        )

    assert result.ok is False
    assert result.rollback_attempted is False, (
        "we must not run install-multiple on missing files"
    )
    assert result.rollback_ok is False


def test_install_rollback_failure_reports_error(tmp_path: Path) -> None:
    """When the rollback itself fails (e.g. customer unplugged the
    USB cable), the result must include ``rollback_error`` so the GUI
    can show "TikTok หาย — ลงใหม่" instead of "พร้อมใช้งาน"."""
    pipeline = _make_pipeline(tmp_path)
    original = [_touch(tmp_path / "orig" / "base.apk")]
    patched = [_touch(tmp_path / "patch" / "base.apk")]

    def fake_run(cmd, **kw):
        if "uninstall" in cmd:
            return _CompletedProcess(0, "Success\n")
        # *Both* install-multiple calls fail (patched + rollback).
        return _CompletedProcess(1, "", "Failure [BOOM]\n")

    with patch("src.lspatch_pipeline.subprocess.run", side_effect=fake_run):
        result = pipeline.install(
            package="com.ss.android.ugc.trill",
            patched_apks=patched,
            original_apks=original,
        )

    assert result.ok is False
    assert result.rollback_attempted is True
    assert result.rollback_ok is False
    assert "BOOM" in result.rollback_error


def test_install_success_does_not_attempt_rollback(tmp_path: Path) -> None:
    """Happy path sanity check: when the patched install succeeds, the
    rollback fields must stay False so the GUI doesn't show a confusing
    "rolled back" hint after a clean Patch."""
    pipeline = _make_pipeline(tmp_path)
    original = [_touch(tmp_path / "orig" / "base.apk")]
    patched = [_touch(tmp_path / "patch" / "base.apk")]

    def fake_run(cmd, **kw):
        if "uninstall" in cmd or "install-multiple" in cmd:
            return _CompletedProcess(0, "Success\n")
        return _CompletedProcess(0, "")

    # _adb_shell is called for the post-install fingerprint read; just
    # return an empty string to skip fingerprint extraction.
    with patch("src.lspatch_pipeline.subprocess.run", side_effect=fake_run), \
         patch.object(pipeline, "_adb_shell", return_value=""):
        result = pipeline.install(
            package="com.ss.android.ugc.trill",
            patched_apks=patched,
            original_apks=original,
        )

    assert result.ok is True
    assert result.rollback_attempted is False
    assert result.rollback_ok is False
