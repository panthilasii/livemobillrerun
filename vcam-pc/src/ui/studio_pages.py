"""NP Create — page widgets.

Four pages live in this module:

* :class:`ActivationPage`   — license-key entry, gate before everything else.
* :class:`DashboardPage`    — main UI: sidebar of devices + per-device controls.
* :class:`WizardPage`       — guided "add a new phone" flow.
* :class:`SettingsPage`     — license info, sign-out, language.

Each page is a ``CTkFrame`` and lives only as long as
``StudioApp.show_page`` keeps it alive. Pages must never store
references to other pages or to widgets that have already been
destroyed; route via ``self.app.go_xxx()`` instead.

Conventions
-----------

* All user-visible strings are Thai by default. We don't go through
  the ``i18n.T`` helper here because the legacy GUI uses it and we
  want to keep the customer-facing copy independent (so localised
  proofreading doesn't touch the developer panel).
* Backend work that takes more than ~100 ms (encode, push, patch)
  runs on a worker thread; the worker re-enters the Tk loop via
  ``self.after(0, …)`` to update widgets safely.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import webbrowser
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image

from ..branding import BRAND, THEME
from ..customer_devices import DeviceEntry
from ..hook_mode import human_bytes
from ..license_key import (
    LicenseError,
    clear_activation,
    save_activation,
    verify_key,
)
from ..playlist import write_playlist

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
#  shared widget helpers
# ──────────────────────────────────────────────────────────────────


def _h1(parent, text: str, **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        parent,
        text=text,
        text_color=THEME.fg_primary,
        font=ctk.CTkFont(size=28, weight="bold"),
        **kw,
    )


def _h2(parent, text: str, **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        parent,
        text=text,
        text_color=THEME.fg_primary,
        font=ctk.CTkFont(size=18, weight="bold"),
        **kw,
    )


def _muted(parent, text: str, **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        parent,
        text=text,
        text_color=THEME.fg_muted,
        font=ctk.CTkFont(size=12),
        **kw,
    )


def _body(parent, text: str, **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        parent,
        text=text,
        text_color=THEME.fg_secondary,
        font=ctk.CTkFont(size=13),
        **kw,
    )


# Cache CTkImage objects so we don't re-decode the logo PNG every
# time a page is rebuilt (re-decoding on every redraw causes UI lag
# on slower laptops and gradual memory growth).
_LOGO_CACHE: dict[tuple[int, int], "ctk.CTkImage"] = {}


def _logo(size: int = 96) -> "ctk.CTkImage | None":
    """Return a CTkImage of the brand logo at ``size`` × ``size``.

    Falls back to ``None`` if the asset is missing (in which case the
    caller should display the plain text branding) so a corrupted
    install never crashes the activation page.
    """
    key = (size, size)
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    # Pick the smallest pre-rendered size ≥ requested to keep things
    # crisp on HiDPI without paying the full 1024² decode cost.
    candidates = [
        (64, BRAND.logo_64_path),
        (128, BRAND.logo_128_path),
        (256, BRAND.logo_256_path),
        (1024, BRAND.logo_path),
    ]
    chosen = next(
        (p for sz, p in candidates if sz >= size and p.is_file()),
        BRAND.logo_path if BRAND.logo_path.is_file() else None,
    )
    if not chosen:
        return None
    try:
        img = Image.open(chosen).convert("RGBA")
    except Exception as e:
        log.warning("logo load failed: %s", e)
        return None
    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    _LOGO_CACHE[key] = ctk_img
    return ctk_img


def _primary_button(parent, text: str, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        fg_color=THEME.primary,
        hover_color=THEME.primary_hover,
        text_color=THEME.fg_primary,
        font=ctk.CTkFont(size=14, weight="bold"),
        height=42,
        corner_radius=8,
        **kw,
    )


def _ghost_button(parent, text: str, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        fg_color=THEME.bg_card,
        hover_color=THEME.bg_hover,
        text_color=THEME.fg_secondary,
        border_color=THEME.border,
        border_width=1,
        font=ctk.CTkFont(size=13),
        height=36,
        corner_radius=8,
        **kw,
    )


def _danger_button(parent, text: str, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        fg_color="transparent",
        hover_color=THEME.bg_hover,
        text_color=THEME.danger,
        border_color=THEME.danger,
        border_width=1,
        font=ctk.CTkFont(size=13),
        height=36,
        corner_radius=8,
        **kw,
    )


def _card(parent, **kw) -> ctk.CTkFrame:
    return ctk.CTkFrame(
        parent,
        fg_color=THEME.bg_card,
        corner_radius=12,
        border_width=1,
        border_color=THEME.border,
        **kw,
    )


# ──────────────────────────────────────────────────────────────────
#  ActivationPage
# ──────────────────────────────────────────────────────────────────


class ActivationPage(ctk.CTkFrame):
    """First screen — gate the whole app behind a valid license."""

    def __init__(self, app, error: str | None = None) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app
        self.error_text = error

        # Center vertically + horizontally with a single grid child.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=0, column=0)

        # Brand mark — real logo if available, fall back to a glyph
        # so the activation page still works on a corrupted install.
        logo = _logo(140)
        if logo is not None:
            ctk.CTkLabel(wrap, text="", image=logo).pack(pady=(0, 4))
        else:
            ctk.CTkLabel(
                wrap, text="🎬", text_color=THEME.primary,
                font=ctk.CTkFont(size=56),
            ).pack(pady=(0, 8))
        _h1(wrap, BRAND.name).pack()
        _muted(wrap, BRAND.tagline_en).pack(pady=(2, 2))
        _muted(wrap, BRAND.company_th).pack(pady=(0, 24))

        # License entry card
        card = _card(wrap)
        card.pack(padx=20, pady=10, fill="x")

        ctk.CTkLabel(
            card,
            text="กรอก License Key ที่ได้รับจากแอดมิน",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(padx=20, pady=(20, 6), anchor="w")

        self.entry_var = ctk.StringVar()
        self.entry = ctk.CTkEntry(
            card,
            textvariable=self.entry_var,
            placeholder_text=f"{BRAND.license_prefix}-XXXX-XXXX-XXXX-…",
            width=520,
            height=42,
            font=ctk.CTkFont(family="Menlo", size=13),
            fg_color=THEME.bg_input,
            border_color=THEME.border,
            text_color=THEME.fg_primary,
        )
        self.entry.pack(padx=20, pady=4, fill="x")
        self.entry.bind("<Return>", lambda _e: self._on_activate())

        # Error / status label
        self.status_var = ctk.StringVar(value=error or "")
        self.status_label = ctk.CTkLabel(
            card,
            textvariable=self.status_var,
            text_color=THEME.danger,
            font=ctk.CTkFont(size=12),
        )
        self.status_label.pack(padx=20, pady=(4, 6), anchor="w")

        _primary_button(
            card,
            "  ✓   เปิดใช้งาน",
            command=self._on_activate,
        ).pack(padx=20, pady=(6, 20), fill="x")

        # Footer: contact admin
        footer = ctk.CTkFrame(wrap, fg_color="transparent")
        footer.pack(pady=(20, 0))

        _muted(footer, "ยังไม่มี License?").pack(side="left", padx=(0, 6))
        link = ctk.CTkLabel(
            footer,
            text=f"ติดต่อแอดมินทาง Line: {BRAND.line_oa}",
            text_color=THEME.primary,
            cursor="hand2",
            font=ctk.CTkFont(size=12, underline=True),
        )
        link.pack(side="left")
        link.bind("<Button-1>", lambda _e: webbrowser.open(BRAND.contact_url))

        _muted(
            wrap,
            f"v{BRAND.version}  ·  เวลาทำการ: {BRAND.support_hours}",
        ).pack(pady=(20, 0))

        # Pre-fill if we got here from a stored-but-expired activation
        if app.activation:
            self.entry_var.set(app.activation.get("license_key", ""))

        self.entry.focus_set()

    def _on_activate(self) -> None:
        key = self.entry_var.get().strip()
        if not key:
            self._show_error("กรุณากรอก License Key")
            return
        try:
            v = verify_key(key)
        except LicenseError as e:
            self._show_error(f"License ไม่ถูกต้อง: {e}")
            return
        if v.is_expired:
            self._show_error(
                f"License หมดอายุเมื่อ {v.expiry.isoformat()} "
                f"— กรุณาต่ออายุกับแอดมิน"
            )
            return
        # Persist and route into the dashboard.
        save_activation(key)
        self.app.activation = {"license_key": key}
        self.app.license = v
        log.info(
            "license activated: customer=%r devices=%d expires=%s",
            v.customer, v.max_devices, v.expiry.isoformat(),
        )
        self.app.go_dashboard()

    def _show_error(self, msg: str) -> None:
        self.status_var.set(msg)
        self.status_label.configure(text_color=THEME.danger)


# ──────────────────────────────────────────────────────────────────
#  DashboardPage
# ──────────────────────────────────────────────────────────────────


class DashboardPage(ctk.CTkFrame):
    """Two-column layout: sidebar (devices) + main (selected-device controls)."""

    SIDEBAR_W = 260

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app

        # Lay the page out as a 2-column grid so the sidebar stays
        # fixed while the main area grows with the window.
        self.grid_columnconfigure(0, minsize=self.SIDEBAR_W, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()

        # Initial selection: first online device, else first known one.
        self._auto_select()
        self._refresh_main()

    # ── sidebar ──────────────────────────────────────────────────

    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(
            self,
            fg_color=THEME.bg_sidebar,
            corner_radius=0,
        )
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(2, weight=1)
        self.side = side

        # Header — small logo + product name, side by side.
        head = ctk.CTkFrame(side, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 4))
        title_row = ctk.CTkFrame(head, fg_color="transparent")
        title_row.pack(anchor="w")
        logo_small = _logo(28)
        if logo_small is not None:
            ctk.CTkLabel(title_row, text="", image=logo_small).pack(
                side="left", padx=(0, 8),
            )
        ctk.CTkLabel(
            title_row,
            text=BRAND.short_name,
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(side="left")

        v = self.app.license
        cap = v.max_devices if v else 0
        used = self.app.devices_lib.count()
        days = v.days_left if v else 0
        self.sidebar_meta = ctk.CTkLabel(
            head,
            text=f"License: {used}/{cap} เครื่อง · เหลือ {days} วัน",
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=11),
        )
        self.sidebar_meta.pack(anchor="w", pady=(2, 0))

        ctk.CTkFrame(side, fg_color=THEME.divider, height=1).grid(
            row=1, column=0, sticky="ew", padx=8, pady=8
        )

        # Device list (scrollable)
        self.devices_scroll = ctk.CTkScrollableFrame(
            side,
            fg_color="transparent",
            scrollbar_button_color=THEME.bg_hover,
            scrollbar_button_hover_color=THEME.primary_dim,
        )
        self.devices_scroll.grid(row=2, column=0, sticky="nsew", padx=6)

        # Footer buttons
        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.grid(row=3, column=0, sticky="ew", padx=14, pady=10)

        self.btn_add_device = _primary_button(
            foot,
            "+ เพิ่มเครื่อง",
            command=self._on_add_device,
        )
        self.btn_add_device.pack(fill="x", pady=(0, 8))

        _ghost_button(
            foot,
            "⚙️  ตั้งค่า",
            command=self.app.go_settings,
        ).pack(fill="x", pady=2)

        # ── Quick "restart adb" — v1.8.1 customer ask.
        # Returning customers regularly hit the symptom "phone
        # plugged in but every device card on the dashboard says
        # offline" because some other process (scrcpy / Vysor /
        # Android Studio left running in the background, or the
        # OS USB stack after sleep/wake) is holding adb's port-5037
        # daemon hostage. The wizard already has a 🔄 button buried
        # in step 1, but customers in this state aren't *trying* to
        # add a device — they're trying to use the dashboard with
        # one they already onboarded. So put the same action one
        # click away from the very page where the "everything
        # offline" symptom presents.
        self.btn_restart_adb = _ghost_button(
            foot,
            "🔄  รีสตาร์ท ADB",
            command=self._on_restart_adb,
        )
        self.btn_restart_adb.pack(fill="x", pady=2)

        # ── Dashboard launcher
        # Spins up the FastAPI server (lazy import + lazy start so a
        # missing fastapi dependency doesn't crash startup) and opens
        # the customer's default browser. Idempotent: if the server's
        # already running we just re-open the browser tab.
        _ghost_button(
            foot,
            "📊  Dashboard ยอดขาย",
            command=self._on_open_dashboard,
        ).pack(fill="x", pady=2)

        # Admin-only: license-issuer console. Hidden on customer
        # builds (no .private_key on disk), so the customer never
        # sees a button they can't use.
        if self.app.is_admin:
            _ghost_button(
                foot,
                "🔑  ออกคีย์ลูกค้า",
                command=self.app.go_admin,
            ).pack(fill="x", pady=2)

        _ghost_button(
            foot,
            f"💬  ติดต่อแอดมิน ({BRAND.line_oa})",
            command=lambda: webbrowser.open(BRAND.contact_url),
        ).pack(fill="x", pady=2)

        self._device_buttons: dict[str, ctk.CTkButton] = {}
        self._refresh_sidebar()

    def _refresh_sidebar(self) -> None:
        # Wipe and rebuild the device rows. Cheap — at most a few
        # devices per customer (license cap).
        for w in self.devices_scroll.winfo_children():
            w.destroy()
        self._device_buttons.clear()

        entries = self.app.devices_lib.list()
        if not entries:
            ctk.CTkLabel(
                self.devices_scroll,
                text="ยังไม่มีเครื่อง\nกด '+ เพิ่มเครื่อง' เพื่อเริ่ม",
                text_color=THEME.fg_muted,
                justify="center",
                font=ctk.CTkFont(size=12),
            ).pack(pady=40)
            return

        sel = self.app.selected_serial
        for e in entries:
            self._build_device_row(e, selected=(e.serial == sel))

        # Update license meta in case device count changed
        v = self.app.license
        if v is not None:
            self.sidebar_meta.configure(
                text=(
                    f"License: {self.app.devices_lib.count()}/{v.max_devices} "
                    f"เครื่อง · เหลือ {v.days_left} วัน"
                )
            )

        # Disable [+ เพิ่มเครื่อง] when at cap.
        if v is not None and not self.app.devices_lib.can_add_more(v.max_devices):
            self.btn_add_device.configure(
                state="disabled",
                text=f"+ เพิ่มเครื่อง (ครบโควต้า {v.max_devices})",
            )
        else:
            self.btn_add_device.configure(state="normal", text="+ เพิ่มเครื่อง")

    def _build_device_row(self, entry: DeviceEntry, *, selected: bool) -> None:
        online = self.app.is_online(entry.serial)
        bg = THEME.primary_dim if selected else THEME.bg_sidebar
        hover_bg = THEME.primary if selected else THEME.bg_hover

        # Why a CTkFrame instead of CTkButton?
        # ------------------------------------
        # We tried CTkButton + placed children, but CTkButton
        # paints a hover/click canvas on top of its content, so
        # ``<Button-1>`` events on the placed CTkLabel children
        # got swallowed and the row felt unclickable on the text.
        # A CTkFrame gives us full control: we bind the click on
        # every widget in the row (frame + dot + title + sub) to
        # the same handler, and emulate the hover effect by
        # toggling fg_color in <Enter>/<Leave>.
        row = ctk.CTkFrame(
            self.devices_scroll,
            fg_color=bg,
            corner_radius=8,
            height=58,
        )
        row.pack(fill="x", pady=2, padx=2)
        row.pack_propagate(False)
        self._device_buttons[entry.serial] = row

        # Click handlers — bound on every child, so wherever the
        # user lands on the row the selection still switches.
        click_pick = lambda _e=None, s=entry.serial: self._on_pick_device(s)

        def _on_enter(_e=None, w=row, c=hover_bg):
            try:
                w.configure(fg_color=c)
            except Exception:
                pass

        def _on_leave(_e=None, w=row, c=bg):
            try:
                w.configure(fg_color=c)
            except Exception:
                pass

        for evt, fn in (
            ("<Button-1>", click_pick),
            ("<Enter>", _on_enter),
            ("<Leave>", _on_leave),
        ):
            row.bind(evt, fn)

        # We render a custom layout: a status dot + two lines of
        # label. ``inner`` is anchored to the left side of the row.
        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.place(relx=0, rely=0.5, x=10, anchor="w")

        dot = ctk.CTkLabel(
            inner,
            text="●",
            text_color=THEME.online_dot if online else THEME.offline_dot,
            font=ctk.CTkFont(size=14),
        )
        dot.grid(row=0, column=0, rowspan=2, padx=(0, 8))

        title_lbl = ctk.CTkLabel(
            inner,
            text=entry.display_name(),
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        title_lbl.grid(row=0, column=1, sticky="w")

        # When the user hasn't set a label yet, two phones with the
        # same model would render identical title+sub. Append the
        # last 4 chars of the serial so they're at least
        # distinguishable until "บัญชี A" / "บัญชี B" is filled in.
        if entry.label:
            sub = entry.model or entry.serial[:12]
        else:
            tail = entry.serial[-4:] if entry.serial else ""
            sub = f"{entry.model or 'Phone'} · …{tail}"
        if online:
            transport = self.app.transport_of(entry.serial)
            if transport == "wifi":
                sub = f"{sub} · 📶 WiFi"
            elif transport == "usb":
                sub = f"{sub} · 🔌 USB"
        else:
            sub = f"{sub} · offline"
        # Live indicator: append "🔴 LIVE MM:SS" to the sub-line so
        # the customer can see at a glance which phones are
        # broadcasting without clicking through. The duration is
        # cheap to compute (timestamp diff) so we re-render it
        # every refresh -- the dashboard's 1 s tick covers
        # propagation. We deliberately don't dim offline live
        # entries: a phone that went off-network mid-broadcast
        # is exactly the case the customer should see flagged.
        if entry.is_live():
            from .. import live_control as _lc
            elapsed = _lc.format_elapsed(entry.live_elapsed_seconds())
            sub = f"{sub} · 🔴 LIVE {elapsed}"
        # Drift badge: TikTok auto-updated and we already warned the
        # customer this session. Surface a small ⚠️ until they
        # Re-Patch so the alarming state stays visible without
        # spawning another modal.
        if entry.tiktok_drift_warned_at:
            sub = f"{sub} · ⚠️ TikTok updated"
        sub_lbl = ctk.CTkLabel(
            inner,
            text=sub,
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=10),
            anchor="w",
        )
        sub_lbl.grid(row=1, column=1, sticky="w")

        # Bind click + hover propagation on every visible child too.
        # Hover events are tricky because moving the cursor between
        # parent and child fires ``<Leave>`` on the parent but not
        # ``<Enter>`` on the child for our colour purposes — we want
        # the row's bg colour to track the cursor across the whole
        # row, so we re-apply <Enter>/<Leave> from each child too.
        for w in (inner, dot, title_lbl, sub_lbl):
            for evt, fn in (
                ("<Button-1>", click_pick),
                ("<Enter>", _on_enter),
                ("<Leave>", _on_leave),
            ):
                w.bind(evt, fn)
        # Cursor hint so users see the row is clickable.
        try:
            row.configure(cursor="hand2")
            for w in (inner, dot, title_lbl, sub_lbl):
                w.configure(cursor="hand2")
        except Exception:
            pass

    # ── main panel ───────────────────────────────────────────────

    def _build_main(self) -> None:
        main = ctk.CTkScrollableFrame(
            self,
            fg_color=THEME.bg_main,
            scrollbar_button_color=THEME.bg_hover,
            scrollbar_button_hover_color=THEME.primary_dim,
        )
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        self.main = main

        # Auto-update banner -- the very top slot. We show it ABOVE
        # announcements because a missing update is the only piece
        # of UI that the customer can act on themselves to make the
        # app behave better right now. Server announcements are
        # informational; updates are interactive.
        self._build_update_banner(main)

        # Announcement banner -- below updates. Hidden by default;
        # ``app.announcements`` (background poller) calls
        # ``show_announcement`` whenever there's something the
        # customer should see (TikTok updates, scheduled outages,
        # new features). All UI logic for the banner lives in
        # ``_build_announcement_banner`` so it can be unit-tested
        # in isolation later.
        self._build_announcement_banner(main)

        # Header card (device label + status + connection)
        self.header_card = _card(main)
        self.header_card.grid(row=2, column=0, sticky="ew", padx=20, pady=(20, 10))
        self.header_card.grid_columnconfigure(0, weight=1)

        # Title row: the device's display name + a tiny rename pencil
        # next to it. Customers running 3+ phones at once frequently
        # have identical models (e.g. three Redmi 14C "23106RN0DA")
        # and otherwise can't tell the device cards apart in the
        # sidebar. The pencil lets them tag each phone with a free-text
        # nickname like "บัญชี A" / "ทดลอง" / "บอส".
        title_row = ctk.CTkFrame(self.header_card, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 0))
        title_row.grid_columnconfigure(0, weight=1)

        self.lbl_device_title = _h2(title_row, "—")
        self.lbl_device_title.grid(row=0, column=0, sticky="w")

        self.btn_rename_device = ctk.CTkButton(
            title_row,
            text="✏️ ตั้งชื่อเครื่อง",
            width=130,
            height=28,
            corner_radius=6,
            fg_color="transparent",
            hover_color=THEME.bg_hover,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=11),
            border_width=1,
            border_color=THEME.border,
            command=self._on_rename_device,
        )
        self.btn_rename_device.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.lbl_device_status = _muted(self.header_card, "—")
        self.lbl_device_status.grid(row=1, column=0, sticky="w", padx=20, pady=(2, 4))

        # Connection sub-row: 🔌 USB / 📶 WiFi badge + ip:port + reconnect button.
        # We pack instead of grid here so the reconnect button can hug
        # the right side without inheriting the card's column weight.
        conn_row = ctk.CTkFrame(self.header_card, fg_color="transparent")
        conn_row.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 14))
        conn_row.grid_columnconfigure(0, weight=1)

        self.lbl_conn_info = _muted(conn_row, "")
        self.lbl_conn_info.grid(row=0, column=0, sticky="w")

        # Two mutually-exclusive WiFi buttons live in this slot:
        #
        # 1. "ตั้งค่า WiFi" — for USB devices that have *never* had WiFi
        #    enabled. Runs `adb tcpip 5555` + records the IP/port,
        #    same flow that runs automatically right after the patch
        #    Wizard finishes. Customers who patched on an older
        #    NP Create build (pre-WiFi-auto) need this.
        #
        # 2. "เชื่อม WiFi อีกครั้ง" — for devices we *already know*
        #    the WiFi address of, but the connection dropped (router
        #    reboot, phone changed networks, etc.). Just re-runs
        #    `adb connect <ip>:<port>`.
        #
        # Showing both at once would confuse the customer, so
        # ``_refresh_main`` decides which one (if any) to grid-place.
        self.btn_setup_wifi = _primary_button(
            conn_row, "📶  ตั้งค่า WiFi",
            command=self._on_setup_wifi,
            width=180,
        )
        self.btn_setup_wifi.grid(row=0, column=1, sticky="e")
        self.btn_setup_wifi.grid_remove()

        self.btn_reconnect_wifi = _ghost_button(
            conn_row, "📶  เชื่อม WiFi อีกครั้ง",
            command=self._on_reconnect_wifi,
            width=180,
        )
        self.btn_reconnect_wifi.grid(row=0, column=1, sticky="e")

        # Hook-status row -- "🟢 vcam ทำงานอยู่ / 🟡 รอเปิด TikTok /
        # 🔴 ยังไม่ Patch". This is the only piece of UI that tells
        # the customer with confidence whether their next "Go Live"
        # tap will use the vcam pipeline; without it they swipe to
        # Live and only find out it's broken when the preview is
        # black. Probed in a worker thread (adb shell calls) and
        # cached for 8 s to keep the dashboard refresh cheap.
        hook_row = ctk.CTkFrame(self.header_card, fg_color="transparent")
        hook_row.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 14))
        hook_row.grid_columnconfigure(0, weight=1)

        self.lbl_hook_status = ctk.CTkLabel(
            hook_row,
            text="กำลังตรวจสอบสถานะ vcam...",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            anchor="w", justify="left",
        )
        self.lbl_hook_status.grid(row=0, column=0, sticky="w")

        self.btn_hook_refresh = ctk.CTkButton(
            hook_row, text="🔄  ตรวจซ้ำ",
            fg_color="transparent",
            hover_color=THEME.bg_hover,
            text_color=THEME.fg_secondary,
            border_width=1,
            border_color=THEME.border,
            corner_radius=6,
            width=110, height=24,
            font=ctk.CTkFont(size=11),
            command=lambda: self._refresh_hook_status(force=True),
        )
        self.btn_hook_refresh.grid(row=0, column=1, sticky="e")

        # Throttle + cache: serial → (last_probe_monotonic, status).
        # Worker thread inflight flag prevents stacking probes when
        # the customer mashes the refresh button.
        self._hook_status_cache: dict[str, tuple[float, object]] = {}
        self._hook_status_inflight: bool = False

        # Video card
        vid = _card(main)
        vid.grid(row=3, column=0, sticky="ew", padx=20, pady=8)
        vid.grid_columnconfigure(0, weight=1)
        self.video_card = vid

        ctk.CTkLabel(
            vid, text="📁  คลิปปัจจุบัน",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))

        self.lbl_video_path = _body(vid, "(ยังไม่ได้เลือกคลิป)", anchor="w", justify="left")
        self.lbl_video_path.grid(row=1, column=0, sticky="ew", padx=20)

        self.lbl_video_meta = _muted(vid, "")
        self.lbl_video_meta.grid(row=2, column=0, sticky="w", padx=20, pady=(2, 8))

        _ghost_button(
            vid, "เปลี่ยนคลิป...",
            command=self._on_pick_video,
        ).grid(row=3, column=0, sticky="w", padx=20, pady=(0, 16))

        # Rotation card
        rot = _card(main)
        rot.grid(row=4, column=0, sticky="ew", padx=20, pady=8)
        rot.grid_columnconfigure(0, weight=1)
        self.rotation_card = rot

        ctk.CTkLabel(
            rot, text="🔄  ทิศทางภาพ",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))

        rrow = ctk.CTkFrame(rot, fg_color="transparent")
        rrow.grid(row=1, column=0, sticky="w", padx=20, pady=(2, 16))

        self.rotation_var = ctk.IntVar(value=0)
        for i, deg in enumerate((0, 90, 180, 270)):
            ctk.CTkRadioButton(
                rrow,
                text=f"{deg}°",
                variable=self.rotation_var,
                value=deg,
                command=self._on_rotation_change,
                fg_color=THEME.primary,
                hover_color=THEME.primary_hover,
                text_color=THEME.fg_secondary,
                font=ctk.CTkFont(size=13),
            ).grid(row=0, column=i, padx=(0, 18), sticky="w")

        self.mirror_h_var = ctk.BooleanVar(value=False)
        self.mirror_v_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            rrow, text="กระจกแนวนอน",
            variable=self.mirror_h_var,
            command=self._on_rotation_change,
            fg_color=THEME.primary,
            hover_color=THEME.primary_hover,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=13),
        ).grid(row=0, column=4, padx=(20, 12), sticky="w")
        ctk.CTkCheckBox(
            rrow, text="กระจกแนวตั้ง",
            variable=self.mirror_v_var,
            command=self._on_rotation_change,
            fg_color=THEME.primary,
            hover_color=THEME.primary_hover,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=13),
        ).grid(row=0, column=5, sticky="w")

        # Audio card — separate audio file overrides the MP4's audio.
        aud = _card(main)
        aud.grid(row=5, column=0, sticky="ew", padx=20, pady=8)
        aud.grid_columnconfigure(0, weight=1)
        self.audio_card = aud

        ctk.CTkLabel(
            aud, text="🎵  ไฟล์เสียง (ทับเสียงในคลิป)",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))

        _muted(
            aud,
            "เลือกไฟล์เสียง (MP3 / WAV / M4A / AAC / OGG) ระบบจะส่งไปไว้ที่ "
            "📁 Music ของโทรศัพท์ — เปิดด้วยแอปเล่นเพลงพื้นหลัง (Mi Music / "
            "Spotify Local / VLC) ตั้ง Loop แล้ววางลำโพงใกล้ๆ ตอนไลฟ์",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 6))

        self.lbl_audio_path = _body(
            aud, "(ใช้เสียงจากคลิปวีดีโอ)",
            anchor="w", justify="left",
        )
        self.lbl_audio_path.grid(row=2, column=0, sticky="ew", padx=20)

        self.lbl_audio_meta = _muted(aud, "")
        self.lbl_audio_meta.grid(row=3, column=0, sticky="w", padx=20, pady=(2, 8))

        aud_btns = ctk.CTkFrame(aud, fg_color="transparent")
        aud_btns.grid(row=4, column=0, sticky="w", padx=20, pady=(0, 8))

        _ghost_button(
            aud_btns, "เลือกไฟล์เสียง...",
            command=self._on_pick_audio,
        ).pack(side="left", padx=(0, 8))

        self.btn_audio_push = _primary_button(
            aud_btns, "▶  ส่งไฟล์เสียงไปเครื่อง",
            command=self._on_push_audio,
            width=210,
        )
        self.btn_audio_push.pack(side="left", padx=(0, 8))

        self.btn_audio_clear = _danger_button(
            aud_btns, "ลบไฟล์เสียงแยก",
            command=self._on_clear_audio,
        )
        self.btn_audio_clear.pack(side="left")

        self.lbl_audio_status = _muted(aud, "")
        self.lbl_audio_status.grid(row=5, column=0, sticky="w", padx=20, pady=(0, 16))

        # Action card (Encode + Push video)
        act = _card(main)
        act.grid(row=6, column=0, sticky="ew", padx=20, pady=8)
        act.grid_columnconfigure(0, weight=1)
        self.action_card = act

        ctk.CTkLabel(
            act, text="▶️  ส่งคลิปไปเครื่อง",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))

        cfg = self.app.cfg
        # Show landscape encode size as portrait (rotation cancels
        # out on the phone so users see WxH portrait on screen).
        portrait_w = max(2, int(cfg.encode_height or 1080))
        portrait_h = max(2, int(cfg.encode_width or 1920))
        _muted(
            act,
            f"Encode คลิปเป็น MP4 {portrait_w}×{portrait_h} + push เข้าเครื่อง. "
            "TikTok ในโทรศัพท์จะดึงไฟล์นี้ขึ้นไลฟ์.",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 8))

        self.btn_encode_push = _primary_button(
            act, "▶  Encode + Push",
            command=self._on_encode_push,
        )
        self.btn_encode_push.grid(row=2, column=0, sticky="ew", padx=20, pady=4)

        self.progress = ctk.CTkProgressBar(
            act, progress_color=THEME.primary,
            fg_color=THEME.bg_input,
            height=6,
        )
        self.progress.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 4))
        self.progress.set(0.0)

        self.lbl_encode_status = _muted(act, "พร้อม")
        self.lbl_encode_status.grid(row=4, column=0, sticky="w", padx=20, pady=(0, 16))

        # Live-control card -- the customer's daily-driver button.
        # Driving start/stop from the PC saves them walking between
        # 5 phones tapping things on each.
        live_ctrl = _card(main)
        live_ctrl.grid(row=7, column=0, sticky="ew", padx=20, pady=(8, 8))
        live_ctrl.grid_columnconfigure(0, weight=1)
        self.live_ctrl_card = live_ctrl
        self._build_live_control_card(live_ctrl)

        # Open-TikTok / Patch row -- mostly a setup-time concern
        # (first run / rebuilding after a TikTok update). Keep it
        # at the bottom; the live-control card above is the one
        # the customer reaches for daily.
        live = _card(main)
        live.grid(row=8, column=0, sticky="ew", padx=20, pady=(8, 24))
        live.grid_columnconfigure(0, weight=1)
        live.grid_columnconfigure(1, weight=1)
        self.live_card = live

        _primary_button(
            live, "🎬  เปิด TikTok บนเครื่อง",
            command=self._on_open_tiktok,
        ).grid(row=0, column=0, sticky="ew", padx=(20, 6), pady=16)

        self.btn_patch = _ghost_button(
            live, "Patch & ติดตั้ง TikTok",
            command=self._on_patch_tiktok,
        )
        self.btn_patch.grid(row=0, column=1, sticky="ew", padx=(6, 20), pady=16)

        # Forced-update Re-Patch helper.
        # ------------------------------
        # TikTok performs a *server-side* version check at the moment
        # the user taps "เริ่มไลฟ์". If the installed (patched) APK
        # is too old, TikTok refuses to open the broadcast and shows
        # "ต้องอัปเดตเวอร์ชันถึงจะ Live ได้" — the user has no
        # choice but to update. After they update via Play Store,
        # the LSPatch overlay is gone and vcam stops working.
        #
        # The fix is *exactly* what our existing Re-Patch flow does:
        # pull whatever TikTok version is now on the phone (the
        # fresh one from Play Store), patch it with LSPatch, and
        # install the patched copy back. So we surface the same
        # ``_trigger_repatch`` logic behind a guided dialog that
        # walks the customer through the update-then-repatch
        # sequence and answers the obvious "but you told me NOT to
        # update?" confusion in plain Thai.
        self.btn_force_update_repatch = _ghost_button(
            live,
            "🆙  TikTok บังคับ update ก่อนไลฟ์? — กดที่นี่",
            command=self._on_force_update_repatch,
        )
        self.btn_force_update_repatch.configure(
            font=ctk.CTkFont(size=12),
            height=32,
            text_color=THEME.fg_muted,
        )
        self.btn_force_update_repatch.grid(
            row=1, column=0, columnspan=2,
            sticky="ew", padx=20, pady=(0, 16),
        )

    # ── auto-update banner ────────────────────────────────────────

    def _build_update_banner(self, parent: ctk.CTkFrame) -> None:
        """Top-of-dashboard banner that surfaces a downloadable
        new version. Shows the new version label, a Thai-language
        changelog, and a one-click "อัปเดตเลย" button. While the
        download/apply is running we replace the buttons with a
        progress bar and status line; the rest of the app stays
        usable on the current version, so a hung download never
        blocks the customer.

        Visibility is controlled with ``grid_remove`` (the same
        pattern as the announcement banner) so refreshing is just
        ``set_update(manifest)`` -- no layout rewiring.

        Stripe color matches THEME.primary (red) -- it's the same
        visual weight as the announcement banner but always sits
        above so it's the *first* thing the customer sees on
        launch when there's an update waiting. We accept that
        "two banners stacked" looks busy on the rare day we have
        both an announcement AND an update; in practice updates
        bring their own changelog so we usually wouldn't ship a
        separate announcement.
        """
        self.upd_card = ctk.CTkFrame(
            parent,
            fg_color=THEME.bg_card,
            border_color=THEME.primary,
            border_width=2,
            corner_radius=8,
        )
        self.upd_card.grid_columnconfigure(0, weight=1)
        self.upd_card.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 0))
        self.upd_card.grid_remove()

        self.upd_title = ctk.CTkLabel(
            self.upd_card,
            text="🚀 อัปเดตใหม่พร้อมใช้งาน",
            text_color=THEME.primary,
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w", justify="left",
        )
        self.upd_title.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 2))

        self.upd_body = ctk.CTkLabel(
            self.upd_card, text="",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            anchor="w", justify="left",
            wraplength=620,
        )
        self.upd_body.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))

        # Determinate progress bar -- shown only while we're
        # downloading/applying. We hide rather than destroy so the
        # widget stays warm for the next install attempt.
        self.upd_progress = ctk.CTkProgressBar(
            self.upd_card,
            progress_color=THEME.primary,
            fg_color=THEME.bg_input,
            height=6,
        )
        self.upd_progress.set(0.0)
        self.upd_progress.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 4))
        self.upd_progress.grid_remove()

        self.upd_status = ctk.CTkLabel(
            self.upd_card, text="",
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=11),
            anchor="w", justify="left",
        )
        self.upd_status.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 4))
        self.upd_status.grid_remove()

        self.upd_actions = ctk.CTkFrame(self.upd_card, fg_color="transparent")
        self.upd_actions.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 12))

        self.upd_btn_install = ctk.CTkButton(
            self.upd_actions,
            text="⬇️  อัปเดตเลย",
            fg_color=THEME.primary,
            hover_color=THEME.primary_hover,
            text_color="white",
            corner_radius=6,
            width=160, height=30,
            command=self._on_update_install,
        )
        self.upd_btn_install.grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.upd_btn_later = ctk.CTkButton(
            self.upd_actions,
            text="ภายหลัง",
            fg_color="transparent",
            hover_color=THEME.bg_hover,
            text_color=THEME.fg_secondary,
            border_width=1,
            border_color=THEME.border,
            corner_radius=6,
            width=110, height=30,
            command=self._on_update_dismiss,
        )
        self.upd_btn_later.grid(row=0, column=1, sticky="w")

        self._current_update = None  # last UpdateManifest passed in
        self._update_installing = False  # guard against double-click

    def set_update(self, manifest) -> None:
        """Show ``manifest`` (an ``auto_update.UpdateManifest``) in
        the banner. Pass ``None`` to hide it. Called by
        ``StudioApp`` from the Tk thread (the poller trampolines
        through ``after``)."""
        if manifest is None:
            self.upd_card.grid_remove()
            self._current_update = None
            return
        self.upd_title.configure(
            text=f"🚀 อัปเดตใหม่ v{manifest.version} พร้อมใช้งาน",
        )
        notes = (manifest.notes_th or "ปรับปรุงทั่วไป").strip()
        if manifest.kind == "full":
            # Full installer: we can't atomically swap the running
            # .exe / .app, so the right thing is to send the
            # customer to the download page. Make that obvious.
            notes += (
                "\n\n⚠️ เวอร์ชันนี้ต้องดาวน์โหลดตัวติดตั้งใหม่ "
                "(เปลี่ยน toolchain ที่ฝังไว้) — กดปุ่มเพื่อเปิดเว็บ"
            )
            self.upd_btn_install.configure(text="🌐  ดาวน์โหลดตัวติดตั้ง")
        else:
            self.upd_btn_install.configure(text="⬇️  อัปเดตเลย")
        self.upd_body.configure(text=notes)
        self._current_update = manifest
        self.upd_card.grid()

    def _on_update_install(self) -> None:
        m = self._current_update
        if m is None or self._update_installing:
            return
        if m.kind == "full":
            # Open the download page in the customer's browser; the
            # in-app download path can't safely replace the live
            # executable.
            try:
                webbrowser.open(m.download_url)
            except Exception:
                log.exception("could not open update download page")
            return

        # Source-only patch: download + apply in a worker thread.
        self._update_installing = True
        self.upd_btn_install.configure(
            state="disabled", text="กำลังดาวน์โหลด...",
        )
        self.upd_btn_later.configure(state="disabled")
        self.upd_progress.set(0.0)
        self.upd_progress.grid()
        self.upd_status.configure(text="เตรียมดาวน์โหลด...")
        self.upd_status.grid()

        threading.Thread(
            target=self._run_update_install,
            args=(m,),
            name="np-update-apply",
            daemon=True,
        ).start()

    def _run_update_install(self, manifest) -> None:
        """Worker thread: download + verify + extract + restart.

        All UI updates trampolined through ``self.after`` because Tk
        widgets are not safe to touch from a worker thread."""
        from .. import auto_update

        def _on_dl_progress(got: int, total: int) -> None:
            if total > 0:
                pct = max(0.0, min(0.85, got / total * 0.85))
                msg = (
                    f"กำลังดาวน์โหลด {got/1024:.0f} / {total/1024:.0f} KB "
                    f"({pct/0.85*100:.0f}%)"
                )
            else:
                # Server didn't send Content-Length -- pretend we're
                # at 50% so the bar moves a bit but doesn't lie.
                pct = 0.5
                msg = f"กำลังดาวน์โหลด {got/1024:.0f} KB"
            self.after(0, lambda p=pct, m=msg: self._update_set_progress(p, m))

        try:
            self.after(0, lambda: self._update_set_progress(
                0.05, "กำลังดาวน์โหลดแพทช์...",
            ))
            zip_path = auto_update.download_patch(
                manifest, progress_cb=_on_dl_progress,
            )
            self.after(0, lambda: self._update_set_progress(
                0.9, "ติดตั้งแพทช์...",
            ))
            auto_update.apply_patch(zip_path)
            self.after(0, lambda: self._update_set_progress(
                1.0, "เสร็จสิ้น — กำลังรีสตาร์ทโปรแกรม...",
            ))
            # Tk needs a tick to repaint the bar at 100% before we
            # tear down the process; without this, the user sees
            # the bar stuck at ~95% the moment before the relaunch.
            time.sleep(1.2)
            auto_update.relaunch()
        except Exception as exc:
            log.exception("auto-update apply failed")
            err = str(exc) or exc.__class__.__name__
            self.after(0, lambda e=err: self._update_install_failed(e))

    def _update_set_progress(self, pct: float, msg: str) -> None:
        try:
            self.upd_progress.set(max(0.0, min(1.0, float(pct))))
            self.upd_status.configure(text=msg)
        except Exception:
            log.debug("update progress update failed", exc_info=True)

    def _update_install_failed(self, err: str) -> None:
        self._update_installing = False
        self.upd_progress.grid_remove()
        self.upd_status.configure(text=f"❌ ติดตั้งไม่สำเร็จ: {err}")
        self.upd_btn_install.configure(
            state="normal", text="⬇️  ลองอัปเดตอีกครั้ง",
        )
        self.upd_btn_later.configure(state="normal")

    def _on_update_dismiss(self) -> None:
        if self._update_installing:
            return
        # Just hide for this session -- next launch re-checks and
        # shows again if the update is still relevant. We don't
        # persist a "skipped versions" list because customers
        # already paid for support and we want them on the latest
        # version every time.
        self.upd_card.grid_remove()

    # ── announcement banner ───────────────────────────────────────

    def _build_announcement_banner(self, parent: ctk.CTkFrame) -> None:
        """Top-of-dashboard banner for server-pushed news / alerts.

        The banner sits in row=0 of ``main`` (above the device
        header) so anything urgent -- "TikTok updated, please patch
        again", "service maintenance tonight" -- is the first thing
        the customer sees on launch.

        We keep the widget tree always-built but ``grid_remove()``
        it when there's nothing to show; that way refreshing the
        content is just a matter of calling ``set_announcement``
        without juggling layout state.
        """
        self.ann_card = ctk.CTkFrame(
            parent,
            fg_color=THEME.bg_card,
            border_color=THEME.border,
            border_width=1,
            corner_radius=8,
        )
        self.ann_card.grid_columnconfigure(0, weight=1)
        # Hidden until ``set_announcement`` is invoked with a real
        # message. ``grid_remove`` (not destroy) preserves the
        # widget so subsequent shows are flicker-free.
        self.ann_card.grid(row=1, column=0, sticky="ew", padx=20, pady=(20, 0))
        self.ann_card.grid_remove()

        # Severity stripe on the left. We bind its color in
        # ``set_announcement`` so the same widget can render info
        # / warning / critical without us tearing down layout.
        self.ann_stripe = ctk.CTkFrame(
            self.ann_card, fg_color=THEME.primary, width=4, corner_radius=2,
        )
        self.ann_stripe.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(8, 0), pady=8)

        self.ann_title = ctk.CTkLabel(
            self.ann_card, text="",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w", justify="left",
        )
        self.ann_title.grid(row=0, column=1, sticky="ew", padx=(12, 12), pady=(12, 2))

        self.ann_body = ctk.CTkLabel(
            self.ann_card, text="",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            anchor="w", justify="left",
            wraplength=600,
        )
        self.ann_body.grid(row=1, column=1, sticky="ew", padx=(12, 12), pady=(0, 8))

        # Action row: optional URL button + dismiss.
        self.ann_actions = ctk.CTkFrame(self.ann_card, fg_color="transparent")
        self.ann_actions.grid(row=2, column=1, sticky="ew", padx=(12, 12), pady=(0, 12))

        self.ann_btn_action = ctk.CTkButton(
            self.ann_actions, text="",
            fg_color=THEME.primary,
            hover_color=THEME.primary_hover,
            text_color="white",
            corner_radius=6,
            width=140, height=28,
            command=self._on_ann_action,
        )
        self.ann_btn_action.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.ann_btn_action.grid_remove()

        self.ann_btn_dismiss = ctk.CTkButton(
            self.ann_actions, text="ปิดข้อความนี้",
            fg_color="transparent",
            hover_color=THEME.bg_hover,
            text_color=THEME.fg_secondary,
            border_width=1,
            border_color=THEME.border,
            corner_radius=6,
            width=130, height=28,
            command=self._on_ann_dismiss,
        )
        self.ann_btn_dismiss.grid(row=0, column=1, sticky="w")

        # Track which announcement is currently displayed so
        # _on_ann_dismiss can persist the right id.
        self._current_announcement: object | None = None

    def set_announcement(self, ann) -> None:
        """Show ``ann`` (an ``announcements.Announcement``) in the
        banner. Called by the background poller via the main
        thread.

        Pass ``None`` to hide the banner.
        """
        if ann is None:
            self.ann_card.grid_remove()
            self._current_announcement = None
            return

        # Severity → stripe color. Critical wins over warning wins
        # over info, but we never show severity in the title so
        # customers from non-tech backgrounds don't have to
        # interpret jargon -- the color is sufficient signal.
        sev_colors = {
            "info": THEME.primary,
            "warning": "#F59E0B",
            "critical": "#DC2626",
        }
        self.ann_stripe.configure(fg_color=sev_colors.get(ann.severity, THEME.primary))

        self.ann_title.configure(text=ann.title)
        self.ann_body.configure(text=ann.body)

        if ann.action_url and ann.action_label:
            self.ann_btn_action.configure(text=ann.action_label)
            self.ann_btn_action.grid()
        else:
            self.ann_btn_action.grid_remove()

        self._current_announcement = ann
        self.ann_card.grid()

    def _on_ann_action(self) -> None:
        ann = self._current_announcement
        if ann is None or not getattr(ann, "action_url", None):
            return
        try:
            webbrowser.open(ann.action_url)
        except Exception:
            log.exception("could not open announcement URL")

    def _on_ann_dismiss(self) -> None:
        ann = self._current_announcement
        if ann is None:
            self.ann_card.grid_remove()
            return
        try:
            from .. import announcements as ann_mod
            ann_mod.dismiss(ann.id)
        except Exception:
            log.exception("could not persist dismiss state")
        self.set_announcement(None)
        # Trigger a fresh poll so the next-priority announcement
        # (if any) pops up without waiting for the 30-min interval.
        try:
            poller = getattr(self.app, "announcements", None)
            if poller is not None:
                poller.refresh_now()
        except Exception:
            log.exception("could not refresh announcement poller")

    # ── selection / refresh ──────────────────────────────────────

    def _auto_select(self) -> None:
        if self.app.selected_serial is not None:
            return
        for e in self.app.devices_lib.list():
            if self.app.is_online(e.serial):
                self.app.select_device(e.serial)
                return
        if self.app.devices_lib.list():
            self.app.select_device(self.app.devices_lib.list()[0].serial)

    def _refresh_main(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            self.lbl_device_title.configure(text="ยังไม่มีเครื่องที่เลือก")
            try:
                self.btn_rename_device.grid_remove()
            except Exception:
                pass
            self.lbl_device_status.configure(
                text="กด '+ เพิ่มเครื่อง' ทางซ้ายมือเพื่อเริ่ม",
                text_color=THEME.fg_muted,
            )
            for card in (
                self.video_card, self.rotation_card, self.audio_card,
                self.action_card, self.live_card,
            ):
                self._set_card_enabled(card, False)
            return

        for card in (
            self.video_card, self.rotation_card, self.audio_card,
            self.action_card, self.live_card,
        ):
            self._set_card_enabled(card, True)

        self.lbl_device_title.configure(text=e.display_name())
        try:
            self.btn_rename_device.grid()
        except Exception:
            pass
        online = self.app.is_online(e.serial)
        transport = self.app.transport_of(e.serial)
        if not online:
            hint = (
                "เสียบ USB หรือกด 'เชื่อม WiFi อีกครั้ง'"
                if e.has_wifi() else "เสียบ USB ก่อน"
            )
            self.lbl_device_status.configure(
                text=f"🔴  ไม่ได้เชื่อมต่อ — {hint}",
                text_color=THEME.danger,
            )
        elif not e.is_patched():
            self.lbl_device_status.configure(
                text="🟠  ออนไลน์ — แต่ยังไม่ได้ Patch TikTok",
                text_color=THEME.warning,
            )
        else:
            via = "WiFi" if transport == "wifi" else "USB"
            self.lbl_device_status.configure(
                text=f"🟢  พร้อมใช้งาน  ·  เชื่อมผ่าน {via}",
                text_color=THEME.success,
            )

        # Connection details row (badge + ip:port + reconnect btn).
        if transport == "wifi":
            badge = f"📶  WiFi  ·  {e.wifi_address()}"
        elif transport == "usb":
            extra = f"  (WiFi สำรอง: {e.wifi_address()})" if e.has_wifi() else ""
            badge = f"🔌  USB{extra}"
        elif e.has_wifi():
            badge = f"⚪  ออฟไลน์  ·  WiFi ที่บันทึก: {e.wifi_address()}"
        else:
            badge = "⚪  ออฟไลน์  ·  ยังไม่ได้ตั้งค่า WiFi"
        self.lbl_conn_info.configure(text=badge)

        # Decide which (if any) of the two WiFi action buttons to show.
        # Three states matter:
        #
        # • USB connected, no WiFi recorded yet  → "ตั้งค่า WiFi" (primary)
        # • Device has WiFi addr saved but currently on USB / offline
        #     → "เชื่อม WiFi อีกครั้ง" (secondary)
        # • Already on WiFi  → hide both (nothing to do)
        if transport == "wifi":
            self.btn_reconnect_wifi.grid_remove()
            self.btn_setup_wifi.grid_remove()
        elif e.has_wifi():
            self.btn_setup_wifi.grid_remove()
            self.btn_reconnect_wifi.grid()
            self.btn_reconnect_wifi.configure(state="normal")
        elif transport == "usb":
            # patched + USB but never set up WiFi — surface the action
            self.btn_reconnect_wifi.grid_remove()
            self.btn_setup_wifi.grid()
            self.btn_setup_wifi.configure(state="normal")
        else:
            # offline + no WiFi info — nothing actionable until USB
            self.btn_reconnect_wifi.grid_remove()
            self.btn_setup_wifi.grid_remove()

        # Video
        if e.last_video and Path(e.last_video).is_file():
            p = Path(e.last_video)
            self.lbl_video_path.configure(text=p.name)
            sz = human_bytes(p.stat().st_size)
            self.lbl_video_meta.configure(text=f"{p.parent}  ·  {sz}")
        else:
            self.lbl_video_path.configure(text="(ยังไม่ได้เลือกคลิป)")
            self.lbl_video_meta.configure(text="")

        # Audio
        if e.last_audio and Path(e.last_audio).is_file():
            p = Path(e.last_audio)
            self.lbl_audio_path.configure(
                text=p.name, text_color=THEME.fg_primary,
            )
            sz = human_bytes(p.stat().st_size)
            self.lbl_audio_meta.configure(
                text=f"{p.parent}  ·  {sz}  ·  รูปแบบ {p.suffix.lower().lstrip('.')}",
            )
            self.btn_audio_push.configure(state="normal")
            self.btn_audio_clear.configure(state="normal")
        else:
            self.lbl_audio_path.configure(
                text="(ใช้เสียงจากคลิปวีดีโอ)",
                text_color=THEME.fg_muted,
            )
            self.lbl_audio_meta.configure(text="")
            self.btn_audio_push.configure(state="disabled")
            self.btn_audio_clear.configure(state="disabled")
        self.lbl_audio_status.configure(text="")

        # Rotation/mirror
        self.rotation_var.set(e.rotation)
        self.mirror_h_var.set(e.mirror_h)
        self.mirror_v_var.set(e.mirror_v)

        # Patch button text
        if e.is_patched():
            self.btn_patch.configure(text=f"Patch สำเร็จเมื่อ {e.patched_at[:10]}")
        else:
            self.btn_patch.configure(text="Patch & ติดตั้ง TikTok")

    def _set_card_enabled(self, card: ctk.CTkFrame, enabled: bool) -> None:
        # CustomTkinter has no built-in disabled state on frames, so
        # we mute the visual cue by lowering opacity via the border.
        card.configure(border_color=THEME.border if enabled else THEME.divider)

    # ── poller hooks ─────────────────────────────────────────────

    def on_devices_changed(self) -> None:
        self._refresh_sidebar()
        self._refresh_main()
        # Cheap throttled probe -- only re-checks when the cache
        # is older than 8 s. Idempotent across the 2 s adb-devices
        # poll, so we don't slam adb shell calls.
        self._refresh_hook_status(force=False)

    def on_selection_changed(self) -> None:
        self._refresh_sidebar()
        self._refresh_main()
        # When the customer flips between phones, force-clear the
        # old phone's badge text so they don't see a stale "🟢"
        # while the new one is still being probed.
        self.lbl_hook_status.configure(
            text="กำลังตรวจสอบสถานะ vcam...",
            text_color=THEME.fg_secondary,
        )
        self._refresh_hook_status(force=True)
        # Snap the live-control card to the new device's state
        # immediately rather than waiting up to 1 s for the next
        # tick -- otherwise the customer sees the OLD phone's
        # "🔴 ไลฟ์อยู่" briefly while the timer ticks.
        try:
            self._render_live_control_state()
        except Exception:
            log.debug("live-control render failed during selection", exc_info=True)

    # ── hook-status probe ────────────────────────────────────────

    HOOK_STATUS_TTL_S = 8.0

    def _refresh_hook_status(self, *, force: bool) -> None:
        """Trigger a probe of TikTok install/patch/running on the
        currently-selected device.

        Caching: results are cached for ``HOOK_STATUS_TTL_S`` per
        serial. ``force=True`` bypasses the cache (Refresh button,
        device selection change). ``force=False`` is a hint from
        the 2 s device poller and respects the cache.

        Threading: we offload the adb shell calls to a daemon
        thread because each probe takes 1-3 s of round trips and
        blocking the Tk loop here would freeze the dashboard.
        """
        import time as _time

        serial = self.app.selected_serial
        if not serial:
            self.lbl_hook_status.configure(
                text="(เลือกเครื่องเพื่อตรวจสอบสถานะ)",
                text_color=THEME.fg_muted,
            )
            return

        if not self.app.is_online(serial):
            self.lbl_hook_status.configure(
                text="🔴  เครื่อง offline — ตรวจไม่ได้",
                text_color=THEME.danger,
            )
            return

        # Cached fresh result?
        now = _time.monotonic()
        cached = self._hook_status_cache.get(serial)
        if cached and not force and (now - cached[0]) < self.HOOK_STATUS_TTL_S:
            self._render_hook_status(cached[1])  # type: ignore[arg-type]
            return

        # Already a probe in flight? Drop the request -- the
        # in-flight one will refresh the UI when it completes,
        # and customer-mashed Refresh clicks shouldn't queue up.
        if self._hook_status_inflight:
            return
        self._hook_status_inflight = True
        self.btn_hook_refresh.configure(state="disabled")

        def _probe_worker():
            try:
                from .. import hook_status as hs
                adb_id = self.app.adb_id_for(serial)
                # Pass our recorded baseline so the probe can use
                # exact-match fingerprint comparison (the only
                # fully-reliable patched detection signal across
                # OEM ROMs and Android versions).
                entry = self.app.devices_lib.get(serial)
                expected_fp = entry.patched_signature if entry else ""
                expected_pkg = entry.tiktok_package if entry else ""
                result = hs.probe(
                    self.app.cfg.adb_path, adb_id,
                    expected_fingerprint=expected_fp,
                    expected_package=expected_pkg,
                )
            except Exception as exc:
                from .. import hook_status as hs
                log.exception("hook_status probe crashed")
                result = hs.HookStatus(error=f"{type(exc).__name__}: {exc}")
            self._hook_status_cache[serial] = (_time.monotonic(), result)
            try:
                self.after(0, lambda r=result: self._on_hook_status_done(r))
            except Exception:
                # Widget destroyed during probe (page navigation).
                # Silently drop -- nobody's listening anyway.
                pass

        threading.Thread(
            target=_probe_worker,
            name="np-hook-status",
            daemon=True,
        ).start()

    def _on_hook_status_done(self, status) -> None:
        self._hook_status_inflight = False
        try:
            self.btn_hook_refresh.configure(state="normal")
        except Exception:
            return
        self._render_hook_status(status)

        # Persist the detected variant so the encode/push pipeline
        # can target the right ``/sdcard/Android/data/<pkg>/files/``
        # path even when the device is offline next launch (cached
        # value > educated guess). Empty package means "could not
        # detect" -- don't overwrite a previously-good value.
        serial = self.app.selected_serial
        dirty = False
        if serial and getattr(status, "package", None):
            entry = self.app.devices_lib.get(serial)
            if entry is not None and entry.tiktok_package != status.package:
                self.app.devices_lib.update_tiktok_package(
                    serial, status.package,
                )
                dirty = True

        # ── Auto-heal: probe says "patched" but entry says "no" ──
        # This happens when the customer's devices.json was wiped,
        # they Patched from a different machine, or they Patched
        # before NP Create started recording the install signature.
        # Either way: the phone IS patched, so refusing to enable
        # the dashboard buttons would be a worse UX than silently
        # adopting the observed state.
        if (
            serial
            and getattr(status, "patched", False)
            and not getattr(status, "error", "")
        ):
            entry = self.app.devices_lib.get(serial)
            if entry is not None and not entry.patched_at:
                healed = self.app.devices_lib.reconcile_observed_patched(
                    serial,
                    signature=getattr(status, "fingerprint", "") or "",
                    tiktok_version=(
                        getattr(status, "version_name", "") or ""
                    ),
                )
                if healed:
                    log.info(
                        "auto-healed patched_at for %s "
                        "(observed signature=%s)",
                        serial,
                        (getattr(status, "fingerprint", "") or "")[:12],
                    )
                    dirty = True
                    # Refresh the main panel: ``e.is_patched()``
                    # is the gate behind "ออนไลน์ — แต่ยังไม่ได้
                    # Patch TikTok"; without re-render the sidebar
                    # would lag a probe cycle behind the heal.
                    try:
                        self._refresh_main()
                    except Exception:
                        log.debug(
                            "main re-render failed after heal",
                            exc_info=True,
                        )

        if dirty:
            self.app.save_devices()

        # ── TikTok auto-update drift detection ────────────────────
        # If the customer (or TikTok itself in the background) has
        # updated TikTok since we last patched, the LSPatch overlay
        # is gone and live broadcasts will fail silently. Show a
        # single warning + offer re-patch. We rate-limit per session
        # so the dialog doesn't reopen every probe tick.
        if serial and status and not status.error:
            self._maybe_warn_tiktok_drift(serial, status)

    def _maybe_warn_tiktok_drift(self, serial: str, status) -> None:
        """Compare the live TikTok versionName against what we
        recorded at patch time. Mismatch + signature lost = a
        TikTok auto-update has overwritten our patched APK; warn
        the customer once per session and offer Re-Patch."""
        try:
            entry = self.app.devices_lib.get(serial)
            if entry is None:
                return
            recorded = (entry.patched_tiktok_version or "").strip()
            current = (getattr(status, "version_name", "") or "").strip()
            # Need both ends — without a recorded baseline we can't
            # know if this is drift or a fresh first install.
            if not recorded or not current:
                return
            # Same version → no drift, nothing to do.
            if recorded == current:
                return
            # Avoid double-warning within the same UI session.
            if getattr(self, "_drift_warned_serials", None) is None:
                self._drift_warned_serials = set()
            if serial in self._drift_warned_serials:
                return
            self._drift_warned_serials.add(serial)
            self.app.devices_lib.mark_tiktok_drift_warned(serial)
            self.app.save_devices()

            label = entry.display_name() or serial
            patched_flag = bool(getattr(status, "patched", False))
            # Two distinct flavors of drift:
            # 1) versionName changed AND signature is no longer
            #    LSPatch's debug key -> TikTok was reinstalled by
            #    Play Store / in-app updater. vcam IS broken now.
            # 2) versionName changed BUT signature still LSPatch
            #    -> rare; usually means the customer re-patched
            #    from another machine. Just refresh our cache.
            if not patched_flag:
                msg = (
                    f"TikTok บนเครื่อง \"{label}\" "
                    f"อัปเดตจากเวอร์ชัน {recorded} → {current}\n\n"
                    "vcam หายเพราะ TikTok ติดตั้งทับด้วยเวอร์ชันใหม่\n"
                    "(ไม่มีลายเซ็น LSPatch อีกแล้ว).\n\n"
                    "ต้องการ Patch ใหม่ตอนนี้ไหม?"
                )
                if messagebox.askyesno("⚠️ TikTok ถูกอัปเดต", msg):
                    self._trigger_repatch(serial)
            else:
                # Silent reconcile: just record the new version so
                # we don't keep nagging.
                self.app.devices_lib.mark_patched(
                    serial, tiktok_version=current,
                )
                self.app.save_devices()
        except Exception:
            log.exception("drift detection failed (non-fatal)")

    def _trigger_repatch(self, serial: str) -> None:
        """One-click Re-Patch from the drift warning dialog. Runs
        the same pipeline as the Settings → Patch button."""
        try:
            self.app.refresh_devices_now()
            if not self.app.is_online(serial):
                messagebox.showwarning(
                    "ต้อง USB",
                    "เสียบสาย USB เครื่องนี้ก่อน "
                    "แล้วกด Re-Patch อีกครั้ง",
                )
                return
            threading.Thread(
                target=self._run_repatch_background,
                args=(serial,),
                daemon=True,
            ).start()
        except Exception:
            log.exception("re-patch trigger failed")

    def _run_repatch_background(self, serial: str) -> None:
        """Background thread: run the LSPatch pipeline and report
        result via a Tk-thread modal."""
        ls = self.app.lspatch
        patched_version = ""
        patched_signature = ""
        try:
            tools = ls.probe_tools()
            if not tools.ok:
                self.after(0, lambda: messagebox.showerror(
                    "เครื่องมือไม่ครบ",
                    "\n".join(tools.errors),
                ))
                return
            pull = ls.pull_tiktok(serial=serial)
            if not pull.ok:
                err_lower = (pull.error or "").lower()
                if "no tiktok variant" in err_lower:
                    err_msg = (
                        "ไม่พบ TikTok ในเครื่อง\n\n"
                        "TikTok อาจถูกถอนระหว่าง patch ครั้งก่อน "
                        "กรุณาลง TikTok ใหม่จาก Play Store แล้ว "
                        "ลอง Re-Patch อีกครั้ง"
                    )
                else:
                    err_msg = f"pull: {pull.error}"
                self.after(0, lambda m=err_msg: messagebox.showerror(
                    "Re-Patch ล้มเหลว", m,
                ))
                return
            patched_version = pull.version_name or ""
            patched = ls.patch(pull.apks)
            if not patched.ok:
                self.after(0, lambda: messagebox.showerror(
                    "Re-Patch ล้มเหลว", f"patch: {patched.error}",
                ))
                return
            inst = ls.install(
                package=pull.package,
                patched_apks=patched.patched_apks,
                serial=serial,
                # Roll back to the pulled originals if the install
                # bombs after we've already uninstalled the previous
                # patched build — same safety net as the main Patch
                # button.
                original_apks=pull.apks,
            )
            if not inst.ok:
                if inst.rollback_attempted and inst.rollback_ok:
                    err_msg = (
                        f"install: {inst.error}\n\n"
                        "✓ กู้ TikTok เดิมกลับเรียบร้อยแล้ว — "
                        "ลอง Re-Patch อีกครั้งได้"
                    )
                elif inst.rollback_attempted:
                    err_msg = (
                        f"install: {inst.error}\n\n"
                        f"⚠️  กู้ TikTok ไม่สำเร็จ: {inst.rollback_error}\n"
                        "กรุณาลง TikTok ใหม่จาก Play Store"
                    )
                else:
                    err_msg = (
                        f"install: {inst.error}\n\n"
                        "⚠️  TikTok เดิมอาจหายจากเครื่อง — "
                        "กรุณาลงใหม่จาก Play Store"
                    )
                self.after(0, lambda m=err_msg: messagebox.showerror(
                    "Re-Patch ล้มเหลว", m,
                ))
                return
            patched_signature = (inst.fingerprint or "").lower()
        except Exception as ex:
            log.exception("re-patch crashed")
            self.after(0, lambda: messagebox.showerror(
                "Re-Patch ล้มเหลว", f"crash: {ex}",
            ))
            return

        self.app.devices_lib.mark_patched(
            serial,
            tiktok_version=patched_version,
            signature=patched_signature,
        )
        self.app.save_devices()
        # Reset the per-session warning so a future drift warns again.
        if getattr(self, "_drift_warned_serials", None):
            self._drift_warned_serials.discard(serial)

        ver_line = (
            f"\n\nเวอร์ชัน TikTok ใหม่: {patched_version}"
            if patched_version else ""
        )
        self.after(0, lambda: messagebox.showinfo(
            "✓ Re-Patch สำเร็จ",
            "ติดตั้ง TikTok ที่ patched ใหม่แล้ว"
            + ver_line
            + "\n\n⚠️ อย่ากด \"อัปเดต\" ใน TikTok อีก",
        ))

    def _render_hook_status(self, status) -> None:
        """Apply a ``HookStatus`` to the badge widgets. Tolerant
        of widget teardown -- if the page is being destroyed, we
        silently drop instead of raising into the Tk loop."""
        try:
            self.lbl_hook_status.configure(
                text=status.label_th,
                text_color=status.color,
            )
        except Exception:
            log.debug("hook status widget unavailable", exc_info=True)

    # ── live control card ────────────────────────────────────────

    def _build_live_control_card(self, parent: ctk.CTkFrame) -> None:
        """Mirror-control card — the customer's daily-driver tool.

        History
        -------
        Earlier versions of this card surfaced a "🔴 เริ่มไลฟ์"
        toggle that drove TikTok's Go-Live flow over uiautomator.
        We pulled it out in v1.7.7 because it created more support
        tickets than it saved time:

        * The auto-control flow gave up at the first un-keyworded
          button (e.g. "Go Live" with no accessibility label, or
          the Korean-region's extra confirmation dialog) — so
          customers tapped it and saw "ระบบกดให้ไม่สำเร็จ", which
          felt broken even though they could finish the broadcast
          manually.
        * Customers regularly hit the big red button by accident,
          shipping a half-prepared live to their followers.
        * The cumulative "ไลฟ์รวม" stat and "จับเวลาเอง" link
          encouraged "let me click to see what happens" exploration
          that didn't help anyone.

        Mirror, on the other hand, is unambiguously useful:

        * Lets the customer drive the broadcast end-to-end from PC
          (mouse + keyboard) without picking up the phone — the
          ergonomic backbone of "วางมือถือไว้เฉยๆ ใช้คอมทำงาน".
        * Phone OLED stays dark via scrcpy's ``--turn-screen-off``,
          which saves battery across multi-hour streams.
        * Works for offline preparation too — read comments, scroll
          products, even before going live.

        The live-tracking module (``live_control``,
        ``DeviceEntry.live_started_at``) still exists for future
        integration with the admin dashboard's session-time
        analytics, but no UI surface in this app triggers it
        right now.

        Layout
        ------
        Row 0 — Title (🪞  Mirror หน้าจอเครื่อง)
        Row 1 — Status line (พร้อมใช้งาน / Mirror เปิดอยู่ Nm /
                 เครื่อง offline / scrcpy ยังไม่ติดตั้ง)
        Row 2 — Big primary toggle button
        Row 3 — Hint label
        Row 4 — Power-user shortcuts tip
        """
        title_row = ctk.CTkFrame(parent, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 4))
        title_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            title_row, text="🪞  Mirror หน้าจอเครื่อง",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        # Status line — driven entirely by ``_render_mirror_state``.
        # Default text below is what the customer sees in the brief
        # window between widget construction and the first 1 s
        # tick (so the card never flashes a blank line).
        self.lbl_mirror_status = ctk.CTkLabel(
            parent,
            text="พร้อมใช้งาน",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=13),
            anchor="w",
        )
        self.lbl_mirror_status.grid(
            row=1, column=0, sticky="w", padx=20, pady=(0, 8),
        )

        # Big primary mirror button. Bumped from the previous
        # 34 px secondary style (when it sat under the live
        # button) to 48 px now that it's the card's main action.
        # ``btn_live_mirror`` keeps its historical name so
        # ``_render_mirror_state`` and the scrcpy_mirror
        # subscribe-callback don't need touching.
        self.btn_live_mirror = ctk.CTkButton(
            parent,
            text="🪞  เปิด Mirror",
            fg_color=THEME.primary,
            hover_color=THEME.primary_hover,
            text_color="white",
            corner_radius=8,
            height=48,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._on_mirror_toggle,
        )
        self.btn_live_mirror.grid(
            row=2, column=0, sticky="ew", padx=20, pady=(0, 6),
        )

        # Hint line — what the button is going to do right now.
        # ``_render_mirror_state`` rewrites this for the offline
        # / scrcpy-missing / mirroring states.
        self.lbl_mirror_hint = ctk.CTkLabel(
            parent,
            text="วางโทรศัพท์เฉยๆ • ใช้เมาส์/คีย์บอร์ดควบคุมจาก PC ได้เลย",
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=11),
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.lbl_mirror_hint.grid(
            row=3, column=0, sticky="w", padx=20, pady=(0, 6),
        )

        # Power-user shortcut tip. Surfaced inline so customers
        # discover scrcpy's killer features without reading a
        # manual. The list is short on purpose — three items max
        # is the useful-vs-noise threshold based on the help-desk
        # tickets we get ("how do I send a video to the phone?"
        # is by far the most common, hence drag-and-drop first).
        ctk.CTkLabel(
            parent,
            text=(
                "💡  ลากไฟล์เข้าหน้าต่าง = ส่งไฟล์เข้ามือถือ  •  "
                "Ctrl+Shift+P = ปิด/เปิดจอมือถือ  •  "
                "Ctrl+Home = ปลุกหน้าจอ"
            ),
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=10),
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=4, column=0, sticky="w", padx=20, pady=(0, 16))

        # Subscribe to scrcpy session lifecycle so closing the
        # mirror window externally re-renders the button label.
        # Tk callbacks fire on the reaper thread; marshal back
        # via ``after(0, ...)`` so widget updates land on the
        # main thread.
        from .. import scrcpy_mirror as _scm
        def _mirror_changed(_adb_id: str) -> None:
            try:
                self.after(0, self._render_mirror_state)
            except Exception:
                log.debug("mirror change after() failed", exc_info=True)
        _scm.subscribe(_mirror_changed)

        # Legacy in-flight guard. The Mirror toggle does its own
        # disable-during-spawn dance (see _on_mirror_toggle), but
        # leaving the attribute alive keeps any background helpers
        # that still reference it (admin dashboard analytics path)
        # from raising AttributeError on older builds.
        self._live_action_inflight = False

        # Kick off the per-second tick. Mirror state is event-
        # driven via the subscribe callback above for the common
        # case (window opened/closed) — the tick exists to keep
        # the elapsed-minutes counter in the status line
        # incrementing while a session is alive.
        self._schedule_live_tick()

    def _schedule_live_tick(self) -> None:
        """Re-render the live status every second. The handle is
        cancelled in ``destroy`` (Tk's ``after`` is automatically
        torn down with the widget tree, but being explicit means
        no spurious 'invalid command name' errors during page
        switches)."""
        try:
            self._render_live_control_state()
        except Exception:
            log.exception("render live state failed")
        try:
            self._live_tick_handle = self.after(1000, self._schedule_live_tick)
        except Exception:
            self._live_tick_handle = None

    def _render_live_control_state(self) -> None:
        """Backwards-compatible alias for ``_render_mirror_state``.

        The live-control card is now Mirror-only (see
        ``_build_live_control_card``). This wrapper still exists
        because:

        * The 1 s tick (``_schedule_live_tick``) calls it, and we
          don't want to rename the tick infra in a UI-only patch.
        * A handful of background paths (e.g. the post-patch
          dialog handlers in ``studio_app``) call it after device
          state changes to force an immediate refresh.

        Once the live-control codepaths are confirmed dead, the
        whole pair can be renamed in a follow-up cleanup PR.
        """
        self._render_mirror_state()

    # ── live-control handlers (removed in v1.7.7) ────────────────
    #
    # The "🔴 เริ่มไลฟ์" / "⏹ จบไลฟ์" / "▶ จับเวลาเอง" buttons
    # used to live in this card. They drove TikTok Live over
    # uiautomator and persisted ``DeviceEntry.live_started_at``.
    # Removed because customers reported the auto-control failed
    # too often (region-specific TikTok dialogs, button labels
    # without accessibility text) and the misleading half-success
    # state ("ระบบกดให้ไม่สำเร็จ" while they ARE actually live)
    # generated more support tickets than the feature saved.
    #
    # The underlying ``live_control`` module is intact and can be
    # re-introduced later behind the admin dashboard, where the
    # scope is "track live minutes for billing analytics" rather
    # than "drive TikTok end-to-end from a customer's PC".

    def _render_mirror_state(self) -> None:
        """Sync the Mirror status line + button + hint to the
        current scrcpy session for the selected device.

        Drives three widgets together so the card never shows a
        torn state (e.g. status saying "offline" while the button
        still reads "เปิด Mirror"):

        * ``lbl_mirror_status`` — the headline state line at the
          top of the card.
        * ``btn_live_mirror`` — the big primary action button.
        * ``lbl_mirror_hint`` — the secondary explainer beneath
          the button.

        Called from the 1 s live-tick AND from the scrcpy
        change-callback so closing the mirror window externally
        flips the button back without waiting for the tick.
        """
        if not hasattr(self, "btn_live_mirror"):
            return

        from .. import scrcpy_mirror as scm

        e = self.app.selected_entry()
        if e is None:
            if hasattr(self, "lbl_mirror_status"):
                self.lbl_mirror_status.configure(
                    text="(เลือกเครื่องก่อน)",
                    text_color=THEME.fg_muted,
                )
            self.btn_live_mirror.configure(
                state="disabled",
                text="🪞  เปิด Mirror",
                fg_color=THEME.primary,
                hover_color=THEME.primary_hover,
                text_color="white",
                border_color=THEME.primary,
            )
            self.lbl_mirror_hint.configure(
                text="วางโทรศัพท์เฉยๆ • ใช้เมาส์/คีย์บอร์ดควบคุมจาก PC ได้เลย",
                text_color=THEME.fg_muted,
            )
            return

        # scrcpy not on PATH → orange status + grey-styled button.
        # We don't fully disable the button: the dialog the click
        # triggers is the entire user-education path for this
        # case (auto-install or open the install URL).
        if not scm.is_available():
            if hasattr(self, "lbl_mirror_status"):
                self.lbl_mirror_status.configure(
                    text="⚠️  ยังไม่ได้ติดตั้ง scrcpy — กดปุ่มเพื่อให้ระบบติดตั้งให้",
                    text_color=THEME.warning,
                )
            self.btn_live_mirror.configure(
                state="normal",
                text="🪞  ติดตั้ง scrcpy แล้วเปิด Mirror",
                fg_color=THEME.warning,
                hover_color=THEME.warning,
                text_color="#1A1206",
                border_color=THEME.warning,
            )
            self.lbl_mirror_hint.configure(
                text="ใช้คำสั่งเดียว ไม่ต้องลงเอง",
                text_color=THEME.warning,
            )
            return

        # Use adb_id_for() so WiFi devices (IP:port) match the key
        # we registered in scrcpy_mirror — otherwise a freshly-
        # opened mirror on a WiFi device would not flip the button
        # state until the next render.
        adb_id = self.app.adb_id_for(e)
        is_online = self.app.is_online(e.serial)
        is_mir = scm.is_mirroring(adb_id)

        if is_mir:
            sess = scm.get_session(adb_id)
            since_min = (
                int((__import__("time").time() - sess.started_at) // 60)
                if sess else 0
            )
            if hasattr(self, "lbl_mirror_status"):
                self.lbl_mirror_status.configure(
                    text=f"🟢  Mirror เปิดอยู่ — {since_min} นาที",
                    text_color=THEME.success,
                )
            self.btn_live_mirror.configure(
                state="normal",
                text="⏹  ปิด Mirror",
                fg_color=THEME.success,
                hover_color=THEME.success,
                text_color="#0F0F14",
                border_color=THEME.success,
            )
            self.lbl_mirror_hint.configure(
                text="หน้าจอมือถือถูกปิดเพื่อประหยัดแบต — ภาพยังสตรีมต่อเหมือนเดิม",
                text_color=THEME.success,
            )
        else:
            if hasattr(self, "lbl_mirror_status"):
                self.lbl_mirror_status.configure(
                    text=(
                        f"พร้อมใช้งาน ({e.display_name()})"
                        if is_online
                        else "เครื่อง offline — เชื่อมต่อก่อน"
                    ),
                    text_color=(
                        THEME.fg_secondary if is_online else THEME.fg_muted
                    ),
                )
            self.btn_live_mirror.configure(
                state=("normal" if is_online else "disabled"),
                text="🪞  เปิด Mirror",
                fg_color=THEME.primary,
                hover_color=THEME.primary_hover,
                text_color="white",
                border_color=THEME.primary,
            )
            self.lbl_mirror_hint.configure(
                text=(
                    "วางโทรศัพท์เฉยๆ • ใช้เมาส์/คีย์บอร์ดควบคุมจาก PC ได้เลย"
                    if is_online
                    else "เสียบสาย USB หรือเชื่อม WiFi ADB ก่อนเปิด Mirror"
                ),
                text_color=THEME.fg_muted,
            )

    def _on_mirror_toggle(self) -> None:
        """Start or stop a scrcpy mirror window for the selected
        device. The button text + hint update via
        ``_render_live_control_state`` so we don't replicate
        widget-state logic here.

        Failure modes are surfaced inline, not as raw exceptions:

        * scrcpy not installed → messagebox with the install URL +
          a "เปิดเว็บ" button. We don't auto-shell-out an install
          script because we won't have sudo and customer trust is
          on the line.
        * Device not visible to ADB right now → friendly toast.
          We do a FRESH ``adb get-state`` query rather than relying
          on the cached ``is_online`` set, because the device
          poller only refreshes every 2 s — a customer who plugs
          the cable and immediately taps Mirror would otherwise
          hit a stale "offline" reading and bounce off this dialog.
        * scrcpy spawned but exited immediately (USB debugging
          revoked between query and spawn, scrcpy version drift) →
          friendly toast + revert button to "Mirror" state.
        """
        e = self.app.selected_entry()
        if e is None:
            return

        from .. import scrcpy_mirror as scm

        # Sync the device cache before resolving adb_id so WiFi
        # rows that JUST landed (or USB rows that just disappeared)
        # are reflected. Without this, ``adb_id_for`` could return
        # the stale wired serial after the customer pulled the
        # cable, and we'd spawn scrcpy against a dead transport.
        self.app.refresh_devices_now()

        # Use adb_id_for() so WiFi devices route to ``IP:port``,
        # not the persistent USB serial that adbd doesn't recognise
        # when the phone is wireless-only.
        adb_id = self.app.adb_id_for(e)

        # Toggle path 1: stop existing session.
        if scm.is_mirroring(adb_id):
            scm.stop_mirror(adb_id)
            self._render_live_control_state()
            return

        # Toggle path 2: start a new session. Realtime ADB probe
        # — this is the bit that makes "plug → tap" work without
        # waiting for the 2 s background poller.
        adb_state = self._adb_get_state(adb_id)
        if adb_state != "device":
            self._show_mirror_offline_dialog(adb_id, adb_state)
            return

        # Quick "I'm working on it" feedback BEFORE we spawn — the
        # subprocess + 250 ms readiness wait can briefly freeze the
        # button hit-test on slow Macs and we don't want a double-
        # click to spawn two windows.
        self.btn_live_mirror.configure(
            state="disabled",
            text="🪞  กำลังเปิด Mirror...",
        )
        self.update_idletasks()

        result = scm.start_mirror(
            self.app.cfg.adb_path,
            adb_id,
            label=e.display_name() or adb_id,
        )

        if result.ok:
            log.info(
                "scrcpy mirror started for %s pid=%s", adb_id, result.pid,
            )
        elif result.error == "scrcpy_not_installed":
            # Don't bother the customer with "go install scrcpy
            # yourself" — offer to download + install in-app and
            # auto-launch the Mirror once it's ready. This is the
            # whole reason scrcpy_installer exists.
            self._offer_auto_install_then_mirror(adb_id, e)
        else:
            messagebox.showwarning(
                "Mirror เปิดไม่สำเร็จ",
                (
                    f"ไม่สามารถเปิด Mirror ได้\n\n"
                    f"สาเหตุ: {result.error}\n\n"
                    "ตรวจ:\n"
                    "• เสียบสาย USB แน่นหรือยัง\n"
                    "• อนุญาต USB Debugging แล้วใช่ไหม\n"
                    "• ลองเปิด/ปิดสายแล้วลองใหม่อีกครั้ง"
                ),
            )

        self._render_live_control_state()

    def _adb_get_state(self, adb_id: str) -> str:
        """Synchronously probe ``adb -s <id> get-state``.

        Returns the raw state string ('device', 'offline',
        'unauthorized', 'no-device', ...) or '' on any error.

        Cheap (~50 ms on USB, ~150 ms on WiFi). We run it on the
        UI thread because the customer pressed a button and is
        actively waiting for feedback — anything < 200 ms is
        indistinguishable from instant.
        """
        import subprocess
        adb = self.app.cfg.adb_path or "adb"
        try:
            r = subprocess.run(
                [adb, "-s", adb_id, "get-state"],
                capture_output=True, text=True, timeout=2.0,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            log.warning("adb get-state %s timed out / not found", adb_id)
            return ""
        if r.returncode != 0:
            # ADB prints "error: device 'XYZ' not found" to stderr
            # with rc=1 when the device isn't known at all. Treat
            # that as "no-device" so the caller dialog can be
            # specific.
            stderr = (r.stderr or "").strip().lower()
            if "not found" in stderr or "no devices" in stderr:
                return "no-device"
            return ""
        return (r.stdout or "").strip().lower()

    def _show_mirror_offline_dialog(self, adb_id: str, state: str) -> None:
        """Friendly per-state explanation of why Mirror can't open.

        We split states because the fix differs:

        * 'unauthorized' → customer needs to tap "Allow USB
          Debugging" on the phone screen (one-time per PC).
        * 'offline' → the cable is loose or the phone fell asleep
          mid-handshake.
        * 'no-device' / '' → adb can't see the phone at all; usually
          a cable / port / driver issue.
        """
        if state == "unauthorized":
            messagebox.showinfo(
                "อนุญาต USB Debugging ก่อน",
                (
                    "มือถือยังไม่อนุญาต USB Debugging สำหรับเครื่อง PC นี้\n\n"
                    "ที่หน้าจอมือถือจะมี popup ขึ้น — กด "
                    "\"Allow / อนุญาต\" + ติ๊ก \"จดจำเครื่องนี้\""
                    "\n\nแล้วลองกด Mirror ใหม่อีกครั้ง"
                ),
            )
        elif state == "offline":
            messagebox.showinfo(
                "เครื่องเชื่อมต่อแบบ offline",
                (
                    "ADB เห็นเครื่อง แต่สถานะเป็น 'offline' "
                    "(มือถือหลับ / เพิ่งเสียบสาย / ติด screen lock)\n\n"
                    "1. ปลดล็อคหน้าจอมือถือ\n"
                    "2. ถ้ายังไม่หาย — ดึงสาย USB ออกแล้วเสียบใหม่\n"
                    "3. แล้วกด Mirror อีกครั้ง"
                ),
            )
        else:
            # no-device / empty — adb doesn't see it
            messagebox.showinfo(
                "ไม่พบเครื่อง",
                (
                    f"ADB ไม่พบเครื่อง '{adb_id}' ตอนนี้\n\n"
                    "ตรวจ:\n"
                    "• สาย USB เสียบแน่นแล้วใช่ไหม "
                    "(ลองเปลี่ยนสาย / ช่อง USB)\n"
                    "• เปิด \"USB Debugging\" ในมือถือแล้วใช่ไหม\n"
                    "• ถ้าใช้ WiFi ADB — เครื่องอยู่ใน LAN เดียวกันใช่ไหม\n\n"
                    "แล้วลอง Mirror ใหม่อีกครั้ง"
                ),
            )

    def _offer_auto_install_then_mirror(
        self, adb_id: str, entry,
    ) -> None:
        """Customer-friendly first-Mirror flow: confirm + download +
        verify + extract + auto-launch.

        We do everything in-app so the non-technical customer never
        has to open a terminal, browser, or app store. The
        download is ~10 MB from GitHub Releases (official asset),
        sha256-pinned to defend against MITM, and stashed under
        ``~/.npcreate/tools/`` so an uninstall is just deleting
        that directory.
        """
        from .. import scrcpy_installer

        try:
            mb = scrcpy_installer.estimated_download_mb()
        except Exception:
            mb = 10

        if not messagebox.askyesno(
            "ติดตั้งฟีเจอร์ Mirror อัตโนมัติ",
            (
                "ระบบจะดาวน์โหลด + ติดตั้งโปรแกรม scrcpy "
                "(โอเพนซอร์ส, ฟรี) ให้อัตโนมัติ\n\n"
                f"• ขนาด: ~{mb} MB\n"
                "• ใช้เวลา: ~30 วินาที (ตามอินเทอร์เน็ต)\n"
                "• ติดตั้งครั้งเดียว ใช้ได้ตลอด\n"
                "• ไม่ต้องมีรหัสผ่าน admin / sudo\n\n"
                "เริ่มติดตั้งเลยไหม?"
            ),
        ):
            return

        self._run_scrcpy_install_with_progress(
            on_done=lambda: self._spawn_mirror_after_install(adb_id, entry),
        )

    def _run_scrcpy_install_with_progress(
        self, on_done,
    ) -> None:
        """Show a modal progress dialog while the installer runs.

        We don't reuse Tk's ttk.Progressbar in CTk-mode because
        determinate-style progress on a CustomTkinter window
        requires a CTkProgressBar — which expects 0..1 floats and
        doesn't render percentages on its own. We stitch it
        together: bar (0..1) + label ("ดาวน์โหลด 4.2 / 9.5 MB").

        ``on_done`` is invoked on the Tk main thread after the
        modal closes, regardless of success or failure (see the
        success guard in :meth:`_spawn_mirror_after_install`).
        """
        from .. import scrcpy_installer

        modal = ctk.CTkToplevel(self)
        modal.title("กำลังติดตั้ง Mirror...")
        modal.geometry("420x180")
        modal.resizable(False, False)
        modal.transient(self.winfo_toplevel())
        modal.grab_set()

        title = ctk.CTkLabel(
            modal,
            text="🪞  กำลังติดตั้ง scrcpy",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        title.pack(pady=(20, 6))

        stage_lbl = ctk.CTkLabel(
            modal, text="กำลังเริ่มต้น...",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
        )
        stage_lbl.pack(pady=(0, 8))

        bar = ctk.CTkProgressBar(modal, width=360)
        bar.set(0.0)
        bar.pack(pady=(0, 6))

        detail_lbl = ctk.CTkLabel(
            modal, text="",
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=11),
        )
        detail_lbl.pack(pady=(0, 4))

        # Track install outcome so the on_done dispatch can branch.
        state: dict = {"ok": False, "error": None, "binary": None}

        def _humanise_bytes(n: int) -> str:
            mb = n / (1024 * 1024)
            return f"{mb:.1f} MB"

        def _progress_cb(stage: str, current: int, total: int) -> None:
            # Worker thread — marshal to Tk before touching widgets.
            def _apply():
                try:
                    if stage == "download":
                        ratio = (current / total) if total else 0.0
                        bar.set(min(1.0, ratio))
                        stage_lbl.configure(text="กำลังดาวน์โหลด...")
                        detail_lbl.configure(
                            text=(
                                f"{_humanise_bytes(current)} / "
                                f"{_humanise_bytes(total)} "
                                f"({int(ratio * 100)}%)"
                            ),
                        )
                    elif stage == "verify":
                        stage_lbl.configure(text="กำลังตรวจไฟล์ (sha256)...")
                        bar.set(1.0)
                        detail_lbl.configure(text="ตรวจความสมบูรณ์ของไฟล์")
                    elif stage == "extract":
                        ratio = (current / total) if total else 0.0
                        stage_lbl.configure(text="กำลังแตกไฟล์...")
                        bar.set(min(1.0, ratio))
                        detail_lbl.configure(
                            text=f"{current}/{total} ไฟล์",
                        )
                    elif stage == "done":
                        stage_lbl.configure(
                            text="✓ ติดตั้งสำเร็จ — กำลังเปิด Mirror...",
                            text_color=THEME.success,
                        )
                        bar.set(1.0)
                        detail_lbl.configure(text="")
                except Exception:
                    log.debug("install progress UI update failed", exc_info=True)
            try:
                self.after(0, _apply)
            except Exception:
                log.debug("install progress after() failed", exc_info=True)

        def _on_complete(binary, err):
            def _close():
                state["ok"] = err is None and binary is not None
                state["error"] = err
                state["binary"] = binary

                # Brief "done" pause so the customer SEES the green
                # message before we yank the modal.
                if state["ok"]:
                    self.after(600, _finish)
                else:
                    _finish()

            def _finish():
                try:
                    modal.grab_release()
                    modal.destroy()
                except Exception:
                    pass
                if state["ok"]:
                    log.info(
                        "scrcpy auto-install succeeded → %s", state["binary"],
                    )
                    try:
                        on_done()
                    except Exception:
                        log.exception("install on_done callback failed")
                else:
                    msg = (
                        str(state["error"])
                        if state["error"]
                        else "เกิดข้อผิดพลาดที่ไม่ทราบสาเหตุ"
                    )
                    messagebox.showerror(
                        "ติดตั้ง scrcpy ไม่สำเร็จ",
                        (
                            f"{msg}\n\n"
                            "ลองอีกครั้ง หรือถ้าติดอินเทอร์เน็ต/firewall "
                            "ให้ทักแอดมิน Line @npcreate"
                        ),
                    )
            try:
                self.after(0, _close)
            except Exception:
                log.debug("install on_complete after() failed", exc_info=True)

        scrcpy_installer.install_async(
            progress=_progress_cb,
            on_complete=_on_complete,
        )

    def _spawn_mirror_after_install(self, adb_id: str, entry) -> None:
        """Re-trigger Mirror right after the installer succeeds.

        We don't recurse back through ``_on_mirror_toggle`` because
        we already have a verified ``adb_id`` and don't want to
        re-prompt for offline / unauthorized — the customer
        already saw the install dialog and chose Mirror, so just
        do it.
        """
        from .. import scrcpy_mirror as scm

        result = scm.start_mirror(
            self.app.cfg.adb_path,
            adb_id,
            label=entry.display_name() or adb_id,
        )
        if result.ok:
            log.info("scrcpy mirror started post-install pid=%s", result.pid)
        else:
            messagebox.showwarning(
                "Mirror เปิดไม่สำเร็จ",
                (
                    f"ติดตั้งสำเร็จ แต่เปิด Mirror ไม่ได้\n\n"
                    f"สาเหตุ: {result.error}\n\n"
                    "ลองตรวจสาย USB / Wi-Fi ADB แล้วกด Mirror ใหม่"
                ),
            )
        self._render_live_control_state()

    # ── interactions ─────────────────────────────────────────────

    def _on_pick_device(self, serial: str) -> None:
        self.app.select_device(serial)

    def _on_rename_device(self) -> None:
        """Pop a small modal asking for a friendly nickname.

        Empty input clears the nickname and falls back to the
        ADB-reported model name. Whitespace is trimmed and we cap the
        nickname at 32 chars so it doesn't blow up the sidebar layout
        (where it has to share a row with the last-4-of-serial chip).

        We rely on the simpler ``simpledialog.askstring`` rather than
        rolling a CustomTkinter modal because it gives us native
        keyboard handling on macOS (Enter to confirm, Esc to cancel,
        Cmd+V works) for free, which is exactly the friction-free UX
        we want for what should be a 5-second action.
        """
        from tkinter import simpledialog

        e = self.app.selected_entry()
        if e is None:
            return
        suggested = e.label or e.model or ""
        new = simpledialog.askstring(
            "ตั้งชื่อเครื่อง",
            (
                "ชื่อที่จะแสดงใน Sidebar (เช่น \"บัญชี A\", \"ทดลอง\")\n"
                "ปล่อยว่าง = ใช้ชื่อรุ่นจาก ADB"
            ),
            initialvalue=suggested,
            parent=self,
        )
        if new is None:  # user cancelled
            return
        new = new.strip()[:32]

        # Persist the new label and force a sidebar+card refresh.
        self.app.devices_lib.upsert(e.serial, label=new)
        self.app.save_devices()
        self._refresh_sidebar()
        self._refresh_main()

    def _on_setup_wifi(self) -> None:
        """Enable wireless ADB on a USB-connected device.

        We reuse ``StudioApp.setup_wifi_after_patch`` here instead of
        duplicating the ``adb tcpip + adb connect`` plumbing — it
        already knows how to (1) read the phone's wlan0 IP, (2) flip
        adbd into TCP mode, (3) probe the resulting wireless transport,
        and (4) persist the IP into ``devices.json`` so the next launch
        auto-reconnects.
        """
        e = self.app.selected_entry()
        if e is None:
            return
        if self.app.transport_of(e.serial) != "usb":
            messagebox.showwarning(
                "ต้องเสียบสาย USB",
                "การตั้งค่า WiFi ครั้งแรก ต้องเสียบสาย USB ไว้ก่อน\n"
                "เพื่อให้ระบบเปิด adb tcpip บนเครื่องได้\n\n"
                "เสียบ USB → เลือกเครื่องนี้ใน Sidebar → กดปุ่มนี้อีกครั้ง",
            )
            return
        self.btn_setup_wifi.configure(state="disabled", text="กำลังตั้งค่า…")
        threading.Thread(
            target=self._run_setup_wifi,
            args=(e.serial,),
            daemon=True,
        ).start()

    def _run_setup_wifi(self, serial: str) -> None:
        msg = self.app.setup_wifi_after_patch(serial)

        def _ui() -> None:
            try:
                self.btn_setup_wifi.configure(
                    state="normal", text="📶  ตั้งค่า WiFi",
                )
            except Exception:
                pass
            # ``setup_wifi_after_patch`` returns a Thai status string
            # that already starts with ✅ / ⚠️ / 📶 — surface it as-is.
            if msg.startswith("✅"):
                messagebox.showinfo("ตั้งค่า WiFi สำเร็จ", msg)
            else:
                messagebox.showwarning("ตั้งค่า WiFi", msg)
            self._refresh_main()

        self.after(0, _ui)

    def _on_reconnect_wifi(self) -> None:
        e = self.app.selected_entry()
        if e is None or not e.has_wifi():
            return
        self.btn_reconnect_wifi.configure(
            state="disabled", text="กำลังเชื่อม…",
        )
        threading.Thread(
            target=self._run_reconnect_wifi,
            args=(e.serial,),
            daemon=True,
        ).start()

    def _run_reconnect_wifi(self, serial: str) -> None:
        ok, msg = self.app.reconnect_wifi(serial)
        def _ui():
            try:
                self.btn_reconnect_wifi.configure(
                    state="normal", text="📶  เชื่อม WiFi อีกครั้ง",
                )
            except Exception:
                pass
            if ok:
                messagebox.showinfo("สำเร็จ", msg)
            else:
                messagebox.showwarning("เชื่อมไม่สำเร็จ", msg)
            self._refresh_main()
        self.after(0, _ui)

    def _on_add_device(self) -> None:
        self.app.go_wizard()

    def _on_restart_adb(self) -> None:
        """Run ``adb kill-server`` + ``adb start-server`` from the
        Dashboard (v1.8.1).

        Identical mechanics to the wizard's step-1 restart button,
        wired to the same ``AdbController.restart_server`` helper —
        we just expose it here too because the symptom most often
        presents on the Dashboard ("every device offline") rather
        than during onboarding.

        Worker thread so the UI doesn't freeze; on completion we
        force a synchronous device refresh so the sidebar stops
        showing stale "offline" badges within a second.
        """
        self.btn_restart_adb.configure(
            state="disabled",
            text="⏳  กำลังรีสตาร์ท ADB…",
        )

        def _worker() -> None:
            ok = False
            try:
                ok = self.app.adb.restart_server()
            except Exception:
                log.exception("Dashboard restart_adb crashed")

            def _show_result() -> None:
                try:
                    self.btn_restart_adb.configure(
                        state="normal",
                        text="🔄  รีสตาร์ท ADB",
                    )
                except Exception:
                    return  # widget gone (page changed)
                if ok:
                    # Force a same-tick device refresh so the
                    # sidebar's "offline" badges go green within
                    # ~1 s (otherwise customers wait the full
                    # 2 s poller interval and assume nothing
                    # happened).
                    try:
                        self.app.refresh_devices_now(timeout=3.0)
                    except Exception:
                        pass
                    self._refresh_sidebar()
                    messagebox.showinfo(
                        "สำเร็จ",
                        "รีสตาร์ท ADB เรียบร้อย\n"
                        "ระบบจะตรวจจับเครื่องที่เสียบ USB อีกครั้งภายใน 1-2 วินาที",
                    )
                else:
                    messagebox.showwarning(
                        "ผิดพลาด",
                        "รีสตาร์ท ADB ไม่สำเร็จ\n"
                        "ลองปิดโปรแกรมที่ใช้ ADB อยู่ (scrcpy / Android Studio "
                        "/ Vysor) แล้วลองกดปุ่มนี้อีกครั้ง",
                    )

            self.after(0, _show_result)

        threading.Thread(
            target=_worker,
            daemon=True,
            name="dashboard-restart-adb",
        ).start()

    def _on_open_dashboard(self) -> None:
        """Launch the embedded sales dashboard.

        We delegate to ``StudioApp.open_dashboard`` so the server
        handle lives on the application object (one server per
        process), not on this page (which gets re-created on every
        navigation). This keeps the FastAPI server running in the
        background while the user navigates Settings → Dashboard
        → back to Dashboard without re-binding the port each time.
        """
        try:
            self.app.open_dashboard()
        except RuntimeError as exc:
            # Most likely cause: missing fastapi/uvicorn install on
            # the customer's Python. The desktop app keeps working;
            # only the dashboard is unavailable.
            messagebox.showerror(
                "เปิด Dashboard ไม่สำเร็จ",
                f"{exc}\n\n"
                "วิธีแก้: เปิด Terminal/Command Prompt แล้วพิมพ์:\n"
                "  pip install fastapi uvicorn httpx\n"
                "จากนั้นเปิดโปรแกรม NP Create ใหม่",
            )
        except Exception as exc:
            log.exception("opening dashboard")
            messagebox.showerror(
                "เปิด Dashboard ไม่สำเร็จ",
                f"เกิดข้อผิดพลาด: {exc}\n\n"
                "ลองปิด-เปิดโปรแกรมใหม่ หรือติดต่อแอดมิน",
            )

    def _on_pick_video(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            return
        path = filedialog.askopenfilename(
            title="เลือกคลิปวีดีโอ",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.app.devices_lib.update_video(e.serial, path)
        self.app.save_devices()
        self._refresh_main()

    # ── audio ────────────────────────────────────────────────────

    def _on_pick_audio(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            return
        path = filedialog.askopenfilename(
            title="เลือกไฟล์เสียง",
            filetypes=[
                ("Audio files", "*.mp3 *.wav *.m4a *.aac *.ogg"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        ext = Path(path).suffix.lower().lstrip(".")
        from ..hook_mode import AUDIO_VALID_EXTS
        if ext not in AUDIO_VALID_EXTS:
            messagebox.showwarning(
                "นามสกุลไฟล์ไม่รองรับ",
                f"รองรับเฉพาะ: {', '.join(AUDIO_VALID_EXTS)}",
            )
            return
        self.app.devices_lib.update_audio(e.serial, path)
        self.app.save_devices()
        self._refresh_main()

    def _on_push_audio(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            return
        if not e.last_audio or not Path(e.last_audio).is_file():
            messagebox.showwarning(
                "ยังไม่มีไฟล์เสียง", "กด 'เลือกไฟล์เสียง...' ก่อน",
            )
            return
        # Real-time refresh — see _on_patch_tiktok for the rationale.
        self.app.refresh_devices_now()
        if not self.app.is_online(e.serial):
            messagebox.showwarning(
                "ไม่ได้เชื่อมต่อ",
                f"เครื่อง {e.display_name()} ไม่ได้เชื่อมต่อ — เสียบ USB ก่อน",
            )
            return
        self.btn_audio_push.configure(state="disabled", text="กำลังส่ง…")
        self.lbl_audio_status.configure(
            text="กำลัง push ไฟล์เสียง…",
            text_color=THEME.fg_secondary,
        )
        threading.Thread(
            target=self._run_push_audio,
            args=(self.app.adb_id_for(e), Path(e.last_audio)),
            daemon=True,
        ).start()

    def _run_push_audio(self, serial: str, source: Path) -> None:
        # Match audio target to whichever TikTok variant the
        # device actually has -- same reasoning as the video
        # push above. ``push_audio_to_phone`` writes to the
        # public /sdcard/Music/ folder anyway (the in-process
        # AudioFeeder workaround) but the per-package fallback
        # path under /sdcard/Android/data/<pkg>/files/ does
        # depend on the variant.
        from ..hook_mode import TIKTOK_PACKAGE_DEFAULT
        entry = self.app.devices_lib.get(serial)
        pkg = (
            entry.tiktok_package
            if entry and entry.tiktok_package
            else TIKTOK_PACKAGE_DEFAULT
        )
        try:
            r = self.app.hook.push_audio_to_phone(
                source, serial=serial, package=pkg,
            )
        except Exception as ex:
            log.exception("push audio crashed")
            self._audio_done(False, f"crash: {ex}")
            return
        if not r.ok:
            self._audio_done(False, f"ล้มเหลว: {r.error}")
            return
        self._audio_done(
            True,
            f"สำเร็จ ({human_bytes(r.bytes)} ใน {r.elapsed_s:.1f} วิ) — "
            f"เปิดแอปเล่นเพลง → ค้นหา 'vcam_audio' ใน Music → ตั้ง Loop "
            f"→ เปิดลำโพงดังตอนไลฟ์",
        )

    def _audio_done(self, ok: bool, msg: str) -> None:
        def _ui():
            self.btn_audio_push.configure(
                state="normal", text="▶  ส่งไฟล์เสียงไปเครื่อง",
            )
            self.lbl_audio_status.configure(
                text=msg,
                text_color=THEME.success if ok else THEME.danger,
            )
        self.after(0, _ui)

    def _on_clear_audio(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            return
        if not messagebox.askyesno(
            "ยืนยันลบไฟล์เสียงแยก",
            "ลบไฟล์เสียงแยกบนโทรศัพท์ + ใน profile? "
            "ระบบจะกลับไปใช้เสียงจากคลิปวีดีโอ",
        ):
            return
        self.app.devices_lib.update_audio(e.serial, "")
        self.app.save_devices()
        if self.app.is_online(e.serial):
            threading.Thread(
                target=self._run_clear_audio,
                args=(self.app.adb_id_for(e),),
                daemon=True,
            ).start()
        self._refresh_main()

    def _run_clear_audio(self, serial: str) -> None:
        try:
            self.app.hook.remove_audio_from_phone(serial=serial)
        except Exception:
            log.exception("remove audio crashed")

    def _on_rotation_change(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            return
        self.app.devices_lib.update_transform(
            e.serial,
            rotation=self.rotation_var.get(),
            mirror_h=self.mirror_h_var.get(),
            mirror_v=self.mirror_v_var.get(),
        )
        self.app.save_devices()

    def _on_encode_push(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            return
        if not e.last_video or not Path(e.last_video).is_file():
            messagebox.showwarning(
                "ยังไม่มีคลิป",
                "กด 'เปลี่ยนคลิป...' เพื่อเลือกไฟล์ก่อนครับ",
            )
            return
        # Real-time refresh — see _on_patch_tiktok for the rationale.
        self.app.refresh_devices_now()
        if not self.app.is_online(e.serial):
            messagebox.showwarning(
                "ไม่ได้เชื่อมต่อ",
                f"เครื่อง {e.display_name()} ไม่ได้เชื่อมต่อ — เสียบ USB ก่อน",
            )
            return

        # Warn before chewing through a multi-GB encode. We choose
        # 1 GB as the warning line because:
        #   * 1 GB of 1080p ~= 9 minutes at typical bitrates -- well
        #     past the "loop a short clip" sweet spot for TikTok Live.
        #   * Encode on a mid-range PC takes 3-5 min for 1 GB; at that
        #     point the customer is right to want a confirmation
        #     before committing.
        #   * adb push of 1 GB over USB 2.0 is ~2 min, which still
        #     fits inside our adaptive timeout but is annoying.
        try:
            src_bytes = Path(e.last_video).stat().st_size
        except OSError:
            src_bytes = 0
        if src_bytes > 1024 ** 3:
            gb = src_bytes / (1024 ** 3)
            cont = messagebox.askyesno(
                "ไฟล์ใหญ่",
                f"คลิปนี้ขนาด {gb:.1f} GB\n\n"
                "การ encode + push อาจใช้เวลา 5-15 นาที (ขึ้นกับเครื่อง).\n"
                "แนะนำตัดให้สั้นลงเหลือ ≤ 500 MB หรือสั้นกว่า 5 นาที\n"
                "เพื่อให้สตรีมลื่นและ loop ได้บ่อย ๆ.\n\n"
                "ดำเนินการต่อ?",
            )
            if not cont:
                return

        self.btn_encode_push.configure(state="disabled", text="กำลังทำงาน…")
        self.progress.set(0.05)
        self.lbl_encode_status.configure(
            text="กำลัง encode คลิป…", text_color=THEME.fg_secondary,
        )
        threading.Thread(
            target=self._run_encode_push,
            args=(self.app.adb_id_for(e), Path(e.last_video)),
            daemon=True,
        ).start()

    def _run_encode_push(self, serial: str, source: Path) -> None:
        # 1. Build a single-file playlist.
        try:
            pl = write_playlist([source], loop=self.app.cfg.loop_playlist)
        except Exception as ex:
            log.exception("playlist write failed")
            self._encode_done(False, f"playlist write failed: {ex}")
            return

        # 2. Use the device's ProfileLibrary entry that matches the
        #    cfg default — for now that's enough; per-device rotation
        #    is layered on by FlipRenderer at runtime, not at encode
        #    time, so we hardcode "no extra rotation in ffmpeg".
        prof = self.app.profiles.get(self.app.cfg.default_profile) or (
            self.app.profiles.profiles[0] if self.app.profiles.profiles else None
        )
        if prof is None:
            self._encode_done(False, "ไม่พบ device profile")
            return

        # ── progress bar split: encode = 0..0.5, push = 0.5..1.0 ──
        #
        # The hook pipeline reports (pct, msg) on a worker thread;
        # we forward to the Tk thread via ``after(0, ...)``. We
        # *can't* call widget methods directly here -- Tk is single-
        # threaded and silently corrupts state if called from the
        # wrong thread. Wrapping every callback in ``after(0, ...)``
        # also coalesces rapid updates: if 50 ffmpeg ticks fire in
        # 100 ms, Tk will only paint once.
        def _on_encode_progress(pct: float, msg: str) -> None:
            self.after(
                0,
                lambda p=pct, m=msg: self._set_progress_status(p * 0.5, m),
            )

        try:
            self.after(0, lambda: self._set_progress_status(
                0.0, "เตรียม encode…",
            ))
            r = self.app.hook.encode_playlist(
                playlist_file=pl,
                profile=prof,
                output_path=self.app.local_mp4,
                progress_cb=_on_encode_progress,
            )
        except Exception as ex:
            log.exception("encode crashed")
            self._encode_done(False, f"encode crashed: {ex}")
            return
        finally:
            try:
                pl.unlink(missing_ok=True)
            except Exception:
                pass

        if not r.ok:
            self._encode_done(False, f"Encode ล้มเหลว: {r.error}")
            return

        self.after(0, lambda sz=r.bytes: self._set_progress_status(
            0.5, f"Encode สำเร็จ ({human_bytes(sz)}). กำลัง push…"
        ))

        def _on_push_progress(pct: float, msg: str) -> None:
            self.after(
                0,
                lambda p=pct, m=msg: self._set_progress_status(
                    0.5 + p * 0.5, m,
                ),
            )

        # Per-device TikTok variant: customers running TikTok Lite
        # or Douyin live under different package names, and the
        # /sdcard/Android/data/<pkg>/files/ path varies accordingly.
        # We saved the detected variant on each successful hook-
        # status probe, so by the time the customer clicks Encode
        # + Push it's almost always populated. Falls back to the
        # global default for legacy devices that pre-date the
        # detection (mostly: customers who patched on v1.4.x and
        # haven't opened the dashboard yet on v1.5+).
        #
        # The ``serial`` param here is actually the adb_id (USB
        # serial OR ip:port for WiFi). For WiFi rows we need to
        # resolve the canonical USB serial to look up the entry,
        # otherwise devices_lib.get() returns None and we'd
        # silently route the broadcast at the wrong package.
        from ..hook_mode import TARGET_PATH_TEMPLATE, TIKTOK_PACKAGE_DEFAULT
        entry_for_target = self.app.devices_lib.get(serial)
        if entry_for_target is None:
            # Likely a WiFi row whose serial here is "ip:port".
            # Reverse-look-up the canonical USB serial via the
            # transport map the poller maintains.
            for canonical, adb_id in self.app.adb_id_for_serial.items():
                if adb_id == serial:
                    entry_for_target = self.app.devices_lib.get(canonical)
                    break
        pkg_for_target = (
            entry_for_target.tiktok_package
            if entry_for_target and entry_for_target.tiktok_package
            else TIKTOK_PACKAGE_DEFAULT
        )
        target = TARGET_PATH_TEMPLATE.format(pkg=pkg_for_target)
        try:
            push = self.app.hook.push_to_phone(
                self.app.local_mp4,
                serial=serial,
                target=target,
                progress_cb=_on_push_progress,
                tiktok_pkg=pkg_for_target,
            )
        except Exception as ex:
            log.exception("push crashed")
            self._encode_done(False, f"push crashed: {ex}")
            return

        if not push.ok:
            self._encode_done(False, f"Push ล้มเหลว: {push.error}")
            return

        self._encode_done(
            True,
            f"สำเร็จ ({human_bytes(push.bytes)} ใน {push.elapsed_s:.1f} วิ)",
        )

    def _set_progress_status(self, value: float, text: str) -> None:
        self.progress.set(value)
        self.lbl_encode_status.configure(text=text)

    def _encode_done(self, ok: bool, msg: str) -> None:
        """Surface the encode+push outcome to the customer.

        Up through v1.7.3 the result lived in a small green/red
        label under the button. Multiple customers (most recently
        the v1.7.4 Patch test) reported "ไฟล์ไม่เข้าเครื่อง" even
        when the push had succeeded — they never registered the
        small-text update because the action takes minutes and
        their attention had drifted.

        We now also pop a modal dialog with the success/failure
        message so it's *impossible* to miss. The dialog also
        nudges the customer to peek at the phone, which closes
        the loop on the most common follow-up question
        ("เห็นแล้วยังเป็นคลิปเก่าอยู่").
        """
        def _ui():
            self.btn_encode_push.configure(
                state="normal", text="▶  Encode + Push",
            )
            self.progress.set(1.0 if ok else 0.0)
            self.lbl_encode_status.configure(
                text=msg,
                text_color=THEME.success if ok else THEME.danger,
            )
            if ok:
                messagebox.showinfo(
                    "✓ ส่งคลิปสำเร็จ",
                    f"{msg}\n\n"
                    "TikTok บนมือถือกำลังโหลดคลิปใหม่ให้อัตโนมัติ "
                    "(≤ 2 วิ).\n\n"
                    "ถ้ายังเห็นคลิปเก่าอยู่ — ปิด-เปิดกล้อง / ปิด TikTok "
                    "แล้วเปิดใหม่ 1 ครั้ง.",
                )
            else:
                messagebox.showerror(
                    "Encode + Push ล้มเหลว",
                    f"{msg}\n\n"
                    "ลองตรวจ:\n"
                    "• สาย USB / WiFi ADB ยังต่ออยู่ไหม\n"
                    "• พื้นที่บนมือถือพอไหม (ต้องมี ≥ 500 MB)\n"
                    "• คลิปต้นฉบับเสีย/ขนาดใหญ่เกินไปไหม",
                )
        self.after(0, _ui)

    def _on_open_tiktok(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            messagebox.showwarning(
                "ไม่ได้เชื่อมต่อ",
                "เสียบ USB หรือเชื่อม WiFi แล้วเลือกเครื่องก่อน",
            )
            return
        # Real-time refresh — see _on_patch_tiktok for the rationale.
        self.app.refresh_devices_now()
        if not self.app.is_online(e.serial):
            messagebox.showwarning(
                "ไม่ได้เชื่อมต่อ",
                "เสียบ USB หรือเชื่อม WiFi แล้วเลือกเครื่องก่อน",
            )
            return
        threading.Thread(
            target=self._run_open_tiktok,
            args=(self.app.adb_id_for(e),),
            daemon=True,
        ).start()

    def _run_open_tiktok(self, serial: str) -> None:
        # Pick the variant we previously detected for this device;
        # avoids "monkey: no activities found" when the customer
        # only has TikTok Lite installed and we'd otherwise try
        # to launch the international package.
        from ..hook_mode import TIKTOK_PACKAGE_DEFAULT
        entry = self.app.devices_lib.find_by_wifi_id(serial)
        if entry is None:
            # ``serial`` here is the adb id (could be ``IP:port``)
            # so we also fall back to a direct serial lookup.
            entry = self.app.devices_lib.get(self.app.selected_serial or "")
        pkg = (
            entry.tiktok_package
            if entry and entry.tiktok_package
            else TIKTOK_PACKAGE_DEFAULT
        )
        cmd = (
            f"monkey -p {pkg} "
            "-c android.intent.category.LAUNCHER 1"
        )
        out = self.app.adb.shell(cmd, serial=serial, timeout=8)
        log.info("launch TikTok %s: %s", pkg, out[:200])

    def _on_force_update_repatch(self) -> None:
        """Walk the customer through the "TikTok forced an update
        before letting me Live" recovery path.

        Why this exists
        ---------------
        TikTok's Live broadcast endpoint runs a server-side minimum
        version check. If our last patch is "too old" by TikTok's
        standards (they raise the bar every 4-8 weeks), tapping
        "เริ่มไลฟ์" pops up a hard "ต้องอัปเดต" dialog with no
        Cancel — the only path forward is letting Play Store
        replace TikTok with the new version, which strips our
        LSPatch overlay.

        After they update, this handler re-runs ``pull → patch →
        install`` against the *new* TikTok version sitting on the
        phone. LSPatch is version-agnostic so a freshly-pulled APK
        patches and reinstalls cleanly, restoring vcam at a
        version high enough to pass TikTok's gate.

        UX note: the existing post-patch warning says "DO NOT tap
        Update inside TikTok." That guidance is correct for the
        common case (TikTok nagging you about a non-mandatory
        update) but contradicts what's needed here. The dialog
        text below disambiguates the two situations explicitly so
        the customer doesn't freeze up wondering which rule to
        follow."""
        e = self.app.selected_entry()
        if e is None:
            messagebox.showwarning(
                "เลือกเครื่องก่อน",
                "เลือกมือถือจากแถบข้างก่อนทำขั้นตอนนี้",
            )
            return

        msg = (
            "เคสนี้: TikTok ขึ้น \"ต้องอัปเดตเวอร์ชั่น\" ตอนกดเริ่มไลฟ์\n"
            "→ ระบบจะช่วย Re-Patch ให้ใช้งานได้เหมือนเดิม\n\n"
            "ทำตามนี้ทีละขั้น:\n\n"
            "  ① เปิด TikTok บนมือถือเครื่องนี้\n"
            "  ② กด \"อัปเดต / Update\" ตามที่ TikTok บอก\n"
            "       (vcam จะหายชั่วคราว — ปกติของขั้นตอน)\n"
            "  ③ รอจน Play Store ติดตั้ง TikTok เสร็จ\n"
            "       (ดูว่าเปิด TikTok แล้วไม่มี popup update อีก)\n"
            "  ④ เสียบสาย USB เครื่องนี้กับคอม\n"
            "  ⑤ กด \"ดำเนินการ Re-Patch\" ด้านล่าง\n\n"
            "พร้อมแล้วใช่ไหม? (ระบบจะ pull TikTok ตัวใหม่ → patch → "
            "ติดตั้งทับให้อัตโนมัติ ใช้เวลา ~1 นาที)"
        )
        if not messagebox.askyesno(
            "🆙 อัปเดต TikTok + Re-Patch",
            msg,
        ):
            return

        # Reuse the existing drift-recovery path. It already does
        # online-check → pull → patch → install → record version.
        self._trigger_repatch(e.serial)

    def _on_patch_tiktok(self) -> None:
        e = self.app.selected_entry()
        if e is None:
            messagebox.showwarning(
                "ไม่ได้เชื่อมต่อ", "เสียบ USB และเลือกเครื่องก่อน",
            )
            return
        # Force a real-time adb refresh BEFORE the gate-keeping
        # checks. The background poller has a ~2s cadence; without
        # this, a customer who just plugged in the cable and
        # clicked Patch immediately would see a misleading "ต้อง
        # เสียบสาย USB" warning even though adb devices already
        # lists the phone over USB.
        self.app.refresh_devices_now()
        if not self.app.is_online(e.serial):
            messagebox.showwarning(
                "ไม่ได้เชื่อมต่อ", "เสียบ USB และเลือกเครื่องก่อน",
            )
            return
        # Patch *must* run over USB because the post-install step
        # `adb tcpip` only works against a wired transport. Refuse
        # politely if the customer is currently on WiFi instead of
        # silently failing in the middle of a 200 MB pull.
        if self.app.transport_of(e.serial) != "usb":
            messagebox.showwarning(
                "ต้องเสียบสาย USB",
                "การ Patch ต้องทำผ่าน USB เพื่อให้โปรแกรมตั้งค่า WiFi "
                "ให้อัตโนมัติได้\n\nกรุณาเสียบสาย USB แล้วลองใหม่",
            )
            return

        # Pre-flight: refuse to even start the patch flow if the
        # phone has no TikTok installed. The pipeline would fail at
        # the first step ("pull ล้มเหลว: no TikTok variant installed"),
        # but customers misread that as our app being broken.
        # Showing a clear, actionable dialog here saves a support
        # ticket and stops users from re-running Patch in confusion.
        if not self._ensure_tiktok_present(e.serial):
            return

        if not messagebox.askyesno(
            "ยืนยัน Patch TikTok",
            "ขั้นตอนนี้จะถอน TikTok เดิม ติดตั้งเวอร์ชัน patched ใหม่. "
            "คุณจะหลุดล็อกอินเดิม. ดำเนินการ?",
        ):
            return
        self.btn_patch.configure(state="disabled", text="กำลัง patch…")
        threading.Thread(
            target=self._run_patch,
            args=(e.serial,),
            daemon=True,
        ).start()

    def _ensure_tiktok_present(self, serial: str) -> bool:
        """Verify TikTok is installed on ``serial`` before patching.

        Returns ``True`` if patching can proceed. Returns ``False``
        and shows a friendly recovery dialog (with a button to launch
        Play Store on the device) if no TikTok variant is detected.
        """
        try:
            pkg = self.app.lspatch.detect_tiktok(serial)
        except Exception:
            log.exception("detect_tiktok crashed; allowing patch to proceed")
            # If detection itself crashes, don't block the user — the
            # pipeline will surface the real error a few seconds later.
            return True

        if pkg:
            return True

        # No TikTok found — offer the Play Store path. The customer's
        # device is the one with Play Store credentials, OTP, etc., so
        # all we can do from PC is launch the right intent.
        choice = messagebox.askyesno(
            "ยังไม่มี TikTok ในเครื่อง",
            (
                "ไม่พบ TikTok ที่ลงในเครื่องนี้\n\n"
                "ก่อน Patch ต้องลง TikTok บนมือถือก่อน "
                "(ผ่าน Play Store ก็ได้) แล้วค่อยกลับมากด Patch อีกครั้ง\n\n"
                "ต้องการให้เปิด Play Store บนเครื่องนี้ "
                "เพื่อค้นหา TikTok ให้ตอนนี้ไหม?"
            ),
        )
        if choice:
            self._open_play_store_search(serial, "TikTok")
        return False

    def _open_play_store_search(self, serial: str, query: str) -> None:
        """Send an `am start` intent to open Play Store to a search query.

        This is the smoothest recovery path when the phone is missing
        TikTok: the customer doesn't have to type the search themselves
        on a tiny screen, and we can't sign in to their Google account
        for them anyway.
        """
        adb = self.app.cfg.adb_path
        intent_uri = f"market://search?q={query}"
        try:
            r = subprocess.run(
                [adb, "-s", serial, "shell",
                 "am", "start",
                 "-a", "android.intent.action.VIEW",
                 "-d", intent_uri],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if r.returncode != 0:
                log.warning("Play Store launch returncode=%s stderr=%r",
                            r.returncode, r.stderr[:200])
                # Fallback to web URL if market:// scheme isn't handled
                # (rare — only on devices without Play Store at all).
                subprocess.run(
                    [adb, "-s", serial, "shell",
                     "am", "start",
                     "-a", "android.intent.action.VIEW",
                     "-d",
                     f"https://play.google.com/store/search?q={query}"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
            messagebox.showinfo(
                "เปิด Play Store แล้ว",
                "Play Store เปิดบนมือถือแล้ว\n\n"
                "1. กด \"Install\" ที่ TikTok\n"
                "2. รอจนลงเสร็จ + เปิด TikTok ดูว่าใช้งานได้\n"
                "3. กลับมาที่ NP Create แล้วกด Patch ใหม่อีกครั้ง",
            )
        except Exception as ex:
            log.exception("open Play Store failed")
            messagebox.showerror(
                "เปิด Play Store ไม่สำเร็จ",
                f"กรุณาเปิด Play Store บนมือถือเอง แล้วค้นหา {query}\n\n"
                f"รายละเอียด: {ex}",
            )

    def _run_patch(self, serial: str) -> None:
        ls = self.app.lspatch
        patched_version = ""
        patched_signature = ""
        try:
            tools = ls.probe_tools()
            if not tools.ok:
                self._patch_done(
                    serial, False,
                    "เครื่องมือไม่ครบ:\n" + "\n".join(tools.errors),
                )
                return
            pull = ls.pull_tiktok(serial=serial)
            if not pull.ok:
                # Translate the most common pull errors into customer
                # language so they know exactly what to fix without
                # reading the raw subprocess stderr.
                err_lower = (pull.error or "").lower()
                if "no tiktok variant" in err_lower:
                    msg = (
                        "ไม่พบ TikTok ในเครื่อง\n\n"
                        "กรุณาลง TikTok บนมือถือก่อน (ผ่าน Play Store) "
                        "แล้วค่อยกด Patch อีกครั้ง"
                    )
                else:
                    msg = f"pull ล้มเหลว: {pull.error}"
                self._patch_done(serial, False, msg)
                return
            # Capture the TikTok versionName we're about to patch so
            # the drift watcher can flag auto-updates later.
            patched_version = pull.version_name or ""
            patched = ls.patch(pull.apks)
            if not patched.ok:
                self._patch_done(serial, False, f"patch ล้มเหลว: {patched.error}")
                return
            inst = ls.install(
                package=pull.package,
                patched_apks=patched.patched_apks,
                serial=serial,
                # Pass the originally pulled APKs so the pipeline can
                # roll back to a working TikTok install if our patched
                # bundle fails to install. This is what prevents the
                # "Patch กดแล้ว TikTok หายไปจากเครื่อง" failure mode.
                original_apks=pull.apks,
            )
            if not inst.ok:
                # Build a customer-friendly message that distinguishes
                # the two very different post-failure states:
                #   1. Rollback OK → TikTok เดิมยังอยู่ — ลองใหม่ได้
                #   2. Rollback ไม่สำเร็จ / ไม่ได้ทำ → TikTok หาย, ต้อง
                #      ลงใหม่จาก Play Store ก่อนค่อย Patch ใหม่
                if inst.rollback_attempted and inst.rollback_ok:
                    msg = (
                        f"install ล้มเหลว: {inst.error}\n\n"
                        "✓ กู้ TikTok เดิมกลับเรียบร้อยแล้ว — "
                        "เครื่องยังใช้งานได้ปกติ\n"
                        "ลองกด Patch อีกครั้งได้เลย"
                    )
                elif inst.rollback_attempted:
                    msg = (
                        f"install ล้มเหลว: {inst.error}\n\n"
                        f"⚠️  กู้ TikTok เดิมไม่สำเร็จ: {inst.rollback_error}\n"
                        "กรุณาลง TikTok ใหม่จาก Play Store แล้วค่อย Patch อีกครั้ง"
                    )
                else:
                    msg = (
                        f"install ล้มเหลว: {inst.error}\n\n"
                        "⚠️  TikTok เดิมอาจหายไปจากเครื่อง — "
                        "กรุณาลง TikTok ใหม่จาก Play Store"
                    )
                self._patch_done(serial, False, msg)
                return
            # Persist the install-time signature as the per-device
            # patched-detection baseline. The hook-status probe
            # uses this for *exact-match* fingerprint comparison,
            # which is the only check immune to OEM-ROM dumpsys
            # output differences.
            patched_signature = (inst.fingerprint or "").lower()
        except Exception as ex:
            log.exception("patch flow crashed")
            self._patch_done(serial, False, f"crash: {ex}")
            return

        wifi_msg = self.app.setup_wifi_after_patch(serial)
        self._patch_done(
            serial, True, "Patch สำเร็จ",
            wifi_msg=wifi_msg,
            tiktok_version=patched_version,
            tiktok_signature=patched_signature,
        )

    def _patch_done(
        self,
        serial: str,
        ok: bool,
        msg: str,
        wifi_msg: str = "",
        tiktok_version: str = "",
        tiktok_signature: str = "",
    ) -> None:
        def _ui():
            self.btn_patch.configure(state="normal")
            if ok:
                self.app.devices_lib.mark_patched(
                    serial,
                    tiktok_version=tiktok_version,
                    signature=tiktok_signature,
                )
                self.app.save_devices()
                # Clear, customer-readable post-patch instructions.
                # The "DO NOT tap Update inside TikTok" warning is
                # the single biggest support-ticket reducer we can
                # add — see customer_devices.DeviceEntry docstring
                # for why TikTok's in-app updater silently breaks
                # our LSPatch overlay.
                ver_line = (
                    f"\n\nเวอร์ชัน TikTok ที่ Patch: {tiktok_version}"
                    if tiktok_version else ""
                )
                wifi_block = ("\n\n" + wifi_msg) if wifi_msg else ""
                messagebox.showinfo(
                    "✓ Patch สำเร็จ",
                    (
                        "ติดตั้ง TikTok ที่ patched แล้วเรียบร้อย"
                        + ver_line
                        + wifi_block
                        + "\n\n"
                        "⚠️  คำเตือนสำคัญ:\n"
                        "• อย่ากด \"อัปเดต / Update\" ใน TikTok ตามปกติ\n"
                        "  (popup ที่เด้งขึ้นเฉย ๆ) — vcam จะหาย\n"
                        "• ปิด auto-update ของ TikTok ใน Play Store\n"
                        "  ถ้าทำได้\n"
                        "\n"
                        "👉 ข้อยกเว้น: ถ้าตอน \"กดเริ่มไลฟ์\" "
                        "TikTok บังคับ\n"
                        "    update (ไม่กดไม่ได้) — ให้ update ตามนั้น\n"
                        "    แล้วกลับมาที่ NP Create กดปุ่ม\n"
                        "    \"🆙 TikTok บังคับ update ก่อนไลฟ์?\"\n"
                        "    เพื่อ Re-Patch ทับเวอร์ชันใหม่อัตโนมัติ"
                    ),
                )
            else:
                messagebox.showerror("ล้มเหลว", msg)
            self._refresh_main()
        self.after(0, _ui)


# ──────────────────────────────────────────────────────────────────
#  ModePickerPage  — v1.8.0: pick how the phone talks to the PC
# ──────────────────────────────────────────────────────────────────
#
# Background
# ----------
# Pre-1.8.0 the only "Add Device" path was USB → ADB → LSPatch
# → install patched TikTok. That hard requirement on Windows
# OEM USB drivers caused weeks of customer-support pain
# (Xiaomi/Redmi/POCO especially — see v1.7.11's driver-help
# dialog) and was a hard block for iPhone customers since
# iPhones don't speak ADB at all.
#
# Mode B (RTMP + virtual-camera app on the phone) sidesteps
# all of that: PC runs MediaMTX, phone runs CameraFi/Larix/DU
# Recorder pulling rtmp://<PC-IP>:1935/live, TikTok sees a
# regular "external camera". No USB cable, no driver, no
# patched APK. Setup is 2-3 minutes via QR code.
#
# We keep Mode A (USB + Patch) as the "premium quality" option
# because the patched TikTok stream has lower latency and no
# transcoding loss. Mode C (Wireless ADB) is reserved for a
# v1.8.1 follow-up — it still needs USB once for pairing on
# Android < 11 so it doesn't fully solve the "Mac เทสได้
# Windows ไม่ได้" complaint.


class ModePickerPage(ctk.CTkFrame):
    """One-screen picker between Mode A (USB+Patch) and Mode B
    (RTMP+virtual-cam). Shown immediately when the customer
    clicks "+ เพิ่มเครื่อง".
    """

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── header
        head = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)

        _ghost_button(
            head, "← ยกเลิก",
            command=self.app.go_dashboard,
        ).grid(row=0, column=0, padx=14, pady=12)

        ctk.CTkLabel(
            head, text="เพิ่มเครื่องใหม่ — เลือกวิธีเชื่อม",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=1, padx=14, sticky="w")

        # ── body
        body = ctk.CTkFrame(self, fg_color=THEME.bg_main)
        body.grid(row=1, column=0, sticky="nsew", padx=24, pady=20)
        body.grid_columnconfigure(0, weight=1)

        _h2(body, "เลือกวิธีที่ลูกค้าจะใช้").grid(
            row=0, column=0, sticky="w", pady=(8, 4)
        )
        _muted(
            body,
            "เลือก mode ตาม use-case ของลูกค้าได้เลย — แต่ละ mode มีข้อดี/ข้อเสียต่างกัน",
        ).grid(row=1, column=0, sticky="w", pady=(0, 16))

        # ── card: Mode B (recommended)
        card_b = self._mode_card(
            body,
            badge="แนะนำ",
            badge_color=THEME.success,
            icon="📡",
            title="ใช้ WiFi (ไม่ต้องเสียบ USB)",
            description=(
                "ลูกค้าลง app ฟรีบนมือถือ (CameraFi / Larix / DU Recorder)\n"
                "→ สแกน QR → เริ่มไลฟ์ได้เลย"
            ),
            pros=[
                "ไม่ต้องลง driver Windows",
                "ใช้ TikTok ตัวจริง — ไม่มี ban risk จาก patched APK",
                "ใช้ได้ทั้ง Android + iPhone",
                "Setup เสร็จใน 2-3 นาที",
            ],
            cons=[
                "Latency ~300-500ms (RTMP transcode)",
                "ลูกค้าต้องเชื่อม WiFi เดียวกับ PC",
                "ลูกค้าต้องเปิด virtual cam app ค้างไว้",
            ],
            command=self.app.go_rtmp_wizard,
        )
        card_b.grid(row=2, column=0, sticky="ew", pady=(0, 12))

        # ── card: Mode A (high quality)
        card_a = self._mode_card(
            body,
            badge="คุณภาพสูง",
            badge_color=THEME.primary,
            icon="⚡",
            title="ใช้ USB + Patch TikTok",
            description=(
                "เสียบ USB → patch TikTok ด้วย LSPatch → ไลฟ์\n"
                "วิธีเดิม (Mode A) ของ NP Create"
            ),
            pros=[
                "Latency ~50ms (เกือบ realtime)",
                "คุณภาพวิดีโอระดับกล้องจริง",
                "ไม่ต้องเชื่อม WiFi",
            ],
            cons=[
                "ต้องลง driver Windows ตามยี่ห้อมือถือ",
                "ต้องเสียบ USB cable",
                "Patched TikTok มี ban risk สูงกว่า",
            ],
            command=self.app.go_usb_wizard,
        )
        card_a.grid(row=3, column=0, sticky="ew", pady=(0, 12))

        # ── card: Mode C (Wireless ADB — Android 11+)
        card_c = self._mode_card(
            body,
            badge="ใหม่",
            badge_color=THEME.warning,
            icon="🔗",
            title="Wireless ADB (Android 11+)",
            description=(
                "Pair ผ่าน WiFi (ไม่ต้องเสียบ USB) → Patch TikTok\n"
                "ได้คุณภาพ Mode A โดยไม่ต้องลง driver Windows"
            ),
            pros=[
                "ไม่ต้องลง driver Windows",
                "คุณภาพระดับเดียวกับ Mode A (Patched TikTok)",
                "ใช้ ADB ปกติหลัง pair เสร็จ",
            ],
            cons=[
                "เฉพาะ Android 11+ เท่านั้น (iPhone ใช้ไม่ได้)",
                "ต้องเปิด Wireless Debugging ใน Developer Options",
                "Pair port หาย เมื่อปิดหน้าจอ Pair บนมือถือ",
            ],
            command=self.app.go_wireless_wizard,
        )
        card_c.grid(row=4, column=0, sticky="ew", pady=(0, 12))

    def _mode_card(
        self, parent, *, badge: str, badge_color: str, icon: str,
        title: str, description: str, pros: list[str], cons: list[str],
        command,
    ) -> ctk.CTkFrame:
        """Render one mode-option card with hover-highlight on
        the whole frame so the customer treats it as a single
        clickable surface, not a button-in-text mix."""
        card = ctk.CTkFrame(parent, fg_color=THEME.bg_card, corner_radius=10)
        card.grid_columnconfigure(0, weight=1)

        # Top row — badge + title
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 4))
        top.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            top, text=icon, font=ctk.CTkFont(size=24),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            top, text=title,
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ctk.CTkLabel(
            top, text=f"  {badge}  ",
            text_color="#000000", fg_color=badge_color,
            corner_radius=4,
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=3, sticky="e")

        ctk.CTkLabel(
            card, text=description,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))

        # Pros / cons row
        if pros or cons:
            pc = ctk.CTkFrame(card, fg_color="transparent")
            pc.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 12))
            pc.grid_columnconfigure(0, weight=1)
            pc.grid_columnconfigure(1, weight=1)

            if pros:
                col = ctk.CTkFrame(pc, fg_color="transparent")
                col.grid(row=0, column=0, sticky="nw", padx=(0, 12))
                for p in pros:
                    ctk.CTkLabel(
                        col, text=f"✓ {p}",
                        text_color=THEME.success,
                        font=ctk.CTkFont(size=11),
                        justify="left", anchor="w",
                    ).pack(anchor="w", pady=1)
            if cons:
                col = ctk.CTkFrame(pc, fg_color="transparent")
                col.grid(row=0, column=1, sticky="nw")
                for c in cons:
                    ctk.CTkLabel(
                        col, text=f"⚠ {c}",
                        text_color=THEME.warning,
                        font=ctk.CTkFont(size=11),
                        justify="left", anchor="w",
                    ).pack(anchor="w", pady=1)

        # Action row
        if command is not None:
            _primary_button(
                card, "เลือก mode นี้", command=command,
            ).grid(row=3, column=0, sticky="e", padx=20, pady=(0, 16))
        else:
            ctk.CTkLabel(
                card, text="(ยังไม่เปิดให้ใช้)",
                text_color=THEME.fg_muted,
                font=ctk.CTkFont(size=11, slant="italic"),
            ).grid(row=3, column=0, sticky="e", padx=20, pady=(0, 16))

        return card


# ──────────────────────────────────────────────────────────────────
#  RTMPWizardPage — Mode B "no-USB" flow (v1.8.0)
# ──────────────────────────────────────────────────────────────────


class RTMPWizardPage(ctk.CTkFrame):
    """Three-step setup for the Mode B WiFi flow.

    Step 1: Pick + install a virtual-camera app on the phone
            (CameraFi / Larix / DU Recorder). We render a Play
            Store QR code so the customer just points the
            phone's camera at the screen.
    Step 2: Start MediaMTX on the PC and show the
            ``rtmp://<PC-IP>:1935/live`` URL as a QR. The
            customer pastes it into their virtual-cam app's
            "RTMP Input" setting.
    Step 3: Pick a nickname for this phone and finish.

    This wizard never touches ADB or the phone's USB stack.
    The phone entry it creates in ``customer_devices`` has
    ``transport="rtmp"`` so the dashboard knows to show the
    streaming controls (start/stop FFmpeg → MediaMTX) instead
    of the Patch button.
    """

    STEPS = ("เลือกแอพ + ลง", "เริ่ม RTMP + สแกน QR", "ตั้งชื่อเล่น")

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app
        self._step = 0
        self._chosen_app_key: str = "camerafi"  # default = recommended()
        self._nickname_var = tk.StringVar(value="")
        self._rtmp_server = None  # lazy: created on step 2

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # header
        head = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)

        _ghost_button(
            head, "← เลือก mode อื่น",
            command=self.app.go_wizard,
        ).grid(row=0, column=0, padx=14, pady=12)
        ctk.CTkLabel(
            head, text="เพิ่มเครื่องใหม่ (Mode B — WiFi)",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=1, padx=14, sticky="w")
        self.lbl_step = _muted(head, "")
        self.lbl_step.grid(row=0, column=2, padx=14, sticky="e")

        # body
        self.body = ctk.CTkFrame(self, fg_color=THEME.bg_main)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        # footer
        foot = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        foot.grid(row=2, column=0, sticky="ew")
        foot.grid_columnconfigure(1, weight=1)
        self.btn_prev = _ghost_button(foot, "← ก่อนหน้า", command=self._prev)
        self.btn_prev.grid(row=0, column=0, padx=14, pady=12)
        self.btn_next = _primary_button(foot, "ถัดไป →", command=self._next)
        self.btn_next.grid(row=0, column=2, padx=14, pady=12)

        self._render_step()

    # ── flow control ─────────────────────────────────────────

    def _render_step(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.lbl_step.configure(
            text=f"ขั้น {self._step + 1}/{len(self.STEPS)} · {self.STEPS[self._step]}"
        )
        getattr(self, f"_render_step_{self._step}")()
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.btn_prev.configure(
            state="normal" if self._step > 0 else "disabled",
        )
        if self._step == len(self.STEPS) - 1:
            self.btn_next.configure(text="✓  เสร็จสิ้น", state="normal")
        else:
            self.btn_next.configure(text="ถัดไป →", state="normal")

    def _next(self) -> None:
        if self._step == len(self.STEPS) - 1:
            self._finish()
            return
        self._step = min(self._step + 1, len(self.STEPS) - 1)
        self._render_step()

    def _prev(self) -> None:
        # Stop the RTMP server when navigating away from step 2 —
        # leaving an orphan mediamtx running confuses customers
        # who later try to start it again from a fresh wizard.
        if self._step == 1 and self._rtmp_server is not None:
            try:
                self._rtmp_server.stop()
            except Exception:
                pass
            self._rtmp_server = None
        self._step = max(self._step - 1, 0)
        self._render_step()

    # ── step 0: pick + install virtual-cam app ───────────────

    def _render_step_0(self) -> None:
        from .. import virtual_cam_apps

        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 1: ลง app บนมือถือ").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )
        _muted(
            wrap,
            "เลือก app ตัวใดตัวหนึ่งด้านล่าง สแกน QR ด้วยกล้องมือถือเพื่อไป Play Store",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        # Selector row
        chosen = virtual_cam_apps.by_key(self._chosen_app_key) or virtual_cam_apps.recommended()

        seg_frame = ctk.CTkFrame(wrap, fg_color="transparent")
        seg_frame.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))
        for i, app in enumerate(virtual_cam_apps.CATALOG):
            seg_frame.grid_columnconfigure(i, weight=1)

            def _pick(k=app.key):
                self._chosen_app_key = k
                self._render_step()

            is_active = app.key == chosen.key
            stars = "⭐" * app.rating
            ctk.CTkButton(
                seg_frame,
                text=f"{app.name}\n{stars}",
                command=_pick,
                fg_color=THEME.primary if is_active else THEME.bg_input,
                hover_color=THEME.primary_hover if is_active else THEME.bg_hover,
                text_color=THEME.fg_primary,
                corner_radius=8,
                height=60,
                font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=0, column=i, sticky="ew", padx=4)

        # Detail panel: QR + steps for chosen app
        detail = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        detail.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 24))
        detail.grid_columnconfigure(1, weight=1)

        # ── left: QR
        qr_label = self._qr_label(detail, chosen.playstore_url, size=200)
        if qr_label is not None:
            qr_label.grid(row=0, column=0, padx=14, pady=14, rowspan=3)

        # ── right: name + description + steps
        ctk.CTkLabel(
            detail, text=chosen.name,
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=1, sticky="w", padx=(0, 14), pady=(14, 0))

        ctk.CTkLabel(
            detail, text=chosen.description_th,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=11),
            justify="left", anchor="w", wraplength=360,
        ).grid(row=1, column=1, sticky="w", padx=(0, 14), pady=(2, 6))

        steps_text = "วิธีตั้งค่าใน app:\n" + "\n".join(
            f"  {i+1}. {s}" for i, s in enumerate(chosen.setup_steps_th)
        )
        ctk.CTkLabel(
            detail, text=steps_text,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=11),
            justify="left", anchor="nw",
        ).grid(row=2, column=1, sticky="nw", padx=(0, 14), pady=(0, 14))

        if chosen.notes_th:
            ctk.CTkLabel(
                wrap, text=f"💡 {chosen.notes_th}",
                text_color=THEME.warning,
                font=ctk.CTkFont(size=11, slant="italic"),
                justify="left", anchor="w", wraplength=600,
            ).grid(row=4, column=0, sticky="w", padx=24, pady=(0, 16))

    # ── step 1: start RTMP + show QR ─────────────────────────

    def _render_step_1(self) -> None:
        from .. import rtmp_server, virtual_cam_apps

        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 2: เปิด RTMP server + สแกน QR").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )
        _muted(
            wrap,
            "ระบบจะเปิด RTMP server บน PC ให้ลูกค้าใส่ URL ในแอพบนมือถือ",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        # Lazy start RTMP server
        if self._rtmp_server is None:
            self._rtmp_server = rtmp_server.RTMPServer()
        if not self._rtmp_server.is_running:
            ok = self._rtmp_server.start()
            if not ok:
                # Show the error and offer retry
                err = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
                err.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))
                ctk.CTkLabel(
                    err, text="❌ เปิด RTMP server ไม่ได้",
                    text_color=THEME.danger,
                    font=ctk.CTkFont(size=14, weight="bold"),
                ).pack(padx=14, pady=(14, 4), anchor="w")
                tail = "\n".join(self._rtmp_server.last_errors()[-5:]) or \
                    "(ไม่มี log — อาจพอร์ต 1935 ถูกใช้อยู่หรือ MediaMTX ไม่อยู่)"
                ctk.CTkLabel(
                    err, text=tail,
                    text_color=THEME.fg_secondary,
                    font=ctk.CTkFont(size=11, family="Menlo"),
                    justify="left", anchor="w", wraplength=600,
                ).pack(padx=14, pady=(0, 14), anchor="w")
                _ghost_button(
                    wrap, "🔄  ลองเปิดใหม่",
                    command=self._render_step,
                ).grid(row=3, column=0, sticky="w", padx=24, pady=(0, 16))
                self.btn_next.configure(state="disabled", text="ยังเปิดไม่ได้")
                return

        url_phone = self._rtmp_server.rtmp_url_for_phone
        is_routable = self._rtmp_server.is_lan_routable

        # Status panel
        status = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        status.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))
        status.grid_columnconfigure(1, weight=1)

        # Left: QR of rtmp:// URL
        qr_label = self._qr_label(status, url_phone, size=220)
        if qr_label is not None:
            qr_label.grid(row=0, column=0, padx=14, pady=14, rowspan=3)

        # Right: status text + URL
        ctk.CTkLabel(
            status, text="✅ RTMP server พร้อมใช้",
            text_color=THEME.success,
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=1, sticky="w", padx=(0, 14), pady=(14, 0))

        ctk.CTkLabel(
            status, text="URL ที่ลูกค้าใส่ในแอพมือถือ:",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=1, sticky="w", padx=(0, 14), pady=(8, 0))

        url_box = ctk.CTkEntry(
            status,
            font=ctk.CTkFont(size=14, family="Menlo", weight="bold"),
            text_color=THEME.fg_primary,
            fg_color=THEME.bg_card,
            border_width=1,
        )
        url_box.insert(0, url_phone)
        url_box.configure(state="readonly")
        url_box.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=(2, 14))

        if not is_routable:
            ctk.CTkLabel(
                wrap,
                text=(
                    "⚠️ PC ไม่ได้อยู่บน LAN — IP ที่แสดงคือ 127.0.0.1 "
                    "ลูกค้าจะเชื่อมไม่ได้ กรุณาเชื่อม WiFi/LAN ก่อน"
                ),
                text_color=THEME.warning,
                font=ctk.CTkFont(size=12),
                justify="left", anchor="w", wraplength=600,
            ).grid(row=3, column=0, sticky="w", padx=24, pady=(0, 8))

        # Recap of chosen app's setup steps
        chosen = virtual_cam_apps.by_key(self._chosen_app_key)
        if chosen:
            recap = ctk.CTkFrame(wrap, fg_color="transparent")
            recap.grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 16))
            ctk.CTkLabel(
                recap,
                text=f"ใน {chosen.name} บนมือถือ:",
                text_color=THEME.fg_primary,
                font=ctk.CTkFont(size=12, weight="bold"),
                anchor="w",
            ).pack(fill="x")
            ctk.CTkLabel(
                recap,
                text="\n".join(
                    f"  {i+1}. {s}"
                    for i, s in enumerate(chosen.setup_steps_th)
                ),
                text_color=THEME.fg_secondary,
                font=ctk.CTkFont(size=11),
                justify="left", anchor="w",
            ).pack(fill="x", pady=(2, 0))

    # ── step 2: nickname + finish ────────────────────────────

    def _render_step_2(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 3: ตั้งชื่อเล่นเครื่อง").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )
        _muted(
            wrap,
            "ตั้งชื่อให้จำง่าย เช่น 'มือถือลูกค้าเอ' หรือ 'iPhone หลัก'",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        ctk.CTkEntry(
            wrap,
            textvariable=self._nickname_var,
            placeholder_text="ชื่อเล่นเครื่อง...",
            font=ctk.CTkFont(size=14),
            height=40,
        ).grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))

    def _finish(self) -> None:
        from datetime import datetime
        import uuid

        from .. import customer_devices, virtual_cam_apps

        chosen = virtual_cam_apps.by_key(self._chosen_app_key)
        label = self._nickname_var.get().strip()
        if not label:
            label = f"WiFi · {chosen.name if chosen else 'RTMP'}"

        # Synthetic serial: "rtmp:" prefix + uuid4 last 12. The
        # rest of the app uses serial as primary key for device
        # entries, so we need something unique that won't ever
        # collide with a real ADB serial (real ones are hex 8/16/
        # 40, never start with a colon and never contain one).
        serial = f"rtmp:{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now().isoformat(timespec="seconds")

        entry = customer_devices.DeviceEntry(
            serial=serial,
            label=label,
            model=(chosen.name if chosen else "RTMP"),
            added_at=now_iso,
            created_at=now_iso,
            transport="rtmp",
            vcam_app_key=self._chosen_app_key,
        )

        try:
            self.app.devices_lib.entries[serial] = entry
            self.app.devices_lib.save()
        except Exception as e:
            messagebox.showerror(
                "บันทึกเครื่องไม่ได้",
                f"เพิ่มเครื่องล้มเหลว:\n{e}",
            )
            return

        # Don't stop the RTMP server — the dashboard wants to
        # keep streaming. The dashboard's "Mirror" / "Live"
        # button adopts ownership of the running mediamtx and
        # the FFmpeg loop on first interaction.
        self.app.go_dashboard()

    # ── helpers ──────────────────────────────────────────────

    def _qr_label(self, parent, data: str, size: int = 200):
        """Render a QR code of ``data`` and return a CTkLabel
        holding the PhotoImage. Returns None if qrcode/Pillow
        aren't importable (e.g. customer running an older
        portable build without the v1.8.0 deps); the caller
        is expected to handle that gracefully and just skip
        the QR — the URL is also displayed as text."""
        try:
            import qrcode
            from PIL import ImageTk
        except Exception:
            return None
        try:
            img = qrcode.make(data, box_size=8, border=2).convert("RGB")
            # Scale to requested pixel size, preserving sharp pixels.
            img = img.resize((size, size))
            tk_img = ImageTk.PhotoImage(img)
            lbl = ctk.CTkLabel(parent, text="", image=tk_img)
            # Keep a ref so GC doesn't reap the PhotoImage out
            # from under Tk before draw — Tk's image-ref dance.
            lbl._qr_image = tk_img  # type: ignore[attr-defined]
            return lbl
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────────
#  WirelessADBWizardPage — Mode C "no USB" flow (Android 11+)
# ──────────────────────────────────────────────────────────────────
#
# Why this page exists
# --------------------
# Mode A (USB+Patch) needs an OEM USB driver on Windows; lots of
# Xiaomi/Redmi/HyperOS customers can't get past that.
# Mode B (RTMP) avoids ADB entirely but trades latency for
# convenience. Mode C splits the difference: pair the phone over
# WiFi *once* via Android 11+'s Wireless Debugging UI, then run
# the same ADB+LSPatch flow as Mode A. Result: same patched-
# TikTok quality as Mode A, but no Windows driver required after
# pairing.
#
# The pairing flow is the manual "Pair device with pairing code"
# variant (not the QR variant — see ``_render_step_0`` for why).
# Customer reads the IP:port + 6-digit code off the phone screen
# and types it into the wizard. Once paired, we ``adb connect``
# against the phone's *separate* live-ADB port (also displayed
# on the same Android screen, just below the pair line) and
# hand the resulting adb-id off to the regular WizardPage so the
# customer continues with the familiar Patch step.


class WirelessADBWizardPage(ctk.CTkFrame):
    """Three steps before handoff to the regular Patch wizard:

    Step 0: เปิด Wireless Debugging + อ่าน IP:port + code
            (text instructions + 3 input fields)
    Step 1: ใส่ live-ADB port + connect
            (one input field + connect button)
    Step 2: hand off to ``WizardPage(pre_paired_serial=<id>)``
            so the customer continues with Patch + nickname
            using the same code path as Mode A.
    """

    STEPS = ("Pair กับมือถือ", "เชื่อมต่อ ADB", "ไปต่อที่ Patch")

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app
        self._step = 0
        # form state
        self._pair_ip = tk.StringVar(value="")
        self._pair_port = tk.StringVar(value="")
        self._pair_code = tk.StringVar(value="")
        self._connect_port = tk.StringVar(value="")
        self._connect_status = tk.StringVar(value="")
        self._paired_ok = False
        self._connected_serial: str | None = None
        self._error: str = ""

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)

        _ghost_button(
            head, "← เลือก mode อื่น",
            command=self.app.go_wizard,
        ).grid(row=0, column=0, padx=14, pady=12)
        ctk.CTkLabel(
            head, text="เพิ่มเครื่องใหม่ (Mode C — Wireless ADB)",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=1, padx=14, sticky="w")
        self.lbl_step = _muted(head, "")
        self.lbl_step.grid(row=0, column=2, padx=14, sticky="e")

        self.body = ctk.CTkFrame(self, fg_color=THEME.bg_main)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        foot = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        foot.grid(row=2, column=0, sticky="ew")
        foot.grid_columnconfigure(1, weight=1)
        self.btn_prev = _ghost_button(foot, "← ก่อนหน้า", command=self._prev)
        self.btn_prev.grid(row=0, column=0, padx=14, pady=12)
        self.btn_next = _primary_button(foot, "ถัดไป →", command=self._next)
        self.btn_next.grid(row=0, column=2, padx=14, pady=12)

        self._render_step()

    def _render_step(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.lbl_step.configure(
            text=f"ขั้น {self._step + 1}/{len(self.STEPS)} · {self.STEPS[self._step]}"
        )
        getattr(self, f"_render_step_{self._step}")()
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.btn_prev.configure(
            state="normal" if self._step > 0 else "disabled",
        )
        if self._step == 0:
            self.btn_next.configure(
                text="ถัดไป →" if self._paired_ok else "Pair ก่อน",
                state="normal" if self._paired_ok else "disabled",
            )
        elif self._step == 1:
            ok = self._connected_serial is not None
            self.btn_next.configure(
                text="ไปต่อ →" if ok else "เชื่อมต่อก่อน",
                state="normal" if ok else "disabled",
            )
        else:
            self.btn_next.configure(text="ไป Patch", state="normal")

    def _next(self) -> None:
        if self._step == len(self.STEPS) - 1:
            self._handoff()
            return
        self._step = min(self._step + 1, len(self.STEPS) - 1)
        self._render_step()

    def _prev(self) -> None:
        self._step = max(self._step - 1, 0)
        self._render_step()

    # ── step 0: pair ─────────────────────────────────────────

    def _render_step_0(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 1: Pair มือถือผ่าน WiFi").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )

        # Instructions
        instructions = (
            "บนมือถือ Android 11+:\n"
            "  1. Settings → About phone → กด 'Build number' 7 ครั้ง "
            "(ปลด Developer mode)\n"
            "  2. Settings → Developer options → เปิด 'Wireless debugging'\n"
            "  3. กด 'Pair device with pairing code'\n"
            "  4. มือถือจะแสดง:\n"
            "       IP address & port:  192.168.1.42 : 38765\n"
            "       Wi-Fi pairing code:  123456\n"
            "  5. ใส่ค่าที่เห็นในช่องข้างล่าง (อย่าปิดหน้าจอ Pair)"
        )
        ctk.CTkLabel(
            wrap, text=instructions,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            justify="left", anchor="nw",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        form = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        form.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 8))
        form.grid_columnconfigure(1, weight=1)
        form.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            form, text="IP มือถือ:",
            text_color=THEME.fg_primary,
        ).grid(row=0, column=0, sticky="e", padx=(14, 6), pady=12)
        ctk.CTkEntry(
            form, textvariable=self._pair_ip,
            placeholder_text="192.168.1.42",
            width=140,
        ).grid(row=0, column=1, sticky="w", pady=12)

        ctk.CTkLabel(
            form, text="พอร์ต Pair:",
            text_color=THEME.fg_primary,
        ).grid(row=0, column=2, sticky="e", padx=(14, 6), pady=12)
        ctk.CTkEntry(
            form, textvariable=self._pair_port,
            placeholder_text="38765",
            width=100,
        ).grid(row=0, column=3, sticky="w", pady=12)

        ctk.CTkLabel(
            form, text="Pairing code (6 หลัก):",
            text_color=THEME.fg_primary,
        ).grid(row=1, column=0, sticky="e", padx=(14, 6), pady=12)
        ctk.CTkEntry(
            form, textvariable=self._pair_code,
            placeholder_text="123456",
            width=140,
        ).grid(row=1, column=1, sticky="w", pady=12)

        _primary_button(
            wrap, "🔐  Pair", command=self._do_pair,
        ).grid(row=3, column=0, sticky="w", padx=24, pady=(8, 16))

        if self._error:
            ctk.CTkLabel(
                wrap, text=f"⚠ {self._error}",
                text_color=THEME.danger,
                font=ctk.CTkFont(size=12),
                justify="left", anchor="w", wraplength=600,
            ).grid(row=4, column=0, sticky="w", padx=24, pady=(0, 16))

        if self._paired_ok:
            ctk.CTkLabel(
                wrap, text="✅ Pair สำเร็จ — ไปขั้นถัดไปได้เลย",
                text_color=THEME.success,
                font=ctk.CTkFont(size=13, weight="bold"),
            ).grid(row=5, column=0, sticky="w", padx=24, pady=(0, 16))

    def _do_pair(self) -> None:
        from .. import platform_tools, wifi_adb

        ip = self._pair_ip.get().strip()
        port_s = self._pair_port.get().strip()
        code = self._pair_code.get().strip()
        if not (ip and port_s and code):
            self._error = "กรุณาใส่ครบทั้ง 3 ช่อง"
            self._render_step()
            return
        try:
            port = int(port_s)
        except ValueError:
            self._error = f"พอร์ตต้องเป็นตัวเลข (ใส่: {port_s!r})"
            self._render_step()
            return

        adb_path = platform_tools.find_adb()
        if not adb_path:
            self._error = "ไม่พบ adb — รัน tools/setup_ci_tools.py ก่อน"
            self._render_step()
            return

        ok, msg = wifi_adb.adb_pair(str(adb_path), ip, port, code)
        if ok:
            self._paired_ok = True
            self._error = ""
            # Pre-fill the IP for the connect step (port differs).
            self._pair_ip.set(ip)
        else:
            self._paired_ok = False
            self._error = f"Pair ไม่สำเร็จ: {msg}"
        self._render_step()

    # ── step 1: connect ──────────────────────────────────────

    def _render_step_1(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 2: เชื่อมต่อ ADB").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )

        instructions = (
            "บนหน้า Wireless debugging ของมือถือ จะมี 2 พอร์ต:\n"
            "   • IP address & port (ใต้หัวข้อ 'Wireless debugging')\n"
            "       ↑ ใช้ตัวนี้ สำหรับเชื่อม ADB\n"
            "   • Pairing IP & port (ที่ใช้ไปแล้วในขั้นที่ 1)\n"
            "       ↑ ตัวนี้ใช้ครั้งเดียว ปิดไปได้แล้ว\n"
            "\n"
            "ใส่พอร์ต ADB ในช่องข้างล่าง (IP เดียวกับขั้นที่ 1)"
        )
        ctk.CTkLabel(
            wrap, text=instructions,
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            justify="left", anchor="nw",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        form = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        form.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 8))
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            form, text=f"IP มือถือ: {self._pair_ip.get()}",
            text_color=THEME.fg_secondary,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=12)

        ctk.CTkLabel(
            form, text="พอร์ต ADB:",
            text_color=THEME.fg_primary,
        ).grid(row=1, column=0, sticky="e", padx=(14, 6), pady=12)
        ctk.CTkEntry(
            form, textvariable=self._connect_port,
            placeholder_text="42135",
            width=120,
        ).grid(row=1, column=1, sticky="w", pady=12)

        _primary_button(
            wrap, "🔗  Connect", command=self._do_connect,
        ).grid(row=3, column=0, sticky="w", padx=24, pady=(8, 16))

        status = self._connect_status.get()
        if status:
            color = (
                THEME.success if self._connected_serial
                else THEME.danger
            )
            ctk.CTkLabel(
                wrap, text=status,
                text_color=color,
                font=ctk.CTkFont(size=12),
                justify="left", anchor="w", wraplength=600,
            ).grid(row=4, column=0, sticky="w", padx=24, pady=(0, 16))

    def _do_connect(self) -> None:
        from .. import platform_tools, wifi_adb

        ip = self._pair_ip.get().strip()
        port_s = self._connect_port.get().strip()
        if not (ip and port_s):
            self._connect_status.set("กรุณาใส่พอร์ต ADB")
            self._connected_serial = None
            self._render_step()
            return
        try:
            port = int(port_s)
        except ValueError:
            self._connect_status.set("พอร์ตต้องเป็นตัวเลข")
            self._connected_serial = None
            self._render_step()
            return

        adb_path = platform_tools.find_adb()
        if not adb_path:
            self._connect_status.set("ไม่พบ adb")
            self._connected_serial = None
            self._render_step()
            return

        ok = wifi_adb.adb_connect(str(adb_path), ip, port)
        if ok:
            self._connected_serial = wifi_adb.format_wifi_id(ip, port)
            self._connect_status.set(
                f"✅ Connect สำเร็จ — adb id: {self._connected_serial}"
            )
        else:
            self._connected_serial = None
            self._connect_status.set(
                "❌ Connect ไม่สำเร็จ — ตรวจว่า phone อยู่ WiFi เดียวกับ PC"
            )
        self._render_step()

    # ── step 2: handoff to Patch wizard ──────────────────────

    def _render_step_2(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 3: ไปต่อที่ Patch TikTok").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )
        _muted(
            wrap,
            "พร้อมแล้ว — กด 'ไป Patch' เพื่อเข้าหน้า Patch + ตั้งชื่อ",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        info = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        info.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))
        ctk.CTkLabel(
            info,
            text=f"ADB id ที่จะใช้: {self._connected_serial}",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=13, family="Menlo"),
        ).pack(padx=14, pady=14, anchor="w")

        ctk.CTkLabel(
            wrap,
            text=(
                "💡 หลังจากนี้ขั้นตอน Patch + ตั้งชื่อจะเหมือน Mode A "
                "ทุกอย่าง — ระบบจะใช้ adb id ด้านบนแทน USB"
            ),
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=11, slant="italic"),
            justify="left", anchor="w", wraplength=600,
        ).grid(row=3, column=0, sticky="w", padx=24, pady=(0, 16))

    def _handoff(self) -> None:
        if not self._connected_serial:
            messagebox.showerror(
                "ยังไม่ได้เชื่อมต่อ",
                "กลับไปขั้น 2 และกด Connect ก่อน",
            )
            return
        self.app.show_page(
            WizardPage,
            pre_paired_serial=self._connected_serial,
        )


# ──────────────────────────────────────────────────────────────────
#  WizardPage  — guided "add a new phone" flow
# ──────────────────────────────────────────────────────────────────


class WizardPage(ctk.CTkFrame):
    """Add Device wizard — 4 steps with prev/next.

    Step 1: ขั้นตอนเปิด USB Debugging (text + tip)
    Step 2: รอตรวจพบเครื่องผ่าน USB (live status)
    Step 3: Patch & ติดตั้ง TikTok (button + progress)
    Step 4: ตั้งชื่อเล่น (entry + finish)
    """

    STEPS = ("USB Debugging", "เสียบ USB", "Patch TikTok", "ตั้งชื่อเล่น")

    def __init__(
        self,
        app,
        pre_paired_serial: str | None = None,
    ) -> None:
        """Construct the USB+Patch wizard.

        ``pre_paired_serial`` — v1.8.0 Mode C entry point. When the
        Wireless-ADB wizard finishes pairing+connecting a phone over
        WiFi (Android 11+ Wireless Debugging), it hands the resulting
        ``IP:port`` adb-id to this wizard so the customer doesn't
        re-walk the "เสียบ USB" step. Skipping straight to the Patch
        screen (step 2) when it's set.
        """
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app
        # Mode C handoff: jump straight to the Patch step if we
        # already have a working adb id.
        self._step = 2 if pre_paired_serial else 0
        self._candidate_serial: str | None = pre_paired_serial
        self._patched_ok = False
        # Stamped the first time step 1 (the "wait for USB device"
        # screen) renders. Used by the Windows driver-help nudge:
        # if we've been waiting > 15 s with no candidate, surface
        # the "ไม่เจอเครื่อง? ลง driver Windows" button. v1.7.11
        # finding — Mac-side ADB works without OEM drivers, but
        # Windows needs an INF; Xiaomi/Redmi customers were
        # silently stuck because the wizard never explained why
        # the popup wasn't firing.
        self._step1_first_shown_at: float | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── header
        head = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)

        _ghost_button(
            head, "← ยกเลิก",
            command=self.app.go_dashboard,
        ).grid(row=0, column=0, padx=14, pady=12)

        ctk.CTkLabel(
            head, text="เพิ่มเครื่องใหม่",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=1, padx=14, sticky="w")

        self.lbl_step = _muted(head, "")
        self.lbl_step.grid(row=0, column=2, padx=14, sticky="e")

        # ── content area
        self.body = ctk.CTkFrame(self, fg_color=THEME.bg_main)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        # ── footer
        foot = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        foot.grid(row=2, column=0, sticky="ew")
        foot.grid_columnconfigure(1, weight=1)

        self.btn_prev = _ghost_button(foot, "← ก่อนหน้า", command=self._prev)
        self.btn_prev.grid(row=0, column=0, padx=14, pady=12)
        self.btn_next = _primary_button(foot, "ถัดไป →", command=self._next)
        self.btn_next.grid(row=0, column=2, padx=14, pady=12)

        self._render_step()

        # tick the wizard once a second so step 2's "waiting for USB"
        # screen reflects live ADB state.
        self.after(1000, self._tick)

    def _tick(self) -> None:
        try:
            if self._step == 1:
                self._render_step()
        finally:
            try:
                self.after(1000, self._tick)
            except Exception:
                pass  # widget destroyed

    # ── step rendering ───────────────────────────────────────────

    def _render_step(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.lbl_step.configure(
            text=f"ขั้น {self._step + 1}/{len(self.STEPS)} · {self.STEPS[self._step]}"
        )
        getattr(self, f"_render_step_{self._step}")()
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.btn_prev.configure(
            state="normal" if self._step > 0 else "disabled",
        )
        if self._step == 0:
            self.btn_next.configure(text="ถัดไป →", state="normal")
        elif self._step == 1:
            ok = self._candidate_serial is not None
            self.btn_next.configure(
                text="ถัดไป →" if ok else "(รอเครื่อง)",
                state="normal" if ok else "disabled",
            )
        elif self._step == 2:
            self.btn_next.configure(
                text="ถัดไป →" if self._patched_ok else "Patch ก่อน",
                state="normal" if self._patched_ok else "disabled",
            )
        elif self._step == 3:
            self.btn_next.configure(text="✓  เสร็จสิ้น", state="normal")

    def _next(self) -> None:
        if self._step == 3:
            self._finish()
            return
        self._step = min(self._step + 1, len(self.STEPS) - 1)
        # Leaving step 1 — drop the "no device" timer so a later
        # back-navigation gets a fresh 15 s grace period before
        # the driver-help nudge re-appears.
        if self._step != 1:
            self._step1_first_shown_at = None
        self._render_step()

    def _prev(self) -> None:
        self._step = max(self._step - 1, 0)
        if self._step != 1:
            self._step1_first_shown_at = None
        self._render_step()

    # ── step 1 — instructions ────────────────────────────────────

    def _render_step_0(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 1: เปิด USB Debugging").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )
        _muted(
            wrap,
            "บนโทรศัพท์ Redmi/POCO/Mi (HyperOS / MIUI):",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        steps = [
            "1. ไปที่  ตั้งค่า → เกี่ยวกับโทรศัพท์",
            "2. กดที่  'เวอร์ชัน MIUI / HyperOS'  ติดต่อกัน  7  ครั้ง",
            "3. กลับไปที่  ตั้งค่า → การตั้งค่าเพิ่มเติม → ตัวเลือกสำหรับนักพัฒนา",
            "4. เปิด  'การแก้ไขข้อบกพร่องผ่าน USB'  (USB Debugging)",
            "5. เปิด  'ติดตั้งผ่าน USB'  (Install via USB)",
            "6. เปิด  'การเข้าถึงผ่าน USB'  (USB Access)",
        ]
        for i, s in enumerate(steps):
            ctk.CTkLabel(
                wrap, text=s,
                text_color=THEME.fg_primary,
                font=ctk.CTkFont(size=13),
                anchor="w", justify="left",
            ).grid(row=2 + i, column=0, sticky="w", padx=32, pady=4)

        _muted(
            wrap,
            "💡  เคล็ดลับ: ถ้าไม่เจอเมนูนักพัฒนา ให้กลับไปกด 'เวอร์ชัน' ซ้ำอีกครั้ง",
        ).grid(row=99, column=0, sticky="w", padx=24, pady=(20, 20))

    # ── step 2 — wait for device ─────────────────────────────────

    def _render_step_1(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 2: เสียบ USB และอนุญาต").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )

        _muted(
            wrap,
            "เสียบสาย USB เข้ากับ PC และกด 'อนุญาต' (Allow) "
            "บนโทรศัพท์เมื่อมีหน้าต่างถามว่า "
            "'อนุญาตการแก้ไขข้อบกพร่อง USB จากคอมพิวเตอร์เครื่องนี้'",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 20))

        # Live device list — bucket every visible row by ADB state so
        # we can show a *specific* status to the customer instead of
        # a generic "🔄 รอเครื่อง…". v1.8.0 fix for the persistent
        # "เสียบสาย กด Allow แล้วระบบไม่เชื่อมโทรศัพท์" complaint:
        # 90 % of those reports turned out to be a phone stuck in
        # ``unauthorized`` because either (a) the bundled adb's RSA
        # fingerprint differs from what the phone has in its
        # "Always allow" cache (needs daemon restart) or (b) the
        # customer hasn't actually tapped Allow yet (no popup
        # because USB mode = "Charge only" on HyperOS). Knowing
        # which bucket the device is in lets the wizard surface
        # the right next-action.
        #
        # We skip:
        # * WiFi rows that map back onto a known canonical serial —
        #   those are already set up.
        # * WiFi rows for unknown phones — never auto-onboard
        #   over WiFi (the phone has to come in over USB so the
        #   subsequent ``adb tcpip`` step has a transport to talk to).
        #
        # We do NOT silently skip already-patched USB devices any
        # more (v1.8.1 fix). Older builds (v1.7.x and the v1.8.0
        # initial release) hard-skipped any serial whose entry had
        # ``patched_at`` set, on the theory that "already onboarded
        # → use the dashboard". The unintended side-effect was the
        # most-common customer complaint we've ever fielded:
        #
        #   "เสียบ USB กด Allow แล้ว — ระบบบอกว่ารอเครื่อง
        #    เชื่อมต่อ ทั้งที่ adb เห็นเครื่องเลย"
        #
        # i.e. the wizard's "🔄 รอเครื่อง…" message looked
        # identical to "no device", so customers spent minutes
        # restarting cables / drivers / phones before realising
        # they were just being silently filtered. v1.8.1 keeps
        # the device visible and offers re-patch.
        from .. import wifi_adb

        patched_serials = {
            e.serial for e in self.app.devices_lib.list() if e.is_patched()
        }
        online_candidates: list = []
        already_patched: list = []
        unauthorized_devs: list = []
        offline_devs: list = []
        for d in self.app.live_devices:
            if wifi_adb.is_wifi_id(d.serial):
                continue
            if d.online:
                if d.serial in patched_serials:
                    already_patched.append(d)
                else:
                    online_candidates.append(d)
            elif (d.state or "").lower() == "unauthorized":
                unauthorized_devs.append(d)
            elif (d.state or "").lower() in ("offline", "bootloader", "recovery"):
                offline_devs.append(d)

        status_box = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        status_box.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 12))

        # ── 1. ONLINE & READY (fresh device) ───────────────────
        if online_candidates:
            d = online_candidates[0]
            self._candidate_serial = d.serial
            ctk.CTkLabel(
                status_box,
                text=f"🟢  พบเครื่อง: {d.model or d.serial}",
                text_color=THEME.success,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(padx=20, pady=14, anchor="w")
            _muted(
                status_box,
                f"serial: {d.serial}  ·  product: {d.product or '-'}",
            ).pack(padx=20, pady=(0, 14), anchor="w")
            return

        # ── 1b. ONLINE BUT ALREADY PATCHED ─────────────────────
        # The customer plugged in a phone that's already been
        # onboarded once. Surface it (don't silently filter) and
        # let them re-patch from the wizard. Re-patching is the
        # right action when:
        #   * TikTok auto-updated and lost the LSPatch hook
        #   * Customer factory-reset the phone
        #   * Patch was applied on an older NP Create version
        #     and we want to refresh to the latest LSPatch
        # The patch pipeline is idempotent (uninstalls the old
        # patched APK first), so re-patching is always safe.
        if already_patched:
            d = already_patched[0]
            self._candidate_serial = d.serial
            ctk.CTkLabel(
                status_box,
                text=f"🟢  พบเครื่อง: {d.model or d.serial}",
                text_color=THEME.success,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(padx=20, pady=14, anchor="w")
            _muted(
                status_box,
                f"serial: {d.serial}  ·  product: {d.product or '-'}\n"
                "ℹ️  เครื่องนี้เคย patch แล้ว — กด 'ถัดไป →' เพื่อ patch ใหม่อีกครั้ง\n"
                "(เช่น ถ้า TikTok update ตัวเอง หรือเปลี่ยนเครื่อง factory reset)",
            ).pack(padx=20, pady=(0, 14), anchor="w")
            return

        self._candidate_serial = None

        # ── 2. UNAUTHORIZED — phone visible, Allow not yet tapped
        #      (or tapped but adb daemon hasn't refreshed yet). ───
        if unauthorized_devs:
            d = unauthorized_devs[0]
            ctk.CTkLabel(
                status_box,
                text="📱  เห็นเครื่องแล้ว — กรุณากด 'Allow' บนหน้าจอมือถือ",
                text_color=THEME.warning,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(padx=20, pady=14, anchor="w")
            _muted(
                status_box,
                f"serial: {d.serial}  ·  state: unauthorized\n"
                "ถ้ามี popup 'อนุญาตการแก้ไขข้อบกพร่อง USB?' → กด 'อนุญาต' (ติ๊ก 'จดจำเสมอ' ด้วย)\n"
                "ถ้าไม่เห็น popup → ลองสายใหม่ / เปลี่ยนพอร์ต USB / กดปุ่มด้านล่าง",
            ).pack(padx=20, pady=(0, 14), anchor="w")

            # Action row: restart adb daemon (fixes the "Allow
            # tapped but state stuck" case where the bundled adb
            # has a different RSA key than the phone's "Always
            # allow" cache expects).
            actions = ctk.CTkFrame(wrap, fg_color="transparent")
            actions.grid(row=3, column=0, sticky="w", padx=24, pady=(0, 8))
            _ghost_button(
                actions,
                "🔄  รีสตาร์ท ADB",
                command=self._restart_adb,
            ).pack(side="left", padx=(0, 8))
            self._render_hyperos_hint(wrap, row=4)
            return

        # ── 3. OFFLINE — phone visible but transport dead. ──────
        if offline_devs:
            d = offline_devs[0]
            ctk.CTkLabel(
                status_box,
                text=f"🔌  เครื่อง offline (state: {d.state})",
                text_color=THEME.warning,
                font=ctk.CTkFont(size=14, weight="bold"),
            ).pack(padx=20, pady=14, anchor="w")
            _muted(
                status_box,
                f"serial: {d.serial}\n"
                "ลองถอดสายแล้วเสียบใหม่ — ถ้ายังไม่ได้กดปุ่ม 'รีสตาร์ท ADB'",
            ).pack(padx=20, pady=(0, 14), anchor="w")
            _ghost_button(
                wrap,
                "🔄  รีสตาร์ท ADB",
                command=self._restart_adb,
            ).grid(row=3, column=0, sticky="w", padx=24, pady=(0, 8))
            return

        # ── 4. NO DEVICE AT ALL ─────────────────────────────────
        ctk.CTkLabel(
            status_box,
            text="🔄  รอเครื่องเชื่อมต่อ…",
            text_color=THEME.warning,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(padx=20, pady=14, anchor="w")
        _muted(
            status_box,
            "ระบบจะตรวจจับเครื่องอัตโนมัติเมื่อเสียบ USB",
        ).pack(padx=20, pady=(0, 14), anchor="w")

        # Action row stacked under the status: restart-adb is
        # cheap and helps customers whose adb daemon predates the
        # phone plug-in (common when scrcpy left a stale v40
        # daemon on port 5037 before NP Create launched).
        actions = ctk.CTkFrame(wrap, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="w", padx=24, pady=(0, 8))
        _ghost_button(
            actions,
            "🔄  รีสตาร์ท ADB",
            command=self._restart_adb,
        ).pack(side="left", padx=(0, 8))

        # Windows-only driver-help nudge. Pop the helper button
        # ~15 s into the wait so we don't startle customers who
        # are still finding their USB cable, but we don't leave
        # the genuinely-stuck-on-driver case to figure it out
        # alone. Mac users never see this — Apple ships ADB
        # support in the kernel.
        import sys as _sys
        import time as _time
        if self._step1_first_shown_at is None:
            self._step1_first_shown_at = _time.monotonic()
        elapsed = _time.monotonic() - self._step1_first_shown_at
        if _sys.platform == "win32" and elapsed > 15.0:
            _ghost_button(
                actions,
                "❓  ไม่เจอเครื่อง? — ลง driver Windows",
                command=self._show_driver_help,
            ).pack(side="left")

    # ── ADB daemon restart ───────────────────────────────────────

    def _restart_adb(self) -> None:
        """Run ``adb kill-server`` + ``adb start-server`` on a worker
        thread so the UI doesn't freeze, then re-render the wizard
        after a short delay so the next ``adb devices`` poll has time
        to populate.

        Why this is wired to a button (v1.8.0): the most-common
        Windows complaint isn't "no driver" — it's "Allow tapped but
        the wizard still says 'รอเครื่อง'". That symptom is almost
        always a stale adb daemon whose RSA key disagrees with what
        the phone has cached under "Always allow from this computer".
        Restarting the daemon under our bundled adb's identity fixes
        it 95 % of the time without making the customer touch
        Settings / Developer Options.
        """

        def _worker() -> None:
            ok = False
            try:
                ok = self.app.adb.restart_server()
            except Exception:
                # Never let a daemon-restart hiccup crash the wizard;
                # the customer can always retry.
                pass

            def _show_result() -> None:
                if ok:
                    messagebox.showinfo(
                        "รีสตาร์ท ADB สำเร็จ",
                        "ลอง:\n"
                        "  1. ถอดสาย USB\n"
                        "  2. เสียบใหม่\n"
                        "  3. รอ 2-3 วินาที\n"
                        "ถ้ามี popup 'อนุญาต' บนมือถือ ให้กด อนุญาต และติ๊ก 'จดจำเสมอ'",
                    )
                else:
                    messagebox.showerror(
                        "รีสตาร์ท ADB ไม่สำเร็จ",
                        "ลองปิดเปิดโปรแกรมใหม่ — ถ้ายังไม่ได้แจ้ง support",
                    )
                # Reset the 15-s timer for the driver-help nudge so
                # the customer can decide if they need to escalate.
                self._step1_first_shown_at = None
                try:
                    self._render_step()
                except Exception:
                    pass

            try:
                self.after(100, _show_result)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True,
                         name="wizard-adb-restart").start()

    # ── HyperOS / Xiaomi hint ────────────────────────────────────

    def _render_hyperos_hint(self, parent, *, row: int) -> None:
        """Inline reminder for Xiaomi/Redmi/POCO customers who tap
        Allow but never advance.

        On HyperOS / MIUI, the ``Allow USB Debugging`` toggle is
        guarded by *two* additional Developer Options the customer
        usually doesn't know to flip:

        * **Install via USB** — without this, ``adb install`` returns
          ``INSTALL_FAILED_USER_RESTRICTED`` and (more annoyingly for
          this wizard) the device often stays in ``unauthorized``
          state right after Allow because adbd validates installs
          before authorising the host.
        * **USB debugging (Security settings)** — separate toggle
          (often greyed out unless the customer has signed in to a
          Mi account on the device). Without it, ``adb shell input``
          / ``adb shell am`` are silently no-ops, which the patch
          step then trips on far down the pipeline.

        We keep this hint inline (not a modal) because customers who
        need it have already seen the wizard pause for several
        seconds — a popup at that point feels accusatory.
        """
        ctk.CTkLabel(
            parent,
            text=(
                "💡 ถ้าเป็น Xiaomi / Redmi / POCO (MIUI / HyperOS):\n"
                "    • Settings → Developer options → เปิด 'Install via USB'\n"
                "    • Settings → Developer options → เปิด 'USB debugging (Security settings)'\n"
                "    • ดึง notification shade ลง → กดไอคอน USB → เลือก 'File transfer (MTP)'"
            ),
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=11),
            justify="left", anchor="w", wraplength=620,
        ).grid(row=row, column=0, sticky="w", padx=24, pady=(0, 16))

    # ── Windows driver help ──────────────────────────────────────

    def _show_driver_help(self) -> None:
        """Pop a Thai-language explainer + one-click actions for the
        Windows ADB driver issue.

        Why this dialog exists (v1.7.11)
        --------------------------------
        macOS ships ADB-over-USB support in the kernel, so customers
        who developed/tested NP Create on Mac never hit the driver
        layer. On Windows, **every** USB-Android connection requires
        an OEM-signed INF — Pixel, Xiaomi, Samsung, etc. all need
        their own. Without one, the phone shows up under "Other
        devices" with a yellow ⚠ in Device Manager and ``adb
        devices`` returns an empty list. That's exactly the
        "phone won't connect" symptom the customer reported.

        We bundle Google's signed USB driver as a generic fallback
        (covers Pixel + most modern WCID-friendly Androids), and
        link out to OEM downloads for brands where the bundled
        one is too old (notably Xiaomi/Redmi running HyperOS).
        """
        import os as _os
        import subprocess as _sub
        import sys as _sys
        import webbrowser as _wb

        from .. import platform_tools

        driver_dir = platform_tools.find_adb_driver_dir()

        win = ctk.CTkToplevel(self)
        win.title("ลง driver USB สำหรับ Windows")
        win.configure(fg_color=THEME.bg_main)
        win.transient(self.winfo_toplevel())
        win.grab_set()
        win.resizable(False, False)
        win.geometry("640x520")

        wrap = ctk.CTkFrame(win, fg_color=THEME.bg_card, corner_radius=12)
        wrap.pack(fill="both", expand=True, padx=16, pady=16)
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "📱 ทำไม Windows ไม่เจอเครื่อง?").grid(
            row=0, column=0, sticky="w", padx=20, pady=(18, 6)
        )

        _muted(
            wrap,
            "Windows ต้องลง driver USB ของยี่ห้อโทรศัพท์ก่อน adb ถึงจะเห็น\n"
            "เครื่อง (Mac ไม่ต้อง — มี driver มาในตัว)",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 10))

        steps = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        steps.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 12))
        ctk.CTkLabel(
            steps,
            text=(
                "ขั้นตอนแก้ (เครื่อง Xiaomi / Redmi / POCO):\n"
                "1.  เปิด \"การตั้งค่านักพัฒนา\" บนมือถือ\n"
                "2.  เปิด USB Debugging  +  USB Debugging (Security settings)\n"
                "3.  เสียบ USB → เลือกโหมด \"File transfer / MTP\" (ห้าม Charging)\n"
                "4.  ลง driver โดยกดปุ่มด้านล่าง → ถอด/เสียบ USB ใหม่\n"
                "5.  มือถือจะมี popup \"อนุญาต USB Debugging\" — กด Allow"
            ),
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=14, pady=12)

        # ── action buttons ───────────────────────────────────────
        actions = ctk.CTkFrame(wrap, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=20, pady=(4, 12))
        actions.grid_columnconfigure(0, weight=1)

        def _open_driver_folder() -> None:
            """Open the bundled-driver folder in Windows Explorer so
            the customer can right-click the .inf and "Install"."""
            if driver_dir is None:
                messagebox.showwarning(
                    "ไม่พบ driver บันเดิล",
                    "โปรแกรมหา driver ที่บันเดิลไม่พบ\n"
                    "กรุณาส่งข้อความให้แอดมิน Line @npcreate",
                )
                return
            try:
                if _sys.platform == "win32":
                    _os.startfile(str(driver_dir))  # type: ignore[attr-defined]
                else:
                    _sub.Popen(["xdg-open", str(driver_dir)])
            except Exception:
                # If startfile crashes (rare), at least show the
                # path so the customer can paste it into Explorer.
                messagebox.showinfo(
                    "ตำแหน่ง driver",
                    f"คัดลอก path นี้ไปวางในช่อง Address ของ\n"
                    f"Windows Explorer:\n\n{driver_dir}",
                )

        def _open_device_manager() -> None:
            """Spawn Device Manager so the customer can do
            'Update driver' → 'Browse my computer' against the
            bundled folder."""
            if _sys.platform != "win32":
                return
            try:
                _sub.Popen(["devmgmt.msc"], shell=True)
            except Exception:
                messagebox.showinfo(
                    "Device Manager",
                    "เปิดอัตโนมัติไม่ได้ — กด Win+X → "
                    "เลือก Device Manager แทนครับ",
                )

        def _open_mi_driver_dl() -> None:
            """OEM-signed Mi USB driver covers older MIUI 10–13
            devices that the Google generic driver can't bind to.
            Opens the official Xiaomi download page."""
            _wb.open(
                "https://www.mi.com/global/service/support/usb-driver.html"
            )

        _primary_button(
            actions,
            "📂  เปิดโฟลเดอร์ driver ที่บันเดิลมา",
            command=_open_driver_folder,
        ).grid(row=0, column=0, sticky="ew", pady=4)

        _ghost_button(
            actions,
            "🔧  เปิด Device Manager",
            command=_open_device_manager,
        ).grid(row=1, column=0, sticky="ew", pady=4)

        _ghost_button(
            actions,
            "🔗  ดาวน์โหลด Mi USB Driver (ถ้า driver ที่บันเดิลใช้ไม่ได้)",
            command=_open_mi_driver_dl,
        ).grid(row=2, column=0, sticky="ew", pady=4)

        _ghost_button(
            actions, "ปิดหน้าต่างนี้", command=win.destroy,
        ).grid(row=3, column=0, sticky="ew", pady=(12, 4))

        if driver_dir is None:
            _muted(
                wrap,
                "(ไม่พบ driver บันเดิล — โปรแกรมตัวนี้ build ก่อน v1.7.11\n"
                " ส่งข้อความให้แอดมิน Line @npcreate ได้เลยครับ)",
            ).grid(row=4, column=0, sticky="w", padx=20, pady=(0, 12))
        else:
            _muted(
                wrap,
                f"driver path: {driver_dir}",
            ).grid(row=4, column=0, sticky="w", padx=20, pady=(0, 12))

    # ── step 3 — patch ───────────────────────────────────────────

    def _render_step_2(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 3: Patch & ติดตั้ง TikTok").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )
        _muted(
            wrap,
            "ระบบจะ:\n"
            "  1. ดึง TikTok APK ที่ติดตั้งอยู่จากเครื่อง\n"
            "  2. ฝัง CameraHook ลงใน APK (ใช้ LSPatch)\n"
            "  3. ติดตั้ง TikTok ฉบับ patched กลับเข้าเครื่อง\n\n"
            "หมายเหตุ: คุณจะถูกออกจากระบบ TikTok เพราะลายเซ็นเปลี่ยน — login ใหม่ตามปกติ",
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        self.btn_run_patch = _primary_button(
            wrap, "▶  เริ่ม Patch",
            command=self._wizard_patch,
        )
        self.btn_run_patch.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 8))

        self.lbl_patch_status = _muted(wrap, "พร้อมเริ่ม")
        self.lbl_patch_status.grid(row=3, column=0, sticky="w", padx=24, pady=(0, 20))

    def _wizard_patch(self) -> None:
        if not self._candidate_serial:
            return
        self.btn_run_patch.configure(state="disabled", text="กำลังทำงาน…")
        self.lbl_patch_status.configure(
            text="กำลัง patch — อาจใช้เวลา 2-3 นาที",
            text_color=THEME.fg_secondary,
        )
        threading.Thread(
            target=self._run_wizard_patch,
            args=(self._candidate_serial,),
            daemon=True,
        ).start()

    def _run_wizard_patch(self, serial: str) -> None:
        ls = self.app.lspatch
        patched_version = ""
        patched_signature = ""
        try:
            tools = ls.probe_tools()
            if not tools.ok:
                self._wizard_patch_done(
                    False,
                    "เครื่องมือไม่ครบ:\n" + "\n".join(tools.errors),
                )
                return
            pull = ls.pull_tiktok(serial=serial)
            if not pull.ok:
                err_lower = (pull.error or "").lower()
                if "no tiktok variant" in err_lower:
                    msg = (
                        "ไม่พบ TikTok ในเครื่อง — กรุณาลง TikTok "
                        "บนมือถือก่อน (ผ่าน Play Store) แล้วกด \"ลองใหม่\""
                    )
                else:
                    msg = f"pull ล้มเหลว: {pull.error}"
                self._wizard_patch_done(False, msg)
                return
            patched_version = pull.version_name or ""
            patched = ls.patch(pull.apks)
            if not patched.ok:
                self._wizard_patch_done(False, f"patch ล้มเหลว: {patched.error}")
                return
            inst = ls.install(
                package=pull.package,
                patched_apks=patched.patched_apks,
                serial=serial,
                # Same rollback safety net as the dashboard Patch button.
                original_apks=pull.apks,
            )
            if not inst.ok:
                if inst.rollback_attempted and inst.rollback_ok:
                    msg = (
                        f"install ล้มเหลว: {inst.error}\n\n"
                        "✓ กู้ TikTok เดิมกลับเรียบร้อย — "
                        "กด \"ลองใหม่\" ได้เลย"
                    )
                elif inst.rollback_attempted:
                    msg = (
                        f"install ล้มเหลว: {inst.error}\n\n"
                        f"⚠️ กู้ TikTok ไม่สำเร็จ: {inst.rollback_error}\n"
                        "กรุณาลง TikTok ใหม่จาก Play Store"
                    )
                else:
                    msg = (
                        f"install ล้มเหลว: {inst.error}\n\n"
                        "⚠️ TikTok เดิมอาจหายจากเครื่อง — "
                        "กรุณาลงใหม่จาก Play Store"
                    )
                self._wizard_patch_done(False, msg)
                return
            patched_signature = (inst.fingerprint or "").lower()
        except Exception as ex:
            log.exception("wizard patch crashed")
            self._wizard_patch_done(False, f"crash: {ex}")
            return
        self.app.devices_lib.mark_patched(
            serial,
            tiktok_version=patched_version,
            signature=patched_signature,
        )
        self.app.save_devices()
        # Best-effort: enable wireless ADB so the customer can unplug
        # the cable from this point on. Failures here aren't fatal —
        # the wizard already showed Patch as successful.
        wifi_msg = ""
        try:
            wifi_msg = self.app.setup_wifi_after_patch(serial)
        except Exception:
            log.exception("wizard wifi-setup failed (non-fatal)")
        success_msg = "Patch สำเร็จ — กด 'ถัดไป' เพื่อตั้งชื่อเครื่อง"
        if wifi_msg:
            success_msg = f"{success_msg}\n\n{wifi_msg}"
        self._wizard_patch_done(True, success_msg)

    def _wizard_patch_done(self, ok: bool, msg: str) -> None:
        def _ui():
            try:
                self.btn_run_patch.configure(
                    state="normal", text="▶  ลองใหม่" if not ok else "✓  สำเร็จ",
                )
                self.lbl_patch_status.configure(
                    text=msg,
                    text_color=THEME.success if ok else THEME.danger,
                )
            except Exception:
                pass  # widgets gone (user navigated away)
            self._patched_ok = ok
            self._update_buttons()
        self.after(0, _ui)

    # ── step 4 — name the phone ──────────────────────────────────

    def _render_step_3(self) -> None:
        wrap = _card(self.body)
        wrap.grid(row=0, column=0, padx=40, pady=30, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)

        _h2(wrap, "ขั้น 4: ตั้งชื่อเล่นให้เครื่อง").grid(
            row=0, column=0, sticky="w", padx=24, pady=(20, 4)
        )
        _muted(
            wrap,
            "ใช้แสดงในแถบรายการทางซ้ายแทนหมายเลข serial",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 16))

        e = (
            self.app.devices_lib.get(self._candidate_serial)
            if self._candidate_serial
            else None
        )
        default = e.label if e and e.label else (
            e.model if e and e.model else "บัญชี A"
        )

        self.label_var = ctk.StringVar(value=default)
        ctk.CTkEntry(
            wrap,
            textvariable=self.label_var,
            placeholder_text="เช่น 'บัญชี A' / 'TikTok หลัก'",
            height=42,
            font=ctk.CTkFont(size=14),
            fg_color=THEME.bg_input,
            border_color=THEME.border,
            text_color=THEME.fg_primary,
        ).grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 24))

    # ── finish ───────────────────────────────────────────────────

    def _finish(self) -> None:
        if not self._candidate_serial:
            self.app.go_dashboard()
            return
        label = self.label_var.get().strip() or "เครื่องใหม่"
        # Find the matching live device for its model name.
        model = ""
        for d in self.app.live_devices:
            if d.serial == self._candidate_serial:
                model = d.model
                break
        self.app.devices_lib.upsert(
            self._candidate_serial, model=model, label=label,
        )
        self.app.save_devices()
        self.app.select_device(self._candidate_serial)
        self.app.go_dashboard()


# ──────────────────────────────────────────────────────────────────
#  SettingsPage
# ──────────────────────────────────────────────────────────────────


class SettingsPage(ctk.CTkFrame):
    """License info, sign-out, contact admin."""

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        head = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(1, weight=1)

        _ghost_button(
            head, "← กลับ",
            command=self.app.go_dashboard,
        ).grid(row=0, column=0, padx=14, pady=12)

        ctk.CTkLabel(
            head, text="ตั้งค่า",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=1, padx=14, sticky="w")

        # Body — vertical stack of cards
        body = ctk.CTkScrollableFrame(self, fg_color=THEME.bg_main)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        v = self.app.license

        # License card
        lic_card = _card(body)
        lic_card.grid(row=0, column=0, sticky="ew", padx=40, pady=20)
        lic_card.grid_columnconfigure(0, weight=1)

        _h2(lic_card, "License").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 8)
        )
        if v is None:
            _body(lic_card, "ยังไม่ได้เปิดใช้งาน").grid(
                row=1, column=0, sticky="w", padx=20, pady=(0, 20)
            )
        else:
            today = date.today()
            exp_color = (
                THEME.success
                if v.days_left > 7
                else THEME.warning
                if v.days_left > 0
                else THEME.danger
            )
            self._kv(lic_card, 1, "ลูกค้า", v.customer)
            self._kv(lic_card, 2, "License Key",
                     v.raw_key, mono=True)
            self._kv(lic_card, 3, "จำนวนเครื่อง",
                     f"{self.app.devices_lib.count()} / {v.max_devices}")
            self._kv(
                lic_card, 4, "หมดอายุ",
                f"{v.expiry.isoformat()}  ({v.days_left} วัน)",
                value_color=exp_color,
            )
            renew_row = ctk.CTkFrame(lic_card, fg_color="transparent")
            renew_row.grid(row=5, column=0, sticky="ew", padx=20, pady=(8, 20))
            _primary_button(
                renew_row,
                f"💬  ต่ออายุผ่าน Line ({BRAND.line_oa})",
                command=lambda: webbrowser.open(BRAND.contact_url),
            ).pack(side="left")

        # Account card
        acc_card = _card(body)
        acc_card.grid(row=1, column=0, sticky="ew", padx=40, pady=10)
        acc_card.grid_columnconfigure(0, weight=1)

        _h2(acc_card, "บัญชี").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 4)
        )
        _muted(
            acc_card,
            "ออกจากระบบเพื่อใช้กับ License อื่น (รายการเครื่องจะคงอยู่)",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))
        _danger_button(
            acc_card, "ออกจากระบบ (ล้าง License บนเครื่องนี้)",
            command=self._on_signout,
        ).grid(row=2, column=0, sticky="w", padx=20, pady=(0, 20))

        # Encode quality card — 720p / 1080p toggle.
        enc_card = _card(body)
        enc_card.grid(row=2, column=0, sticky="ew", padx=40, pady=10)
        enc_card.grid_columnconfigure(0, weight=1)
        self._build_encode_card(enc_card)

        # Compatibility card — what phones the customer can use.
        # We surface this in-app (not just in the bundled MANUAL_TH.md)
        # because customers reliably check Settings before reading
        # docs, and the #1 sales question is "ใช้กับเครื่องอะไรได้".
        compat_card = _card(body)
        compat_card.grid(row=3, column=0, sticky="ew", padx=40, pady=10)
        compat_card.grid_columnconfigure(0, weight=1)
        self._build_compat_card(compat_card)

        # Support / diagnostics card -- "ส่ง Log ให้แอดมิน".
        # Placed right above About so the customer's eye lands on
        # it after reviewing license + version (the typical
        # pre-bug-report sweep).
        support_card = _card(body)
        support_card.grid(row=4, column=0, sticky="ew", padx=40, pady=10)
        support_card.grid_columnconfigure(0, weight=1)
        self._build_support_card(support_card)

        # About card
        about_card = _card(body)
        about_card.grid(row=5, column=0, sticky="ew", padx=40, pady=10)
        about_card.grid_columnconfigure(0, weight=1)

        _h2(about_card, "เกี่ยวกับ").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 4)
        )
        self._kv(about_card, 1, "ผลิตภัณฑ์", BRAND.name)
        self._kv(about_card, 2, "เวอร์ชัน", BRAND.version)
        self._kv(about_card, 3, "ติดต่อ", BRAND.line_oa)
        self._kv(about_card, 4, "เวลาทำการ", BRAND.support_hours)
        ctk.CTkFrame(about_card, fg_color="transparent", height=10).grid(
            row=10, column=0, sticky="ew", padx=20, pady=(0, 10)
        )

    # ── encode quality card ──────────────────────────────────────
    def _build_encode_card(self, parent: ctk.CTkFrame) -> None:
        """Encode resolution + horizontal-mirror toggle.

        Stored as ``cfg.encode_width`` / ``cfg.encode_height``
        (landscape) — the phone's rotation chain shows it portrait
        on screen, so we label the button with the *portrait* size
        (1080×1920) which matches what the user sees in TikTok.

        ``cfg.mirror_horizontal`` controls a pre-encode ``hflip``
        that cancels TikTok's implicit front-camera mirror. Default
        on; turn off if the customer's phone routes through the
        rear camera and the broadcast already looks correct.
        """
        _h2(parent, "🎞  คุณภาพวิดีโอ").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 4)
        )
        _muted(
            parent,
            "เลือกความละเอียดของไฟล์ MP4 ที่ส่งให้ TikTok ใช้ไลฟ์.\n"
            "1080p = คมที่สุด (แนะนำ)  •  720p = ใช้บนเครื่องสเปคต่ำ.",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        cfg = self.app.cfg
        cur_w = int(cfg.encode_width or 1920)
        # Map to a friendly preset key.
        preset = "1080p" if cur_w >= 1920 else "720p"
        self._encode_preset_var = ctk.StringVar(value=preset)

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 12))

        ctk.CTkRadioButton(
            row, text="1080p  (1080×1920 portrait, 1920×1080 landscape)",
            variable=self._encode_preset_var, value="1080p",
            text_color=THEME.fg_primary,
            fg_color=THEME.primary,
            hover_color=THEME.primary_hover,
            command=self._on_encode_preset_change,
        ).grid(row=0, column=0, sticky="w", pady=4)

        ctk.CTkRadioButton(
            row, text="720p   (720×1280 portrait, 1280×720 landscape)",
            variable=self._encode_preset_var, value="720p",
            text_color=THEME.fg_primary,
            fg_color=THEME.primary,
            hover_color=THEME.primary_hover,
            command=self._on_encode_preset_change,
        ).grid(row=1, column=0, sticky="w", pady=4)

        # Mirror toggle — keep it on the same card because
        # "encoder behavior" mentally groups with resolution.
        ctk.CTkFrame(
            parent, fg_color=THEME.bg_input, height=1,
        ).grid(row=3, column=0, sticky="ew", padx=20, pady=(4, 12))

        ctk.CTkLabel(
            parent, text="ภาพสะท้อน (Mirror)",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=4, column=0, sticky="w", padx=20, pady=(0, 4))
        _muted(
            parent,
            "TikTok ดึงคลิปผ่าน front-camera ทำให้ภาพสะท้อนเป็นกระจก\n"
            "(ตัวอักษร/โลโก้กลับด้าน). เปิดสวิตช์นี้เพื่อกลับด้านล่วงหน้า\n"
            "ให้คนดูเห็นภาพปกติ.",
        ).grid(row=5, column=0, sticky="w", padx=20, pady=(0, 8))

        self._mirror_var = ctk.BooleanVar(
            value=bool(getattr(cfg, "mirror_horizontal", True))
        )
        ctk.CTkSwitch(
            parent, text="แก้ภาพสะท้อนอัตโนมัติ (แนะนำเปิดไว้)",
            variable=self._mirror_var,
            text_color=THEME.fg_primary,
            progress_color=THEME.primary,
            command=self._on_mirror_change,
        ).grid(row=6, column=0, sticky="w", padx=20, pady=(0, 20))

    def _on_encode_preset_change(self) -> None:
        preset = self._encode_preset_var.get()
        cfg = self.app.cfg
        if preset == "720p":
            cfg.encode_width, cfg.encode_height = 1280, 720
        else:
            cfg.encode_width, cfg.encode_height = 1920, 1080
        try:
            cfg.save()
        except Exception:
            log.exception("ไม่สามารถบันทึก config")
            return
        # Dashboard rebuilds on navigation, so the label updates the
        # next time the user opens it. Nothing else to refresh here.

    def _on_mirror_change(self) -> None:
        cfg = self.app.cfg
        cfg.mirror_horizontal = bool(self._mirror_var.get())
        try:
            cfg.save()
        except Exception:
            log.exception("ไม่สามารถบันทึก config (mirror)")

    # ── compatibility card ────────────────────────────────────────
    def _build_compat_card(self, parent: ctk.CTkFrame) -> None:
        """Render the supported / unsupported / caveat lists.

        Kept as a builder method (not inline in __init__) so the same
        widget can be embedded elsewhere later without duplicating
        the data tables.
        """
        _h2(parent, "📱 เครื่อง Android ที่รองรับ").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 4)
        )
        _muted(
            parent,
            "ระบบนี้รองรับ Android เท่านั้น — ไม่รองรับ iPhone / iPad / iOS",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        # Min spec
        spec_frame = ctk.CTkFrame(parent, fg_color="transparent")
        spec_frame.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 12))
        spec_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            spec_frame, text="สเปคขั้นต่ำ",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        for line in (
            "• Android 8.0 ขึ้นไป   (แนะนำ Android 11+)",
            "• RAM 3 GB ขึ้นไป      (แนะนำ 4 GB)",
            "• พื้นที่ว่าง ≥ 2 GB",
            "• CPU ARM64 (เครื่องที่ผลิตหลังปี 2018 ผ่านสเปคนี้)",
        ):
            ctk.CTkLabel(
                spec_frame, text=line,
                text_color=THEME.fg_primary,
                font=ctk.CTkFont(size=12),
                anchor="w", justify="left",
            ).grid(sticky="w", padx=(8, 0))

        # ── ✅ supported brands ──
        ok_frame = ctk.CTkFrame(parent, fg_color="transparent")
        ok_frame.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 6))
        ok_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            ok_frame,
            text="✅ ใช้ได้สบาย (เทสจริง / ตลาดหลัก)",
            text_color=THEME.success,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(sticky="w", pady=(0, 4))
        OK_BRANDS = [
            ("Xiaomi / Redmi / POCO", "MIUI / HyperOS — ทดสอบแล้วกับ Redmi 14C"),
            ("Samsung Galaxy", "One UI 4+ — ปิด Knox / Secure Folder ก่อน"),
            ("OPPO / Realme / OnePlus", "ColorOS / RealmeOS / OxygenOS"),
            ("Vivo / iQOO", "OriginOS / FuntouchOS — ปิด Power Saver โหมดลึก"),
            ("Google Pixel", "Stock Android — ใช้ดีที่สุด"),
            ("Asus / ROG / Sony / Nokia / Motorola", "ใกล้ stock Android — ใช้ได้ปกติ"),
            ("Infinix / Tecno / Itel", "XOS / HiOS — ตลาด entry"),
        ]
        for name, note in OK_BRANDS:
            self._compat_row(ok_frame, name, note)

        # ── ⚠️ check-first brands ──
        warn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        warn_frame.grid(row=4, column=0, sticky="ew", padx=20, pady=(8, 6))
        warn_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            warn_frame,
            text="⚠️  ใช้ได้ แต่ต้องเช็คเพิ่ม",
            text_color=THEME.warning,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(sticky="w", pady=(0, 4))
        for name, note in [
            ("Huawei รุ่นเก่า (EMUI)", "Google Services อาจไม่ครบ"),
            ("Honor (MagicOS)", "เหมือน Huawei เก่า — เช็ค Google Services"),
            ("เครื่อง Root อยู่แล้ว", "ปิด Magisk Hide ของ TikTok ก่อน Patch"),
            ("Custom ROM (LineageOS ฯลฯ)", "ต้องไม่ strip Xposed compat"),
        ]:
            self._compat_row(warn_frame, name, note)

        # ── ❌ unsupported ──
        no_frame = ctk.CTkFrame(parent, fg_color="transparent")
        no_frame.grid(row=5, column=0, sticky="ew", padx=20, pady=(8, 6))
        no_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            no_frame,
            text="❌ ไม่รองรับ",
            text_color=THEME.danger,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(sticky="w", pady=(0, 4))
        for name, note in [
            ("iPhone / iPad / iOS", "ทุกรุ่น ทุกเวอร์ชัน"),
            ("Huawei HarmonyOS NEXT (Mate 60+)", "ตัด Android ออก ใช้ HarmonyOS เอง"),
            ("Android 7 หรือต่ำกว่า", "TikTok ใหม่ขึ้นต่ำ Android 8"),
            ("Android Tablet", "TikTok Live จำกัดบางฟีเจอร์ฝั่ง TikTok เอง"),
            ("Smart TV / Watch / 32-bit", "ไม่รองรับ"),
        ]:
            self._compat_row(no_frame, name, note)

        # ── tip box ──
        tip_frame = ctk.CTkFrame(
            parent,
            fg_color=THEME.bg_input,
            corner_radius=8,
        )
        tip_frame.grid(row=6, column=0, sticky="ew", padx=20, pady=(12, 20))
        tip_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            tip_frame,
            text=(
                "💡  ลูกค้าไม่แน่ใจว่ามือถือใช้ได้ไหม:\n"
                "    ส่งภาพ ตั้งค่า → เกี่ยวกับโทรศัพท์\n"
                "    มาให้แอดมินเช็คฟรีภายใน 5 นาที"
            ),
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            anchor="w", justify="left",
        ).grid(sticky="w", padx=12, pady=10)

    def _compat_row(
        self,
        parent: ctk.CTkFrame,
        name: str,
        note: str,
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(sticky="ew", padx=(8, 0), pady=1)
        row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            row, text="•",
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=12),
            width=12, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            row, text=name,
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            row, text=f"  — {note}",
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=0, column=2, sticky="w")

    def _build_support_card(self, parent: ctk.CTkFrame) -> None:
        """Diagnostics + log export.

        Two buttons that solve the entire customer-support flow:

        * **ส่ง Log ให้แอดมิน** -- bundles redacted logs + system
          info + (redacted) config into a single ZIP. Customer
          attaches the ZIP to a Line message; admin reads the
          stack trace + version + adb output without the customer
          ever needing to find ``%APPDATA%\\NPCreate\\logs``.
        * **เปิดโฟลเดอร์ Log** -- opens the OS file explorer at
          ``logs/`` for power users who want to skim the file
          themselves.

        Sensitive values (license key, OAuth tokens, signing seed)
        are removed BEFORE they enter the ZIP. Customer doesn't
        have to remember which fields to scrub; the redactor does
        it by key-name pattern.
        """
        _h2(parent, "🛟  ความช่วยเหลือ").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 4)
        )
        _muted(
            parent,
            "ถ้าเจอปัญหา กดปุ่มข้างล่างเพื่อสร้างไฟล์ ZIP สำหรับส่งให้แอดมิน\n"
            "(ภายในมีบันทึกการทำงาน + ข้อมูลเครื่อง — License Key + รหัสผ่าน\n"
            "ถูกลบออกก่อนเซฟ)",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 8))

        _primary_button(
            btns, "📋  สร้าง / ส่ง Log ให้แอดมิน",
            command=self._on_export_log,
            width=240,
        ).pack(side="left", padx=(0, 8))

        _ghost_button(
            btns, "📂  เปิดโฟลเดอร์ Log",
            command=self._on_open_log_dir,
            width=180,
        ).pack(side="left", padx=(0, 8))

        # Status line under the buttons; we re-use it for both
        # success ("เซฟไว้ที่ ...") and failure messages so the
        # customer always gets feedback in the same spot.
        self.lbl_log_status = _muted(parent, "")
        self.lbl_log_status.grid(
            row=3, column=0, sticky="w", padx=20, pady=(0, 12),
        )

        # Divider before backup/restore -- different concern
        # (reformat-survival) but the same support card; mixing
        # them in one place cuts the number of cards a customer
        # has to scroll past.
        ctk.CTkFrame(
            parent, fg_color=THEME.bg_input, height=1,
        ).grid(row=4, column=0, sticky="ew", padx=20, pady=(4, 12))

        _h2(parent, "💾  สำรอง / กู้คืนการตั้งค่า").grid(
            row=5, column=0, sticky="w", padx=20, pady=(0, 4)
        )
        _muted(
            parent,
            "เก็บ License + รายการเครื่อง + การตั้งค่า ไว้เป็นไฟล์ ZIP\n"
            "ไว้ใช้กู้คืนตอนย้ายคอม / ลง Windows ใหม่ — ไม่ต้องตั้งค่าใหม่\n"
            "ทุกเครื่อง",
        ).grid(row=6, column=0, sticky="w", padx=20, pady=(0, 12))

        backup_btns = ctk.CTkFrame(parent, fg_color="transparent")
        backup_btns.grid(row=7, column=0, sticky="w", padx=20, pady=(0, 8))

        _primary_button(
            backup_btns, "💾  สำรอง (Backup)",
            command=self._on_create_backup,
            width=180,
        ).pack(side="left", padx=(0, 8))

        _ghost_button(
            backup_btns, "📥  กู้คืน (Restore)",
            command=self._on_restore_backup,
            width=180,
        ).pack(side="left", padx=(0, 8))

        self.lbl_backup_status = _muted(parent, "")
        self.lbl_backup_status.grid(
            row=8, column=0, sticky="w", padx=20, pady=(0, 20),
        )

    def _on_export_log(self) -> None:
        from .. import log_setup
        # Default to the user's Desktop (works on macOS + Windows
        # 10/11 -- both have a Desktop folder; if it doesn't exist
        # the dialog will simply ignore the suggested directory
        # and start in the OS default).
        default_dir = Path.home() / "Desktop"
        suggested = log_setup.suggest_diagnostic_filename()

        out = filedialog.asksaveasfilename(
            title="เซฟ Diagnostic ZIP",
            defaultextension=".zip",
            initialfile=suggested,
            initialdir=str(default_dir) if default_dir.exists() else None,
            filetypes=[("ZIP archive", "*.zip"), ("All files", "*.*")],
        )
        if not out:
            return  # customer cancelled

        try:
            written = log_setup.collect_diagnostic_zip(Path(out))
        except Exception as exc:
            log.exception("diagnostic zip export failed")
            self.lbl_log_status.configure(
                text=f"❌ สร้างไฟล์ไม่สำเร็จ: {exc}",
                text_color=THEME.danger,
            )
            return

        size_kb = written.stat().st_size / 1024
        self.lbl_log_status.configure(
            text=(
                f"✅ เซฟเรียบร้อย: {written.name} ({size_kb:.0f} KB)\n"
                f"ส่งไฟล์นี้ให้แอดมินที่ Line OA: {BRAND.line_oa}"
            ),
            text_color=THEME.success,
        )

    def _on_open_log_dir(self) -> None:
        from .. import log_setup
        ok = log_setup.open_log_dir_in_explorer()
        if ok:
            self.lbl_log_status.configure(
                text=f"📂 เปิด {log_setup.LOG_DIR}",
                text_color=THEME.fg_secondary,
            )
        else:
            self.lbl_log_status.configure(
                text=(
                    "❌ เปิดโฟลเดอร์ไม่ได้ — ลองหาด้วยตนเอง:\n"
                    f"   {log_setup.LOG_DIR}"
                ),
                text_color=THEME.danger,
            )

    # ── backup / restore ─────────────────────────────────────────

    def _on_create_backup(self) -> None:
        from .. import backup_restore
        default_dir = Path.home() / "Desktop"
        suggested = backup_restore.suggest_backup_filename()
        out = filedialog.asksaveasfilename(
            title="เซฟไฟล์ Backup",
            defaultextension=".zip",
            initialfile=suggested,
            initialdir=str(default_dir) if default_dir.exists() else None,
            filetypes=[("NP Create backup", "*.zip"), ("All files", "*.*")],
        )
        if not out:
            return
        try:
            written = backup_restore.create_backup(Path(out))
        except Exception as exc:
            log.exception("backup creation failed")
            self.lbl_backup_status.configure(
                text=f"❌ สร้าง Backup ไม่สำเร็จ: {exc}",
                text_color=THEME.danger,
            )
            return
        size_kb = written.stat().st_size / 1024
        self.lbl_backup_status.configure(
            text=(
                f"✅ Backup เรียบร้อย: {written.name} ({size_kb:.0f} KB)\n"
                f"เก็บไฟล์นี้ไว้ที่ USB / Cloud Drive — ใช้กู้คืน "
                f"ตอนเปลี่ยนคอม / ลง Windows ใหม่"
            ),
            text_color=THEME.success,
        )

    def _on_restore_backup(self) -> None:
        from .. import backup_restore

        fname = filedialog.askopenfilename(
            title="เลือกไฟล์ Backup สำหรับกู้คืน",
            filetypes=[("NP Create backup", "*.zip"), ("All files", "*.*")],
        )
        if not fname:
            return

        manifest = backup_restore.read_backup_manifest(Path(fname))
        if manifest is None:
            messagebox.showerror(
                "ไฟล์ไม่ถูกต้อง",
                "ไฟล์นี้ไม่ใช่ Backup ของ NP Create (ไม่พบ manifest)\n"
                "กรุณาเลือกไฟล์ที่สร้างจากปุ่ม 'สำรอง (Backup)' เท่านั้น",
            )
            return

        # Confirmation popup -- restoring overwrites current state.
        # Customers running 5 phones won't appreciate accidentally
        # nuking their device list with an old backup.
        if not messagebox.askyesno(
            "ยืนยันการกู้คืน",
            (
                f"จะกู้คืนการตั้งค่าจาก Backup นี้\n\n"
                f"   เวอร์ชัน:  {manifest.app_version}\n"
                f"   สร้างเมื่อ:  {manifest.created_at}\n"
                f"   จำนวนไฟล์:  {len(manifest.files)}\n\n"
                f"การตั้งค่าและรายการเครื่องปัจจุบันจะถูก *เขียนทับ*\n"
                f"ดำเนินการต่อไหม?"
            ),
        ):
            return

        try:
            restored = backup_restore.restore_backup(Path(fname))
        except ValueError as exc:
            messagebox.showerror("กู้คืนไม่สำเร็จ", str(exc))
            return
        except Exception as exc:
            log.exception("restore failed")
            messagebox.showerror(
                "กู้คืนไม่สำเร็จ", f"ข้อผิดพลาด: {exc}"
            )
            return

        self.lbl_backup_status.configure(
            text=(
                f"✅ กู้คืนเรียบร้อย ({len(restored)} ไฟล์)\n"
                f"กรุณา *ปิด-เปิดโปรแกรมใหม่* เพื่อให้การตั้งค่ามีผล"
            ),
            text_color=THEME.success,
        )
        messagebox.showinfo(
            "กู้คืนเรียบร้อย",
            "ปิดโปรแกรมแล้วเปิดใหม่ — License และรายการเครื่องจะถูกโหลด\n"
            "ตามที่เก็บไว้ใน Backup",
        )

    def _kv(
        self,
        parent: ctk.CTkFrame,
        row: int,
        key: str,
        value: str,
        *,
        mono: bool = False,
        value_color: str | None = None,
    ) -> None:
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.grid(row=row, column=0, sticky="ew", padx=20, pady=2)
        f.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            f, text=key,
            text_color=THEME.fg_muted,
            font=ctk.CTkFont(size=12),
            width=140, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            f, text=value,
            text_color=value_color or THEME.fg_primary,
            font=(
                ctk.CTkFont(family="Menlo", size=12)
                if mono
                else ctk.CTkFont(size=13)
            ),
            anchor="w", justify="left",
        ).grid(row=0, column=1, sticky="w", padx=8)

    def _on_signout(self) -> None:
        if not messagebox.askyesno(
            "ยืนยันออกจากระบบ",
            "ลบ License บนเครื่องนี้? ต้องกรอกคีย์ใหม่ทุกครั้งที่เปิดโปรแกรม",
        ):
            return
        clear_activation()
        self.app.activation = None
        self.app.license = None
        self.app.go_activation()


# ──────────────────────────────────────────────────────────────────
#  AdminPage — admin-only license issuer + history
# ──────────────────────────────────────────────────────────────────


class AdminPage(ctk.CTkFrame):
    """In-app license issuer (admin-only).

    Visible only when ``app.is_admin`` is true (i.e. the
    ``.private_key`` file lives on this machine). The page lets the
    seller create a fresh license key, copy it to the clipboard for
    pasting into Line, and review every key issued so far.

    The page is intentionally read-mostly: we never delete history
    entries (the JSON store is append-only). To revoke a key the
    admin uses the "Revoke" button which simply flags the entry —
    rotating the keypair (`init_keys.py --force`) is the only way
    to *cryptographically* invalidate keys, and we don't expose
    that from the UI because it nukes every customer at once.
    """

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app

        # Lazy import to avoid pulling license_history at module
        # load on the customer build (the file should never be
        # present there, but keeping it lazy is belt-and-braces).
        from ..license_history import LicenseHistory

        self.history = LicenseHistory.load()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── header ──────────────────────────────────────────────
        head = ctk.CTkFrame(self, fg_color=THEME.bg_sidebar, corner_radius=0)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(2, weight=1)

        _ghost_button(
            head, "← กลับ", command=self.app.go_dashboard,
        ).grid(row=0, column=0, padx=14, pady=12)

        ctk.CTkLabel(
            head, text="🔑  ออกคีย์ลูกค้า",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=1, padx=(0, 14), sticky="w")

        ctk.CTkLabel(
            head,
            text=f"ADMIN ONLY · {self.history.count()} คีย์ที่ออกแล้ว",
            text_color=THEME.warning,
            font=ctk.CTkFont(size=11, weight="bold"),
        ).grid(row=0, column=2, padx=14, sticky="e")

        # ── body — split into "issue" form (top) + history (below) ──
        body = ctk.CTkScrollableFrame(self, fg_color=THEME.bg_main)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        self._build_issuer(body)
        self._build_history(body)

    # ── issuer card ─────────────────────────────────────────────

    def _build_issuer(self, parent: ctk.CTkScrollableFrame) -> None:
        card = _card(parent)
        card.grid(row=0, column=0, sticky="ew", padx=40, pady=(20, 10))
        card.grid_columnconfigure(0, weight=1)

        _h2(card, "ออกคีย์ใหม่").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 4),
        )
        _muted(
            card,
            f"ดีฟอลต์: {BRAND.default_devices_per_key} เครื่อง / "
            f"{BRAND.default_license_days} วัน · "
            f"คีย์เซ็นด้วย Ed25519, ลูกค้า verify ได้ออฟไลน์",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 14))

        form = ctk.CTkFrame(card, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))
        form.grid_columnconfigure(1, weight=1)
        form.grid_columnconfigure(3, weight=1)

        # Customer name
        ctk.CTkLabel(
            form, text="ชื่อลูกค้า",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            width=80, anchor="w",
        ).grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        self.var_customer = ctk.StringVar()
        ctk.CTkEntry(
            form, textvariable=self.var_customer,
            placeholder_text="เช่น คุณสมชาย / Acme TikTok",
            fg_color=THEME.bg_input,
            border_color=THEME.border,
        ).grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)

        # Devices + days, side by side
        ctk.CTkLabel(
            form, text="จำนวนเครื่อง",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            width=80, anchor="w",
        ).grid(row=1, column=0, padx=(0, 8), pady=4, sticky="w")
        self.var_devices = ctk.StringVar(
            value=str(BRAND.default_devices_per_key)
        )
        ctk.CTkEntry(
            form, textvariable=self.var_devices,
            width=80,
            fg_color=THEME.bg_input,
            border_color=THEME.border,
        ).grid(row=1, column=1, sticky="w", pady=4)

        ctk.CTkLabel(
            form, text="อายุ (วัน)",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).grid(row=1, column=2, padx=(20, 8), pady=4, sticky="w")
        self.var_days = ctk.StringVar(
            value=str(BRAND.default_license_days)
        )
        ctk.CTkEntry(
            form, textvariable=self.var_days,
            width=80,
            fg_color=THEME.bg_input,
            border_color=THEME.border,
        ).grid(row=1, column=3, sticky="w", pady=4)

        # Note (free-form)
        ctk.CTkLabel(
            form, text="หมายเหตุ",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            width=80, anchor="w",
        ).grid(row=2, column=0, padx=(0, 8), pady=4, sticky="w")
        self.var_note = ctk.StringVar()
        ctk.CTkEntry(
            form, textvariable=self.var_note,
            placeholder_text="เช่น เลขสลิป, Line, เบอร์โทร",
            fg_color=THEME.bg_input,
            border_color=THEME.border,
        ).grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)

        # Actions
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="ew", padx=20, pady=(8, 8))
        _primary_button(
            btns, "✓  สร้างคีย์", command=self._on_issue,
        ).pack(side="left", padx=(0, 8))
        _ghost_button(
            btns, "ล้างฟอร์ม", command=self._reset_form,
        ).pack(side="left")

        # Result row (key + copy button)
        self.result_card = ctk.CTkFrame(
            card,
            fg_color=THEME.bg_input,
            corner_radius=10,
            border_width=1,
            border_color=THEME.success,
        )
        self.result_card.grid(
            row=4, column=0, sticky="ew", padx=20, pady=(8, 20),
        )
        self.result_card.grid_columnconfigure(0, weight=1)
        self.result_card.grid_remove()  # hide until first issue

        self.lbl_result_meta = ctk.CTkLabel(
            self.result_card, text="",
            text_color=THEME.fg_secondary,
            font=ctk.CTkFont(size=12),
            anchor="w", justify="left",
        )
        self.lbl_result_meta.grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 4),
        )
        self.lbl_result_key = ctk.CTkLabel(
            self.result_card, text="",
            text_color=THEME.success,
            font=ctk.CTkFont(family="Menlo", size=12),
            anchor="w", justify="left", wraplength=820,
        )
        self.lbl_result_key.grid(
            row=1, column=0, sticky="ew", padx=12, pady=(0, 8),
        )
        copy_row = ctk.CTkFrame(self.result_card, fg_color="transparent")
        copy_row.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 10))
        _primary_button(
            copy_row, "📋  ก๊อปคีย์",
            command=self._copy_last_key,
        ).pack(side="left", padx=4)
        _ghost_button(
            copy_row, "ก๊อปข้อความ Line",
            command=self._copy_line_message,
        ).pack(side="left", padx=4)

        self._last_key: str = ""
        self._last_meta: str = ""

    # ── history list ────────────────────────────────────────────

    def _build_history(self, parent: ctk.CTkScrollableFrame) -> None:
        card = _card(parent)
        card.grid(row=1, column=0, sticky="ew", padx=40, pady=10)
        card.grid_columnconfigure(0, weight=1)

        _h2(card, f"ประวัติ ({self.history.count()})").grid(
            row=0, column=0, sticky="w", padx=20, pady=(20, 4),
        )
        _muted(
            card,
            "บันทึกแบบ append-only ที่ vcam-pc/license_history.json — "
            "ไฟล์นี้ไม่ติดไปกับ customer build",
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 8))

        list_frame = ctk.CTkFrame(card, fg_color="transparent")
        list_frame.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 20))
        list_frame.grid_columnconfigure(0, weight=1)

        if self.history.count() == 0:
            _muted(
                list_frame,
                "ยังไม่มีคีย์ที่ออก — ออกคีย์ใบแรกได้ที่ฟอร์มข้างบน",
            ).grid(row=0, column=0, sticky="w", pady=8)
            return

        for i, ent in enumerate(self.history.recent(50)):
            row = ctk.CTkFrame(
                list_frame, fg_color=THEME.bg_input, corner_radius=8,
            )
            row.grid(row=i, column=0, sticky="ew", pady=3)
            row.grid_columnconfigure(0, weight=1)

            head_txt = (
                f"{ent.customer}  ·  {ent.max_devices} เครื่อง  ·  "
                f"หมดอายุ {ent.expiry}"
            )
            if ent.revoked:
                head_txt += "  ·  REVOKED"
            ctk.CTkLabel(
                row, text=head_txt,
                text_color=(
                    THEME.danger if ent.revoked else THEME.fg_primary
                ),
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))

            sub = f"ออกเมื่อ {ent.issued_at[:16].replace('T', ' ')}"
            if ent.note:
                sub += f"  ·  {ent.note}"
            ctk.CTkLabel(
                row, text=sub,
                text_color=THEME.fg_muted,
                font=ctk.CTkFont(size=11),
                anchor="w",
            ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 2))

            ctk.CTkLabel(
                row, text=ent.key,
                text_color=THEME.fg_secondary,
                font=ctk.CTkFont(family="Menlo", size=10),
                anchor="w", justify="left", wraplength=820,
            ).grid(row=2, column=0, sticky="w", padx=12, pady=(0, 4))

            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.grid(row=3, column=0, sticky="w", padx=8, pady=(0, 8))
            _ghost_button(
                btns, "ก๊อป",
                command=lambda k=ent.key: self._copy_to_clipboard(k),
            ).pack(side="left", padx=4)
            if not ent.revoked:
                _ghost_button(
                    btns, "Mark revoked",
                    command=lambda k=ent.key: self._on_revoke(k),
                ).pack(side="left", padx=4)

    # ── handlers ────────────────────────────────────────────────

    def _on_issue(self) -> None:
        from ..license_key import LicenseError, generate_key, verify_key

        customer = self.var_customer.get().strip()
        if not customer:
            messagebox.showerror("ผิดพลาด", "กรุณากรอกชื่อลูกค้า")
            return
        if "|" in customer:
            messagebox.showerror(
                "ผิดพลาด", "ชื่อลูกค้าห้ามมีอักขระ '|'",
            )
            return
        try:
            devices = int(self.var_devices.get())
            days = int(self.var_days.get())
        except ValueError:
            messagebox.showerror(
                "ผิดพลาด", "จำนวนเครื่อง/อายุต้องเป็นตัวเลข",
            )
            return
        if devices < 1 or devices > 100:
            messagebox.showerror(
                "ผิดพลาด", "จำนวนเครื่องต้องอยู่ระหว่าง 1–100",
            )
            return
        if days < 1 or days > 3650:
            messagebox.showerror(
                "ผิดพลาด", "อายุต้องอยู่ระหว่าง 1–3650 วัน",
            )
            return

        try:
            key = generate_key(
                customer=customer,
                max_devices=devices,
                days=days,
            )
            v = verify_key(key)  # round-trip sanity check
        except LicenseError as e:
            messagebox.showerror("ผิดพลาด", f"สร้างคีย์ไม่สำเร็จ: {e}")
            return
        except FileNotFoundError as e:
            messagebox.showerror(
                "ผิดพลาด",
                f"private key ไม่พบ: {e}\n"
                f"รัน 'python tools/init_keys.py' ก่อน",
            )
            return

        # Persist to history first (we're append-only and fail-safe;
        # losing a record while the customer already has the key
        # would be worse than the user seeing the success dialog
        # twice).
        self.history.append(
            customer=customer,
            max_devices=devices,
            expiry=v.expiry.isoformat(),
            key=key,
            note=self.var_note.get().strip(),
        )
        try:
            self.history.save()
        except OSError as e:
            log.warning("history save failed: %s", e)

        self._last_key = key
        self._last_meta = (
            f"ลูกค้า: {customer}  ·  "
            f"{devices} เครื่อง  ·  หมดอายุ {v.expiry.isoformat()}  "
            f"({v.days_left} วัน)"
        )
        self.lbl_result_meta.configure(text=self._last_meta)
        self.lbl_result_key.configure(text=key)
        self.result_card.grid()
        # Auto-copy so the admin can paste straight into Line.
        self._copy_to_clipboard(key)
        log.info(
            "issued license: customer=%r devices=%d days=%d expiry=%s",
            customer, devices, days, v.expiry.isoformat(),
        )

    def _reset_form(self) -> None:
        self.var_customer.set("")
        self.var_devices.set(str(BRAND.default_devices_per_key))
        self.var_days.set(str(BRAND.default_license_days))
        self.var_note.set("")

    def _copy_last_key(self) -> None:
        if self._last_key:
            self._copy_to_clipboard(self._last_key)

    def _copy_line_message(self) -> None:
        if not self._last_key:
            return
        msg = (
            f"{BRAND.name} — License Key ของคุณ\n"
            f"{self._last_meta}\n\n"
            f"{self._last_key}\n\n"
            f"วิธีใช้: เปิดโปรแกรม → กรอกคีย์ → กด 'เปิดใช้งาน'\n"
            f"ติดต่อแอดมิน: Line {BRAND.line_oa}"
        )
        self._copy_to_clipboard(msg)

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            self.app.clipboard_clear()
            self.app.clipboard_append(text)
            self.app.update_idletasks()
        except Exception as e:
            log.warning("clipboard copy failed: %s", e)
            messagebox.showwarning("Clipboard",
                                    f"ก๊อปไม่สำเร็จ: {e}")

    def _on_revoke(self, key: str) -> None:
        if not messagebox.askyesno(
            "ยืนยัน",
            "Mark คีย์นี้ว่าถูกเรียกคืน?\n"
            "(เครื่องลูกค้ายังใช้คีย์ได้จนกว่าจะ rotate keypair — "
            "นี่เป็นแค่ note ในระบบ)",
        ):
            return
        if self.history.mark_revoked(key):
            try:
                self.history.save()
            except OSError as e:
                log.warning("history save failed: %s", e)
            self.app.show_page(AdminPage)  # re-render
