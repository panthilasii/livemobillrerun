"""Regression tests for v1.8.0's virtual-cam app catalogue.

Lightweight — the module is mostly static data, so we just
guard against schema regressions (every entry has the fields
the wizard expects, keys are unique, recommended() picks the
top-rated one) and accidentally-broken Play Store URLs.
"""

from __future__ import annotations

import re

from src import virtual_cam_apps


def test_catalog_not_empty():
    assert virtual_cam_apps.CATALOG, "wizard needs at least one app"


def test_keys_are_unique_and_lowercase_ascii():
    keys = [a.key for a in virtual_cam_apps.CATALOG]
    assert len(keys) == len(set(keys)), "duplicate app keys"
    for k in keys:
        assert re.fullmatch(r"[a-z0-9_]+", k), f"bad key {k!r}"


def test_packages_unique_and_dotted():
    pkgs = [a.package for a in virtual_cam_apps.CATALOG]
    assert len(pkgs) == len(set(pkgs)), "duplicate android packages"
    for p in pkgs:
        assert "." in p, f"package {p!r} doesn't look like a.b.c"


def test_playstore_urls_are_valid():
    """Every link must point at the Play Store with an ``id``
    query that matches the package name. The wizard renders
    this URL as a QR; a typo here = customer scans and lands
    on the wrong app."""
    for app in virtual_cam_apps.CATALOG:
        assert app.playstore_url.startswith(
            "https://play.google.com/store/apps/details"
        ), f"{app.key}: {app.playstore_url!r}"
        assert f"id={app.package}" in app.playstore_url


def test_setup_steps_non_empty():
    for app in virtual_cam_apps.CATALOG:
        assert app.setup_steps_th, f"{app.key} missing setup steps"
        assert all(
            isinstance(s, str) and s.strip() for s in app.setup_steps_th
        )


def test_ratings_in_range():
    for app in virtual_cam_apps.CATALOG:
        assert 1 <= app.rating <= 5, f"{app.key} rating={app.rating}"


def test_by_key_returns_match_or_none():
    assert virtual_cam_apps.by_key("camerafi") is not None
    assert virtual_cam_apps.by_key("does-not-exist") is None


def test_recommended_is_top_rated():
    rec = virtual_cam_apps.recommended()
    top = max(a.rating for a in virtual_cam_apps.CATALOG)
    assert rec.rating == top
