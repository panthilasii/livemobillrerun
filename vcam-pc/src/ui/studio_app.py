"""Live Studio Pro — main application shell.

This is the customer-facing entry point (the "shop window"). The
older ``ui/app.py`` survives as ``--legacy`` for power users / dev
testing; it has all the diagnostic widgets that would only confuse
end users.

Architecture
------------

We use a tiny home-rolled router. Each page is a ``CTkFrame``
subclass that lives in ``studio_pages.py``. ``StudioApp.show_page``
destroys the previous page and packs the new one. Pages keep a
back-reference to the app so they can call shared services (ADB,
device library, license, hook pipeline, …).

A background thread (``_DevicePoller``) polls ``adb devices`` every
2 s and pushes updates onto the Tk event loop via ``after()`` —
never touch Tk widgets from a worker thread directly.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

import customtkinter as ctk

from ..adb import AdbController, AdbDevice
from ..branding import BRAND, THEME
from ..config import ProfileLibrary, StreamConfig
from ..customer_devices import DeviceEntry, DeviceLibrary
from ..hook_mode import HookModePipeline, default_local_mp4
from ..license_key import (
    LicenseError,
    VerifiedLicense,
    is_machine_bound,
    load_activation,
    verify_key,
)
from ..lspatch_pipeline import LSPatchPipeline
from .. import wifi_adb

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
#  background device poller
# ──────────────────────────────────────────────────────────────────


class _DevicePoller(threading.Thread):
    """Polls ``adb devices`` periodically and forwards results to a
    callback that runs on the Tk thread.

    The poller also tries to *reconnect* saved WiFi devices that
    aren't in the current ``adb devices`` list — usually because
    the customer just opened the Studio and the previous
    ``adb connect`` was forgotten by adbd. The reconnect attempt is
    cheap and silent: ``adb connect`` returns instantly if it
    succeeds, ~6 s if the LAN doesn't have anyone listening on
    that IP. We rate-limit it to once every ``RECONNECT_EVERY_S``
    so the UI stays smooth.

    All adb calls happen on this worker thread; the callback is
    dispatched to the Tk loop via ``after(0, ...)`` so widget
    updates never race with the main loop.
    """

    INTERVAL_S = 2.0
    RECONNECT_EVERY_S = 10.0

    def __init__(
        self,
        adb: AdbController,
        on_devices,
        tk_after,
        get_wifi_targets,
    ) -> None:
        super().__init__(daemon=True, name="livestudio-device-poller")
        self._adb = adb
        self._on_devices = on_devices
        self._tk_after = tk_after
        # Callback returning the list of saved (ip, port) tuples to
        # try `adb connect` on. We don't read the device library
        # directly so the poller stays decoupled from the UI's data
        # model.
        self._get_wifi_targets = get_wifi_targets
        self._stop = threading.Event()
        self._last_reconnect_attempt = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._maybe_reconnect_wifi()
            try:
                devs = self._adb.devices() if self._adb.is_available() else []
            except Exception:
                log.exception("adb devices() crashed")
                devs = []
            self._tk_after(0, lambda d=devs: self._safe(d))
            self._stop.wait(self.INTERVAL_S)

    def _maybe_reconnect_wifi(self) -> None:
        now = time.monotonic()
        if now - self._last_reconnect_attempt < self.RECONNECT_EVERY_S:
            return
        self._last_reconnect_attempt = now
        try:
            targets = self._get_wifi_targets() or []
        except Exception:
            log.exception("wifi-targets callback failed")
            return
        if not targets:
            return
        # Build the set of WiFi ids already online so we don't
        # re-issue `adb connect` against them every tick.
        try:
            already_online = {
                d.serial for d in self._adb.devices() if d.online
            }
        except Exception:
            already_online = set()
        for ip, port in targets:
            wifi_id = f"{ip}:{port}"
            if wifi_id in already_online:
                continue
            try:
                wifi_adb.adb_connect(self._adb.adb_path, ip, port, timeout=4)
            except Exception:
                log.debug("adb connect %s failed", wifi_id, exc_info=True)

    def _safe(self, devs: list[AdbDevice]) -> None:
        try:
            self._on_devices(devs)
        except Exception:
            log.exception("on_devices callback failed")


# ──────────────────────────────────────────────────────────────────
#  main application
# ──────────────────────────────────────────────────────────────────


class StudioApp(ctk.CTk):
    """The Live Studio Pro main window."""

    WIDTH = 1100
    HEIGHT = 720

    def __init__(
        self,
        port_override: int | None = None,
        no_adb_reverse: bool = False,
    ) -> None:
        super().__init__(fg_color=THEME.bg_main)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")  # base, we override per-widget

        self.title(f"{BRAND.name} — {BRAND.tagline_th}")
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.minsize(960, 640)

        # ── shared services
        self.cfg = StreamConfig.load()
        if port_override:
            self.cfg.tcp_port = port_override
        self.profiles = ProfileLibrary.load()
        self.adb = AdbController(self.cfg.adb_path)
        self.hook = HookModePipeline(self.cfg)
        self.lspatch = LSPatchPipeline(self.cfg)
        self.devices_lib = DeviceLibrary.load()
        self.no_adb_reverse = no_adb_reverse

        # ── runtime state
        self.license: VerifiedLicense | None = None
        self.activation: dict | None = None
        self.online_serials: set[str] = set()
        self.live_devices: list[AdbDevice] = []
        # canonical-serial → "usb"|"wifi" for the badge in the UI
        self.transport_for: dict[str, str] = {}
        # canonical-serial → adb id we should pass to ``adb -s …``
        # when issuing commands. Falls back to the serial itself
        # when the device is offline.
        self.adb_id_for_serial: dict[str, str] = {}
        self.selected_serial: str | None = None
        self._current_page: Optional[ctk.CTkFrame] = None

        self.local_mp4 = default_local_mp4(self.cfg)

        # ── poller
        self._poller = _DevicePoller(
            self.adb,
            on_devices=self._on_devices_polled,
            tk_after=self.after,
            get_wifi_targets=self._wifi_targets,
        )
        self._poller.start()

        # ── window close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── macOS clipboard fix
        self._install_clipboard_bindings()

        # ── show first page
        self._route_initial()

    # ── clipboard bindings ───────────────────────────────────────

    def _install_clipboard_bindings(self) -> None:
        """Make ⌘V/⌘C/⌘X/⌘A actually work in Entry widgets.

        macOS Tk's Cocoa backend only auto-routes the system Cmd-key
        accelerators to the focused widget if **a real Tk menu bar
        with an Edit menu is wired up at the application level**.
        Without it, Cmd-V opens the AppKit edit menu (or beeps) and
        the keystroke never reaches the Entry's ``<<Paste>>`` handler.

        We do three things here, layered for safety:

        1. Install a Tk menu bar with an Edit menu whose items fire
           the standard virtual events. This is the only thing macOS
           genuinely respects.
        2. Bind class-level shortcuts for ⌘/Ctrl combinations as a
           belt-and-braces backup.
        3. Wire a right-click context menu so users who can't paste
           at all still have a visible escape hatch.
        """
        import tkinter as tk

        def _do(virt: str):
            def fn(_e=None):
                try:
                    w = self.focus_get()
                    if w is not None:
                        w.event_generate(virt)
                except Exception:
                    log.debug("clipboard %s failed", virt, exc_info=True)
            return fn

        def _select_all(_e=None):
            try:
                w = self.focus_get()
                if w is not None:
                    w.select_range(0, "end")
                    w.icursor("end")
            except Exception:
                log.debug("select-all failed", exc_info=True)

        # ── (1) menu bar
        try:
            menubar = tk.Menu(self)
            edit = tk.Menu(menubar, tearoff=0)
            edit.add_command(label="Cut",        accelerator="Cmd+X",
                             command=_do("<<Cut>>"))
            edit.add_command(label="Copy",       accelerator="Cmd+C",
                             command=_do("<<Copy>>"))
            edit.add_command(label="Paste",      accelerator="Cmd+V",
                             command=_do("<<Paste>>"))
            edit.add_separator()
            edit.add_command(label="Select All", accelerator="Cmd+A",
                             command=_select_all)
            menubar.add_cascade(label="Edit", menu=edit)
            self.configure(menu=menubar)
        except Exception:
            log.exception("failed to attach Edit menu")

        # ── (2) class-level shortcuts (backup)
        def paste_evt(e):
            try:
                e.widget.event_generate("<<Paste>>")
            except Exception:
                pass
            return "break"

        def copy_evt(e):
            try:
                e.widget.event_generate("<<Copy>>")
            except Exception:
                pass
            return "break"

        def cut_evt(e):
            try:
                e.widget.event_generate("<<Cut>>")
            except Exception:
                pass
            return "break"

        def selall_evt(e):
            try:
                e.widget.select_range(0, "end")
                e.widget.icursor("end")
            except Exception:
                pass
            return "break"

        for cls in ("Entry", "TEntry"):
            for accel, fn in (
                ("<Command-v>", paste_evt), ("<Command-V>", paste_evt),
                ("<Control-v>", paste_evt), ("<Control-V>", paste_evt),
                ("<Command-c>", copy_evt),  ("<Command-C>", copy_evt),
                ("<Control-c>", copy_evt),  ("<Control-C>", copy_evt),
                ("<Command-x>", cut_evt),   ("<Command-X>", cut_evt),
                ("<Control-x>", cut_evt),   ("<Control-X>", cut_evt),
                ("<Command-a>", selall_evt), ("<Command-A>", selall_evt),
                ("<Control-a>", selall_evt), ("<Control-A>", selall_evt),
            ):
                self.bind_class(cls, accel, fn)

        # ── (3) right-click context menu (manual paste fallback)
        ctx = tk.Menu(self, tearoff=0)
        ctx.add_command(label="วาง (Paste)",
                        command=_do("<<Paste>>"))
        ctx.add_command(label="ก๊อป (Copy)",
                        command=_do("<<Copy>>"))
        ctx.add_command(label="ตัด (Cut)",
                        command=_do("<<Cut>>"))
        ctx.add_separator()
        ctx.add_command(label="เลือกทั้งหมด (Select All)",
                        command=_select_all)

        def popup(e):
            # macOS uses Button-2 for right-click; X11/Win uses
            # Button-3. Bind both so we don't have to detect.
            try:
                e.widget.focus_set()
                ctx.tk_popup(e.x_root, e.y_root)
            finally:
                ctx.grab_release()
            return "break"

        for btn in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            for cls in ("Entry", "TEntry"):
                self.bind_class(cls, btn, popup)

    # ── routing ──────────────────────────────────────────────────

    def _route_initial(self) -> None:
        """Decide whether to show Activation (no/expired license) or
        the Dashboard."""
        from .studio_pages import ActivationPage, DashboardPage

        act = load_activation()
        if act is None:
            self.show_page(ActivationPage)
            return

        try:
            verified = verify_key(act["license_key"])
        except LicenseError as e:
            log.warning("stored license failed verification: %s", e)
            self.show_page(
                ActivationPage,
                error=f"License เดิมไม่ถูกต้อง: {e}",
            )
            return

        if verified.is_expired:
            self.activation = act
            self.license = verified
            self.show_page(
                ActivationPage,
                error=f"License หมดอายุเมื่อ {verified.expiry.isoformat()}",
            )
            return

        # Warn (not block) if the activation was bound to another
        # machine — the user might have moved their PC.
        if not is_machine_bound(act):
            log.info("license bound to a different machine — proceeding anyway")

        self.activation = act
        self.license = verified
        self.show_page(DashboardPage)

    def show_page(self, page_cls, **kwargs) -> None:
        """Replace the current page with a fresh instance of
        ``page_cls``. ``kwargs`` are forwarded to the page constructor
        so callers can pass things like ``error="..."``.
        """
        if self._current_page is not None:
            try:
                self._current_page.destroy()
            except Exception:
                log.exception("error destroying previous page")
        page = page_cls(self, **kwargs)
        page.pack(fill="both", expand=True)
        self._current_page = page

    # ── shortcuts the pages use as navigation actions ────────────

    def go_dashboard(self) -> None:
        from .studio_pages import DashboardPage

        self.show_page(DashboardPage)

    def go_settings(self) -> None:
        from .studio_pages import SettingsPage

        self.show_page(SettingsPage)

    def go_wizard(self) -> None:
        from .studio_pages import WizardPage

        self.show_page(WizardPage)

    def go_activation(self, error: str | None = None) -> None:
        from .studio_pages import ActivationPage

        if error:
            self.show_page(ActivationPage, error=error)
        else:
            self.show_page(ActivationPage)

    # ── device polling ───────────────────────────────────────────

    def _wifi_targets(self) -> list[tuple[str, int]]:
        """Snapshot of saved WiFi addresses for the poller's
        reconnect loop. Called from a worker thread."""
        out: list[tuple[str, int]] = []
        for e in self.devices_lib.list():
            if e.has_wifi():
                out.append((e.wifi_ip, int(e.wifi_port or 5555)))
        return out

    def _on_devices_polled(self, devs: list[AdbDevice]) -> None:
        """Callback from the poller, executed on the Tk thread.

        ``devs`` may contain a mix of USB-serial rows and WiFi
        ``IP:port`` rows, possibly both for the same physical device
        (briefly, right after `adb tcpip`). We fold the WiFi rows
        back onto their canonical USB-serial entry, mark each
        entry's transport, and decide which adb id to use for
        commands.
        """
        self.live_devices = devs

        online_serials: set[str] = set()
        transport_for: dict[str, str] = {}
        adb_id_for: dict[str, str] = {}

        for d in devs:
            if not d.online:
                continue
            if wifi_adb.is_wifi_id(d.serial):
                # Map WiFi row → canonical USB serial.
                entry = self.devices_lib.find_by_wifi_id(d.serial)
                if entry is None:
                    # Unknown WiFi device — skip; we never auto-add
                    # over WiFi because we have no model info and
                    # no way to know the customer trusts this LAN.
                    continue
                online_serials.add(entry.serial)
                # USB takes priority if the cable is also plugged in.
                if transport_for.get(entry.serial) != "usb":
                    transport_for[entry.serial] = "wifi"
                    adb_id_for[entry.serial] = d.serial
            else:
                # USB row — d.serial *is* the canonical serial.
                online_serials.add(d.serial)
                transport_for[d.serial] = "usb"
                adb_id_for[d.serial] = d.serial
                # Auto-track unknown USB devices so they show up in
                # the sidebar before the user runs the wizard.
                if self.devices_lib.get(d.serial) is None:
                    self.devices_lib.upsert(d.serial, model=d.model)
                else:
                    self.devices_lib.upsert(d.serial, model=d.model)

        self.online_serials = online_serials
        self.transport_for = transport_for
        self.adb_id_for_serial = adb_id_for

        # Persist the most recent transport on each entry so the
        # badge survives an offline gap.
        for serial, t in transport_for.items():
            self.devices_lib.mark_seen_via(serial, t)

        # Bubble the update to the current page, if it cares.
        page = self._current_page
        if page is not None and hasattr(page, "on_devices_changed"):
            try:
                page.on_devices_changed()
            except Exception:
                log.exception("page.on_devices_changed crashed")

    def is_online(self, serial: str) -> bool:
        return serial in self.online_serials

    def adb_id_for(self, entry_or_serial) -> str:
        """Return the adb id (USB serial or ``IP:port``) we should
        pass to ``adb -s …`` for this device right now. Falls back
        to the canonical serial when offline so callers always get
        *something* back."""
        serial = (
            entry_or_serial.serial
            if isinstance(entry_or_serial, DeviceEntry)
            else str(entry_or_serial)
        )
        return self.adb_id_for_serial.get(serial, serial)

    def transport_of(self, serial: str) -> str:
        """``"usb"`` / ``"wifi"`` / ``""`` (offline) for the device."""
        return self.transport_for.get(serial, "")

    # ── WiFi setup helpers (called from Dashboard + Wizard) ──────

    def setup_wifi_after_patch(self, serial: str) -> str:
        """Best-effort: read the phone's LAN IP over USB, flip
        adbd into TCP mode, then verify a WiFi reconnect works.

        Returns a Thai status string for the patch success dialog.
        Failures here do *not* invalidate the patch — the customer
        can still use USB and try again later from the Dashboard.

        Must be called while the phone is still connected via USB
        (``adb tcpip`` requires that).
        """
        adb_path = self.cfg.adb_path
        ip = wifi_adb.get_device_wifi_ip(adb_path, serial)
        if not ip:
            return (
                "⚠️ ไม่พบ WiFi บนโทรศัพท์ — ใช้สาย USB ไปก่อน "
                "เชื่อม WiFi ภายหลังที่หน้า Dashboard ได้"
            )

        if not wifi_adb.enable_tcpip(adb_path, serial):
            return (
                "⚠️ เปิดโหมด WiFi ไม่สำเร็จ — ใช้สาย USB ไปก่อน "
                "ลอง 'เชื่อม WiFi อีกครั้ง' ที่ Dashboard"
            )

        # adbd needs ~1 s to restart in TCP mode; the USB transport
        # disappears around the same time, so wait a beat before
        # probing the wireless port.
        time.sleep(1.5)
        connected = wifi_adb.adb_connect(adb_path, ip)
        # Persist whether or not the connect probe succeeded —
        # even if it fails right now (the cable can still hold
        # priority for a moment after tcpip), the next poller
        # tick will retry.
        self.devices_lib.update_wifi(serial, ip, port=5555)
        self.save_devices()
        if connected:
            self.devices_lib.mark_seen_via(serial, "wifi")
            return (
                f"✅ เปิด WiFi สำเร็จ — โทรศัพท์อยู่ที่ {ip}:5555\n"
                "ถอดสาย USB ได้เลย (โปรแกรมจะเชื่อม WiFi อัตโนมัติ "
                "ทุกครั้งที่เปิดโปรแกรม)"
            )
        return (
            f"📶 บันทึก WiFi {ip}:5555 ไว้แล้ว — แต่ยังเชื่อมไม่ติดตอนนี้\n"
            "ลองถอดสาย USB แล้วกด 'เชื่อม WiFi อีกครั้ง' ที่ Dashboard"
        )

    def reconnect_wifi(self, serial: str) -> tuple[bool, str]:
        """Manual reconnect from the Dashboard. Tries the saved
        ``IP:port`` for this entry. Returns ``(ok, thai_msg)``."""
        e = self.devices_lib.get(serial)
        if e is None or not e.has_wifi():
            return False, "ยังไม่ได้ตั้งค่า WiFi — เสียบ USB แล้ว Patch ใหม่"
        if wifi_adb.adb_connect(
            self.cfg.adb_path, e.wifi_ip, int(e.wifi_port or 5555),
        ):
            return True, f"✅ เชื่อมต่อ WiFi สำเร็จ ({e.wifi_address()})"
        return False, (
            f"❌ เชื่อม {e.wifi_address()} ไม่สำเร็จ\n"
            "• เช็คว่าโทรศัพท์อยู่วง WiFi เดียวกับคอม\n"
            "• ลอง reboot โทรศัพท์แล้วเสียบ USB เพื่อ enable tcpip ใหม่"
        )

    # ── selected device helpers ──────────────────────────────────

    def select_device(self, serial: str | None) -> None:
        self.selected_serial = serial
        page = self._current_page
        if page is not None and hasattr(page, "on_selection_changed"):
            try:
                page.on_selection_changed()
            except Exception:
                log.exception("page.on_selection_changed crashed")

    def selected_entry(self):
        if not self.selected_serial:
            return None
        return self.devices_lib.get(self.selected_serial)

    # ── persistence ──────────────────────────────────────────────

    def save_devices(self) -> None:
        try:
            self.devices_lib.save()
        except Exception:
            log.exception("failed to persist devices.json")

    # ── shutdown ─────────────────────────────────────────────────

    def _on_close(self) -> None:
        try:
            self._poller.stop()
        except Exception:
            pass
        self.save_devices()
        self.destroy()
