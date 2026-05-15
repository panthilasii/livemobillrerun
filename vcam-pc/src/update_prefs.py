"""Persistent preferences for the auto-update system (v1.8.13).

What lives here
---------------

A tiny JSON file at ``<project>/cache/update_prefs.json`` that
remembers per-machine choices the customer made in Settings →
"อัปเดต" (Updates) card:

* ``install_on_close`` — apply a pre-downloaded patch when the
  customer closes the app, so the next launch is on the new
  version without an explicit "อัปเดตเลย" click.
* ``auto_prefetch`` — start downloading the patch in the
  background the moment the banner appears, so by the time the
  customer clicks "อัปเดตเลย" the bytes are already on disk and
  the install is instant.
* ``last_check_ts`` — wall-clock seconds when we last successfully
  pinged the manifest URL. Surfaced in the Settings card as
  "ตรวจล่าสุดเมื่อ ..." so the customer can tell whether the 6 h
  poller has fired today or whether their machine has been
  offline since lunch.

Why a separate module (not StreamConfig)
----------------------------------------

``StreamConfig`` is heavy (encoder params, bitrates, device paths,
hook flags) and reading it forces a full schema parse. The
auto-updater can fire on every app launch, sometimes from contexts
where the full streaming config isn't relevant yet (e.g. the
Activation page before a license has been entered). A standalone
prefs file keeps the dependency direction simple: ``auto_update``
imports ``update_prefs`` but never the other way around, so a
broken streaming config can't break the updater.

Failure semantics
-----------------

Every public function returns a *valid* ``UpdatePrefs`` instance
even when the underlying file is missing, malformed, or
unreadable. That matches the "auto-updater is best-effort"
principle from ``auto_update``: we never want a corrupt prefs
file to stop the app from launching, so we silently fall back to
defaults and log a warning. Saving is similarly forgiving — a
disk-full / readonly-FS condition logs and swallows rather than
raising into the UI.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_PREFS_FILENAME = "update_prefs.json"


def _default_prefs_path(project_root: Optional[Path] = None) -> Path:
    """Resolve the on-disk path the prefs live at.

    ``project_root`` override exists so unit tests can isolate the
    file (otherwise the tests would clobber the developer's real
    prefs every run).
    """
    if project_root is None:
        from .config import PROJECT_ROOT
        project_root = PROJECT_ROOT
    return Path(project_root) / "cache" / _PREFS_FILENAME


@dataclass
class UpdatePrefs:
    """Mutable struct mirroring the JSON schema on disk.

    Every field has a default so a stripped-down or first-launch
    prefs file (``{}``) still produces a valid instance. New fields
    in future versions can be added with sensible defaults and old
    on-disk files will quietly upgrade.
    """

    install_on_close: bool = False
    auto_prefetch: bool = True
    last_check_ts: float = 0

    @classmethod
    def load(cls, *, project_root: Optional[Path] = None) -> "UpdatePrefs":
        """Read prefs from disk; return defaults on any error.

        We deliberately do NOT raise: a corrupted JSON file should
        never block launch. Missing keys produce field defaults so
        an old on-disk file gracefully gains new fields.
        """
        path = _default_prefs_path(project_root)
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                log.warning(
                    "update_prefs: %s not a JSON object — using defaults",
                    path,
                )
                return cls()
            # Filter to known fields so unknown keys in older files
            # don't crash the dataclass constructor.
            known = {f for f in cls.__dataclass_fields__}
            kwargs = {k: v for k, v in raw.items() if k in known}
            return cls(**kwargs)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "update_prefs: could not read %s (%s) — using defaults",
                path, exc,
            )
            return cls()
        except TypeError as exc:
            log.warning(
                "update_prefs: schema mismatch (%s) — using defaults",
                exc,
            )
            return cls()

    def save(self, *, project_root: Optional[Path] = None) -> bool:
        """Write prefs to disk. Returns True on success, False on
        any I/O failure (logged but never raised — Settings UI
        should keep working even if the disk is read-only).
        """
        path = _default_prefs_path(project_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(asdict(self), indent=2),
                encoding="utf-8",
            )
            return True
        except OSError as exc:
            log.warning("update_prefs: save failed: %s", exc)
            return False

    def mark_checked(self, *, project_root: Optional[Path] = None) -> None:
        """Stamp ``last_check_ts`` to *now* and persist.

        Used by ``poll_now`` and the background poller so the
        Settings card's "ตรวจล่าสุด..." line reflects every
        successful manifest fetch (whether it found a new version
        or not).
        """
        self.last_check_ts = time.time()
        self.save(project_root=project_root)


__all__ = ["UpdatePrefs", "_default_prefs_path"]
