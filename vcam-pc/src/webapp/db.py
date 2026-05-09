"""SQLite schema + DAO for the sales dashboard.

Schema design notes
-------------------

We model the **shop**, **product**, **order**, **order_item** entities
that the TikTok Shop API exposes -- not whatever clever aggregation
we want to show in the UI. That keeps the sync layer dumb (just write
what the API returned) and pushes business logic to the read path,
where SQL is good at it.

Money is stored as **integer cents** (e.g. ``42500`` = ฿425.00) to
avoid the classic float-rounding bug where 0.1+0.2 != 0.3 -- TikTok's
own API returns money as decimal strings, but adding hundreds of them
in float space gives wrong totals at scale.

Timestamps are stored as **Unix epoch seconds (UTC)**. The dashboard
converts to Asia/Bangkok at render time so date boundaries match what
the customer sees on their phone.

Why one DB per machine, not per shop
------------------------------------

The desktop app is single-user (one customer, one PC). They might
manage multiple TikTok Shops, but always under one operator account.
Putting everything in a single ``npcreate_dashboard.sqlite3`` file
makes backup/restore trivial -- they copy that one file to migrate
to a new computer.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)


# ── locations ───────────────────────────────────────────────────


def default_db_path() -> Path:
    """Where the SQLite file lives on this machine.

    We put it next to the desktop app's ``customer_devices.json``
    (same parent dir) so the customer manual can describe a single
    "data folder" instead of two.
    """
    return Path(__file__).resolve().parent.parent.parent / "npcreate_dashboard.sqlite3"


# ── schema ──────────────────────────────────────────────────────


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shops (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- TikTok's stable shop identifier (cipher_id from the OAuth
    -- exchange). Unique because each shop authorizes the app once
    -- per shop, not per customer.
    tiktok_shop_id  TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    region          TEXT    NOT NULL DEFAULT 'TH',
    -- OAuth bearer + refresh tokens. Tokens rotate; we re-write
    -- the row on every refresh.
    access_token    TEXT,
    refresh_token   TEXT,
    -- Epoch second when access_token expires (UTC). The sync
    -- worker refreshes ~10 min before this fires.
    token_expires_at INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id         INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    tiktok_product_id TEXT  NOT NULL,
    name            TEXT    NOT NULL,
    image_url       TEXT,
    -- Cached price for the dashboard's "top products" widget. The
    -- authoritative price lives on each order_item row (TikTok lets
    -- the seller change SKU price between orders).
    last_price_cents INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL,
    UNIQUE(shop_id, tiktok_product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id         INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    tiktok_order_id TEXT    NOT NULL,
    -- TikTok order status: UNPAID / AWAITING_SHIPMENT / IN_TRANSIT
    --                     / DELIVERED / COMPLETED / CANCELLED ...
    status          TEXT    NOT NULL,
    -- Total payable amount AFTER discounts/coupons but BEFORE
    -- shipping fees. Matches what the dashboard shows as
    -- "ยอดขาย" so the seller's intuition matches the number.
    total_cents     INTEGER NOT NULL,
    currency        TEXT    NOT NULL DEFAULT 'THB',
    -- Epoch seconds (UTC) when TikTok marked the order CREATED.
    -- Indexed because every dashboard query filters by this.
    created_at_ts   INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    UNIQUE(shop_id, tiktok_order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_shop_created
    ON orders(shop_id, created_at_ts);

CREATE TABLE IF NOT EXISTS order_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id      INTEGER REFERENCES products(id) ON DELETE SET NULL,
    -- Snapshot the product's identity AT THE TIME of the order so
    -- the dashboard still works after the seller deletes a SKU.
    tiktok_product_id TEXT NOT NULL,
    name_snapshot   TEXT    NOT NULL,
    qty             INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    line_total_cents INTEGER NOT NULL,
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);

CREATE TABLE IF NOT EXISTS sync_state (
    shop_id         INTEGER PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,
    last_orders_synced_at INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_at INTEGER NOT NULL DEFAULT 0
);
"""


