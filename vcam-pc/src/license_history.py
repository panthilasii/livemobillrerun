"""Local audit log of issued license keys (admin only).

Lives at ``vcam-pc/license_history.json`` next to ``.private_key``
so a single backup of the admin folder captures both the signing
seed *and* the trail of who got what. Append-only on purpose; we
never delete an entry, only annotate (e.g. mark a key as
'revoked' if a customer asks for a replacement).

Schema
~~~~~~

::

    {
      "entries": [
        {
          "issued_at": "2026-05-07T23:30:12",
          "customer": "คุณสมชาย",
          "max_devices": 3,
          "expiry": "2026-06-06",
          "key": "888-AAAA-…",
          "note": "Line slip #44211"
        }
      ]
    }

The file is **never** included in customer ZIPs; ``build_release.py``
puts it on the deny-list along with ``.private_key``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT


HISTORY_PATH = PROJECT_ROOT / "license_history.json"


@dataclass
class IssuedLicense:
    issued_at: str
    customer: str
    max_devices: int
    expiry: str        # ISO yyyy-mm-dd
    key: str
    note: str = ""
    revoked: bool = False


@dataclass
class LicenseHistory:
    entries: list[IssuedLicense] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock,
                                    repr=False, compare=False)

    @classmethod
    def load(cls, path: Path = HISTORY_PATH) -> "LicenseHistory":
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        ents = [
            IssuedLicense(**{
                k: v for k, v in raw.items()
                if k in IssuedLicense.__dataclass_fields__
            })
            for raw in data.get("entries", [])
        ]
        return cls(entries=ents)

    def save(self, path: Path = HISTORY_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = {
                "entries": [
                    {k: v for k, v in asdict(e).items() if not k.startswith("_")}
                    for e in self.entries
                ],
            }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def append(
        self,
        *,
        customer: str,
        max_devices: int,
        expiry: str,
        key: str,
        note: str = "",
    ) -> IssuedLicense:
        ent = IssuedLicense(
            issued_at=datetime.now().isoformat(timespec="seconds"),
            customer=customer,
            max_devices=max_devices,
            expiry=expiry,
            key=key,
            note=note,
        )
        with self._lock:
            self.entries.append(ent)
        return ent

    def count(self) -> int:
        with self._lock:
            return len(self.entries)

    def recent(self, n: int = 20) -> list[IssuedLicense]:
        """Return the ``n`` most recently issued keys, newest first."""
        with self._lock:
            return list(reversed(self.entries[-n:]))

    def mark_revoked(self, key: str) -> bool:
        with self._lock:
            for e in self.entries:
                if e.key == key:
                    e.revoked = True
                    return True
        return False
