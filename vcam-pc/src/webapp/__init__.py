"""NP Create -- embedded TikTok Shop sales dashboard.

The webapp lives inside the same Python process as the desktop app
(spawned in a daemon thread by ``server.start_in_thread``). It binds
``localhost:8765`` only -- never the public interface -- so we don't
need authentication on the local install.

Public surface
--------------

* ``server.start_in_thread(port=8765)`` -- launch uvicorn in a daemon
  thread; returns a handle the desktop app can stop on shutdown.
* ``db.connect()`` -- get a sqlite3 connection bound to the local
  data directory.
* ``tiktok_shop.TikTokShopClient`` -- API client (OAuth + signed
  requests).

The package is *optional* for the desktop app: if the customer's
Python is missing ``fastapi`` or ``uvicorn``, the Dashboard button
falls back to a popup explaining how to install. The video-injection
pipeline is unaffected.
"""
from __future__ import annotations

# Re-exports so ``from src.webapp import db, server`` works without
# having to know the file layout.
from . import db, server, tiktok_shop  # noqa: F401
