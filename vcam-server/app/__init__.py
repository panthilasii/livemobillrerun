"""NP Create license & customer admin server.

Top-level package. The runnable ASGI app is ``app.main:app`` —
``uvicorn app.main:app`` is the one-liner used by both local dev
and the Docker container.

Submodule overview
------------------

* ``config`` — env-driven settings (DB path, cookie secret, etc.).
* ``db``     — SQLite schema + session helpers.
* ``crypto`` — Ed25519 signing for license keys + revocation lists.
* ``auth``   — bcrypt admin passwords + signed cookie sessions.
* ``models`` — typed dataclasses passed between routes and DB layer.
* ``routes`` — split per resource (customers, licenses, etc.).
* ``main``   — wires everything into a FastAPI instance.
"""
from __future__ import annotations
