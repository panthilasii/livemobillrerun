"""NP Create -- logging configuration and diagnostic export.

Why this module exists
----------------------

When a non-technical customer hits a bug ("Patched ล้มเหลว เซ็ตอัป
WiFi ไม่ติด"), the support workflow is:

    customer  ──Line OA──>  admin

Without on-disk logs there is nothing for the admin to read. The
customer can describe the symptom but not the stack trace. So we:

1. Configure a **rotating file handler** that writes structured
   logs to ``<DATA_ROOT>/logs/npcreate.log`` for every run (on frozen
   macOS ``DATA_ROOT`` is ``~/Library/Application Support/NP Create``
   so App Translocation read-only mounts do not break startup). Three
   rotated copies of 5 MB each = ~20 MB total ceiling.
2. Provide ``collect_diagnostic_zip()`` that bundles the recent
   logs + a sanitised system snapshot into a single ZIP the
   customer can attach to a Line message.

Privacy / redaction
-------------------

Diagnostics MUST NEVER leak the customer's:

* License key (would let anyone impersonate them).
* Admin private signing key (would let anyone forge updates,
  licenses, announcements).
* Authentication tokens for TikTok Shop (OAuth refresh).

The ``_redact`` helper walks the included config.json + activation
files and removes those fields by name BEFORE they enter the ZIP.
We deliberately keep device serials, file paths, and version
numbers -- they're necessary for diagnosis and aren't sensitive on
their own.

Why not stdlib's ``logging.handlers`` directly
----------------------------------------------

We DO use ``RotatingFileHandler``; we just wrap configuration so
the call site (``main.py``, tests) can stay one-liner-simple.
"""
from __future__ import annotations

import datetime as _dt
import io as _io
import json
import logging
import logging.handlers
import os
import platform
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from .branding import BRAND
from . import config as _cfg


# ── locations ───────────────────────────────────────────────────


LOG_DIR = _cfg.DATA_ROOT / "logs"
LOG_FILE = LOG_DIR / "npcreate.log"

# 5 MB per file × 3 backups = ~20 MB ceiling per install. The
# customer's "C:\Program Files\NP Create" almost always has more
# than that free; if not, the rotating handler silently truncates
# rather than blowing up the app.
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3


# Field names whose values must be stripped before any disk write
# of customer data. Match is case-insensitive and substring-based
# so derivatives (``access_token``, ``refresh_token``) get caught
# by ``token``.
_REDACT_KEY_FRAGMENTS = (
    "private_key",
    "license_key",
    "license",   # covers ``license_key``, ``raw_license``, etc.
    "token",     # covers ``access_token``, ``refresh_token``
    "secret",
    "password",
    "seed",
)


# ── logging configuration ──────────────────────────────────────


def configure_logging(verbose: bool = False) -> Path:
    """Wire stdlib ``logging`` once for the whole app.

    Effects
    -------

    * Adds a rotating ``FileHandler`` writing UTF-8 to
      ``LOG_FILE``, level DEBUG (file gets everything for forensic
      replay -- the bottleneck is stdout, not disk).
    * Adds a console ``StreamHandler`` at DEBUG (if ``verbose``)
      or INFO (default) so terminal usage stays useful.
    * Sets the root logger to DEBUG so neither handler upstreams
      filters away anything before the per-handler level kicks in.

    Idempotent: re-calling it does NOT pile on duplicate handlers
    (unit tests + ``--studio`` paths both call once on launch and
    we don't want each call to multiply the output).

    Returns the path of the active log file so the UI can show it
    to the customer.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Tag our handlers with a sentinel attribute so we can find +
    # replace them on a re-configure call without touching any
    # third-party handlers a library might have registered.
    for h in list(root.handlers):
        if getattr(h, "_npcreate_managed", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    file_h = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    file_h._npcreate_managed = True  # type: ignore[attr-defined]
    root.addHandler(file_h)

    console_h = logging.StreamHandler(stream=sys.stderr)
    console_h.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_h.setFormatter(console_fmt)
    console_h._npcreate_managed = True  # type: ignore[attr-defined]
    root.addHandler(console_h)

    # Mark the boundary in the file so a customer's ZIP shows
    # exactly when this run started -- helps the admin skip past
    # noise from previous launches.
    root.info(
        "── %s v%s started (%s %s) ──",
        BRAND.name, BRAND.version,
        platform.system(), platform.release(),
    )
    return LOG_FILE


def open_log_dir_in_explorer() -> bool:
    """Open the OS file explorer at ``LOG_DIR`` so the customer can
    inspect or hand off log files manually. Returns ``True`` on
    success, ``False`` if the platform-specific call failed (we
    don't want to crash the UI for a help-button miss)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "darwin":
            os.system(f'open "{LOG_DIR}"')   # noqa: S605
        elif sys.platform == "win32":
            os.startfile(str(LOG_DIR))   # type: ignore[attr-defined]
        else:
            os.system(f'xdg-open "{LOG_DIR}"')   # noqa: S605
        return True
    except Exception:
        logging.getLogger(__name__).exception("could not open log dir")
        return False


# ── diagnostic export ──────────────────────────────────────────


def _redact_value(key: str, value):
    """Substitute placeholder for any value whose key looks
    sensitive. Walks containers recursively."""
    klow = key.lower()
    if any(frag in klow for frag in _REDACT_KEY_FRAGMENTS):
        if isinstance(value, str) and value:
            return f"<redacted:{len(value)}-chars>"
        return "<redacted>"
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v) for v in value]
    return value


