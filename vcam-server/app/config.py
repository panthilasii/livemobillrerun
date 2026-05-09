"""Settings for the license/admin server.

All knobs live here so a Docker deploy can override any of them via
environment variables without touching code. We deliberately use
plain ``os.environ`` rather than pydantic-settings to keep the
dependency tree minimal — three settings don't justify another
package.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


# Project root = the directory CONTAINING this ``app`` package, i.e.
# ``vcam-server/``. Used to resolve default storage locations and to
# locate the bundled ``static/`` and ``templates/`` directories.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def _env_path(var: str, default: Path) -> Path:
    """Read a path from env, defaulting to ``default``. Always
    expanded to an absolute path so that relative-cwd surprises in
    Docker don't bite us at runtime."""
    raw = os.environ.get(var)
    return Path(raw).expanduser().resolve() if raw else default


def _env_str(var: str, default: str) -> str:
    return os.environ.get(var, default)


def _env_int(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _persistent_random_secret() -> str:
    """Return a cookie-signing secret that survives across restarts.

    Order of preference:
    1. ``SESSION_SECRET`` env var (production).
    2. ``data/.session_secret`` on disk (dev — generated once,
       reused on subsequent boots so existing sessions don't break
       on restart).

    NEVER use a per-process random in production: every restart
    would invalidate every signed cookie, kicking everyone out.
    """
    env = os.environ.get("SESSION_SECRET", "").strip()
    if env:
        return env
    keyfile = DATA_DIR / ".session_secret"
    if keyfile.is_file():
        try:
            return keyfile.read_text(encoding="utf-8").strip() or secrets.token_hex(32)
        except OSError:
            pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = secrets.token_hex(32)
    try:
        keyfile.write_text(new, encoding="utf-8")
    except OSError:
        # Read-only FS (some hosting). Fall back to in-memory secret;
        # restart will boot users out, but that's better than failing.
        pass
    return new


# ── canonical paths ─────────────────────────────────────────────────


# Default data directory. Override with ``DATA_DIR=...`` env in
# Docker so you can mount a volume at e.g. ``/var/lib/npcreate``.
DATA_DIR: Path = _env_path("DATA_DIR", PROJECT_ROOT / "data")
DB_PATH: Path = _env_path("DB_PATH", DATA_DIR / "npcreate.sqlite3")
UPLOAD_DIR: Path = _env_path("UPLOAD_DIR", DATA_DIR / "uploads")

# Where the Ed25519 *signing* seed (private key) lives. We allow
# this to live OUTSIDE the data dir so it can be locked down with
# stricter perms (``chmod 600``) than the rest. Defaults to the
# data dir for ease of bootstrap.
SIGNING_KEY_PATH: Path = _env_path(
    "SIGNING_KEY_PATH", DATA_DIR / ".private_key"
)
PUBLIC_KEY_PATH: Path = _env_path(
    "PUBLIC_KEY_PATH", DATA_DIR / "public_key.hex"
)

STATIC_DIR: Path = PROJECT_ROOT / "app" / "static"
TEMPLATES_DIR: Path = PROJECT_ROOT / "app" / "templates"


# ── runtime knobs ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:
    """Snapshot of all server settings at startup.

    Frozen so accidental mutation in handlers (e.g. an admin trying
    to "change DB at runtime") is a clear AttributeError.
    """

    db_path: Path
    upload_dir: Path
    signing_key_path: Path
    public_key_path: Path
    session_secret: str
    cookie_name: str
    cookie_max_age: int
    cookie_secure: bool
    bind_host: str
    bind_port: int
    license_prefix: str
    license_default_devices: int
    license_default_days: int
    revocation_cache_seconds: int
    upload_max_bytes: int


def load_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    return Settings(
        db_path=DB_PATH,
        upload_dir=UPLOAD_DIR,
        signing_key_path=SIGNING_KEY_PATH,
        public_key_path=PUBLIC_KEY_PATH,
        session_secret=_persistent_random_secret(),
        cookie_name=_env_str("COOKIE_NAME", "npc_admin"),
        cookie_max_age=_env_int("COOKIE_MAX_AGE", 60 * 60 * 24 * 7),  # 7 days
        # In dev (HTTP) we MUST allow cookies over plain http; in
        # production behind Caddy/Nginx we set ``COOKIE_SECURE=1``
        # so the browser refuses to send them over HTTP.
        cookie_secure=_env_int("COOKIE_SECURE", 0) == 1,
        bind_host=_env_str("BIND_HOST", "127.0.0.1"),
        bind_port=_env_int("BIND_PORT", 8000),
        license_prefix=_env_str("LICENSE_PREFIX", "888"),
        license_default_devices=_env_int("LICENSE_DEFAULT_DEVICES", 3),
        license_default_days=_env_int("LICENSE_DEFAULT_DAYS", 30),
        # Customer apps poll the revocation list periodically; cache
        # the signed JSON so we don't re-sign on every request. Keep
        # short enough that a "revoke now" admin action propagates
        # within minutes, not hours.
        revocation_cache_seconds=_env_int("REVOCATION_CACHE_SECONDS", 60),
        # Per-upload cap for support log zips. 50 MB matches the
        # rotating log size in vcam-pc/src/log_setup.py + headroom
        # for system_info.json + redacted configs.
        upload_max_bytes=_env_int(
            "UPLOAD_MAX_BYTES", 50 * 1024 * 1024,
        ),
    )


# Convenience for routes/tests that want a single global instance.
SETTINGS: Settings = load_settings()
