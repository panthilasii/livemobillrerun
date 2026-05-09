"""NP Create — main application shell.

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
        super().__init__(daemon=True, name="npcreate-device-poller")
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
    """The NP Create main window."""

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
        self._install_window_icon()

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

        # ── embedded sales dashboard server (lazy)
        # Started on demand when the user clicks the "Dashboard"
        # sidebar button. Kept on the app instance (not the page)
        # so re-rendering Dashboard / Settings doesn't re-bind 8765
        # or kill the running uvicorn loop.
        self._dashboard_handle = None  # type: ignore[var-annotated]

        # ── announcements (server-pushed news / alerts)
        # Polls the signed JSON feed in a daemon thread; the
        # callback re-routes to the Tk thread via ``after(0, ...)``
        # so widget updates never race the main loop. Failure
        # modes (DNS down, JSON malformed, sig mismatch) are silent
        # logs -- the app must NEVER refuse to launch because of
        # the announcement subsystem.
        from .. import announcements as _ann_mod
        self.announcements = _ann_mod.AnnouncementPoller(
            app_version=BRAND.version,
            on_update=self._on_announcements_updated,
        )
        self.announcements.start()
        self._latest_announcements: list = []

        # ── auto-update poller
        # Same threading model as announcements: signed JSON feed,
        # background polls, callback bounces to the Tk thread. We
        # keep the latest manifest so that any time the dashboard
        # is rebuilt (page switch, language change), we can
        # re-show the banner without waiting for the 6 h tick.
        from .. import auto_update as _au
        self._latest_update = None  # type: ignore[var-annotated]
        self.update_poller = _au.UpdatePoller(
            on_update=self._on_update_available,
        )
        self.update_poller.start()

        # ── window close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── macOS clipboard fix
        self._install_clipboard_bindings()

        # ── show first page
        self._route_initial()

    # ── clipboard bindings ───────────────────────────────────────

    def _install_window_icon(self) -> None:
        """Set the OS-native window/dock icon. We try the multi-res
        ``.ico`` on Windows (Tk's native path), then fall back to a
        PNG ``iconphoto`` which Linux + macOS pick up. macOS Finder
        also reads the icon from a code-signed ``.app`` bundle, but
        for a bare ``run.command`` launch the dock icon will show
        the Python rocket — that's a packaging-time concern, not a
        runtime one."""
        try:
            if BRAND.icon_ico_path.is_file():
                # iconbitmap on Windows accepts .ico directly.
                try:
                    self.iconbitmap(default=str(BRAND.icon_ico_path))
                except Exception:
                    pass
            png = BRAND.logo_64_path if BRAND.logo_64_path.is_file() else BRAND.logo_path
            if png.is_file():
                from tkinter import PhotoImage

                self._icon_img = PhotoImage(file=str(png))
                # Ignore failures (some macOS Tk-Cocoa builds throw).
                try:
                    self.iconphoto(True, self._icon_img)
                except Exception:
                    pass
        except Exception as e:
            log.debug("window-icon install failed: %s", e)

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

        # If we just rebuilt the dashboard, hand it any pending
        # announcement so the banner doesn't go blank between page
        # switches. Page-construction is synchronous so the widget
        # tree exists by the time we get here.
        self._refresh_dashboard_announcement()
        self._refresh_dashboard_update()

    def _on_announcements_updated(self, anns: list) -> None:
        """Background poller callback. ``anns`` is the *visible*
        list (already filtered for dismissal / expiry / version
        applicability) sorted by the feed.

        We just snapshot the list here and trampoline UI work to
        the Tk loop -- the poller thread must never touch widgets
        directly.
        """
        self._latest_announcements = list(anns)
        try:
            self.after(0, self._refresh_dashboard_announcement)
        except Exception:
            log.exception("could not schedule announcement refresh")

    # ── auto-update plumbing ─────────────────────────────────────

    def _on_update_available(self, manifest) -> None:
        """Background-poller callback (NOT on the Tk thread).

        Snapshot the manifest and trampoline a UI refresh onto the
        main loop. We never touch widgets from the poller thread.
        """
        self._latest_update = manifest
        try:
            self.after(0, self._refresh_dashboard_update)
        except Exception:
            log.exception("could not schedule update banner refresh")

    def _refresh_dashboard_update(self) -> None:
        """If the dashboard is currently mounted, push the latest
        manifest into its banner. Idempotent -- safe to call after
        every page transition."""
        page = self._current_page
        from .studio_pages import DashboardPage
        if not isinstance(page, DashboardPage):
            return
        if not hasattr(page, "set_update"):
            return
        page.set_update(self._latest_update)

    def _refresh_dashboard_announcement(self) -> None:
        """If the dashboard is the currently-displayed page, push
        the highest-priority announcement into its banner. We pick
        critical > warning > info, then earliest published_at as
        the tiebreaker. The other announcements simply queue up;
        when the user dismisses one, ``refresh_now`` re-evaluates
        and surfaces the next."""
        page = self._current_page
        from .studio_pages import DashboardPage
        if not isinstance(page, DashboardPage):
            return
        if not self._latest_announcements:
            page.set_announcement(None)
            return

        sev_rank = {"critical": 0, "warning": 1, "info": 2}
        ranked = sorted(
            self._latest_announcements,
            key=lambda a: (
                sev_rank.get(a.severity, 99),
                a.published_at or "",
            ),
        )
        page.set_announcement(ranked[0])

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

    def go_admin(self) -> None:
        from .studio_pages import AdminPage

        self.show_page(AdminPage)

    def open_dashboard(self) -> None:
        """Start the embedded FastAPI server (idempotent) and open
        the customer's default browser at the dashboard URL.

        Raises ``RuntimeError`` if the webapp dependencies are
        missing -- the caller turns that into a friendly Thai
        message rather than letting the import error bubble up.
        """
        try:
            from .. import webapp  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ไม่พบไลบรารี FastAPI / uvicorn"
            ) from exc

        # Reuse the existing server if it's still alive. Otherwise
        # start a fresh one. The handle is None on first call and
        # also after the user explicitly stopped it (rare path).
        h = self._dashboard_handle
        if h is None or not h.is_alive():
            try:
                h = webapp.server.start_in_thread()
            except OSError as exc:
                # Address-already-in-use, etc.
                raise RuntimeError(
                    f"เปิดเซิร์ฟเวอร์ Dashboard ไม่ได้: {exc}"
                ) from exc
            self._dashboard_handle = h

        import webbrowser as _wb
        try:
            _wb.open(h.url)
        except Exception:
            log.exception("could not open browser")

    @property
    def is_admin(self) -> bool:
        """True iff the .private_key signing seed lives next to this
        install — i.e. we're on the admin's machine, not a customer's.
        Customers never receive the file (build_release.py strips it),
        so the admin tab is invisible on their builds.
        """
        from ..license_key import PRIVATE_KEY_PATH

        return PRIVATE_KEY_PATH.is_file()

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

    def refresh_devices_now(self, timeout: float = 3.0) -> bool:
        """Force a synchronous ``adb devices -l`` refresh and update
        the cached state immediately.

        The background poller only runs every ~2 seconds. That's
        fine for Dashboard rendering, but action handlers gated on
        ``is_online`` / ``transport_of`` (Patch, Mirror, Start
        Live, etc.) hit a race window: customer plugs in the
        cable, immediately clicks the action button within ~2s,
        and gets a misleading "เครื่องไม่ได้เชื่อมต่อ" or
        "ต้องเสียบสาย USB" warning even though ``adb devices``
        already lists the device.

        Calling this method right before the gate-keeping check
        closes the race. Side-effects:

        * ``self.live_devices`` updated.
        * ``self.online_serials`` / ``self.transport_for`` /
          ``self.adb_id_for_serial`` updated.
        * Any subscribed page's ``on_devices_changed`` re-fires.

        Returns True on success, False on adb-side failure (in
        which case the cached state is left intact — better to
        gate against stale-but-real than to nuke the cache and
        guess wrong).
        """
        try:
            devs = self.adb.devices()
        except Exception:
            log.exception("refresh_devices_now: adb.devices() raised")
            return False
        # Fold WiFi rows + propagate the same way the poller does
        # so the two paths can never disagree about what "online"
        # means.
        try:
            self._on_devices_polled(devs)
            return True
        except Exception:
            log.exception("refresh_devices_now: _on_devices_polled raised")
            return False

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
        try:
            self.announcements.stop()
        except Exception:
            pass
        try:
            self.update_poller.stop()
        except Exception:
            pass
        try:
            if self._dashboard_handle is not None:
                self._dashboard_handle.stop()
        except Exception:
            pass
        # Tear down any active scrcpy mirror windows so we don't
        # leak orphan subprocesses after the dashboard exits. Each
        # mirror is its own native window and would otherwise keep
        # showing a frozen frame until the customer manually killed
        # it via the dock / task manager.
        try:
            from .. import scrcpy_mirror
            scrcpy_mirror.stop_all()
        except Exception:
            log.exception("scrcpy mirror cleanup on shutdown failed")
        self.save_devices()
        self.destroy()