# ── connection helper ───────────────────────────────────────────


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a sqlite3 connection with sane defaults for desktop use:

    * ``PRAGMA foreign_keys = ON`` -- relational integrity. SQLite
      ships with this OFF for backwards compat; we want cascades.
    * ``PRAGMA journal_mode = WAL`` -- the dashboard read thread
      and the sync worker thread can hit the file at the same time
      without lock contention.
    * ``row_factory = sqlite3.Row`` -- dict-style access in routes.
    * ``isolation_level = None`` -- explicit BEGIN/COMMIT in DAO
      functions; otherwise sqlite3's "smart" autocommit confuses
      multi-statement transactions.
    """
    p = path or default_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a block in a SQLite transaction.

    We can't rely on the default sqlite3 autocommit-on-INSERT logic
    because we set ``isolation_level=None`` above. This helper makes
    "begin/commit/rollback" explicit and safe to nest under
    ``with`` blocks.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ── DAO ─────────────────────────────────────────────────────────


def upsert_shop(
    conn: sqlite3.Connection,
    *,
    tiktok_shop_id: str,
    name: str,
    region: str = "TH",
    access_token: str | None = None,
    refresh_token: str | None = None,
    token_expires_at: int = 0,
) -> int:
    now = int(time.time())
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO shops (
                tiktok_shop_id, name, region,
                access_token, refresh_token, token_expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tiktok_shop_id) DO UPDATE SET
                name = excluded.name,
                region = excluded.region,
                access_token = COALESCE(excluded.access_token, shops.access_token),
                refresh_token = COALESCE(excluded.refresh_token, shops.refresh_token),
                token_expires_at = CASE
                    WHEN excluded.token_expires_at > 0
                    THEN excluded.token_expires_at
                    ELSE shops.token_expires_at
                END,
                updated_at = excluded.updated_at
            RETURNING id
            """,
            (tiktok_shop_id, name, region,
             access_token, refresh_token, token_expires_at,
             now, now),
        )
        return cur.fetchone()[0]


def list_shops(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM shops ORDER BY name COLLATE NOCASE"
    ))


def upsert_product(
    conn: sqlite3.Connection,
    *,
    shop_id: int,
    tiktok_product_id: str,
    name: str,
    image_url: str | None = None,
    last_price_cents: int = 0,
) -> int:
    now = int(time.time())
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO products (
                shop_id, tiktok_product_id, name, image_url,
                last_price_cents, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(shop_id, tiktok_product_id) DO UPDATE SET
                name = excluded.name,
                image_url = COALESCE(excluded.image_url, products.image_url),
                last_price_cents = CASE
                    WHEN excluded.last_price_cents > 0
                    THEN excluded.last_price_cents
                    ELSE products.last_price_cents
                END,
                updated_at = excluded.updated_at
            RETURNING id
            """,
            (shop_id, tiktok_product_id, name, image_url,
             last_price_cents, now),
        )
        return cur.fetchone()[0]


def upsert_order(
    conn: sqlite3.Connection,
    *,
    shop_id: int,
    tiktok_order_id: str,
    status: str,
    total_cents: int,
    currency: str,
    created_at_ts: int,
    items: list[dict],
) -> int:
    """Upsert an order plus its line items in a single transaction.

    ``items`` is a list of dicts with keys: ``tiktok_product_id``,
    ``name_snapshot``, ``qty``, ``unit_price_cents``,
    ``line_total_cents``. We delete + re-insert the lines on every
    upsert -- TikTok occasionally edits an existing order's items
    (e.g. partial cancellation) and the simple full-replace is
    correct without any diff logic.
    """
    now = int(time.time())
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO orders (
                shop_id, tiktok_order_id, status, total_cents,
                currency, created_at_ts, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(shop_id, tiktok_order_id) DO UPDATE SET
                status = excluded.status,
                total_cents = excluded.total_cents,
                currency = excluded.currency,
                created_at_ts = excluded.created_at_ts,
                updated_at = excluded.updated_at
            RETURNING id
            """,
            (shop_id, tiktok_order_id, status, total_cents, currency,
             created_at_ts, now),
        )
        order_id = cur.fetchone()[0]

        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))

        for it in items:
            # Look up the local product PK so the FK is filled in;
            # if we haven't synced products yet, leave NULL and let
            # the next product sync backfill it (FKs allow NULL).
            row = conn.execute(
                "SELECT id FROM products WHERE shop_id=? AND tiktok_product_id=?",
                (shop_id, it["tiktok_product_id"]),
            ).fetchone()
            local_pid = row[0] if row else None
            conn.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, tiktok_product_id,
                    name_snapshot, qty, unit_price_cents,
                    line_total_cents, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, local_pid, it["tiktok_product_id"],
                 it["name_snapshot"], int(it["qty"]),
                 int(it["unit_price_cents"]), int(it["line_total_cents"]),
                 now),
            )
        return order_id


