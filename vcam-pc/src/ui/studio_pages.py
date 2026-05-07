"""Live Studio Pro — page widgets.

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
import threading
import time
import webbrowser
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

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

        # Brand mark
        ctk.CTkLabel(
            wrap,
            text="🎬",
            font=ctk.CTkFont(size=56),
            text_color=THEME.primary,
        ).pack(pady=(0, 8))
        _h1(wrap, BRAND.name).pack()
        _muted(wrap, BRAND.tagline_th).pack(pady=(2, 24))

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

        # Header
        head = ctk.CTkFrame(side, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 4))

        ctk.CTkLabel(
            head,
            text=f"🎬 {BRAND.short_name}",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w")

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

        # Header card (device label + status + connection)
        self.header_card = _card(main)
        self.header_card.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 10))
        self.header_card.grid_columnconfigure(0, weight=1)

        self.lbl_device_title = _h2(self.header_card, "—")
        self.lbl_device_title.grid(row=0, column=0, sticky="w", padx=20, pady=(16, 0))

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

        self.btn_reconnect_wifi = _ghost_button(
            conn_row, "📶  เชื่อม WiFi อีกครั้ง",
            command=self._on_reconnect_wifi,
            width=180,
        )
        self.btn_reconnect_wifi.grid(row=0, column=1, sticky="e")

        # Video card
        vid = _card(main)
        vid.grid(row=1, column=0, sticky="ew", padx=20, pady=8)
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
        rot.grid(row=2, column=0, sticky="ew", padx=20, pady=8)
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
        aud.grid(row=3, column=0, sticky="ew", padx=20, pady=8)
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
        act.grid(row=4, column=0, sticky="ew", padx=20, pady=8)
        act.grid_columnconfigure(0, weight=1)
        self.action_card = act

        ctk.CTkLabel(
            act, text="▶️  ส่งคลิปไปเครื่อง",
            text_color=THEME.fg_primary,
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))

        _muted(
            act,
            "Encode คลิปเป็น MP4 1280×720 + push เข้าเครื่อง. "
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

        # Live + Patch row
        live = _card(main)
        live.grid(row=5, column=0, sticky="ew", padx=20, pady=(8, 24))
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

        # Reconnect button is meaningful only when (a) WiFi is set up
        # and (b) we're not already connected via WiFi. When already
        # on WiFi a manual reconnect would just churn the transport.
        if e.has_wifi() and transport != "wifi":
            self.btn_reconnect_wifi.grid()
            self.btn_reconnect_wifi.configure(state="normal")
        else:
            self.btn_reconnect_wifi.grid_remove()

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

    def on_selection_changed(self) -> None:
        self._refresh_sidebar()
        self._refresh_main()

    # ── interactions ─────────────────────────────────────────────

    def _on_pick_device(self, serial: str) -> None:
        self.app.select_device(serial)

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
        try:
            r = self.app.hook.push_audio_to_phone(source, serial=serial)
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
        if not self.app.is_online(e.serial):
            messagebox.showwarning(
                "ไม่ได้เชื่อมต่อ",
                f"เครื่อง {e.display_name()} ไม่ได้เชื่อมต่อ — เสียบ USB ก่อน",
            )
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

        try:
            self.after(0, lambda: self.progress.set(0.25))
            r = self.app.hook.encode_playlist(
                playlist_file=pl,
                profile=prof,
                output_path=self.app.local_mp4,
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
            0.6, f"Encode สำเร็จ ({human_bytes(sz)}). กำลัง push เข้าเครื่อง…"
        ))

        from ..hook_mode import TARGET_PATH_TEMPLATE, TIKTOK_PACKAGE_DEFAULT
        target = TARGET_PATH_TEMPLATE.format(pkg=TIKTOK_PACKAGE_DEFAULT)
        try:
            push = self.app.hook.push_to_phone(
                self.app.local_mp4,
                serial=serial,
                target=target,
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
        def _ui():
            self.btn_encode_push.configure(
                state="normal", text="▶  Encode + Push",
            )
            self.progress.set(1.0 if ok else 0.0)
            self.lbl_encode_status.configure(
                text=msg,
                text_color=THEME.success if ok else THEME.danger,
            )
        self.after(0, _ui)

    def _on_open_tiktok(self) -> None:
        e = self.app.selected_entry()
        if e is None or not self.app.is_online(e.serial):
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
        from ..hook_mode import TIKTOK_PACKAGE_DEFAULT
        cmd = (
            f"monkey -p {TIKTOK_PACKAGE_DEFAULT} "
            "-c android.intent.category.LAUNCHER 1"
        )
        out = self.app.adb.shell(cmd, serial=serial, timeout=8)
        log.info("launch TikTok: %s", out[:200])

    def _on_patch_tiktok(self) -> None:
        e = self.app.selected_entry()
        if e is None or not self.app.is_online(e.serial):
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

    def _run_patch(self, serial: str) -> None:
        ls = self.app.lspatch
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
                self._patch_done(serial, False, f"pull ล้มเหลว: {pull.error}")
                return
            patched = ls.patch(pull.apks)
            if not patched.ok:
                self._patch_done(serial, False, f"patch ล้มเหลว: {patched.error}")
                return
            inst = ls.install(
                package=pull.package,
                patched_apks=patched.patched_apks,
                serial=serial,
            )
            if not inst.ok:
                self._patch_done(serial, False, f"install ล้มเหลว: {inst.error}")
                return
        except Exception as ex:
            log.exception("patch flow crashed")
            self._patch_done(serial, False, f"crash: {ex}")
            return

        wifi_msg = self.app.setup_wifi_after_patch(serial)
        self._patch_done(serial, True, "Patch สำเร็จ", wifi_msg=wifi_msg)

    def _patch_done(
        self, serial: str, ok: bool, msg: str, wifi_msg: str = "",
    ) -> None:
        def _ui():
            self.btn_patch.configure(state="normal")
            if ok:
                self.app.devices_lib.mark_patched(serial)
                self.app.save_devices()
                full = "ติดตั้ง TikTok ที่ patched แล้วเรียบร้อย"
                if wifi_msg:
                    full += "\n\n" + wifi_msg
                messagebox.showinfo("สำเร็จ", full)
            else:
                messagebox.showerror("ล้มเหลว", msg)
            self._refresh_main()
        self.after(0, _ui)


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

    def __init__(self, app) -> None:
        super().__init__(app, fg_color=THEME.bg_main)
        self.app = app
        self._step = 0
        self._candidate_serial: str | None = None
        self._patched_ok = False

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
        self._render_step()

    def _prev(self) -> None:
        self._step = max(self._step - 1, 0)
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

        # Live device list — pick the first online USB device that's
        # not already in the customer's library. Critically, we skip:
        #
        # * WiFi rows (``IP:port``) that map back onto a known
        #   canonical serial — those are already set up and would
        #   confuse the user if the wizard claimed to have "found a
        #   new device". The whole point of "+ เพิ่มเครื่อง" is to
        #   onboard a *fresh* phone over USB.
        # * WiFi rows for unknown phones — we never auto-add over
        #   WiFi (no trust, no transport for `adb tcpip`).
        # * USB rows whose serial is already in the library.
        from .. import wifi_adb

        # An entry is "fully onboarded" once it's been patched. The
        # device poller auto-creates a barebones entry for every
        # USB device it sees so the dashboard sidebar shows it right
        # away — that helper shouldn't make the wizard think the
        # device is "already in the library".
        patched_serials = {
            e.serial for e in self.app.devices_lib.list() if e.is_patched()
        }
        candidates = []
        for d in self.app.live_devices:
            if not d.online:
                continue
            if wifi_adb.is_wifi_id(d.serial):
                # WiFi-only — never a wizard candidate. The phone
                # has to come in over USB so we can run `adb tcpip`
                # on it.
                continue
            if d.serial in patched_serials:
                # Already onboarded; the dashboard handles re-patch.
                continue
            candidates.append(d)

        status_box = ctk.CTkFrame(wrap, fg_color=THEME.bg_input, corner_radius=8)
        status_box.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 24))

        if not candidates:
            self._candidate_serial = None
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
        else:
            d = candidates[0]
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
                self._wizard_patch_done(False, f"pull ล้มเหลว: {pull.error}")
                return
            patched = ls.patch(pull.apks)
            if not patched.ok:
                self._wizard_patch_done(False, f"patch ล้มเหลว: {patched.error}")
                return
            inst = ls.install(
                package=pull.package,
                patched_apks=patched.patched_apks,
                serial=serial,
            )
            if not inst.ok:
                self._wizard_patch_done(False, f"install ล้มเหลว: {inst.error}")
                return
        except Exception as ex:
            log.exception("wizard patch crashed")
            self._wizard_patch_done(False, f"crash: {ex}")
            return
        self.app.devices_lib.mark_patched(serial)
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

        # About card
        about_card = _card(body)
        about_card.grid(row=2, column=0, sticky="ew", padx=40, pady=10)
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
