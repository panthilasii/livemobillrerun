"""Seed the SQLite DB with realistic demo orders.

Why we ship this
----------------

TikTok Shop API approval can take 1-2 weeks for a new developer
account. During that gap, the dashboard would otherwise show
empty charts -- which both makes the product look broken AND makes
it impossible for us to test the read path against believable
data shapes.

The seeder generates a 7-day window of orders following a
realistic diurnal pattern (peak around 19:00-22:00 BKK, near zero
4 AM-9 AM) using only the stdlib ``random`` -- no extra dep.

Idempotent
----------

We tag every demo row with the prefix ``DEMO-`` on its
``tiktok_order_id`` / ``tiktok_product_id`` so:

* The sync worker NEVER overwrites real TikTok data with demo
  data (real ids never start with ``DEMO-``).
* ``clear_demo_data`` can wipe just the seeded rows without
  touching whatever's been pulled from a real shop.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

log = logging.getLogger(__name__)


_DEMO_PRODUCTS = [
    ("เซรั่มหน้าใส 30ml", 25000, "https://placehold.co/120/FF7E47/fff?text=Serum"),
    ("ครีมกันแดด SPF50+", 18000, "https://placehold.co/120/FFB347/fff?text=SPF"),
    ("ลิปทินท์ Velvet", 14900, "https://placehold.co/120/E63946/fff?text=Lip"),
    ("ผงล้างหน้า Foam", 9900,  "https://placehold.co/120/A8DADC/000?text=Foam"),
    ("มาส์กหน้า 10 ชิ้น", 29900, "https://placehold.co/120/457B9D/fff?text=Mask"),
    ("เจลล้างหน้า Aloe", 12900, "https://placehold.co/120/2A9D8F/fff?text=Aloe"),
    ("ครีมบำรุงรอบดวงตา", 35000, "https://placehold.co/120/E76F51/fff?text=Eye"),
    ("เซตเครื่องสำอาง 5 ชิ้น", 89000, "https://placehold.co/120/F4A261/fff?text=Set"),
]


def _hour_weight(hour_bkk: int) -> float:
    """Diurnal sales curve scaled to a 0..1 weight.

    Peaks 19-22 (post-dinner browse), troughs 04-08 (sleep).
    Calibrated so the weighted sum across 24 hours is roughly 1.
    """
    if 19 <= hour_bkk <= 22:
        return 1.0
    if 12 <= hour_bkk <= 14:
        return 0.7   # lunch break browsing
    if 9 <= hour_bkk <= 18:
        return 0.4
    if 23 <= hour_bkk or hour_bkk <= 1:
        return 0.5   # late-night impulse
    return 0.05      # 02-08


def seed(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    rng_seed: int = 42,
) -> int:
    """Insert demo data spanning ``days`` ending now. Returns the
    number of orders created.

    Calling repeatedly does NOT duplicate -- the order ids are
    deterministic given ``rng_seed`` and our upsert logic just
    overwrites.
    """
    rng = random.Random(rng_seed)

    shop_id = db.upsert_shop(
        conn,
        tiktok_shop_id="DEMO-SHOP-1",
        name="ร้านตัวอย่าง (DEMO)",
        region="TH",
    )

    # Products first so order_items can FK to them.
    product_local_ids: list[int] = []
    for i, (name, price, img) in enumerate(_DEMO_PRODUCTS):
        pid = db.upsert_product(
            conn,
            shop_id=shop_id,
            tiktok_product_id=f"DEMO-PROD-{i:03d}",
            name=name,
            image_url=img,
            last_price_cents=price,
        )
        product_local_ids.append(pid)

    # Orders distributed across the window with the diurnal curve.
    BKK = timezone(timedelta(hours=7))
    end = datetime.now(BKK)
    start = end - timedelta(days=days)

    # Target ~30-60 orders per peak day; scale by hour weight.
    n_created = 0
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        weight = _hour_weight(cursor.hour)
        # Poisson-ish: expected orders per hour = 6 * weight; jitter
        # +-2 so the chart doesn't look perfectly smooth.
        n_orders = max(0, int(rng.gauss(6 * weight, 1.5 * weight + 0.3)))
        for _ in range(n_orders):
            offset = timedelta(seconds=rng.randint(0, 3599))
            ts = int((cursor + offset).timestamp())
            order_idx = n_created
            n_items = rng.choices([1, 1, 1, 2, 2, 3], k=1)[0]
            items: list[dict] = []
            total = 0
            for _ in range(n_items):
                p_idx = rng.randrange(len(_DEMO_PRODUCTS))
                p_name, p_price, _ = _DEMO_PRODUCTS[p_idx]
                qty = rng.choices([1, 1, 1, 2, 3], k=1)[0]
                line = p_price * qty
                total += line
                items.append({
                    "tiktok_product_id": f"DEMO-PROD-{p_idx:03d}",
                    "name_snapshot": p_name,
                    "qty": qty,
                    "unit_price_cents": p_price,
                    "line_total_cents": line,
                })

            # Status mix: most orders complete, a few cancelled.
            status = rng.choices(
                ["COMPLETED", "DELIVERED", "IN_TRANSIT",
                 "AWAITING_SHIPMENT", "CANCELLED"],
                weights=[55, 20, 10, 10, 5],
                k=1,
            )[0]
            db.upsert_order(
                conn,
                shop_id=shop_id,
                tiktok_order_id=f"DEMO-ORD-{rng_seed}-{order_idx:06d}",
                status=status,
                total_cents=total,
                currency="THB",
                created_at_ts=ts,
                items=items,
            )
            n_created += 1
        cursor += timedelta(hours=1)

    log.info("seeded %d demo orders across %d days", n_created, days)
    return n_created


def clear_demo_data(conn: sqlite3.Connection) -> int:
    """Wipe rows whose TikTok ids start with ``DEMO-``. Returns
    the total rows deleted (orders + items + products + shop)."""
    deleted = 0
    with db.transaction(conn):
        cur = conn.execute(
            "DELETE FROM orders WHERE tiktok_order_id LIKE 'DEMO-%'"
        )
        deleted += cur.rowcount
        cur = conn.execute(
            "DELETE FROM products WHERE tiktok_product_id LIKE 'DEMO-%'"
        )
        deleted += cur.rowcount
        cur = conn.execute(
            "DELETE FROM shops WHERE tiktok_shop_id LIKE 'DEMO-%'"
        )
        deleted += cur.rowcount
    return deleted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    n = seed(conn)
    print(f"seeded {n} demo orders into {db.default_db_path()}")
