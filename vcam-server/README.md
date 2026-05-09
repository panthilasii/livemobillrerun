# NP Create — License & Customer Admin Server

A small FastAPI + SQLite + Tailwind app that the **admin** (you)
runs on a VPS so you don't have to use CLI tools (`gen_license.py`)
every time a customer pays.

Runs alongside `vcam-pc` (the customer desktop app). Customers'
`vcam-pc` installs reach this server for:

- `POST /api/v1/activate` — phone-home when a key is entered.
- `GET  /api/v1/revocations` — periodic poll for revoked keys.
- `POST /api/v1/support/upload` — one-tap "send log to admin".

You (the admin) reach it through a browser:

- `https://your.domain/admin` — login.
- After login: customers, licenses, activations, payments, support.

## Why a separate project?

`vcam-pc` is a **per-machine desktop app** with embedded ffmpeg, ADB
and a TikTok-Shop dashboard. `vcam-server` is a **multi-tenant
public web service** with auth, persistent storage, billing.
Different lifecycle, different deps, different security model.

The two share **only** the Ed25519 crypto so license keys issued
here verify against the public key baked into the customer build.

## Stack

| Layer       | Choice                               |
|-------------|--------------------------------------|
| HTTP        | FastAPI + Uvicorn                    |
| DB          | SQLite (single file, easy backups)   |
| Auth        | bcrypt + signed cookie session       |
| License sig | Ed25519 (re-uses `vcam-pc/src/_ed25519.py`) |
| Frontend    | HTML + Tailwind CDN + Alpine.js CDN  |
| Deploy      | Docker Compose + Caddy (auto HTTPS)  |

No npm. No webpack. No build step. The `static/` files are served
as-is.

## Quick start (local dev)

```bash
cd vcam-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.cli init-db
python -m app.cli create-admin --email you@np.local --password change-me
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000/admin
```

## Deploy to VPS

See `DEPLOY.md` for the Docker Compose + Caddy recipe.
