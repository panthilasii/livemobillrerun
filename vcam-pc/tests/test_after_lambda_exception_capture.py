"""Regression: ``except as e`` + ``self.after(0, lambda)`` lifetime bug.

Why this test exists
~~~~~~~~~~~~~~~~~~~~

Python 3 deletes the ``except`` clause's name binding the moment the
block exits — a refcycle-breaking measure introduced in PEP 3134::

    try:
        raise ValueError("boom")
    except Exception as ex:
        cb = lambda: print(ex)   # ← lambda closes over name `ex`
    cb()  # NameError: free variable 'ex' not associated with a value

Three customer-facing handlers in this codebase had this exact
shape::

    self.after(0, lambda: messagebox.showerror("...", f"crash: {ex}"))

The lambda runs on the *next* Tk tick (≥ 1 ms later), by which
point ``ex`` has been deleted. The customer never sees the actual
error message — they see a different ``NameError`` traceback in
the log instead. The fix in each case is to capture into a default-
arg::

    err_msg = f"crash: {ex}"
    self.after(0, lambda m=err_msg: messagebox.showerror("...", m))

This test pins down the **language behaviour** so the next dev
who sees the lambda pattern doesn't "simplify it back" thinking
it's identical. We also verify the corrected pattern works.
"""
from __future__ import annotations

import pytest


def _build_lambda_with_dead_capture() -> "callable[[], str]":
    """Reproduces the broken pattern: lambda references the
    ``except as`` name *after* the block has exited.

    ``noqa: F841,F821`` because we are *intentionally* exhibiting
    the bug pattern here — ruff's F841 ("ex unused") and F821
    ("ex undefined") are exactly the warnings that flagged this
    bug in production code.
    """
    try:
        raise ValueError("boom")
    except ValueError as ex:  # noqa: F841 - test deliberately captures
        return lambda: f"got {ex}"  # noqa: F821 - dead-name reference under test


def _build_lambda_with_snapshot() -> "callable[[], str]":
    """Reproduces the fixed pattern: snapshot ``ex`` into a
    local before binding the lambda."""
    try:
        raise ValueError("boom")
    except ValueError as ex:
        snapshot = str(ex)
        return lambda m=snapshot: f"got {m}"


def test_dead_capture_pattern_actually_fails():
    """If this ever starts passing, Python changed its semantics
    and the codebase comment / fix can be revisited."""
    cb = _build_lambda_with_dead_capture()
    with pytest.raises(NameError):
        cb()


def test_snapshot_pattern_works():
    cb = _build_lambda_with_snapshot()
    assert cb() == "got boom"


def test_repatch_failure_handler_uses_snapshot_pattern():
    """The Re-Patch crash handler in ``DashboardPage`` was the
    third (and most user-visible) site of the dead-capture
    pattern. Pin the source-level shape so a future "make it
    a one-liner" refactor can't quietly reintroduce the bug.
    """
    import inspect

    from src.ui import studio_pages

    src = inspect.getsource(studio_pages)
    # The fix introduces ``err_msg = f"crash: {ex}"`` immediately
    # before the after-lambda. If somebody collapses that back to
    # ``f"crash: {ex}"`` inside the lambda body the regression
    # would re-emerge.
    assert 'err_msg = f"crash: {ex}"' in src, (
        "Re-Patch handler must snapshot ``ex`` into ``err_msg`` "
        "before scheduling the lambda — otherwise Python deletes "
        "``ex`` when the except block exits and the lambda raises "
        "NameError on the next Tk tick instead of showing the "
        "customer the actual error."
    )
    # And the lambda itself must use a default-arg capture.
    assert "lambda m=err_msg: messagebox.showerror(" in src
