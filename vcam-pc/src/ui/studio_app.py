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
from ..encode_push_tasks import EncodePushRegistry, device_local_mp4
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

    Two responsibilities, two threads (v1.8.2 fix)
    ----------------------------------------------
    Pre-v1.8.2, this class did *both* device polling AND WiFi
    re-connection in a single ``run()`` loop. That worked fine
    when customers had at most one or two saved WiFi devices —
    ``adb connect <unreachable>:5555`` returns in ~4 s, so even
    if every saved IP was offline the poller blocked for ~8 s
    every 10 s, which the UI hid behind the existing 2 s
    INTERVAL_S without anyone noticing.

    Real customer libraries grew faster than that. With 4–6
    saved WiFi targets — all on a different LAN than the one the
    customer is currently on, e.g. they pack up the rig at
    home and carry it to a coworking space — the inline
    reconnect attempt blocks the poller for 16–24 s straight.
    During that window ``adb devices`` is not called, so
    ``live_devices`` doesn't refresh, so every device card on
    the dashboard shows "offline" even when the customer has
    a phone freshly USB-plugged-in. Customer-facing symptom:
    "ระบบไม่ซิงค์ที่จอเลย / ไม่อ่านค่าการเชื่อมต่อ".

    The fix: device polling stays in this thread on a tight
    ``INTERVAL_S`` cadence; WiFi reconnect runs in a sibling
    thread (``_WifiReconnector``) that can block as long as it
    likes without affecting the dashboard's freshness. The two
    threads share nothing mutable — they both call
    ``self._adb`` independently.

    All adb calls happen on these worker threads; the device
    callback is dispatched to the Tk loop via ``after(0, ...)``
    so widget updates never race with the main loop.
    """

    INTERVAL_S = 2.0

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
        # NOT ``self._stop`` — that name collides with
        # ``threading.Thread._stop()`` which CPython's join()
        # calls during cleanup; a stray ``Event`` there raises
        # ``TypeError: 'Event' object is not callable`` and
        # leaks the worker thread on app exit.
        self._shutdown = threading.Event()

        # Sibling thread handles wifi reconnect so it can't
        # starve the device-poll loop. Started/stopped in
        # lock-step with this thread.
        self._wifi = _WifiReconnector(adb, get_wifi_targets, self._shutdown)

    def start(self) -> None:  # type: ignore[override]
        super().start()
        self._wifi.start()

    def stop(self) -> None:
        self._shutdown.set()

    def run(self) -> None:
        while not self._shutdown.is_set():
            try:
                devs = self._adb.devices() if self._adb.is_available() else []
            except Exception:
                log.exception("adb devices() crashed")
                devs = []
            self._tk_after(0, lambda d=devs: self._safe(d))
            self._shutdown.wait(self.INTERVAL_S)

    def _safe(self, devs: list[AdbDevice]) -> None:
        try:
            self._on_devices(devs)
        except Exception:
            log.exception("on_devices callback failed")


class _WifiReconnector(threading.Thread):
    """Best-effort ``adb connect`` for saved WiFi devices.

    Independent of ``_DevicePoller`` so a slow / unreachable LAN
    can't block the dashboard's freshness. Per-target adaptive
    backoff stops re-trying targets that have failed N times in
    a row, which protects the customer from the "dead WiFi entry
    in library + new LAN" → "every poll cycle wastes 4 s on
    timeout" cascade.

    Targets that *do* succeed reset their failure counter, so a
    customer who fixed their WiFi (e.g. came back home) sees the
    phone reconnect within a single ``INTERVAL_S`` window.
    """

    INTERVAL_S = 10.0
    CONNECT_TIMEOUT_S = 3.0
    # After this many consecutive failures, back off to one
    # attempt per ``MAX_BACKOFF_S`` seconds. 3 attempts × 3 s
    # timeout = ~9 s of wasted I/O per dead target before we
    # stop hammering it.
    FAIL_THRESHOLD = 3
    MAX_BACKOFF_S = 120.0

    def __init__(self, adb: AdbController, get_targets, stop_event) -> None:
        super().__init__(daemon=True, name="npcreate-wifi-reconnect")
        self._adb = adb
        self._get_targets = get_targets
        # See _DevicePoller for why this isn't named _stop —
        # threading.Thread reserves that attribute name.
        self._shutdown = stop_event
        # wifi_id → (failure count, last attempt monotonic time)
        self._fail_state: dict[str, tuple[int, float]] = {}

    def run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("wifi reconnector tick crashed")
            self._shutdown.wait(self.INTERVAL_S)

    def _tick(self) -> None:
        try:
            targets = self._get_targets() or []
        except Exception:
            log.exception("wifi-targets callback failed")
            return
        if not targets:
            return
        try:
            already_online = {
                d.serial for d in self._adb.devices() if d.online
            }
        except Exception:
            already_online = set()

        now = time.monotonic()
        for ip, port in targets:
            if self._shutdown.is_set():
                return
            wifi_id = f"{ip}:{port}"
            if wifi_id in already_online:
                # Reset on success so a transient outage doesn't
                # poison the backoff state forever.
                self._fail_state.pop(wifi_id, None)
                continue
            fails, last_at = self._fail_state.get(wifi_id, (0, 0.0))
            if fails >= self.FAIL_THRESHOLD:
                # Adaptive backoff: linear in fail count, capped.
                wait = min(self.MAX_BACKOFF_S, 10.0 * (fails - 1))
                if now - last_at < wait:
                    continue
            ok = False
            try:
                ok = wifi_adb.adb_connect(
                    self._adb.adb_path, ip, port,
                    timeout=self.CONNECT_TIMEOUT_S,
                )
            except Exception:
                log.debug("adb connect %s failed", wifi_id, exc_info=True)
            if ok:
                self._fail_state.pop(wifi_id, None)
            else:
                self._fail_state[wifi_id] = (fails + 1, time.monotonic())


# ──────────────────────────────────────────────────────────────────
#  main application
# ──────────────────────────────────────────────────────────────────


class StudioApp(ctk.CTk):
    """The NP Create main window."""

    WIDTH = 1100
    HEIGHT = 720

    # ``reconnect_wifi`` consecutive-failure budget before the saved
    # WiFi address is treated as stale and cleared from the library.
    # Three matches the ``_WifiReconnector`` backoff threshold so the
    # background poller and the dashboard manual-reconnect agree on
    # when an IP is "dead enough" to give up on. Lower = clears too
    # eagerly when WiFi is briefly flaky; higher = customer keeps
    # mashing a button that's never going to work again because they
    # rebooted the phone last week and DHCP issued a new IP.
    WIFI_RECONNECT_FAIL_LIMIT = 3

    def __init__(
        self,
        port_override: int | None = None,
        no_adb_reverse: bool = False,
    ) -> None:
        super().__init__(fg_color=THEME.bg_main)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")  # base, we override per-widget

        # Window title carries the version — customers can confirm
        # "what am I actually running" at a glance instead of having
        # to dig into Settings or a hidden About dialog. This is the
        # single most-asked support question after every release.
        self.title(f"{BRAND.name} v{BRAND.version} — {BRAND.tagline_th}")
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

        # If adb is unreachable, the wizard will sit on
        # "🔄 รอเครื่องเชื่อมต่อ…" forever with no hint as to why —
        # which was the v1.7.8 / v1.7.9 customer-facing symptom of
        # the bundled adb.exe lookup being broken. Detect that here
        # and surface a clear dialog the moment the main window
        # opens, with a pointer to the diagnostic file the support
        # team can read. We don't *quit* — the rest of the UI is
        # still useful (browse files, license, settings) — we just
        # tell the customer up front that USB onboarding can't work
        # in the current state.
        self._adb_resolution_warned = False
        self.after(800, self._check_adb_or_warn)

        # Pre-warm Gatekeeper / SmartScreen on the bundled JDK so
        # the customer's first ``Patch & ติดตั้ง TikTok`` click
        # doesn't pay the 10-30 s cold-start cost. The JDK lives
        # under ``.tools/<os>/jdk-21/`` and on first launch the OS
        # walks every ``Contents/Home/lib/*.dylib`` (macOS) or
        # ``bin/*.dll`` (Windows) checking notarization / MOTW.
        # That verdict is cached for the rest of the boot. By
        # firing ``java -version`` in a daemon thread *now* (as the
        # window opens), the cache is hot well before the customer
        # navigates to the Dashboard. v1.8.13 fix for "tools fail
        # check on first patch" reports across both Win + Mac.
        try:
            from ..lspatch_pipeline import warm_up_java
            from .. import platform_tools as _pt
            warm_up_java(_pt.find_java())
        except Exception:
            log.debug("java warm-up dispatch failed", exc_info=True)

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

        # Per-serial consecutive failure counter for the dashboard
        # ``Reconnect WiFi`` button. Reset on success, incremented
        # when both the saved-IP probe AND any USB-fallback re-tcpip
        # fail. After ``WIFI_RECONNECT_FAIL_LIMIT`` strikes we drop
        # the saved address so the UI flips back to "ตั้งค่า WiFi"
        # and stops the customer from re-trying a dead IP forever.
        self._wifi_reconnect_fails: dict[str, int] = {}

        # Per-device encode + push task registry (v1.8.6).
        # ``self.local_mp4`` was the single shared cache path used
        # by the v1.8.5-and-earlier "one device at a time" flow;
        # we keep it as a fallback for the legacy ``ui/app.py``
        # window (kept under ``--legacy``) but the studio dashboard
        # now allocates a per-serial cache via ``device_local_mp4``
        # so several encodes can run in parallel without their
        # ffmpeg children clobbering each other's bytes. The
        # registry below tracks who's currently running so the
        # dashboard button can reject double-clicks and the
        # sidebar can render a per-device progress badge.
        self.encode_tasks = EncodePushRegistry()
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
        """Entry point for "+ เพิ่มเครื่อง" — v1.8.0 routes
        through the mode picker so the customer can opt out of
        USB/ADB entirely. Old code paths that called
        ``go_wizard()`` keep working: the picker just becomes
        an extra screen in front of the existing wizard.
        """
        from .studio_pages import ModePickerPage

        self.show_page(ModePickerPage)

    def go_usb_wizard(self) -> None:
        """Direct entry to the classic USB+LSPatch flow (Mode A).
        Picker calls this when the customer chooses
        "ใช้ USB + Patch (คุณภาพสูงสุด)"."""
        from .studio_pages import WizardPage

        self.show_page(WizardPage)

    def go_rtmp_wizard(self) -> None:
        """Mode B — RTMP + virtual-cam app on the phone.
        No USB, no driver, no patch. Set up over WiFi only."""
        from .studio_pages import RTMPWizardPage

        self.show_page(RTMPWizardPage)

    def go_wireless_wizard(self) -> None:
        """Mode C — Android 11+ Wireless Debugging pair → connect.
        Same Patch flow as Mode A but no Windows driver needed."""
        from .studio_pages import WirelessADBWizardPage

        self.show_page(WirelessADBWizardPage)

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

    # ── adb sanity check ─────────────────────────────────────────

    def _check_adb_or_warn(self) -> None:
        """Pop a one-shot dialog if the bundled adb is missing.

        Runs ~0.8 s after the main window appears (so the dialog
        opens *over* a real, drawn window — not a half-painted
        loading screen). If the resolved ``adb_path`` doesn't exist
        on disk OR ``adb version`` fails, the customer is told what
        broke and where the diagnostic file lives. Without this,
        the wizard's Step 2 just shows "🔄 รอเครื่อง" forever and
        the customer pings support with "doesn't work".

        The dialog adapts per-OS so the cause-hint + help steps
        actually point at the right knob:

        * Windows → Defender quarantine + VC++ Redistributable
        * macOS   → Gatekeeper + ``chmod +x`` + "Allow Anyway"
        * Linux   → ``chmod +x`` + libc / executable-flag mismatch
        """
        if self._adb_resolution_warned:
            return
        try:
            adb_path = Path(self.adb.adb_path)
        except Exception:
            adb_path = None  # type: ignore[assignment]

        import sys as _sys
        is_windows = _sys.platform.startswith("win")
        is_macos = _sys.platform == "darwin"
        adb_name = "adb.exe" if is_windows else "adb"

        if is_windows:
            cause_hint = (
                " แต่รันไม่ได้ (อาจถูก antivirus กักหรือ\n"
                "ขาด Visual C++ Redistributable)"
            )
            help_steps = (
                "1. รีสตาร์ทเครื่อง แล้วเปิด NP Create อีกครั้ง\n"
                f"2. ตรวจสอบว่า Windows Defender ไม่กัก {adb_name}"
                " ไว้\n3. ส่งไฟล์ diagnostic ให้แอดมิน:\n"
            )
        elif is_macos:
            cause_hint = (
                " แต่รันไม่ได้ (อาจถูก macOS Gatekeeper บล็อก\n"
                "หรือไฟล์ไม่มี execute permission)"
            )
            help_steps = (
                f'1. เปิด Terminal แล้วพิมพ์:  chmod +x "<path>/'
                f'{adb_name}'
                '"\n   (path เห็นข้างบน) แล้วเปิด NP Create อีกครั้ง\n'
                "2. ถ้ายังไม่ผ่าน ลอง: System Settings → Privacy & Security\n"
                f"   → กด 'Allow Anyway' ถ้าเห็นการบล็อก {adb_name}"
                "\n3. ส่งไฟล์ diagnostic ให้แอดมิน:\n"
            )
        else:
            cause_hint = (
                " แต่รันไม่ได้ (ตรวจสอบ execute permission\n"
                "หรือ libc/library version ไม่ตรง)"
            )
            help_steps = (
                f'1. ลอง:  chmod +x "<path>/{adb_name}'
                '"  ตามตำแหน่งข้างบน\n'
                "2. ส่งไฟล์ diagnostic ให้แอดมิน:\n"
            )

        broken_reason: str | None = None
        if not adb_path or (
            adb_path.is_absolute() and not adb_path.is_file()
        ):
            broken_reason = (
                f"ไม่พบ {adb_name} ที่ตำแหน่ง:\n  {adb_path}"
            )
        elif not self.adb.is_available():
            broken_reason = f"พบ {adb_name}{cause_hint}"

        if broken_reason is None:
            return

        from tkinter import messagebox

        from ..config import DATA_ROOT
        from .._startup_diagnostic import DIAGNOSTIC_FILENAME

        diag_path = DATA_ROOT / "logs" / DIAGNOSTIC_FILENAME

        self._adb_resolution_warned = True
        messagebox.showwarning(
            "NP Create — adb ไม่พร้อมใช้งาน",
            (
                f"ไม่สามารถใช้งาน {adb_name}"
                " ที่บันเดิลมาในโปรแกรมได้ครับ\n"
                "หน้า 'เพิ่มเครื่อง' จะค้างที่ 'รอเครื่อง...' เพราะเหตุนี้\n\n"
                f"{broken_reason}\n\n"
                "วิธีช่วยแก้ไข\n"
                f"{help_steps}"
                f"   {diag_path}\n\n"
                "ติดต่อ Line OA: @npcreate"
            ),
        )

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
        """Manual reconnect from the Dashboard with smart fallback (v1.8.5).

        Three-level recovery — designed around the observation that
        the saved ``wifi_ip`` was captured at *patch time* and may be
        days or weeks old by the time the customer hits this button.
        DHCP renewals on phone reboot/WiFi reconnect routinely flip
        the address (we've seen 6 stale entries in a 7-device library
        from a single customer with the LAN's lease set to 24 h), so
        a single ``adb connect`` against a frozen IP is the wrong
        primitive to ship as the only recovery action.

        Recovery ladder
        ---------------

        1. **Fast path** — ``adb connect <saved_ip>:<port>``. Wins in
           the common case where the phone hasn't rebooted since
           patch (~80 % of "the cable just dropped" reports).

        2. **USB-assisted re-discovery** — if the phone is *also*
           plugged in over USB right now, run the same
           ``setup_wifi_after_patch`` plumbing the wizard uses:
           re-read ``wlan0``'s current IP, re-run ``adb tcpip``,
           and persist the fresh address. Customers patching today
           will almost always fall here when their saved IP is
           stale, because the cable is still plugged in from the
           preceding patch step. One click → fresh IP → connected.

        3. **Adaptive give-up** — track consecutive failures per
           serial. After ``WIFI_RECONNECT_FAIL_LIMIT`` strikes, drop
           the saved ``wifi_ip``. The dashboard refresh then flips
           the button from "เชื่อม WiFi อีกครั้ง" to "ตั้งค่า WiFi",
           which tells the customer in plain terms what they need
           to do next (plug the cable back in for the wizard's
           re-tcpip flow). Without this clearing step, the customer
           would mash the same dead button forever, because every
           ``adb connect`` to a non-existent host takes ~3 s and
           still feels "responsive" — there's no obvious signal that
           the IP itself has aged out.

        Returns ``(ok, thai_msg)`` for the dashboard's status dialog.
        """
        e = self.devices_lib.get(serial)
        if e is None or not e.has_wifi():
            return False, (
                "ยังไม่ได้ตั้งค่า WiFi — เสียบ USB แล้วกด '📶 ตั้งค่า WiFi'"
            )

        # ── 1. Fast path: try the saved IP ────────────────────────
        if wifi_adb.adb_connect(
            self.cfg.adb_path, e.wifi_ip, int(e.wifi_port or 5555),
        ):
            self._wifi_reconnect_fails.pop(serial, None)
            return True, f"✅ เชื่อมต่อ WiFi สำเร็จ ({e.wifi_address()})"

        # ── 2. USB-assisted re-discovery ──────────────────────────
        # If USB transport is up right now, the customer is most
        # likely in "just patched, cable still plugged" territory.
        # Re-running ``adb tcpip`` is cheap (~2 s) and fixes the
        # stale-IP problem at the source instead of just nagging
        # the customer to "reboot phone + plug USB" (the old copy).
        if self.transport_of(serial) == "usb":
            log.info(
                "WiFi reconnect: saved IP %s stale, attempting USB-assisted "
                "re-discovery for %s",
                e.wifi_ip, serial,
            )
            stale_ip = e.wifi_ip
            msg = self.setup_wifi_after_patch(serial)
            # ``setup_wifi_after_patch`` returns a Thai status string
            # whose first emoji classifies the outcome:
            #   ✅  full success (re-tcpip + adb connect verified)
            #   📶  partial   (got fresh IP but not yet connected -
            #                  cable still holding adbd in USB mode)
            #   ⚠️  failure   (couldn't read wlan0 / tcpip rc!=0)
            if msg.startswith("✅"):
                self._wifi_reconnect_fails.pop(serial, None)
                # Surface whatever fresh IP we actually landed on,
                # not the (now-stale) IP the user clicked from.
                refreshed = self.devices_lib.get(serial)
                addr = (
                    refreshed.wifi_address()
                    if refreshed is not None and refreshed.has_wifi()
                    else stale_ip
                )
                return True, (
                    f"✅ เชื่อม WiFi สำเร็จที่ IP ใหม่ ({addr})\n"
                    f"(IP เก่า {stale_ip} ใช้ไม่ได้แล้ว — ระบบอัปเดตให้เรียบร้อย)"
                )
            # Partial / fail — bubble the diagnostic up to the
            # customer, but DON'T increment the fail counter:
            # ``setup_wifi_after_patch`` already persisted whatever
            # progress it made (e.g. fresher IP, mark_seen_via), so
            # the next click has a real chance of succeeding without
            # us nuking the saved IP prematurely.
            return False, msg

        # ── 3. Adaptive give-up: increment fail counter ───────────
        fails = self._wifi_reconnect_fails.get(serial, 0) + 1
        self._wifi_reconnect_fails[serial] = fails

        if fails >= self.WIFI_RECONNECT_FAIL_LIMIT:
            # Clear stale IP so the next dashboard refresh shows
            # the "ตั้งค่า WiFi" button instead, and reset the
            # counter for whenever the customer plugs the cable
            # back in to re-pair.
            self.devices_lib.clear_wifi(serial)
            self.save_devices()
            self._wifi_reconnect_fails.pop(serial, None)
            log.info(
                "WiFi reconnect: cleared stale IP for %s after %d failures",
                serial, fails,
            )
            return False, (
                f"❌ เชื่อม {e.wifi_address()} ไม่สำเร็จ {fails} ครั้งติด "
                "— ลบ IP เก่าออกแล้ว\n\n"
                "วิธีต่อ: เสียบสาย USB → กดปุ่ม '📶 ตั้งค่า WiFi' "
                "(ปุ่มจะปรากฏแทนที่ปุ่มนี้)"
            )

        return False, (
            f"❌ เชื่อม {e.wifi_address()} ไม่สำเร็จ "
            f"({fails}/{self.WIFI_RECONNECT_FAIL_LIMIT})\n\n"
            "วิธีแก้:\n"
            "1. เสียบสาย USB เครื่องนี้\n"
            "2. กดปุ่มนี้อีกครั้ง — ระบบจะอ่าน IP ใหม่ให้อัตโนมัติ\n\n"
            "หรือเช็คว่าโทรศัพท์อยู่ LAN เดียวกับคอม (อาจ DHCP เปลี่ยน IP "
            "หลัง phone reboot / WiFi reconnect)"
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

    def device_local_mp4(self, serial: str) -> Path:
        """Per-serial MP4 cache path (v1.8.6+).

        Two devices encoding at the same time get different output
        files so libx264 can't race on the same descriptor — that
        was the silent-corruption hazard that capped the v1.8.5
        dashboard at one task at a time. Mirrors
        ``hook_mode.default_local_mp4``'s ``cache/`` directory but
        with a serial-derived filename suffix.
        """
        return device_local_mp4(self.cfg, serial)

    # ── persistence ──────────────────────────────────────────────

    def save_devices(self) -> None:
        try:
            self.devices_lib.save()
        except Exception:
            log.exception("failed to persist devices.json")

    # ── shutdown ─────────────────────────────────────────────────

    def _on_close(self) -> None:
        # Cancel every in-flight encode/push BEFORE any other
        # teardown so the pipeline's polling loops see the event
        # and ``proc.kill()`` their ffmpeg / adb children. Without
        # this step, daemon worker threads die when ``destroy()``
        # exits the Tk mainloop but their subprocess children
        # outlive the parent process — customers ended up with
        # ghost ffmpeg encoders pinning a CPU core and orphaned
        # ``adb push`` transfers blocking the next session's USB.
        # See encode_push_runner cancellation contract.
        try:
            cancelled = self.encode_tasks.cancel_all_running()
        except Exception:
            log.exception("cancel_all_running failed during close")
            cancelled = 0
        if cancelled:
            log.info(
                "shutdown: signalled %d encode/push task(s) to cancel",
                cancelled,
            )
            # Brief, bounded wait for daemon threads to drain. We
            # do NOT block forever — if a worker is genuinely stuck
            # we'd rather close the window than freeze the customer's
            # exit. 3 s is enough for ffmpeg / adb to receive SIGKILL
            # and for the runner thread to mark STATE_CANCELLED.
            deadline = time.monotonic() + 3.0
            drained = False
            while time.monotonic() < deadline:
                still_running = any(
                    t.is_running() for t in self.encode_tasks.snapshot()
                )
                if not still_running:
                    drained = True
                    break
                time.sleep(0.1)
            if not drained:
                log.warning(
                    "shutdown: %d encode/push task(s) still running "
                    "after 3s grace; closing anyway",
                    sum(
                        1 for t in self.encode_tasks.snapshot()
                        if t.is_running()
                    ),
                )

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
        # Apply prefetched patch right before destroying the window
        # so the *next* launch boots on the new version. The customer
        # gets to skip the explicit "อัปเดตเลย" click when they
        # opted in to install-on-close in Settings.
        try:
            self._apply_update_on_close_if_ready()
        except Exception:
            log.exception("install-on-close hook")
        self.save_devices()
        self.destroy()

    def _apply_update_on_close_if_ready(self) -> None:
        """Apply a prefetched patch on close when the customer
        opted in via Settings → "ติดตั้งตอนปิดโปรแกรม". Returns
        silently if the toggle is off, no manifest is in hand, no
        cached zip is on disk, or the cached zip is the wrong sha
        (``find_cached_patch`` handles the sha check + cleanup).
        """
        try:
            from .. import auto_update, update_prefs
        except Exception:
            log.exception("install-on-close: prefs load failed")
            return
        try:
            prefs = update_prefs.UpdatePrefs.load()
        except Exception:
            log.exception("install-on-close: prefs load failed")
            return
        if not prefs.install_on_close:
            return

        manifest = getattr(self, "_latest_update", None)
        if manifest is None:
            return
        if getattr(manifest, "kind", None) != "source":
            return

        try:
            cached = auto_update.find_cached_patch(manifest)
        except Exception:
            log.exception("install-on-close: find_cached_patch failed")
            return

        if cached is None:
            log.info(
                "install-on-close: no cached patch for v%s — skipping",
                manifest.version,
            )
            return

        log.info(
            "install-on-close: applying prefetched patch v%s",
            manifest.version,
        )
        try:
            auto_update.apply_patch(cached)
        except Exception:
            log.exception("install-on-close: apply_patch failed")
            return
        log.info(
            "install-on-close: applied — next launch will run v%s",
            manifest.version,
        )
