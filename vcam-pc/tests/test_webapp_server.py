"""FastAPI route smoke tests.

We use the synchronous ``TestClient`` from FastAPI itself rather
than spawning uvicorn -- that way each test gets a fresh
in-memory app and we don't fight a long-running port binding.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.webapp import seed_demo, server


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = server.create_app(db_path=tmp_path / "test.sqlite3")
    return TestClient(app)


def _seed(client: TestClient) -> None:
    """Helper: seed via the public API (not the seed_demo module
    directly) so we exercise the route end-to-end."""
    r = client.post("/api/demo/seed")
    assert r.status_code == 200
    assert r.json()["seeded"] > 0


class TestHealth:
    def test_health_returns_version(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["name"]
        assert body["version"]


class TestSummary:
    def test_summary_zero_when_empty(self, client):
        r = client.get("/api/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["today"]["orders"] == 0
        assert body["week"]["orders"] == 0
        assert body["month"]["orders"] == 0

    def test_summary_after_seed(self, client):
        _seed(client)
        body = client.get("/api/summary").json()
        # The seeder spans 7 days; month total must include
        # week total which includes today.
        m = int(body["month"]["orders"])
        w = int(body["week"]["orders"])
        t = int(body["today"]["orders"])
        assert m >= w >= t >= 0
        assert m > 0


class TestRevenueHourly:
    def test_returns_buckets(self, client):
        _seed(client)
        r = client.get("/api/revenue/hourly?days=7")
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 7
        assert isinstance(body["buckets"], list)
        # Seeded data spans 7 days × ~16 active hours; we expect a
        # nontrivial number of buckets.
        assert len(body["buckets"]) > 5
        for b in body["buckets"]:
            assert "ts" in b and "iso" in b and "revenue_baht" in b

    def test_days_clamped(self, client):
        # Out-of-range params shouldn't crash; we clamp 1..30.
        r = client.get("/api/revenue/hourly?days=999")
        assert r.status_code == 200
        assert r.json()["days"] == 30
        r2 = client.get("/api/revenue/hourly?days=0")
        assert r2.status_code == 200
        assert r2.json()["days"] == 1


class TestTopProducts:
    def test_returns_items(self, client):
        _seed(client)
        body = client.get("/api/products/top?days=7&limit=10").json()
        items = body["items"]
        assert isinstance(items, list)
        assert len(items) > 0
        # Sorted by revenue DESC -- pin contract for the dashboard
        # which relies on the ordering for its leaderboard.
        revs = [float(it["revenue_baht"].replace(",", "")) for it in items]
        assert revs == sorted(revs, reverse=True)

    def test_limit_respected(self, client):
        _seed(client)
        body = client.get("/api/products/top?days=30&limit=3").json()
        assert len(body["items"]) <= 3


class TestStaticIndex:
    def test_root_serves_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        body = r.text
        assert "NP Create" in body
        assert "Dashboard" in body


class TestOAuthCallback:
    def test_callback_acknowledges_params(self, client):
        r = client.get("/oauth/tiktok/callback?code=abc&state=s1")
        assert r.status_code == 200
        body = r.json()
        assert body["received"] is True
        assert body["params"]["code"] == "abc"
        assert body["params"]["state"] == "s1"
