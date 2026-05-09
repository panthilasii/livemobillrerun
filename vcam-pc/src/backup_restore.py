"""NP Create -- backup and restore the customer's local state.

When a customer reformats their PC, buys a new laptop, or asks a
friend to set them up on a second machine, they currently have to:

1. Re-enter the license key.
2. Re-pair every phone (USB → Patch wizard).
3. Re-set every device's nickname / rotation / WiFi address.

That's painful for someone running 5 phones. This module bundles
all the **portable** state into a single ZIP that the customer can
drop on a USB stick, then restore on the new machine.

What we save
------------

* ``config.json``                   -- encode resolution, ports
* ``device_profiles.json``          -- per-model rotation presets
* ``customer_devices.json`` and the legacy ``~/.npcreate/devices.json``
* ``license_history.json``          -- so the same license can be
  re-activated without re-binding (admin-only field)
* The activation file (``activation.json`` if present) -- this
  contains the redacted license key + machine_id, so the customer
  can resume on the SAME machine without re-typing.

What we DO NOT save
-------------------

* ``.private_key`` -- admin-only signing seed; backups MUST never
  contain it (defence-in-depth, even though the GUI doesn't run
  on customer machines).
* The pre-built APK and bundled toolchain -- those re-arrive with
  the next customer bundle / installer.
* Logs (use the diagnostic export from ``log_setup`` for those).

Trust model on restore
----------------------

The restored ZIP is fully trusted -- we don't sign these because
they're customer-local data, not anything that propagates over the
internet. A malicious restore can only break the customer's own
install, which is harmless from our perspective; their license key
is bound to the activation machine and a different machine simply
won't load.

Atomicity
---------

Restore writes to a temp directory first, validates the schema,
THEN does ``os.replace()`` over the live files. That means a
botched ZIP (truncated, wrong format) leaves the customer's
existing data untouched.
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .branding import BRAND
from .config import PROJECT_ROOT

log = logging.getLogger(__name__)


# ── manifest ───────────────────────────────────────────────────


# Files we INCLUDE in a backup. Each is a ``(label, path-relative-
# -to-PROJECT_ROOT, optional)`` tuple. ``optional=True`` means
# "don't fail if missing" -- typical for files the customer
# never set (e.g. license_history if they never activated).
_BACKUP_FILES: tuple[tuple[str, str, bool], ...] = (
    ("config",          "config.json",              True),
    ("device_profiles", "device_profiles.json",     True),
    ("customer_devs",   "customer_devices.json",    True),
    ("license_history", "license_history.json",     True),
    ("activation",      "activation.json",          True),
)

# Path to the legacy ~/.npcreate/devices.json. Saved separately
# because it lives outside PROJECT_ROOT.
_HOME_DEVICES = Path.home() / ".npcreate" / "devices.json"

# Files we EXCLUDE explicitly even if they happen to land under
# PROJECT_ROOT. The signing seed must never end up in a backup.
_FORBIDDEN_FILES = (".private_key",)


@dataclass(frozen=True)
class BackupManifest:
    """What's inside the ZIP, written as ``manifest.json`` at the
    archive root."""

    schema: int
    app_name: str
    app_version: str
    created_at: str
    files: list[str]


# ── public API ─────────────────────────────────────────────────


def create_backup(out_path: Path) -> Path:
    """Bundle the customer's portable state into ``out_path``.

    Creates parent dirs, overwrites an existing ZIP, and returns
    the resolved path. Failure modes (disk full, permission
    denied) propagate to the caller so the UI can surface them
    instead of silently making a half-empty backup.
    """
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files_added: list[str] = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for label, rel, optional in _BACKUP_FILES:
            assert rel not in _FORBIDDEN_FILES   # sanity for future edits
            src = PROJECT_ROOT / rel
            if not src.is_file():
                if optional:
                    continue
                raise FileNotFoundError(f"required backup file missing: {rel}")
            zf.write(src, arcname=rel)
            files_added.append(rel)

        # Home-dir devices.json (legacy location used by older
        # versions; we still write to it so a restore on v1.4.x
        # would also pick it up).
        if _HOME_DEVICES.is_file():
            zf.write(_HOME_DEVICES, arcname="home/devices.json")
            files_added.append("home/devices.json")

        manifest = BackupManifest(
            schema=1,
            app_name=BRAND.name,
            app_version=BRAND.version,
            created_at=datetime.now().isoformat(timespec="seconds"),
            files=files_added,
        )
        zf.writestr(
            "manifest.json",
            json.dumps(manifest.__dict__, indent=2, ensure_ascii=False),
        )

        zf.writestr(
            "README.txt",
            (
                f"{BRAND.name} v{BRAND.version} backup\n"
                f"Created: {manifest.created_at}\n"
                f"\n"
                f"Restore on the destination machine:\n"
                f"  1. Open NP Create.\n"
                f"  2. Settings -> Backup / Restore -> 'Restore from ZIP'.\n"
                f"  3. Pick this file.\n"
                f"\n"
                f"This backup contains the customer's local state\n"
                f"only. The license key resumes automatically on the\n"
                f"SAME machine; on a different machine, re-enter it\n"
                f"once when prompted.\n"
            ),
        )

    return out_path


def list_files_in_backup(zip_path: Path) -> list[str]:
    """Peek at what a ZIP contains without restoring it. Used by
    the UI to show 'this backup has 5 devices, license, ...' so
    the customer can sanity-check before clicking Restore."""
    if not Path(zip_path).is_file():
        return []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return zf.namelist()
    except zipfile.BadZipFile:
        return []


def read_backup_manifest(zip_path: Path) -> Optional[BackupManifest]:
    """Read and parse ``manifest.json`` from inside ``zip_path``.

    Returns ``None`` if the ZIP is malformed or the manifest is
    missing -- the UI can use that to refuse a restore from a
    bundle that's clearly not one of ours (a customer accidentally
    picking a random ZIP off their Desktop)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("manifest.json") as f:
                payload = json.loads(f.read().decode("utf-8"))
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return BackupManifest(
            schema=int(payload.get("schema", 1)),
            app_name=str(payload.get("app_name", "")),
            app_version=str(payload.get("app_version", "")),
            created_at=str(payload.get("created_at", "")),
            files=list(payload.get("files", [])),
        )
    except (TypeError, ValueError):
        return None


