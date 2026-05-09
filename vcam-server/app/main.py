"""ASGI entrypoint for the license/admin server.

Run locally::

    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

In production (Docker), the same command is in the container's
``CMD``. uvicorn is the WSGI/ASGI server; we don't put gunicorn
in front because (a) we're a single-process admin tool, not a
multi-worker fan-out, and (b) FastAPI works fine on uvicorn alone.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import SETTINGS, STATIC_DIR
from .routes import (
    admin_customers,
    admin_licenses,
    admin_payments,
    admin_support,
    public_activate,
    public_support,
    ui,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialise DB on startup. Idempotent — safe to call every
    boot. Logs the configured paths so an ops engineer can see
    where data goes the moment a container comes up."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("DB     = %s", SETTINGS.db_path)
    log.info("Upload = %s", SETTINGS.upload_dir)
    log.info("Cookie secure = %s", SETTINGS.cookie_secure)
    db.init_db()
    yield


def create_app() -> FastAPI:
    """Construct the FastAPI app. Factory style so tests can call
    this with a fresh ``TestClient`` per test, without process-wide
    state leakage."""
    app = FastAPI(
        title="NP Create — License & Customer Admin",
        version="0.1.0",
        lifespan=_lifespan,
        # Disable docs in production unless explicitly wanted —
        # the OpenAPI schema reveals every admin endpoint to
        # anyone who can reach the server. For dev we leave them
        # on at /docs so curl-style debugging works.
        docs_url="/docs",
        redoc_url=None,
    )

    # Static files. Tailwind comes from the CDN; this dir holds our
    # own ``app.js`` + favicon + any local assets.
    if STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    # ── HTML pages ──
    app.include_router(ui.router)

    # ── admin JSON APIs (cookie-gated) ──
    app.include_router(admin_customers.router)
    app.include_router(admin_licenses.router)
    app.include_router(admin_payments.router)
    app.include_router(admin_support.router)

    # ── customer public APIs (no auth, signed payloads) ──
    app.include_router(public_activate.router)
    app.include_router(public_support.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        """Liveness probe for Docker / Caddy. Cheapest possible —
        no DB query so a stuck DB doesn't make the container
        appear dead and trigger a restart loop."""
        return {"ok": True}

    @app.get("/api/v1/health", include_in_schema=True)
    def api_health() -> dict:
        """Public health endpoint — exposed to ``vcam-pc`` so it
        can detect server-down before attempting an activation
        call. Reports the public key fingerprint so the customer
        can detect a server-key swap (which would reject all
        existing licenses)."""
        from . import crypto
        try:
            pkh = crypto.public_key_hex()
        except crypto.CryptoError:
            pkh = ""
        return {
            "ok": True,
            "service": "npc-server",
            "public_key_hex": pkh,
        }

    return app


app = create_app()
