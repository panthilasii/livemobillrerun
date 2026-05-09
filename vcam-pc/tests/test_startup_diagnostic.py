"""Tests for ``src._startup_diagnostic``.

The diagnostic writer is the support team's primary tool for
triaging "the wizard never finds my phone" reports — when a
customer pings Line OA we walk them through:

    1. Open ``%LOCALAPPDATA%\\NP Create\\logs\\startup-diagnostic.txt``
    2. Send the file.
    3. Admin reads find_adb / find_ffmpeg / adb version output and
       knows within seconds which layer broke.

So the writer MUST:

* never raise (diagnostic must not break the app launching),
* always produce a UTF-8 text file at the requested path,
* include all the path-resolution facts a tech reading the file
  needs to diagnose without bouncing screenshots back and forth.

These tests pin those invariants.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock


def test_writes_utf8_text_file(tmp_path):
    from src._startup_diagnostic import (
        DIAGNOSTIC_FILENAME,
        write_diagnostic,
    )

    out = write_diagnostic(tmp_path)
    assert out is not None
    assert out.is_file()
    assert out.name == DIAGNOSTIC_FILENAME

    text = out.read_text(encoding="utf-8")
    # Must include the major sections — lets support do a quick
    # scroll instead of a careful read.
    assert "Python / runtime" in text
    assert "Project paths" in text
    assert "Tool resolution" in text
    assert "ADB liveness test" in text
    assert "Environment overrides" in text


def test_includes_key_path_values(tmp_path):
    """The diagnostic must surface the *actual* values of the
    resolution layers, not just placeholders. Otherwise support
    can't tell whether ``_tools_root_base()`` returned a sane path
    or wandered off into the filesystem.
    """
    from src._startup_diagnostic import write_diagnostic
    from src.config import PROJECT_ROOT

    out = write_diagnostic(tmp_path)
    assert out is not None
    text = out.read_text(encoding="utf-8")

    # PROJECT_ROOT in dev mode is the vcam-pc/ directory; either
    # the absolute path or its trailing dirname must appear.
    assert "PROJECT_ROOT" in text
    assert str(PROJECT_ROOT) in text or PROJECT_ROOT.name in text


def test_handles_broken_imports_gracefully():
    """If a downstream import fails (e.g. on a stripped-down build
    where ``branding`` was renamed mid-refactor), the diagnostic
    must return None instead of propagating the exception.

    The actual runtime contract: ``main()`` calls write_diagnostic
    inside a try/except too, so this is belt-and-suspenders — but
    we still want the function itself to be safe.
    """
    from src import _startup_diagnostic

    with mock.patch.object(
        _startup_diagnostic, "_write_unsafe", side_effect=RuntimeError("boom")
    ):
        result = _startup_diagnostic.write_diagnostic()
        assert result is None


def test_default_log_dir_is_project_root_logs(tmp_path):
    """When no log_dir is passed, the diagnostic lands at
    ``PROJECT_ROOT/logs/`` — same place as ``npcreate.log``, so
    one ZIP from "Send Logs" picks up both files.
    """
    from src import _startup_diagnostic, config

    fake_root = tmp_path / "fakeproj"
    fake_root.mkdir()

    with mock.patch.object(config, "PROJECT_ROOT", fake_root):
        # Reload the module's binding of PROJECT_ROOT.
        import importlib

        importlib.reload(_startup_diagnostic)
        out = _startup_diagnostic.write_diagnostic()

    assert out is not None
    assert out.parent == fake_root / "logs"
    assert out.is_file()


def test_walks_up_for_dot_tools(tmp_path):
    """Pin the v1.7.10 defensive walk: when ``.tools/`` lives at
    PROJECT_ROOT.parent.parent (e.g. portable ZIP user clicks
    ``app/NP-Create.exe`` instead of ``run.bat``), the resolver
    must still find it.
    """
    import importlib
    from src import config

    bundle = tmp_path / "NP-Create-bundle"
    app_dir = bundle / "app"
    app_dir.mkdir(parents=True)
    # .tools/ at bundle root, two levels above app_dir.
    (bundle / ".tools" / "windows" / "platform-tools").mkdir(parents=True)
    adb_exe = bundle / ".tools" / "windows" / "platform-tools" / "adb.exe"
    adb_exe.write_bytes(b"fake")

    with mock.patch.object(sys, "frozen", True, create=True), \
         mock.patch.object(config, "PROJECT_ROOT", app_dir):
        from src import platform_tools as pt
        importlib.reload(pt)
        # The walk must pick up bundle as the base, not app_dir.
        base = pt._tools_root_base()
        assert base == bundle, (
            f"Defensive walk failed — expected {bundle} (where .tools "
            f"lives), got {base}. This is the regression that broke "
            f"the portable ZIP for users who launched app/NP-Create.exe."
        )