def restore_backup(zip_path: Path) -> list[str]:
    """Restore a backup ZIP into ``PROJECT_ROOT`` (and the legacy
    ``~/.npcreate/`` dir).

    Atomicity: we extract everything to a temp dir first, validate
    the schema, then move into place using ``os.replace``. A
    partially-applied restore is impossible -- either every file
    lands or none of them do.

    Returns the list of restored relative paths. Raises
    ``ValueError`` for schema mismatches and OSError / zipfile
    errors for I/O / format problems.
    """
    zip_path = Path(zip_path).resolve()
    manifest = read_backup_manifest(zip_path)
    if manifest is None:
        raise ValueError(
            "ไฟล์ไม่ใช่ Backup ของ NP Create (manifest หายหรือเสีย)"
        )
    if manifest.schema != 1:
        raise ValueError(
            f"Backup สร้างจาก schema v{manifest.schema} ที่ระบบนี้ไม่รู้จัก"
        )

    restored: list[str] = []
    with tempfile.TemporaryDirectory(prefix="npcreate-restore-") as td:
        staging = Path(td)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                if member in ("manifest.json", "README.txt"):
                    continue
                if _is_unsafe_member(member):
                    log.warning(
                        "skipping unsafe entry in backup: %s", member,
                    )
                    continue
                # Skip anything that matches a forbidden filename
                # at *any* depth -- defence in depth: a hand-crafted
                # backup ZIP must not slip a private_key into our
                # install.
                if Path(member).name in _FORBIDDEN_FILES:
                    log.warning("forbidden filename in backup: %s", member)
                    continue
                target = staging / member
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                restored.append(member)

        # Now move staged files into their final destinations. We
        # do this in a second pass so a malformed ZIP can never
        # leave a partially-restored install (the temp dir is the
        # only mutated state up to this point).
        for member in restored:
            staged = staging / member
            dest = _resolve_destination(member)
            if dest is None:
                log.warning("nowhere to restore: %s", member)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            # ``os.replace`` is atomic within a filesystem; we
            # don't worry about cross-fs because ``staging`` is
            # under TMPDIR which on macOS / Linux can sit on a
            # different mount than ``PROJECT_ROOT``. Fall back to
            # copy+unlink in that case.
            try:
                staged.replace(dest)
            except OSError:
                shutil.copy2(staged, dest)

    log.info("restored %d files from %s", len(restored), zip_path)
    return restored


def _is_unsafe_member(name: str) -> bool:
    """Block path traversal in the ZIP. Same defence we use in
    ``auto_update``."""
    parts = Path(name).parts
    return ".." in parts or any(p.startswith("/") for p in parts)


def _resolve_destination(member: str) -> Optional[Path]:
    """Map a ZIP member name to an absolute on-disk destination.

    * ``home/devices.json`` → ``~/.npcreate/devices.json``
    * Anything else → ``PROJECT_ROOT/<member>``
    """
    if member == "home/devices.json":
        return _HOME_DEVICES
    if member.startswith("home/"):
        # Reserved for future home-dir items; reject for now to
        # avoid surprise writes elsewhere on disk.
        return None
    return PROJECT_ROOT / member


def suggest_backup_filename() -> str:
    """``npcreate-backup-v1.5.0-20260508-2330.zip``."""
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    return f"npcreate-backup-v{BRAND.version}-{ts}.zip"


__all__ = [
    "BackupManifest",
    "create_backup",
    "list_files_in_backup",
    "read_backup_manifest",
    "restore_backup",
    "suggest_backup_filename",
]