def _safe_read_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _system_info() -> dict:
    """Snapshot of the host environment. Everything here is safe
    to ship to support: nothing user-identifying beyond the install
    location (which is already exfilltated through the bug report
    text the customer types anyway)."""
    info = {
        "app_name": BRAND.name,
        "app_version": BRAND.version,
        "platform": platform.platform(),
        "python": sys.version,
        "executable": sys.executable,
        "project_root": str(_cfg.PROJECT_ROOT),
        "data_root": str(_cfg.DATA_ROOT),
        "log_file": str(LOG_FILE),
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }

    # Try to capture adb version (well-known support question:
    # "what version of adb does the customer have?"). Failure
    # silently omits the field; we don't want a missing adb to
    # break the whole export.
    try:
        from . import platform_tools
        adb_path = platform_tools.find_adb()
        if adb_path:
            import subprocess
            r = subprocess.run(
                [str(adb_path), "version"],
                capture_output=True, text=True, timeout=4,
            )
            info["adb_path"] = str(adb_path)
            info["adb_version"] = (
                r.stdout.splitlines()[0] if r.stdout else "(no output)"
            )
    except Exception:
        info["adb_version"] = "(probe failed)"

    return info


def _collect_log_files() -> Iterable[Path]:
    """All on-disk log files in chronological-ish order
    (newest first, then rotated backups)."""
    if not LOG_DIR.is_dir():
        return []
    out: list[Path] = []
    if LOG_FILE.is_file():
        out.append(LOG_FILE)
    # RotatingFileHandler writes ``.log.1``, ``.log.2`` etc. We
    # also tolerate any other ``*.log`` files a future feature may
    # write into the same directory.
    for p in sorted(LOG_DIR.glob("*.log*")):
        if p == LOG_FILE:
            continue
        out.append(p)
    return out


def collect_diagnostic_zip(
    out_path: Path,
    *,
    include_devices: bool = True,
    include_config: bool = True,
) -> Path:
    """Bundle redacted logs + system info into ``out_path`` (a ZIP
    file). Creates parent directory if needed.

    Parameters
    ----------
    out_path
        Where to write the ZIP. Caller picks via Save File dialog.
    include_devices
        Add ``customer_devices.json`` (device serials + WiFi IPs +
        nicknames). Almost always wanted -- "ไม่เจอเครื่อง" cases
        depend on it.
    include_config
        Add ``config.json`` (encode resolution, ADB path, etc.).
        Defaults to ``True`` because the bulk of "how does the
        customer have it set up?" questions answer themselves
        from there.

    Returns the path written.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger(__name__)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 1) System info -- always at the top so the admin sees it
        #    before scrolling through log lines.
        info = _system_info()
        zf.writestr(
            "system_info.json",
            json.dumps(info, indent=2, ensure_ascii=False),
        )

        # 2) Logs (raw, but truncated to the most recent
        #    ~5 MB / file -- which the rotating handler already
        #    enforces). We do not redact log lines because doing it
        #    properly is an arms race and we don't write secrets to
        #    the log file in the first place; the dangerous stuff
        #    lives in config + activation, handled below.
        for p in _collect_log_files():
            try:
                zf.write(p, arcname=f"logs/{p.name}")
            except OSError:
                log.exception("could not zip log %s", p)

        # 3) Redacted config.json
        if include_config:
            cfg = _safe_read_json(_cfg.CONFIG_PATH)
            if cfg is not None:
                redacted = _redact_value("config", cfg)
                zf.writestr(
                    "config.redacted.json",
                    json.dumps(redacted, indent=2, ensure_ascii=False),
                )

        # 4) Redacted activation (license activation file lives in
        #    PROJECT_ROOT/activation.json on most installs -- we
        #    redact license_key but keep version + machine_id so
        #    the admin can debug "stuck activation" issues).
        for fname in ("activation.json",):
            data = _safe_read_json(_cfg.DATA_ROOT / fname)
            if data is not None:
                zf.writestr(
                    f"{fname.replace('.json', '.redacted.json')}",
                    json.dumps(_redact_value(fname, data), indent=2,
                               ensure_ascii=False),
                )

        # 5) Devices (NOT redacted -- serials are fine to share for
        #    support, and "ไม่เจอเครื่อง" investigations need them).
        if include_devices:
            devs = _safe_read_json(_cfg.DATA_ROOT / "customer_devices.json")
            if devs is not None:
                zf.writestr(
                    "customer_devices.json",
                    json.dumps(devs, indent=2, ensure_ascii=False),
                )

        # 6) README so the admin knows what they're looking at.
        zf.writestr(
            "README.txt",
            (
                f"{BRAND.name} v{BRAND.version} diagnostic bundle\n"
                f"Exported: {info['exported_at']}\n"
                f"Platform: {info['platform']}\n"
                f"\n"
                f"Files inside:\n"
                f"  system_info.json    -- host details\n"
                f"  logs/npcreate.log*  -- rolling app log\n"
                f"  config.redacted.json    -- user settings (license redacted)\n"
                f"  activation.redacted.json -- license activation (redacted)\n"
                f"  customer_devices.json    -- known phones (serials, WiFi)\n"
                f"\n"
                f"Sensitive values (license keys, OAuth tokens, signing seeds)\n"
                f"are removed from the JSON files BEFORE they enter this ZIP.\n"
            ),
        )

    return out_path


def suggest_diagnostic_filename() -> str:
    """Default filename for the Save File dialog. Includes app
    version + timestamp so the admin can sort multiple bundles."""
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M")
    return f"npcreate-diag-v{BRAND.version}-{ts}.zip"


__all__ = [
    "LOG_DIR",
    "LOG_FILE",
    "configure_logging",
    "open_log_dir_in_explorer",
    "collect_diagnostic_zip",
    "suggest_diagnostic_filename",
]
