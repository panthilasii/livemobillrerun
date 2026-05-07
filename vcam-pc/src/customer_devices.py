"""Per-device customer profile storage.

Each phone the customer pairs becomes a ``DeviceEntry`` keyed by its
ADB serial. We persist:

* ``label`` — the customer-chosen nickname ("บัญชี A")
* ``model`` — what ``adb shell getprop ro.product.model`` reported
* ``last_video`` — absolute path to the MP4 most recently encoded for
  this device
* ``rotation`` / ``mirror`` — current FlipRenderer transform
* ``patched_at`` — when the user last ran "Patch & install TikTok"

The whole library lives in a single JSON file at
``~/.npcreate/devices.json`` so it follows the user across PC
re-installs of the program but stays per-machine.

Why a separate module from ``config.py``?
-----------------------------------------

``config.py`` describes *static* per-model rotation profiles
(Redmi 13C → none, Pixel 6 → transpose=1, …) shipped with the
program. This module is *runtime* state for an individual end
customer's phones. They serve different concerns and deserve
separate files; mixing them makes config edits dangerous.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LIBRARY_PATH = Path.home() / ".npcreate" / "devices.json"


@dataclass
class DeviceEntry:
    serial: str
    label: str = ""
    model: str = ""
    last_video: str = ""           # absolute path to local MP4
    last_audio: str = ""           # absolute path to local audio override
    rotation: int = 0              # 0 / 90 / 180 / 270
    mirror_h: bool = False
    mirror_v: bool = False
    patched_at: str = ""           # iso timestamp; empty = never
    added_at: str = ""             # iso timestamp; set on first sight
    # ── WiFi / wireless-ADB state ───────────────────────────────
    # Filled in once after a successful Patch over USB (we run
    # ``adb tcpip`` + read the phone's wlan0 address). After that
    # the Studio can talk to the phone over LAN until it reboots.
    wifi_ip: str = ""
    wifi_port: int = 5555
    last_seen_via: str = ""        # "usb" | "wifi" | ""

    def display_name(self) -> str:
        return self.label or self.model or self.serial

    def is_patched(self) -> bool:
        return bool(self.patched_at)

    def has_audio_override(self) -> bool:
        return bool(self.last_audio)

    def has_wifi(self) -> bool:
        return bool(self.wifi_ip)

    def wifi_address(self) -> str:
        """Formatted ``IP:port`` string, or empty if WiFi never set up."""
        return f"{self.wifi_ip}:{self.wifi_port}" if self.wifi_ip else ""


@dataclass
class DeviceLibrary:
    """A serial → DeviceEntry map persisted as JSON. All public
    methods are thread-safe so the device-watcher thread and the UI
    thread can both call into it."""

    entries: dict[str, DeviceEntry] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── persistence ──────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path = LIBRARY_PATH) -> "DeviceLibrary":
        lib = cls()
        if not path.is_file():
            return lib
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("devices.json is corrupt — starting fresh")
            return lib
        for serial, raw in (data.get("entries") or {}).items():
            try:
                lib.entries[serial] = DeviceEntry(
                    serial=serial,
                    label=str(raw.get("label", "")),
                    model=str(raw.get("model", "")),
                    last_video=str(raw.get("last_video", "")),
                    last_audio=str(raw.get("last_audio", "")),
                    rotation=int(raw.get("rotation", 0)) % 360,
                    mirror_h=bool(raw.get("mirror_h", False)),
                    mirror_v=bool(raw.get("mirror_v", False)),
                    patched_at=str(raw.get("patched_at", "")),
                    added_at=str(raw.get("added_at", "")),
                    wifi_ip=str(raw.get("wifi_ip", "")),
                    wifi_port=int(raw.get("wifi_port", 5555) or 5555),
                    last_seen_via=str(raw.get("last_seen_via", "")),
                )
            except Exception:
                log.exception("skipping malformed device entry %r", serial)
        return lib

    def save(self, path: Path = LIBRARY_PATH) -> None:
        with self._lock:
            payload = {
                "entries": {
                    s: {k: v for k, v in asdict(e).items() if k != "serial"}
                    for s, e in self.entries.items()
                },
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(path)

    # ── lookup / mutation ────────────────────────────────────────

    def list(self) -> list[DeviceEntry]:
        with self._lock:
            return list(self.entries.values())

    def get(self, serial: str) -> DeviceEntry | None:
        with self._lock:
            return self.entries.get(serial)

    def upsert(
        self,
        serial: str,
        *,
        model: str | None = None,
        label: str | None = None,
    ) -> DeviceEntry:
        """Insert or update an entry; returns the live entry. Pass
        ``label`` to rename, ``model`` when ADB has just reported the
        product model."""
        with self._lock:
            e = self.entries.get(serial)
            if e is None:
                e = DeviceEntry(serial=serial)
                e.added_at = datetime.now().isoformat(timespec="seconds")
                self.entries[serial] = e
            if model is not None and model:
                e.model = model
            if label is not None:
                e.label = label
            return e

    def remove(self, serial: str) -> bool:
        with self._lock:
            return self.entries.pop(serial, None) is not None

    def update_video(self, serial: str, video_path: str) -> None:
        with self._lock:
            e = self.entries.get(serial)
            if e is not None:
                e.last_video = video_path

    def update_audio(self, serial: str, audio_path: str) -> None:
        """Record the chosen audio override path. Pass an empty
        string to clear it (UI ‑> 'ใช้เสียงจากคลิป')."""
        with self._lock:
            e = self.entries.get(serial)
            if e is not None:
                e.last_audio = audio_path

    def update_transform(
        self,
        serial: str,
        *,
        rotation: int | None = None,
        mirror_h: bool | None = None,
        mirror_v: bool | None = None,
    ) -> None:
        with self._lock:
            e = self.entries.get(serial)
            if e is None:
                return
            if rotation is not None:
                e.rotation = int(rotation) % 360
            if mirror_h is not None:
                e.mirror_h = bool(mirror_h)
            if mirror_v is not None:
                e.mirror_v = bool(mirror_v)

    def mark_patched(self, serial: str) -> None:
        with self._lock:
            e = self.entries.get(serial)
            if e is not None:
                e.patched_at = datetime.now().isoformat(timespec="seconds")

    def update_wifi(
        self,
        serial: str,
        ip: str,
        port: int = 5555,
    ) -> None:
        """Record the LAN address we obtained for this phone, so we
        can ``adb connect`` it next time the Studio launches."""
        with self._lock:
            e = self.entries.get(serial)
            if e is None:
                return
            e.wifi_ip = ip or ""
            e.wifi_port = int(port or 5555)

    def clear_wifi(self, serial: str) -> None:
        with self._lock:
            e = self.entries.get(serial)
            if e is not None:
                e.wifi_ip = ""
                e.last_seen_via = "usb" if e.last_seen_via == "wifi" else e.last_seen_via

    def mark_seen_via(self, serial: str, transport: str) -> None:
        """``transport`` ∈ {"usb","wifi"}. Pure cosmetic — drives the
        🔌 / 📶 badge in the Dashboard."""
        if transport not in ("usb", "wifi"):
            return
        with self._lock:
            e = self.entries.get(serial)
            if e is not None:
                e.last_seen_via = transport

    def find_by_wifi_id(self, adb_id: str) -> "DeviceEntry | None":
        """Look up the entry whose stored ``IP:port`` matches the
        given WiFi-style adb id. Used by the poller to fold a
        ``192.168.x.x:5555`` row back onto its USB-serial owner."""
        with self._lock:
            for e in self.entries.values():
                if e.wifi_address() and e.wifi_address() == adb_id:
                    return e
            return None

    # ── license-aware helpers ────────────────────────────────────

    def can_add_more(self, max_devices: int) -> bool:
        """True iff adding one more device wouldn't exceed the user's
        subscription cap. We never auto-delete; over-cap simply
        disables the [+ Add device] button in the UI."""
        with self._lock:
            return len(self.entries) < max(1, int(max_devices))

    def count(self) -> int:
        with self._lock:
            return len(self.entries)
