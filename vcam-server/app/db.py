"""SQLite schema, connection helpers, and tiny DAO functions.

Why raw sqlite3 instead of SQLAlchemy
-------------------------------------

The whole admin server is ~6 tables and ~30 queries. Pulling in
SQLAlchemy + Alembic would more than double the dependency tree
for no real ergonomic gain at this scale, and would make the file
storage format opaque (binary metadata in extra tables). Raw
sqlite3 keeps the on-disk schema human-readable so a panic-mode
operator can ``sqlite3 npcreate.sqlite3 "select * from licenses"``
from the VPS console without learning ORM internals.

If we ever migrate to PostgreSQL or scale to >5 admins, the ORM
swap is local to this file plus the route-level callers — every
query lives in ``_dao_*`` helpers below, not scattered through
handlers.

Money + time conventions
------------------------

* Money is INTEGER **satang** (1 baht = 100 satang). Float baht
  was the source of one bug already (0.1+0.2 != 0.3 inflated
  totals by 0.0001 baht over a few thousand rows).
* Timestamps are TEXT ISO-8601 with seconds precision in UTC.
  We display in Asia/Bangkok in the UI, but storage stays in UTC
  so daylight changes (Thailand has none, but humanitarian: future
  TZ migrations) don't rewrite history.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import SETTINGS

log = logging.getLogger(__name__)


# ── schema ──────────────────────────────────────────────────────────


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS admins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Email is the login handle. Unique so a typo'd duplicate
    -- account doesn't silently shadow the original.
    email           TEXT    NOT NULL UNIQUE,
    -- bcrypt hash. We rely on bcrypt's built-in salt; no separate
    -- salt column needed.
    password_hash   TEXT    NOT NULL,
    display_name    TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    last_login_at   TEXT    NOT NULL DEFAULT '',
    -- Soft-delete: revoked admins can't log in but their audit
    -- trail (who issued which keys) stays linked.
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS customers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    -- Line is the customer's primary contact for us; nullable for
    -- corner cases (B2B contracts via email only).
    line_id         TEXT    NOT NULL DEFAULT '',
    phone           TEXT    NOT NULL DEFAULT '',
    email           TEXT    NOT NULL DEFAULT '',
    notes           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    -- Free-text status: "active", "trial", "churned". We don't
    -- enum-ify because new statuses appear quarterly.
    status          TEXT    NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name);
CREATE INDEX IF NOT EXISTS idx_customers_line ON customers(line_id);

CREATE TABLE IF NOT EXISTS licenses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    -- Full hyphenated key as we hand it to the customer. Indexed
    -- + UNIQUE because phone-home activation looks the row up by
    -- this string.
    key             TEXT    NOT NULL UNIQUE,
    -- The 6-char nonce embedded in the key payload. Used by the
    -- revocation-list endpoint so we don't have to publish the
    -- full key (which the customer is paying us to keep secret).
    nonce           TEXT    NOT NULL,
    customer_name   TEXT    NOT NULL,
    max_devices     INTEGER NOT NULL,
    expiry          TEXT    NOT NULL,           -- yyyy-mm-dd UTC
    issued_at       TEXT    NOT NULL,
    issued_by_admin INTEGER REFERENCES admins(id),
    note            TEXT    NOT NULL DEFAULT '',
    -- 'active' | 'revoked' | 'expired'. We compute "expired" lazily
    -- (date.today() > expiry) but persist explicit revocations so
    -- a future date can't undo an admin's revoke.
    status          TEXT    NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_licenses_customer ON licenses(customer_id);
CREATE INDEX IF NOT EXISTS idx_licenses_status   ON licenses(status);

CREATE TABLE IF NOT EXISTS activations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id      INTEGER NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    -- Customer-app-derived ID (hostname+MAC hash). Stable per PC.
    machine_id      TEXT    NOT NULL,
    machine_label   TEXT    NOT NULL DEFAULT '',  -- friendly hostname
    -- For ops debugging. We don't lat/lon — just IP-as-string.
    last_ip         TEXT    NOT NULL DEFAULT '',
    user_agent      TEXT    NOT NULL DEFAULT '',
    app_version     TEXT    NOT NULL DEFAULT '',
    activated_at    TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    -- 'active' | 'blocked'. An admin can block a single machine
    -- (e.g. customer's stolen laptop) without revoking the key
    -- as a whole.
    status          TEXT    NOT NULL DEFAULT 'active',
    UNIQUE(license_id, machine_id)
);

CREATE INDEX IF NOT EXISTS idx_act_license ON activations(license_id);
CREATE INDEX IF NOT EXISTS idx_act_status  ON activations(status);

CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    -- license_id is nullable: a payment can come in BEFORE we
    -- issue the matching key (e.g. "wait for slip then issue").
    license_id      INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
    amount_satang   INTEGER NOT NULL,
    method          TEXT    NOT NULL DEFAULT 'promptpay',
    -- Free-form ref number from the payment slip / bank statement.
    reference       TEXT    NOT NULL DEFAULT '',
    -- 'pending' | 'received' | 'refunded'. Most payments go
    -- pending → received instantly (we record after seeing the
    -- slip) but the state machine leaves room for slow flows.
    status          TEXT    NOT NULL DEFAULT 'received',
    note            TEXT    NOT NULL DEFAULT '',
    received_at     TEXT    NOT NULL,
    recorded_by_admin INTEGER REFERENCES admins(id)
);

CREATE INDEX IF NOT EXISTS idx_pay_customer ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_pay_received ON payments(received_at);

CREATE TABLE IF NOT EXISTS support_tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- license_id is the only customer link — we trust the customer
    -- app to send a key it owns. (If they spoof a key, the worst
    -- they can do is send us their own log under someone else's
    -- name — there's no PII leakage risk on the admin side.)
    license_id      INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
    customer_name   TEXT    NOT NULL DEFAULT '',  -- snapshot
    -- Path on disk to the uploaded log zip. Relative to UPLOAD_DIR
    -- so backups can move the dir without rewriting rows.
    log_path        TEXT    NOT NULL,
    log_size_bytes  INTEGER NOT NULL,
    message         TEXT    NOT NULL DEFAULT '',
    -- 'open' | 'in_progress' | 'closed'.
    status          TEXT    NOT NULL DEFAULT 'open',
    submitted_at    TEXT    NOT NULL,
    last_admin_reply TEXT   NOT NULL DEFAULT '',
    last_admin_reply_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON support_tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_submitted ON support_tickets(submitted_at);

-- Audit log of admin actions. Append-only by convention; enforce
-- in code (no DELETE, no UPDATE). When a junior admin shows up
-- next year and revokes a key by accident, we want to know who.
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id        INTEGER REFERENCES admins(id),
    action          TEXT    NOT NULL,            -- 'license.issue', 'license.revoke', ...
    target_kind     TEXT    NOT NULL DEFAULT '', -- 'license' | 'customer' | ...
    target_id       INTEGER NOT NULL DEFAULT 0,
    details         TEXT    NOT NULL DEFAULT '',
    at              TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""


def init_db() -> None:
    """Run the schema migration. Idempotent — safe to call on every
    boot, which is what the FastAPI lifespan handler does."""
    db_path: Path = SETTINGS.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("init db at %s", db_path)
    with connect() as cx:
        cx.executescript(SCHEMA_SQL)
        cx.commit()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection with FK enforcement and Row factory.

    The Row factory makes columns accessible by name in handlers —
    ``row["customer_name"]`` instead of ``row[3]`` — which dramatically
    cuts down on bugs when the schema gains a column.

    We set busy_timeout so concurrent uvicorn workers don't immediately
    raise on a write contention; SQLite WAL mode handles the rest.
    """
    cx = sqlite3.connect(
        SETTINGS.db_path,
        isolation_level=None,  # we manage transactions explicitly
        timeout=10.0,
    )
    try:
        cx.execute("PRAGMA foreign_keys = ON")
        cx.execute("PRAGMA busy_timeout = 5000")
        cx.row_factory = sqlite3.Row
        yield cx
    finally:
        cx.close()


# ── time helpers ────────────────────────────────────────────────────


def now_iso() -> str:
    """ISO-8601 UTC seconds — the exact format we store everywhere.

    Centralised so we can swap to nanosecond precision later
    without grepping the codebase.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
