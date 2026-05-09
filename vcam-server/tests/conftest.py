"""Test fixtures.

Every test gets a fresh SQLite + a fresh signing key so writes
from one test never leak into another. We do this by overriding
the env vars BEFORE importing app code: the ``app.config`` module
snapshots ``Settings`` at import, so any test that wants isolation
must arrange its env first.

Usage::

    def test_my_thing(client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Iterator

import pytest

# Ensure ``import app`` works when pytest is invoked from anywhere.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator:
    """Spin up a fresh ASGI app rooted in a temp data dir.

    We:
    1. point DATA_DIR + SIGNING_KEY_PATH at tmp_path so each test
       sees a clean DB and a fresh seed file;
    2. wipe the in-memory crypto cache so the new seed is read;
    3. re-import ``app.config`` so SETTINGS reflects the env;
    4. hand back a TestClient bound to the freshly built app.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv(
        "SIGNING_KEY_PATH", str(tmp_path / ".private_key"),
    )
    monkeypatch.setenv(
        "PUBLIC_KEY_PATH", str(tmp_path / "public_key.hex"),
    )
    # Stable secret so cookies don't break across the in-test calls.
    monkeypatch.setenv(
        "SESSION_SECRET", "test-secret-deterministic-not-for-prod",
    )
    monkeypatch.setenv("COOKIE_SECURE", "0")

    # Reload modules in dependency order so SETTINGS picks up env.
    # We have to be careful: `app.crypto` caches the seed at module
    # scope, so a stale import would point at the previous tmp_path.
    for mod in [
        "app.config", "app.db", "app.crypto", "app.auth",
        "app.routes.admin_customers",
        "app.routes.admin_licenses",
        "app.routes.admin_payments",
        "app.routes.admin_support",
        "app.routes.public_activate",
        "app.routes.public_support",
        "app.routes.ui",
        "app.main",
    ]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)

    from fastapi.testclient import TestClient
    from app.main import create_app
    from app import crypto, db

    db.init_db()
    crypto.init_new_keypair(force=True)

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_client(client):
    """A test client that's already authenticated as a fresh admin.

    Calls ``create-admin`` via the auth helpers + posts the login
    form so subsequent requests carry the session cookie.
    """
    from app import auth, db
    pwd_hash = auth.hash_password("test-password-123")
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO admins (email, password_hash, display_name, "
            "created_at, is_active) VALUES (?, ?, ?, ?, 1)",
            (
                "test@example.com",
                pwd_hash,
                "Test Admin",
                db.now_iso(),
            ),
        )
    resp = client.post(
        "/admin/login",
        data={"email": "test@example.com", "password": "test-password-123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client
