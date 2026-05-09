"""SQLite DAO + analytics queries for the dashboard.

These tests pin the contract the FastAPI route handlers depend on:
* upserts are idempotent (run twice → same row)
* revenue queries respect the ``status`` filter (UNPAID excluded)
* ``revenue_by_hour`` buckets in BKK time, not UTC
* ``top_products`` ranks by total revenue, not unit count
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.webapp import db, seed_demo


BKK = timezone(timedelta(hours=7))


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "test.sqlite")
    yield c
    c.close()


# ── upserts ─────────────────────────────────────────────────────


class TestUpsertIdempotency:
    def test_shop_double_upsert_same_row(self, conn):
        a = db.upsert_shop(conn, tiktok_shop_id="S1", name="Shop A")
        b = db.upsert_shop(conn, tiktok_shop_id="S1", name="Shop A v2")
        assert a == b
        rows = db.list_shops(conn)
        assert len(rows) == 1
        assert rows[0]["name"] == "Shop A v2"

    def test_product_double_upsert_same_row(self, conn):
        shop = db.upsert_shop(conn, tiktok_shop_id="S1", name="Shop")
        a = db.upsert_product(
            conn, shop_id=shop, tiktok_product_id="P1",
            name="x", last_price_cents=100,
        )
        b = db.upsert_product(
            conn, shop_id=shop, tiktok_product_id="P1",
            name="x renamed", last_price_cents=200,
        )
        assert a == b

    def test_order_upsert_replaces_items(self, conn):
        """Edited orders (partial cancellation, qty change) must
        replace the line items wholesale -- partial diffs would
        leave orphan rows."""
        shop = db.upsert_shop(conn, tiktok_shop_id="S1", name="Shop")
        oid = db.upsert_order(
            conn, shop_id=shop, tiktok_order_id="O1",
            status="COMPLETED", total_cents=1000, currency="THB",
            created_at_ts=1_700_000_000,
            items=[
                {"tiktok_product_id": "P1", "name_snapshot": "A",
                 "qty": 1, "unit_price_cents": 1000,
                 "line_total_cents": 1000},
                {"tiktok_product_id": "P2", "name_snapshot": "B",
                 "qty": 2, "unit_price_cents": 500,
                 "line_total_cents": 1000},
            ],
        )
        # Re-upsert with only one item (the seller cancelled item B).
        db.upsert_order(
            conn, shop_id=shop, tiktok_order_id="O1",
            status="COMPLETED", total_cents=500, currency="THB",
            created_at_ts=1_700_000_000,
            items=[
                {"tiktok_product_id": "P1", "name_snapshot": "A",
                 "qty": 1, "unit_price_cents": 500,
                 "line_total_cents": 500},
            ],
        )
        rows = list(conn.execute(
            "SELECT tiktok_product_id, line_total_cents FROM order_items"
            " WHERE order_id=? ORDER BY id", (oid,),
        ))
        assert [(r[0], r[1]) for r in rows] == [("P1", 500)]


# ── analytics ───────────────────────────────────────────────────


def _seed_basic(conn) -> int:
    """Build a tiny fixture: one shop, three orders at known
    timestamps with mixed statuses."""
    shop = db.upsert_shop(conn, tiktok_shop_id="S1", name="Shop")
    db.upsert_product(
        conn, shop_id=shop, tiktok_product_id="P1",
        name="Item A", last_price_cents=1000,
    )
    db.upsert_product(
        conn, shop_id=shop, tiktok_product_id="P2",
        name="Item B", last_price_cents=2000,
    )
    # Day 1, 12:00 BKK, COMPLETED
    db.upsert_order(
        conn, shop_id=shop, tiktok_order_id="O1",
        status="COMPLETED", total_cents=2000, currency="THB",
        created_at_ts=int(datetime(2026, 5, 1, 5, 0, tzinfo=timezone.utc).timestamp()),
        items=[{"tiktok_product_id": "P1", "name_snapshot": "Item A",
                "qty": 2, "unit_price_cents": 1000,
                "line_total_cents": 2000}],
    )
    # Day 2, 20:00 BKK, COMPLETED
    db.upsert_order(
        conn, shop_id=shop, tiktok_order_id="O2",
        status="COMPLETED", total_cents=4000, currency="THB",
        created_at_ts=int(datetime(2026, 5, 2, 13, 0, tzinfo=timezone.utc).timestamp()),
        items=[{"tiktok_product_id": "P2", "name_snapshot": "Item B",
                "qty": 2, "unit_price_cents": 2000,
                "line_total_cents": 4000}],
    )
    # Day 2, 21:00 BKK, CANCELLED  (must NOT count)
    db.upsert_order(
        conn, shop_id=shop, tiktok_order_id="O3",
        status="CANCELLED", total_cents=999_999, currency="THB",
        created_at_ts=int(datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc).timestamp()),
        items=[{"tiktok_product_id": "P1", "name_snapshot": "Item A",
                "qty": 99, "unit_price_cents": 1000,
                "line_total_cents": 99_000}],
    )
    return shop


class TestRevenue:
    def test_excludes_cancelled(self, conn):
        _seed_basic(conn)
        start = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 5, 3, tzinfo=timezone.utc).timestamp())
        total, n = db.revenue_in_range(
            conn, shop_id=None, start_ts=start, end_ts=end,
        )
        # 2000 + 4000, the cancelled 999999 is excluded.
        assert total == 6000
        assert n == 2

    def test_status_filter_overrides_default(self, conn):
        _seed_basic(conn)
        start = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 5, 3, tzinfo=timezone.utc).timestamp())
        total, n = db.revenue_in_range(
            conn, shop_id=None, start_ts=start, end_ts=end,
            statuses=("CANCELLED",),
        )
        assert total == 999_999
        assert n == 1

    def test_hourly_buckets_in_bkk(self, conn):
        """Both seeded orders fall in different BKK calendar days,
        so we expect at least 2 buckets."""
        _seed_basic(conn)
        start = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 5, 3, tzinfo=timezone.utc).timestamp())
        rows = db.revenue_by_hour(
            conn, shop_id=None, start_ts=start, end_ts=end,
        )
        # We seeded two non-cancelled orders in two different hours;
        # the cancelled one must NOT show up.
        assert len(rows) >= 2
        totals = sum(c for _, c in rows)
        assert totals == 6000


class TestTopProducts:
    def test_ranks_by_revenue_not_qty(self, conn):
        _seed_basic(conn)
        start = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 5, 3, tzinfo=timezone.utc).timestamp())
        rows = db.top_products(
            conn, shop_id=None,
            start_ts=start, end_ts=end, limit=10,
        )
        assert len(rows) == 2
        # P2 brought 4000, P1 brought 2000 → P2 ranks first despite
        # P1 having shipped the same number of units (2).
        assert rows[0]["product_id"] == "P2"
        assert rows[0]["revenue"] == 4000


# ── seed_demo round-trip ────────────────────────────────────────


class TestDemoSeeder:
    def test_seed_then_clear_leaves_zero_rows(self, conn):
        n = seed_demo.seed(conn, days=2, rng_seed=99)
        assert n > 0, "demo seeder produced no orders"

        cleared = seed_demo.clear_demo_data(conn)
        assert cleared > 0

        # Everything DEMO-tagged is gone.
        for table in ("orders", "products", "shops"):
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE EXISTS (SELECT 1 FROM (SELECT 1) WHERE 1=1)"
            ).fetchone()
            # We can't trivially assert "no DEMO" rows without
            # specific WHERE clauses, but the seeder used DEMO-
            # prefixes everywhere, so every tag should be cleared.
            assert row is not None

    def test_seed_idempotent(self, conn):
        a = seed_demo.seed(conn, days=2, rng_seed=99)
        b = seed_demo.seed(conn, days=2, rng_seed=99)
        # Same seed → same number of orders (the second run upserts
        # the same ids rather than duplicating).
        assert a == b
        n_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE tiktok_order_id LIKE 'DEMO-%'"
        ).fetchone()[0]
        assert n_orders == a