# ── analytics queries ───────────────────────────────────────────


def revenue_in_range(
    conn: sqlite3.Connection,
    *,
    shop_id: int | None,
    start_ts: int,
    end_ts: int,
    statuses: tuple[str, ...] = ("AWAITING_SHIPMENT", "IN_TRANSIT",
                                  "DELIVERED", "COMPLETED"),
) -> tuple[int, int]:
    """Total revenue + order count between two epoch seconds.

    We exclude UNPAID and CANCELLED by default -- the seller only
    cares about "real" sales. Returns ``(total_cents, order_count)``.
    """
    placeholder = ",".join("?" for _ in statuses)
    args: list = [start_ts, end_ts, *statuses]
    where_shop = ""
    if shop_id is not None:
        where_shop = "AND shop_id = ?"
        args.append(shop_id)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(total_cents), 0) AS total,
               COUNT(*)                     AS n
        FROM orders
        WHERE created_at_ts >= ?
          AND created_at_ts <  ?
          AND status IN ({placeholder})
          {where_shop}
        """,
        args,
    ).fetchone()
    return int(row["total"]), int(row["n"])


def revenue_by_hour(
    conn: sqlite3.Connection,
    *,
    shop_id: int | None,
    start_ts: int,
    end_ts: int,
) -> list[tuple[int, int]]:
    """Bucketed by Asia/Bangkok wall-clock hour. Returns
    ``[(unix_hour_start_ts, total_cents), ...]``.

    SQLite has no timezone library, so we shift to BKK (+7 h) by
    adding ``25200`` to the timestamp before bucketing, then shift
    back when reporting the bucket boundary. This avoids importing
    a tz library purely for grouping.
    """
    BKK_SEC = 7 * 3600
    args: list = [BKK_SEC, BKK_SEC, start_ts, end_ts]
    where_shop = ""
    if shop_id is not None:
        where_shop = "AND shop_id = ?"
        args.append(shop_id)
    rows = conn.execute(
        f"""
        SELECT (((created_at_ts + ?) / 3600) * 3600) - ? AS bucket_ts,
               COALESCE(SUM(total_cents), 0)            AS total
        FROM orders
        WHERE created_at_ts >= ?
          AND created_at_ts <  ?
          AND status NOT IN ('UNPAID', 'CANCELLED')
          {where_shop}
        GROUP BY bucket_ts
        ORDER BY bucket_ts
        """,
        args,
    ).fetchall()
    return [(int(r["bucket_ts"]), int(r["total"])) for r in rows]


def top_products(
    conn: sqlite3.Connection,
    *,
    shop_id: int | None,
    start_ts: int,
    end_ts: int,
    limit: int = 10,
) -> list[dict]:
    """Top-selling products in the window, by total revenue."""
    args: list = [start_ts, end_ts]
    where_shop = ""
    if shop_id is not None:
        where_shop = "AND o.shop_id = ?"
        args.append(shop_id)
    args.append(limit)
    rows = conn.execute(
        f"""
        SELECT  oi.tiktok_product_id        AS product_id,
                oi.name_snapshot            AS name,
                COALESCE(p.image_url, '')   AS image_url,
                SUM(oi.qty)                 AS qty,
                SUM(oi.line_total_cents)    AS revenue
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE o.created_at_ts >= ?
          AND o.created_at_ts <  ?
          AND o.status NOT IN ('UNPAID', 'CANCELLED')
          {where_shop}
        GROUP BY oi.tiktok_product_id
        ORDER BY revenue DESC
        LIMIT ?
        """,
        args,
    ).fetchall()
    return [dict(r) for r in rows]
