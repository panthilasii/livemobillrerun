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
    # ── TikTok variant detection ────────────────────────────────
    # Different customers run different TikTok builds (regular,
    # Lite, Business, Douyin). We probe ``pm list packages`` per
    # device and remember which package the customer actually has
    # installed. The hook pipeline targets THIS package when it
    # pushes ``vcam_final.mp4``; without per-device detection we'd
    # be writing to ``com.ss.android.ugc.trill/files/`` on a phone
    # that only has TikTok Lite (``com.zhiliaoapp.musically.go``)
    # installed, and the Live preview would stay black.
    # Empty = "use the default" (legacy entries that pre-date this
    # field; resolved at hook time by ``hook_status.probe`` if the
    # device is online).
    tiktok_package: str = ""
    # ── TikTok version drift watch ──────────────────────────────
    # ``patched_tiktok_version`` is the ``versionName`` we recorded
    # on the phone *immediately after* a successful Patch (e.g.
    # "39.5.4"). The background hook-status probe re-reads
    # versionName on every visit; if it diverges, we know TikTok
    # auto-updated itself out from under us and the customer's
    # vcam stops working until they re-patch.
    #
    # Why this matters: TikTok's in-app "Update available" prompt
    # is one tap away from replacing our LSPatched APK with a
    # vanilla one (it goes through Play Store / TikTok CDN, neither
    # of which know about LSPatch). The customer typically taps
    # "OK" without reading and only finds out their live broadcast
    # is showing the front camera again.
    #
    # We surface drift in the sidebar (⚠️ badge) and offer a
    # one-click "Re-patch" so they can recover without an admin
    # call. Empty string for entries that haven't been patched yet.
    patched_tiktok_version: str = ""
    # APK signature fingerprint (lowercase hex) recorded the moment
    # we successfully installed the patched APK. Used by the hook
    # status probe as the *primary* "is this patched?" check —
    # exact match against a known-good baseline is the only signal
    # that survives Android-version / OEM-ROM differences in
    # ``dumpsys package`` output without false negatives.
    #
    # Empty string for legacy entries; the probe falls back to a
    # list of known LSPatch keystore prefixes in that case.
    patched_signature: str = ""
    # Last time we showed the customer the "TikTok updated, please
    # re-patch" warning dialog. Used to rate-limit so we don't nag
    # every 2-second probe — once per session is plenty annoying.
    tiktok_drift_warned_at: str = ""
    # ── Live session tracking ───────────────────────────────────
    # ISO 8601 timestamp of the moment the customer clicked
    # "เริ่มไลฟ์" on this device. Empty = not currently live.
    # Persisted to disk so closing the desktop app mid-broadcast
    # doesn't lose the timer; on next launch we restore the
    # value and resume counting from the original start instant.
    # We deliberately store start-time only (not duration) -- a
    # crash + restart still surfaces the correct elapsed minutes
    # because the wall clock is the source of truth.
    live_started_at: str = ""
    # Total minutes broadcast lifetime-to-date for this phone --
    # cosmetic ("เครื่องนี้ไลฟ์ไปทั้งหมด 12:34 ชม.") + lets the
    # customer compare phones without a separate analytics DB.
    # Updated at stop_live() time.
    total_live_seconds: int = 0
    # ── Transport (v1.8.0) ──────────────────────────────────────
    # How NP Create talks to this phone. "usb" = the classic Mode A
    # ADB+LSPatched-TikTok flow (default for legacy entries that
    # pre-date this field). "rtmp" = Mode B; the phone runs a
    # virtual-cam app (see vcam_app_key) and pulls our PC's RTMP
    # stream over WiFi — no ADB at all.
    #
    # Code that branches on transport should default to "usb" for
    # the empty string so old config files keep working without a
    # migration step.
    transport: str = "usb"
    # When transport == "rtmp" — which Play Store app the customer
    # picked in the v1.8.0 RTMP wizard ("camerafi" / "larix" /
    # "du_recorder"). Empty for "usb" entries. The dashboard reads
    # this so the per-device card can show app-specific
    # troubleshooting tips ("กด Start Virtual Camera ใน CameraFi
    # ก่อนเปิด TikTok") instead of generic ADB advice.
    vcam_app_key: str = ""
    # ISO timestamp the customer first added this entry. Distinct
    # from ``added_at`` only when migrating very old configs that
    # only had ``patched_at`` filled — kept here so dashboards
    # that sort by "newest" don't get confused by NULL values.
    created_at: str = ""
    # ── Live clip visibility (v1.8.15) ──────────────────────────
    # Mirrors the broadcast we last fired to the hook for THIS
    # device: True = ``SET_MODE`` mode 2 (replace camera with clip),
    # False = ``SET_MODE`` mode 0 (passthrough, show the real
    # camera). The PC client toggles this independently of the
    # push pipeline so the customer can hide the clip mid-live
    # without re-encoding or re-pushing the MP4.
    #
    # Persisted so reopening the desktop app restores the right
    # button label ("⏸ หยุดแสดงคลิป" vs "▶ แสดงคลิป") even if the
    # phone has rebooted since (the hook will be re-armed at the
    # state we last asked for, not the state the phone happens to
    # be in right now).
    clip_showing: bool = True

    def display_name(self) -> str:
        return self.label or self.model or self.serial

    def is_patched(self) -> bool:
        return bool(self.patched_at)

    def is_live(self) -> bool:
        """``True`` if a Live session is currently in progress on
        this phone according to our local state. The actual
        TikTok broadcast might have ended without us noticing
        (network hiccup, force-quit), so this is best-effort --
        UI code should rate-limit any actions that depend on it."""
        return bool(self.live_started_at)

    def live_elapsed_seconds(self, now: "datetime | None" = None) -> int:
        """Seconds since this phone went live, or 0 when not
        currently broadcasting. Returns 0 (not negative) on a
        malformed timestamp -- never crash a UI tick."""
        if not self.live_started_at:
            return 0
        try:
            t0 = datetime.fromisoformat(self.live_started_at)
        except (ValueError, TypeError):
            return 0
        ref = now or datetime.now()
        if t0.tzinfo is None and ref.tzinfo is not None:
            # Naive started_at compared against aware now -- normalize
            # by stripping the tz of the reference. Both are local
            # wall-clock-ish in our app.
            ref = ref.replace(tzinfo=None)
        elif t0.tzinfo is not None and ref.tzinfo is None:
            ref = ref.astimezone(t0.tzinfo)
        delta = (ref - t0).total_seconds()
        return max(0, int(delta))

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
                    tiktok_package=str(raw.get("tiktok_package", "")),
                    patched_tiktok_version=str(
                        raw.get("patched_tiktok_version", "")
                    ),
                    patched_signature=str(
                        raw.get("patched_signature", "")
                    ),
                    tiktok_drift_warned_at=str(
                        raw.get("tiktok_drift_warned_at", "")
                    ),
                    live_started_at=str(raw.get("live_started_at", "")),
                    total_live_seconds=int(raw.get("total_live_seconds", 0) or 0),
                    transport=str(raw.get("transport", "usb") or "usb"),
                    vcam_app_key=str(raw.get("vcam_app_key", "")),
                    created_at=str(raw.get("created_at", "")),
                    clip_showing=bool(raw.get("clip_showing", True)),
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

    def mark_patched(
        self,
        serial: str,
        tiktok_version: str = "",
        signature: str = "",
    ) -> None:
        """Record a successful Patch + (optionally) the TikTok
        ``versionName`` and APK signature we just installed.

        Storing the version is what enables the drift watcher in
        ``hook_status`` — at runtime it compares the live
        versionName on the phone to this value and warns the
        customer if TikTok has auto-updated itself out from under
        our LSPatch overlay.

        Storing the signature is what makes the patched/unpatched
        detection *reliable* across Android versions: the probe
        compares the live signature against this baseline by
        exact match, eliminating false negatives caused by
        ``dumpsys package`` output format differences between
        AOSP / MIUI / HyperOS / ColorOS / OneUI etc.

        Calling without ``tiktok_version`` / ``signature`` (the
        legacy single-arg form) is still supported for back-compat
        with older code paths; in that case the probe falls back
        to its known-prefix list, which is less precise but still
        useful for legacy entries.
        """
        with self._lock:
            e = self.entries.get(serial)
            if e is None:
                return
            e.patched_at = datetime.now().isoformat(timespec="seconds")
            e.patched_tiktok_version = (tiktok_version or "").strip()
            e.patched_signature = (signature or "").strip().lower()
            # Reset the drift-warning rate-limit so the next genuine
            # update event triggers a fresh warning rather than
            # being suppressed by a stale "warned at <date>" stamp
            # from before the user re-patched.
            e.tiktok_drift_warned_at = ""

    def reconcile_observed_patched(
        self,
        serial: str,
        signature: str = "",
        tiktok_version: str = "",
    ) -> bool:
        """Auto-heal: the hook-status probe just observed that this
        device IS patched (signature matches a known-good prefix
        or the recorded baseline), but our entry has no
        ``patched_at`` timestamp. Set one now so the rest of the UI
        (sidebar status, dashboard "พร้อมใช้งาน" badge, encode
        gating) lights up correctly without forcing the customer
        to re-run a Patch they already did.

        Two real-world triggers for this path:

        1. Customer migrated their ``devices.json`` (or it got
           wiped) but the actual phone is still patched.
        2. Customer Patched from a different machine, then plugged
           the same phone into this PC.

        Returns ``True`` when we mutated state (caller should
        ``save_devices()`` after).
        """
        with self._lock:
            e = self.entries.get(serial)
            if e is None or e.patched_at:
                return False
            e.patched_at = datetime.now().isoformat(timespec="seconds")
            sig_clean = (signature or "").strip().lower()
            ver_clean = (tiktok_version or "").strip()
            # Don't overwrite a richer existing baseline (rare —
            # patched_at empty but signature non-empty would only
            # happen via a manual file edit), but DO record the
            # observed values when the entry is otherwise blank.
            if sig_clean and not e.patched_signature:
                e.patched_signature = sig_clean
            if ver_clean and not e.patched_tiktok_version:
                e.patched_tiktok_version = ver_clean
            return True

    def mark_tiktok_drift_warned(self, serial: str) -> None:
        """Stamp the rate-limit so the drift dialog won't reopen
        every 2-second probe tick. UI calls this right after the
        customer dismisses the warning."""
        with self._lock:
            e = self.entries.get(serial)
            if e is not None:
                e.tiktok_drift_warned_at = datetime.now().isoformat(
                    timespec="seconds",
                )

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

    # ── live session helpers ────────────────────────────────────

    def start_live(self, serial: str) -> str:
        """Mark ``serial`` as currently live and return the ISO
        timestamp we recorded. Idempotent: if the device is
        already marked live, we *keep* the original start time --
        clicking "Start" twice doesn't reset the elapsed counter
        and lose the customer's first hour of broadcast.
        """
        with self._lock:
            e = self.entries.get(serial)
            if e is None:
                e = DeviceEntry(serial=serial)
                e.added_at = datetime.now().isoformat(timespec="seconds")
                self.entries[serial] = e
            if not e.live_started_at:
                e.live_started_at = datetime.now().isoformat(timespec="seconds")
            return e.live_started_at

    def stop_live(self, serial: str) -> int:
        """Clear the live flag and accumulate the session length
        into ``total_live_seconds``. Returns the duration we just
        recorded (0 if the device wasn't marked live)."""
        with self._lock:
            e = self.entries.get(serial)
            if e is None or not e.live_started_at:
                return 0
            elapsed = e.live_elapsed_seconds()
            e.live_started_at = ""
            e.total_live_seconds = int(e.total_live_seconds or 0) + elapsed
            return elapsed

    def list_live_serials(self) -> list[str]:
        """All device serials currently in a live session. Used
        by the sidebar to paint the 🔴 dot."""
        with self._lock:
            return [s for s, e in self.entries.items() if e.is_live()]

    def update_tiktok_package(self, serial: str, package: str) -> None:
        """Remember which TikTok variant the device has installed.

        Called by the dashboard's hook-status probe whenever it
        successfully resolves a package, and by the patch wizard
        right after a successful install. Pass an empty string to
        clear (rare; only useful if the customer uninstalls
        TikTok entirely)."""
        with self._lock:
            e = self.entries.get(serial)
            if e is not None:
                e.tiktok_package = (package or "").strip()

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
