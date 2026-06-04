"""Windows console-flash suppression (``src/_win_no_window.py``).

We can't spawn a real Windows console from CI (mac/linux runners), so
we pin the two behaviours that matter:

* ``merge_flags`` always OR-s in CREATE_NO_WINDOW and is idempotent.
* ``install`` is a no-op off Windows, and on a simulated win32 it
  wraps ``subprocess.Popen.__init__`` exactly once (idempotent).
"""
from __future__ import annotations

import subprocess
import sys

import src._win_no_window as wnw


def test_merge_flags_sets_bit():
    assert wnw.merge_flags(0) == wnw.CREATE_NO_WINDOW
    assert wnw.merge_flags(None) == wnw.CREATE_NO_WINDOW


def test_merge_flags_idempotent():
    once = wnw.merge_flags(0)
    twice = wnw.merge_flags(once)
    assert once == twice == wnw.CREATE_NO_WINDOW


def test_merge_flags_preserves_other_bits():
    other = 0x00000010  # arbitrary unrelated creationflag bit
    merged = wnw.merge_flags(other)
    assert merged & other == other
    assert merged & wnw.CREATE_NO_WINDOW == wnw.CREATE_NO_WINDOW


def test_install_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(wnw, "_installed", False)
    orig = subprocess.Popen.__init__
    assert wnw.install() is False
    assert subprocess.Popen.__init__ is orig


def test_install_patches_once_on_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wnw, "_installed", False)
    orig = subprocess.Popen.__init__
    try:
        assert wnw.install() is True
        assert subprocess.Popen.__init__ is not orig
        # Second call must not double-wrap.
        patched = subprocess.Popen.__init__
        assert wnw.install() is False
        assert subprocess.Popen.__init__ is patched
    finally:
        subprocess.Popen.__init__ = orig
        wnw._installed = False
