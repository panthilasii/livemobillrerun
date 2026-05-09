"""FastAPI server that backs the embedded sales dashboard.

Lifecycle
---------

The desktop app calls ``start_in_thread(port=8765)`` from its
"Dashboard" button handler. We spin up uvicorn in a daemon thread
so the server dies with the parent process -- if the user kills
NP Create, we don't leave a stale dashboard server bound to 8765
(which would then refuse to bind on the next launch).

Endpoints
---------

* ``GET  /``                       -- serves ``static/index.html``
* ``GET  /api/health``             -- returns ``{"ok": true, "version": ...}``
* ``GET  /api/summary``            -- KPIs: today/week/month revenue + counts
* ``GET  /api/revenue/hourly``     -- 7-day chart data
* ``GET  /api/products/top``       -- top-N products
* ``POST /api/demo/seed``          -- (admin) (re)seed demo data
* ``POST /api/demo/clear``         -- (admin) clear demo data
* ``GET  /oauth/tiktok/callback``  -- OAuth landing for TikTok Shop

Auth (or lack thereof)
----------------------

We bind ONLY to ``127.0.0.1`` -- never ``0.0.0.0``. The customer's
LAN can't see this server. Inside their machine, anything they run
already has full filesystem access; adding a token check would be
security theatre.

If we ever go public-cloud (Q2 plan), the auth wrapper goes here.
The file is structured so an ``@require_user_id`` decorator can be
slotted in without touching the route handlers.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, seed_demo
from .. import branding

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
BKK_TZ = timezone(timedelta(hours=7))

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"


# ── helpers ─────────────────────────────────────────────────────


def _bkk_today_window() -> tuple[int, int]:
    """Return ``(start_of_today_bkk, now)`` as epoch seconds.

    Days roll over at 00:00 Asia/Bangkok -- the seller's wall clock,
    not the server's. We don't store BKK in SQLite (always UTC),
    so the conversion happens here every time a route is hit.
    """
    now = datetime.now(BKK_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _bkk_window_days_ago(days: int) -> tuple[int, int]:
    end = datetime.now(BKK_TZ)
    start = (end - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return int(start.timestamp()), int(end.timestamp())


def _cents_to_baht(cents: int) -> str:
    return f"{cents / 100:.2f}"


# ── app factory ─────────────────────────────────────────────────


def create_app(db_path: Path | None = None) -> FastAPI:
    """Build the FastAPI app. Separated from ``start_in_thread`` so
    tests can drive it via httpx.AsyncClient without spawning
    uvicorn."""
    app = FastAPI(
        title=f"{branding.BRAND.name} Dashboard",
        version=branding.BRAND.version,
        docs_url=None,
        redoc_url=None,
    )

    # Single connection per app -- SQLite + WAL is happy under
    # concurrent reads and the single sync writer hits the same
    # connection. We attach to ``app.state`` so tests can swap it
    # for an in-memory DB.
    app.state.db = db.connect(db_path)

    # Static asset mount BEFORE route registration so ``/static/*``
    # doesn't shadow our ``/`` index handler.
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ── routes ──────────────────────────────────────────────────

    @app.get("/")
    def root() -> FileResponse:
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(500, "dashboard html missing")
        return FileResponse(index)

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "name": branding.BRAND.name,
            "version": branding.BRAND.version,
        }

    @app.get("/api/summary")
    def summary() -> dict:
        conn = app.state.db
        today_start, now = _bkk_today_window()
        week_start, _ = _bkk_window_days_ago(7)
        month_start, _ = _bkk_window_days_ago(30)

        today_total, today_n = db.revenue_in_range(
            conn, shop_id=None,
            start_ts=today_start, end_ts=now,
        )
        week_total, week_n = db.revenue_in_range(
            conn, shop_id=None,
            start_ts=week_start, end_ts=now,
        )
        month_total, month_n = db.revenue_in_range(
            conn, shop_id=None,
            start_ts=month_start, end_ts=now,
        )

        avg_today = int(today_total / today_n) if today_n else 0

        return {
            "today": {
                "revenue_baht": _cents_to_baht(today_total),
                "revenue_cents": today_total,
                "orders": today_n,
                "avg_order_baht": _cents_to_baht(avg_today),
            },
            "week": {
                "revenue_baht": _cents_to_baht(week_total),
                "orders": week_n,
            },
            "month": {
                "revenue_baht": _cents_to_baht(month_total),
                "orders": month_n,
            },
            "generated_at": int(time.time()),
        }

    @app.get("/api/revenue/hourly")
    def revenue_hourly(days: int = 7) -> dict:
        days = max(1, min(30, int(days)))
        start, end = _bkk_window_days_ago(days)
        rows = db.revenue_by_hour(
            app.state.db, shop_id=None,
            start_ts=start, end_ts=end,
        )
        return {
            "days": days,
            "buckets": [
                {
                    "ts": ts,
                    "iso": datetime.fromtimestamp(ts, BKK_TZ).isoformat(),
                    "revenue_baht": _cents_to_baht(c),
                }
                for ts, c in rows
            ],
        }

    @app.get("/api/products/top")
    def products_top(days: int = 7, limit: int = 10) -> dict:
        days = max(1, min(30, int(days)))
        limit = max(1, min(50, int(limit)))
        start, end = _bkk_window_days_ago(days)
        rows = db.top_products(
            app.state.db, shop_id=None,
            start_ts=start, end_ts=end,
            limit=limit,
        )
        return {
            "items": [
                {
                    "product_id": r["product_id"],
                    "name": r["name"],
                    "image_url": r["image_url"],
                    "qty": int(r["qty"]),
                    "revenue_baht": _cents_to_baht(int(r["revenue"])),
                }
                for r in rows
            ],
        }

    @app.post("/api/demo/seed")
    def demo_seed() -> dict:
        n = seed_demo.seed(app.state.db)
        return {"seeded": n}

    @app.post("/api/demo/clear")
    def demo_clear() -> dict:
        n = seed_demo.clear_demo_data(app.state.db)
        return {"cleared": n}

    # ── OAuth landing ───────────────────────────────────────────
    #
    # When the seller clicks "Connect TikTok Shop" in the dashboard,
    # they're sent to TikTok's authorize URL with our app_key. After
    # they grant permission, TikTok redirects them BACK to:
    #     http://localhost:8765/oauth/tiktok/callback?code=...&state=...
    # We exchange ``code`` for tokens here and persist via db.
    @app.get("/oauth/tiktok/callback")
    def oauth_callback(request: Request) -> JSONResponse:
        # We don't ship app_key/app_secret yet -- those come from
        # the partner.tiktokshop.com developer portal. For now this
        # endpoint just records the inbound redirect so the user
        # can copy the auth code into the desktop app's credentials
        # screen (built in v1).
        params = dict(request.query_params)
        log.info("oauth callback received: keys=%s", list(params.keys()))
        return JSONResponse(
            status_code=200,
            content={
                "received": True,
                "params": params,
                "next_step": (
                    "นำ 'code' ที่ได้รับไปกรอกในหน้า Settings ของ NP Create "
                    "เพื่อเชื่อม TikTok Shop ของคุณ"
                ),
            },
        )

    return app


# ── thread launcher ─────────────────────────────────────────────


class _ServerHandle:
    """Reference returned by ``start_in_thread`` so callers can
    stop the uvicorn loop cleanly on app shutdown.

    We don't return a ``uvicorn.Server`` directly because uvicorn's
    public stop signal (``server.should_exit = True``) is checked
    only between request loops -- we wrap it so ``stop()`` is a
    one-line thing the desktop app can fire from Tk's window-close
    handler.
    """

    def __init__(self, server, thread, url: str) -> None:
        self._server = server
        self._thread = thread
        self.url = url

    def stop(self) -> None:
        try:
            self._server.should_exit = True
        except Exception:  # pragma: no cover -- defensive
            log.exception("stopping dashboard server")

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def start_in_thread(
    *,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    db_path: Path | None = None,
) -> _ServerHandle:
    """Spawn uvicorn in a daemon thread bound to ``host:port``.

    Returns immediately with a handle the caller can use to shut
    down the server (e.g. on app close). ``daemon=True`` means the
    server thread dies with the parent if the desktop app exits
    abruptly -- no orphaned port left listening.
    """
    import uvicorn

    app = create_app(db_path=db_path)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(
        target=server.run,
        name="np-dashboard-server",
        daemon=True,
    )
    thread.start()

    # Wait until the socket is actually listening so the desktop app
    # can ``webbrowser.open(url)`` without racing the uvicorn boot
    # (which otherwise produces a "site cannot be reached" flash).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        log.warning("dashboard server did not signal started in 5 s")

    url = f"http://{host}:{port}/"
    log.info("dashboard server up at %s", url)
    return _ServerHandle(server=server, thread=thread, url=url)


__all__ = ["create_app", "start_in_thread", "DEFAULT_PORT", "DEFAULT_HOST"]
