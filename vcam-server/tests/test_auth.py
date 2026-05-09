"""Login / session / authorization gating tests."""
from __future__ import annotations


def test_login_redirects_unauthed_to_login_page(client):
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["location"]


def test_root_redirects_to_admin_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin" in resp.headers["location"]


def test_admin_api_requires_cookie(client):
    resp = client.get("/api/admin/customers")
    assert resp.status_code == 401


def test_login_with_wrong_creds_redirects_with_error(client):
    resp = client.post(
        "/admin/login",
        data={"email": "no-such@admin", "password": "bad"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=invalid" in resp.headers["location"]


def test_login_then_admin_api_works(admin_client):
    resp = admin_client.get("/api/admin/customers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_logout_clears_cookie(admin_client):
    resp = admin_client.post("/admin/logout", follow_redirects=False)
    assert resp.status_code == 303
    # After logout the cookie is gone, so the next admin call 401s.
    # Reuse the same TestClient (cookies cleared by the response).
    r2 = admin_client.get("/api/admin/customers")
    assert r2.status_code == 401


def test_password_hash_uses_bcrypt(client):
    """Cheap sanity that we're not storing plaintext."""
    from app import auth
    h = auth.hash_password("hunter2-test")
    assert h.startswith("$2") and len(h) > 50
    assert auth.verify_password("hunter2-test", h)
    assert not auth.verify_password("wrong", h)


def test_password_too_short_rejected(client):
    from app import auth
    import pytest
    with pytest.raises(ValueError):
        auth.hash_password("short")
