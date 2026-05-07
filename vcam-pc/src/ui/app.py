"""Tkinter GUI for vcam-pc.

Single window, four sections:
1. Device profile picker
2. Playlist (videos in `videos/` folder)
3. Stream settings (resolution, fps, bitrate)
4. Status / Start / Stop
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..adb import AdbController
from ..config import PROJECT_ROOT, ProfileLibrary, StreamConfig
from ..health import HealthMonitor
from ..hook_mode import (
    HookModePipeline,
    HookStatus,
    default_local_mp4,
    human_bytes,
)
from ..lspatch_pipeline import LSPatchPipeline
from ..playlist import VIDEO_EXTS, list_videos, write_playlist
from ..tcp_server import TcpStreamServer
from .i18n import T

log = logging.getLogger(__name__)


class VcamApp(tk.Tk):
    REFRESH_MS = 500

    def __init__(
        self,
        port_override: int | None = None,
        no_adb_reverse: bool = False,
    ) -> None:
        super().__init__()
        self.title(T("livemobillrerun — vcam-pc"))
        self.geometry("760x1180")
        self.minsize(680, 980)
        self._apply_dark_mode_safe_theme()

        self.cfg = StreamConfig.load()
        if port_override:
            self.cfg.tcp_port = port_override
        self.profiles = ProfileLibrary.load()
        self.adb = AdbController(self.cfg.adb_path)
        self.no_adb_reverse = no_adb_reverse

        self.server: TcpStreamServer | None = None
        self.monitor: HealthMonitor | None = None
        self._playlist_path: Path | None = None
        self._status_var = tk.StringVar(value=T("idle"))
        self._stat_var = tk.StringVar(value="—")
        self._phone_yuv_var = tk.StringVar(value=T("phone yuv: —"))
        self._apk_var = tk.StringVar(value="—")

        # Hook Mode (Phase 4c) — encode an MP4 once and push to phone.
        self.hook = HookModePipeline(self.cfg)
        self._hook_local_mp4: Path = default_local_mp4(self.cfg)
        self._hook_status_var = tk.StringVar(value=T("hook file: —"))
        self._hook_flag_var = tk.StringVar(value=T("enabled flag: —"))

        # LSPatch (Phase 4d) — fuse vcam-app into TikTok APK, no root.
        self.lspatch = LSPatchPipeline(self.cfg)
        self._tiktok_var = tk.StringVar(value=T("TikTok: —"))
        self._tiktok_patched_var = tk.StringVar(value=T("patched: —"))

        self._build_ui()
        self._refresh_videos()
        self._refresh_devices()
        self._refresh_apk()
        self._refresh_hook_status()
        self._refresh_lspatch_status()
        self.after(self.REFRESH_MS, self._tick)

    # ── theme ──────────────────────────────────────────────────

    BG = "#f4f4f4"
    FG = "#111111"
    ACCENT = "#1a73e8"
    MUTED = "#555555"

    def _apply_dark_mode_safe_theme(self) -> None:
        """macOS ships with Tk 8.5, whose `aqua` theme renders ttk
        widgets *transparent* under Dark Mode + recent macOS releases
        (text colour ends up equal to the window bg, so the entire
        UI looks blank).

        Strategy: avoid ttk almost entirely and use classic `tk.*`
        widgets, which honour `bg=` directly with no theme involved.
        We pre-load class-level defaults via `option_add` so each
        widget construction picks up our colours automatically.

        We also call `tk_setPalette` which sets the fallback colour
        for every classic-tk widget property in one call (works even
        on Tk 8.5 because it's a Tcl-level command, not ttk).
        """
        try:
            self.tk_setPalette(
                background=self.BG, foreground=self.FG,
                activeBackground=self.BG, activeForeground=self.FG,
                disabledForeground="#888888",
                highlightBackground=self.BG, highlightColor=self.ACCENT,
                selectBackground=self.ACCENT, selectForeground="#ffffff",
                troughColor="#e0e0e0",
            )
        except Exception:
            log.exception("tk_setPalette failed")

        self.configure(bg=self.BG)

        # ── classic-tk widget defaults via option database ──
        cls_defaults = {
            "Frame":         {"background": self.BG, "borderwidth": 0},
            "Label":         {"background": self.BG, "foreground": self.FG},
            "Labelframe":    {"background": self.BG, "foreground": self.FG,
                              "borderwidth": 1, "relief": "groove"},
            "Button":        {"background": "#e8e8e8", "foreground": self.FG,
                              "activeBackground": "#d8d8d8",
                              "activeForeground": self.FG,
                              "highlightBackground": self.BG,
                              "borderwidth": 1, "relief": "raised"},
            "Entry":         {"background": "white", "foreground": self.FG,
                              "insertBackground": self.FG,
                              "highlightBackground": self.BG,
                              "borderwidth": 1, "relief": "solid"},
            "Listbox":       {"background": "white", "foreground": self.FG,
                              "selectBackground": self.ACCENT,
                              "selectForeground": "white",
                              "highlightBackground": self.BG,
                              "borderwidth": 1, "relief": "solid"},
            "Checkbutton":   {"background": self.BG, "foreground": self.FG,
                              "activeBackground": self.BG,
                              "selectColor": "white"},
            "Menubutton":    {"background": "#e8e8e8", "foreground": self.FG,
                              "activeBackground": "#d8d8d8",
                              "borderwidth": 1, "relief": "raised"},
        }
        for cls, opts in cls_defaults.items():
            for opt, val in opts.items():
                try:
                    self.option_add(f"*{cls}.{opt}", val)
                except Exception:
                    pass

        # ── ttk fallback (only used for Combobox) ──
        try:
            style = ttk.Style(self)
            chosen = None
            for theme in ("clam", "alt", "default", "classic"):
                if theme in style.theme_names():
                    style.theme_use(theme)
                    chosen = theme
                    break
            log.info("ttk theme: %s (was: aqua/system)", chosen)
            style.configure(
                "TCombobox", fieldbackground="white", background="white",
                foreground=self.FG, arrowcolor=self.FG,
            )
        except Exception:
            log.exception("ttk Combobox style setup failed")

    # ── layout ─────────────────────────────────────────────────

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        root = tk.Frame(self, bg=self.BG)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        # Section 1: profile
        sect_profile = tk.LabelFrame(root, text=T("1. Device profile"))
        sect_profile.pack(fill="x", **pad)

        names = self.profiles.names() or ["Generic / unknown"]
        default = self.cfg.default_profile if self.cfg.default_profile in names else names[0]
        self.profile_var = tk.StringVar(value=default)
        ttk.Combobox(
            sect_profile,
            textvariable=self.profile_var,
            values=names,
            state="readonly",
            width=36,
        ).pack(side="left", padx=8, pady=8)

        self.profile_notes_var = tk.StringVar()
        tk.Label(sect_profile, textvariable=self.profile_notes_var, fg="#666").pack(
            side="left", padx=8
        )
        self.profile_var.trace_add("write", lambda *_: self._on_profile_change())
        self._on_profile_change()

        # Section 2: playlist + adb device
        sect_pl = tk.LabelFrame(root, text=T("2. Playlist + ADB"))
        sect_pl.pack(fill="both", expand=True, **pad)

        top_row = tk.Frame(sect_pl)
        top_row.pack(fill="x", pady=(8, 4), padx=8)

        tk.Label(top_row, text=f"folder: {self.cfg.videos_path}").pack(side="left")
        tk.Button(top_row, text=T("refresh"), command=self._refresh_videos).pack(side="right")
        tk.Button(
            top_row, text=T("Open folder"), command=self._on_open_videos_folder
        ).pack(side="right", padx=(0, 6))
        tk.Button(
            top_row, text=T("Add videos..."), command=self._on_add_videos
        ).pack(side="right", padx=(0, 6))

        # Listbox is multi-select (extended mode) so user can pick
        # several files at once and bulk-delete them.
        self.video_list = tk.Listbox(sect_pl, height=6, selectmode="extended")
        self.video_list.pack(fill="both", expand=True, padx=8, pady=4)

        list_btn_row = tk.Frame(sect_pl)
        list_btn_row.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(
            list_btn_row,
            text=T("Tip: Cmd-click or Shift-click to select multiple files"),
            fg=self.MUTED,
        ).pack(side="left")
        self.btn_delete_videos = tk.Button(
            list_btn_row,
            text=T("Delete selected"),
            command=self._on_delete_videos,
            fg="#a30",
        )
        self.btn_delete_videos.pack(side="right")
        tk.Button(
            list_btn_row,
            text=T("Select all"),
            command=lambda: self.video_list.select_set(0, "end"),
        ).pack(side="right", padx=(0, 6))

        adb_row = tk.Frame(sect_pl)
        adb_row.pack(fill="x", padx=8, pady=(0, 4))
        self.adb_var = tk.StringVar(value="(no device)")
        tk.Label(adb_row, text=T("adb:")).pack(side="left")
        tk.Label(adb_row, textvariable=self.adb_var).pack(side="left", padx=4)
        tk.Button(adb_row, text=T("rescan"), command=self._refresh_devices).pack(side="right")

        apk_row = tk.Frame(sect_pl)
        apk_row.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(apk_row, text=T("apk:")).pack(side="left")
        tk.Label(apk_row, textvariable=self._apk_var, fg=self.MUTED).pack(
            side="left", padx=4
        )
        self.btn_install = tk.Button(
            apk_row, text=T("Install vcam app on phone"), command=self._on_install_apk
        )
        self.btn_install.pack(side="right")

        # Section 3: stream settings
        sect_set = tk.LabelFrame(root, text=T("3. Stream settings"))
        sect_set.pack(fill="x", **pad)

        grid = tk.Frame(sect_set)
        grid.pack(fill="x", padx=8, pady=8)

        self.res_var = tk.StringVar(value=self.cfg.resolution)
        self.fps_var = tk.StringVar(value=str(self.cfg.fps))
        self.bitrate_var = tk.StringVar(value=self.cfg.video_bitrate)
        self.port_var = tk.StringVar(value=str(self.cfg.tcp_port))

        for col, (label, var, width) in enumerate(
            [
                ("Resolution", self.res_var, 12),
                ("FPS", self.fps_var, 6),
                ("Bitrate", self.bitrate_var, 10),
                ("TCP port", self.port_var, 8),
            ]
        ):
            tk.Label(grid, text=label).grid(row=0, column=col, sticky="w", padx=4)
            tk.Entry(grid, textvariable=var, width=width).grid(
                row=1, column=col, sticky="w", padx=4
            )

        # Section 4: status + controls
        sect_ctl = tk.LabelFrame(root, text=T("4. Control"))
        sect_ctl.pack(fill="x", **pad)

        st = tk.Frame(sect_ctl)
        st.pack(fill="x", padx=8, pady=8)

        tk.Label(st, text=T("status:")).pack(side="left")
        tk.Label(st, textvariable=self._status_var, fg="#070").pack(
            side="left", padx=4
        )
        tk.Label(st, textvariable=self._stat_var, fg=self.MUTED).pack(
            side="right"
        )

        phone_row = tk.Frame(sect_ctl)
        phone_row.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(
            phone_row, textvariable=self._phone_yuv_var, fg=self.MUTED
        ).pack(side="left")

        btn_row = tk.Frame(sect_ctl)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_start = tk.Button(
            btn_row, text=T("Start streamer + phone"), command=self._on_start
        )
        self.btn_start.pack(side="left")
        self.btn_stop = tk.Button(
            btn_row, text=T("Stop"), command=self._on_stop, state="disabled"
        )
        self.btn_stop.pack(side="left", padx=8)
        self.btn_open_app = tk.Button(
            btn_row, text=T("Open app on phone"), command=self._on_open_app
        )
        self.btn_open_app.pack(side="left", padx=8)

        # Section 5: TikTok Live Screen-Share auto-pilot
        sect_live = tk.LabelFrame(
            root, text=T("5. Live Mode (TikTok Screen Share)")
        )
        sect_live.pack(fill="x", **pad)

        live_row = tk.Frame(sect_live)
        live_row.pack(fill="x", padx=8, pady=8)

        tk.Label(
            live_row,
            text=(
                "Streamer must already be running. This puts the receiver app in "
                "fullscreen Live Mode, then drives TikTok to the Screen Share "
                "screen. Tap 'Start Now' yourself for the final go-ahead."
            ),
            fg=self.MUTED,
            wraplength=560,
            justify="left",
        ).pack(side="left", fill="x", expand=True)

        self.btn_go_live = tk.Button(
            sect_live,
            text=T("Go Live on TikTok"),
            command=self._on_go_live,
            bg="#d92e3a", fg="white",
            activebackground="#b81f29", activeforeground="white",
            font=("Helvetica", 13, "bold"),
        )
        self.btn_go_live.pack(fill="x", padx=8, pady=(0, 8), ipady=4)

        # Section 6: Hook Mode — encode + push the camera-replacement MP4
        sect_hook = tk.LabelFrame(
            root, text=T("6. Hook Mode")
        )
        sect_hook.pack(fill="x", **pad)

        tk.Label(
            sect_hook,
            text=T(
                "Encodes the playlist into a single TikTok-friendly MP4 and "
                "pushes it to /sdcard/vcam_final.mp4 on the phone. The "
                "CameraHook embedded in TikTok (see Section 7) picks the file "
                "up and feeds it to TikTok's encoder in place of the camera. "
                "Works on stock locked phones — no root, no Mi Unlock."
            ),
            fg=self.MUTED,
            wraplength=560,
            justify="left",
        ).pack(fill="x", padx=8, pady=(8, 4))

        hook_row = tk.Frame(sect_hook)
        hook_row.pack(fill="x", padx=8, pady=4)
        tk.Label(hook_row, textvariable=self._hook_status_var,
                 fg=self.FG).pack(side="left")
        tk.Label(hook_row, textvariable=self._hook_flag_var,
                 fg=self.FG).pack(side="right")

        hook_btn_row = tk.Frame(sect_hook)
        hook_btn_row.pack(fill="x", padx=8, pady=(4, 8))

        self.btn_hook_encode = tk.Button(
            hook_btn_row, text=T("Encode + push MP4"),
            command=self._on_hook_encode_push,
        )
        self.btn_hook_encode.pack(side="left", padx=(0, 6))

        self.btn_hook_enable = tk.Button(
            hook_btn_row, text=T("Activate hook"),
            command=lambda: self._on_hook_set_enabled(True),
        )
        self.btn_hook_enable.pack(side="left", padx=6)

        self.btn_hook_disable = tk.Button(
            hook_btn_row, text=T("Deactivate hook"),
            command=lambda: self._on_hook_set_enabled(False),
        )
        self.btn_hook_disable.pack(side="left", padx=6)

        self.btn_hook_refresh = tk.Button(
            hook_btn_row, text=T("Refresh status"),
            command=self._refresh_hook_status,
        )
        self.btn_hook_refresh.pack(side="right")

        # Live-stream mode toggle. When enabled, the StreamReceiver
        # inside TikTok pulls H.264 bytes straight from this PC over
        # TCP — no MP4 round-trip, no choppy hot-reloads. The PC
        # streamer (Section 4 → "Start") must be running so port 8888
        # has a server listening on it.
        live_row = tk.Frame(sect_hook)
        live_row.pack(fill="x", padx=8, pady=(4, 8))
        self._live_stream_var = tk.StringVar(
            value=T("Live-stream mode: OFF (using MP4 loop)")
        )
        tk.Label(live_row, textvariable=self._live_stream_var,
                 fg=self.MUTED).pack(side="left")
        self.btn_live_stream_on = tk.Button(
            live_row, text=T("Use LIVE stream"),
            command=lambda: self._on_live_stream_toggle(True),
        )
        self.btn_live_stream_on.pack(side="right", padx=(0, 4))
        self.btn_live_stream_off = tk.Button(
            live_row, text=T("Use MP4 loop"),
            command=lambda: self._on_live_stream_toggle(False),
        )
        self.btn_live_stream_off.pack(side="right", padx=(0, 4))

        # Section 7: LSPatch — fuse vcam-app into TikTok APK (no root)
        sect_patch = tk.LabelFrame(
            root, text=T("7. LSPatch — embed CameraHook into TikTok (no root)")
        )
        sect_patch.pack(fill="x", **pad)

        tk.Label(
            sect_patch,
            text=T(
                "Pulls the user's installed TikTok APKs over ADB, runs LSPatch "
                "to embed vcam-app as an Xposed module, then re-installs the "
                "patched bundle. After this, TikTok itself loads the hook on "
                "every launch — no Magisk, no LSPosed, no bootloader unlock. "
                "Requires only USB Debugging + 'Install via USB'. The user "
                "will be logged out of TikTok (signature changes)."
            ),
            fg=self.MUTED,
            wraplength=560,
            justify="left",
        ).pack(fill="x", padx=8, pady=(8, 4))

        patch_row = tk.Frame(sect_patch)
        patch_row.pack(fill="x", padx=8, pady=4)
        tk.Label(patch_row, textvariable=self._tiktok_var,
                 fg=self.FG).pack(side="left")
        tk.Label(patch_row, textvariable=self._tiktok_patched_var,
                 fg=self.FG).pack(side="right")

        patch_btn_row = tk.Frame(sect_patch)
        patch_btn_row.pack(fill="x", padx=8, pady=(4, 8))

        self.btn_lspatch_run = tk.Button(
            patch_btn_row,
            text=T("Patch & install TikTok"),
            command=self._on_lspatch_run,
            font=("Helvetica", 12, "bold"),
        )
        self.btn_lspatch_run.pack(side="left", padx=(0, 6))

        self.btn_lspatch_status = tk.Button(
            patch_btn_row, text=T("Refresh"),
            command=self._refresh_lspatch_status,
        )
        self.btn_lspatch_status.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── data refresh ───────────────────────────────────────────

    def _on_profile_change(self) -> None:
        prof = self.profiles.get(self.profile_var.get())
        if prof:
            txt = f"rotation: {prof.rotation_filter}"
            if prof.notes:
                txt += f"   ·   {prof.notes}"
            self.profile_notes_var.set(txt)

    def _refresh_videos(self) -> None:
        self.video_list.delete(0, "end")
        videos = list_videos(self.cfg.videos_path)
        # Keep an internal index → filename map so the delete handler
        # knows the exact Path even if the displayed text changes.
        self._video_paths: list[Path] = list(videos)
        if not videos:
            self.video_list.insert("end", f"(no videos in {self.cfg.videos_path}/ — add some .mp4)")
        else:
            for v in videos:
                size_mb = v.stat().st_size / (1024 * 1024)
                self.video_list.insert("end", f"{v.name}   ({size_mb:.1f} MB)")

    def _on_delete_videos(self) -> None:
        """Delete the files highlighted in the listbox. Refuses to
        delete while a stream is running because FFmpeg may have an
        open handle on the file (especially on macOS where unlinking
        an open file leaves it lingering on disk anyway)."""
        sel = self.video_list.curselection()
        paths = getattr(self, "_video_paths", [])
        if not sel or not paths:
            messagebox.showinfo(
                "vcam-pc",
                "Select one or more videos in the list first.\n"
                "Cmd-click / Shift-click to multi-select.",
            )
            return

        if self.server and self.server.is_running():
            messagebox.showwarning(
                "vcam-pc",
                "Stop the streamer before deleting videos — FFmpeg "
                "may still have a file handle open.",
            )
            return

        targets = [paths[i] for i in sel if 0 <= i < len(paths)]
        if not targets:
            return

        msg = "Delete these videos permanently?\n\n" + "\n".join(
            f"  • {p.name}  ({p.stat().st_size / (1024 * 1024):.1f} MB)"
            for p in targets
        )
        if not messagebox.askyesno("vcam-pc", msg):
            return

        deleted: list[str] = []
        errors: list[str] = []
        for p in targets:
            try:
                p.unlink()
                deleted.append(p.name)
            except Exception as e:
                errors.append(f"{p.name}: {e}")

        self._refresh_videos()

        parts = []
        if deleted:
            parts.append("Deleted:\n" + "\n".join(f"  • {n}" for n in deleted))
        if errors:
            parts.append("Errors:\n" + "\n".join(f"  • {e}" for e in errors))
        if errors:
            messagebox.showerror("vcam-pc", "\n\n".join(parts))
        elif parts:
            messagebox.showinfo("vcam-pc", "\n\n".join(parts))

    # ── add / open videos folder ───────────────────────────────

    def _on_open_videos_folder(self) -> None:
        """Open the videos folder in Finder / Explorer / xdg-open so
        the user can drag-drop files instead of clicking Add."""
        folder = self.cfg.videos_path.resolve()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            import sys
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            log.exception("open folder failed")
            messagebox.showerror("vcam-pc", f"Couldn't open folder:\n{e}")

    def _on_add_videos(self) -> None:
        """Pick one or more video files from anywhere on disk and copy
        them into the videos folder. After copying we refresh the
        listbox so the new files show up immediately."""
        ext_filter = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        paths = filedialog.askopenfilenames(
            title="Pick videos to add",
            filetypes=[
                ("Video files", ext_filter),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return

        target = self.cfg.videos_path.resolve()
        target.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []

        import shutil
        for src_str in paths:
            src = Path(src_str)
            dst = target / src.name
            try:
                if dst.resolve() == src.resolve():
                    skipped.append(f"{src.name} (already in folder)")
                    continue
                if dst.exists():
                    if not messagebox.askyesno(
                        "vcam-pc",
                        f"{dst.name} already exists — overwrite?",
                    ):
                        skipped.append(f"{src.name} (kept existing)")
                        continue
                shutil.copy2(src, dst)
                size_mb = dst.stat().st_size / (1024 * 1024)
                copied.append(f"{src.name} ({size_mb:.1f} MB)")
            except Exception as e:
                errors.append(f"{src.name}: {e}")

        self._refresh_videos()

        msg_parts: list[str] = []
        if copied:
            msg_parts.append("Added:\n" + "\n".join(f"  • {c}" for c in copied))
        if skipped:
            msg_parts.append("Skipped:\n" + "\n".join(f"  • {s}" for s in skipped))
        if errors:
            msg_parts.append("Errors:\n" + "\n".join(f"  • {e}" for e in errors))

        if errors:
            messagebox.showerror("vcam-pc", "\n\n".join(msg_parts))
        elif msg_parts:
            messagebox.showinfo("vcam-pc", "\n\n".join(msg_parts))

    def _refresh_devices(self) -> None:
        if not self.adb.is_available():
            self.adb_var.set("(adb not installed)")
            return
        devs = self.adb.devices()
        if not devs:
            self.adb_var.set("(no device)")
            return
        labels = [
            f"{d.serial}{' ['+d.model+']' if d.model else ''} → {d.state}" for d in devs
        ]
        self.adb_var.set("  |  ".join(labels))

    # ── APK install ────────────────────────────────────────────

    APK_PATH = (
        PROJECT_ROOT.parent / "vcam-app" / "app" / "build" / "outputs"
        / "apk" / "debug" / "app-debug.apk"
    )
    APK_PACKAGE = "com.livemobillrerun.vcam"

    def _refresh_apk(self) -> None:
        if not self.APK_PATH.exists():
            self._apk_var.set(f"(apk not built — {self.APK_PATH.name} missing)")
            self.btn_install.config(state="disabled")
            return
        size_mb = self.APK_PATH.stat().st_size / (1024 * 1024)
        installed = self._apk_installed_on_phone()
        suffix = " · installed on phone" if installed else " · not installed"
        self._apk_var.set(f"{self.APK_PATH.name} ({size_mb:.1f} MB){suffix}")
        self.btn_install.config(state="normal")

    def _apk_installed_on_phone(self) -> bool:
        if not self.adb.is_available() or not self.adb.devices():
            return False
        try:
            r = subprocess.run(
                [self.cfg.adb_path, "shell", "pm", "list", "packages", self.APK_PACKAGE],
                capture_output=True, text=True, timeout=4,
            )
            return self.APK_PACKAGE in (r.stdout or "")
        except Exception:
            return False

    def _on_install_apk(self) -> None:
        if not self.adb.is_available():
            messagebox.showerror("vcam-pc", "adb not found. Source vcam-pc/tools/bin/env.sh first.")
            return
        if not self.adb.devices():
            messagebox.showerror(
                "vcam-pc",
                "No phone detected. Plug in via USB, accept the\n"
                "'Allow USB debugging?' dialog, then click 'rescan'.",
            )
            return
        if not self.APK_PATH.exists():
            messagebox.showerror("vcam-pc", f"APK not found at:\n{self.APK_PATH}")
            return
        self.btn_install.config(state="disabled")
        self._apk_var.set("installing…")
        threading.Thread(target=self._do_install_apk, daemon=True).start()

    def _do_install_apk(self) -> None:
        try:
            r = subprocess.run(
                [self.cfg.adb_path, "install", "-r", "-g", str(self.APK_PATH)],
                capture_output=True, text=True, timeout=120,
            )
            ok = r.returncode == 0 and "Success" in (r.stdout or "")
            msg = (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            ok, msg = False, str(e)

        def done() -> None:
            if ok:
                self._apk_var.set("installed ✓ — open 'livemobillrerun vcam' on phone")
                messagebox.showinfo(
                    "vcam-pc",
                    "Installed.\nOpen 'livemobillrerun vcam' on the phone and tap Start.",
                )
            else:
                messagebox.showerror("vcam-pc", f"adb install failed:\n\n{msg.strip()[:600]}")
                self._refresh_apk()
            self.btn_install.config(state="normal")
        self.after(0, done)

    # ── Phase 5: Go Live on TikTok (Screen Share, no root) ─────

    def _on_go_live(self) -> None:
        if not self.adb.is_available() or not self.adb.devices():
            messagebox.showwarning(
                "vcam-pc",
                "No phone detected. Plug in via USB and click 'rescan'.",
            )
            return
        if not self._apk_installed_on_phone():
            messagebox.showwarning(
                "vcam-pc",
                "vcam-app is not installed on the phone yet.\n"
                "Click 'Install vcam app on phone' first.",
            )
            return
        if not (self.server and self.server.is_running()):
            if not messagebox.askyesno(
                "vcam-pc",
                "Streamer isn't running yet — TikTok Live will see a "
                "black frame.\n\nProceed anyway?",
            ):
                return
        self.btn_go_live.config(state="disabled")
        threading.Thread(target=self._do_go_live, daemon=True).start()

    def _do_go_live(self) -> None:
        try:
            # 1. tell the receiver app to switch to Live Mode (immersive
            # fullscreen). We use Intent extras handled by MainActivity.
            subprocess.run(
                [
                    self.cfg.adb_path, "shell", "am", "start",
                    "-n", f"{self.APK_PACKAGE}/.MainActivity",
                    "--ez", "vcam_auto_start", "true",
                    "--ez", "vcam_live", "true",
                ],
                capture_output=True, text=True, timeout=8,
            )
            # 2. give the activity ~2 s to switch into Live Mode and
            # the streamer pipeline a beat to feed the first frame.
            time.sleep(2.5)

            # 3. TikTok controller — local import to avoid pulling
            # subprocess overhead at GUI startup.
            from ..tiktok_controller import TikTokAutoController

            ctrl = TikTokAutoController(
                adb_path=self.cfg.adb_path,
                log_callback=lambda m: log.info("[tiktok] %s", m),
            )
            results = ctrl.run_to_screen_share(confirm_start=False)

            ok = all(r.ok for r in results)
            summary = "\n".join(f"{'✓' if r.ok else '✗'} {r.name}: {r.detail}" for r in results)

            def show() -> None:
                if ok:
                    messagebox.showinfo(
                        "vcam-pc — Go Live",
                        "TikTok is at the Screen Share screen.\n\n"
                        "Now on the phone: tap 'Start Now' to begin "
                        "broadcasting. The receiver app's Live Mode is "
                        "already fullscreen — TikTok will only capture "
                        "the streamed video.\n\n" + summary,
                    )
                else:
                    messagebox.showwarning(
                        "vcam-pc — Go Live (partial)",
                        "Couldn't drive every step:\n\n" + summary,
                    )
                self.btn_go_live.config(state="normal")
            self.after(0, show)
        except Exception as e:
            log.exception("Go Live failed")
            self.after(0, lambda: messagebox.showerror("vcam-pc", f"Go Live error:\n{e}"))
            self.after(0, lambda: self.btn_go_live.config(state="normal"))

    # ── one-click "open the app on the phone" ──────────────────

    def _on_open_app(self) -> None:
        """Bring the receiver app to the foreground on the connected
        phone, then auto-tap its Start button. This skips the manual
        step of switching focus to the phone."""
        if not self.adb.is_available() or not self.adb.devices():
            messagebox.showwarning(
                "vcam-pc",
                "No phone detected. Plug in via USB and click 'rescan'.",
            )
            return
        if not self._apk_installed_on_phone():
            messagebox.showwarning(
                "vcam-pc",
                "App is not installed on the phone yet.\n"
                "Click 'Install vcam app on phone' first.",
            )
            return
        threading.Thread(target=self._do_open_app, daemon=True).start()

    def _do_open_app(self) -> None:
        try:
            subprocess.run(
                [
                    self.cfg.adb_path, "shell", "am", "start", "-n",
                    f"{self.APK_PACKAGE}/.MainActivity",
                ],
                capture_output=True, text=True, timeout=8,
            )
            # Give the activity a moment to inflate before auto-tapping.
            time.sleep(1.2)
            tx, ty = self._find_start_button() or self._fallback_tap()
            log.info("auto-tap Start at (%d, %d)", tx, ty)
            subprocess.run(
                [self.cfg.adb_path, "shell", "input", "tap", str(tx), str(ty)],
                capture_output=True, text=True, timeout=4,
            )
        except Exception:
            log.exception("auto-launch failed")

    def _find_start_button(self) -> tuple[int, int] | None:
        """Robust path: ask uiautomator for the Start button's bounds.
        This works regardless of the device DPI / orientation."""
        try:
            subprocess.run(
                [
                    self.cfg.adb_path, "shell", "uiautomator",
                    "dump", "/sdcard/vcam_ui.xml",
                ],
                capture_output=True, text=True, timeout=5,
            )
            r = subprocess.run(
                [self.cfg.adb_path, "shell", "cat", "/sdcard/vcam_ui.xml"],
                capture_output=True, text=True, timeout=5,
            )
            xml = r.stdout or ""
            # Find first node with our btn_start id.
            import re
            m = re.search(
                r'resource-id="com\.livemobillrerun\.vcam:id/btn_start"[^/]*'
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                xml,
            )
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                return ((x1 + x2) // 2, (y1 + y2) // 2)
        except Exception:
            log.debug("uiautomator dump failed", exc_info=True)
        return None

    def _fallback_tap(self) -> tuple[int, int]:
        """Heuristic if uiautomator is unavailable. Tuned for the new
        layout (Start button below the preview + checkboxes)."""
        w, h = self._screen_size()
        return int(w * 0.27), int(h * 0.63)

    def _screen_size(self) -> tuple[int, int]:
        """Best-effort `wm size` lookup, with a sensible default."""
        try:
            out = subprocess.run(
                [self.cfg.adb_path, "shell", "wm", "size"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            for line in out.splitlines():
                if "Physical size:" in line:
                    w, h = line.split(":")[-1].strip().split("x")
                    return int(w), int(h)
        except Exception:
            pass
        return 720, 1600

    # ── start / stop ───────────────────────────────────────────

    def _on_start(self) -> None:
        # Reapply edits to cfg
        try:
            self.cfg.resolution = self.res_var.get()
            self.cfg.fps = int(self.fps_var.get())
            self.cfg.video_bitrate = self.bitrate_var.get()
            self.cfg.tcp_port = int(self.port_var.get())
        except ValueError as e:
            messagebox.showerror("vcam-pc", f"Invalid stream setting: {e}")
            return

        videos = list_videos(self.cfg.videos_path)
        if not videos:
            messagebox.showwarning(
                "vcam-pc",
                f"No videos in {self.cfg.videos_path}/\nDrop some .mp4 files there first.",
            )
            return

        prof = self.profiles.get(self.profile_var.get())
        if prof is None:
            messagebox.showerror("vcam-pc", "Pick a device profile.")
            return

        self._playlist_path = write_playlist(videos, loop=self.cfg.loop_playlist)

        self.server = TcpStreamServer(self.cfg, on_state=self._on_server_state)
        try:
            self.server.start(self._playlist_path, prof)
        except Exception as e:
            log.exception("server start failed")
            messagebox.showerror("vcam-pc", f"Failed to start: {e}")
            self.server = None
            return

        if self.cfg.auto_adb_reverse and not self.no_adb_reverse:
            threading.Thread(target=self._do_adb_reverse, daemon=True).start()

        self.monitor = HealthMonitor(server=self.server, adb=self.adb, interval_s=2.0)
        self.monitor.start()

        # Best-effort: also wake up the phone-side receiver so the user
        # doesn't have to switch windows. If the app isn't installed
        # yet we just skip — the streamer will sit there listening.
        if self._apk_installed_on_phone():
            threading.Thread(target=self._do_open_app, daemon=True).start()

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

    def _do_adb_reverse(self) -> None:
        if not self.adb.is_available():
            return
        if not self.adb.devices():
            return
        ok = self.adb.reverse(self.cfg.tcp_port)
        log.info("adb reverse: %s", "OK" if ok else "failed")

    def _on_server_state(self, msg: str) -> None:
        # Called from server thread, marshal to UI thread.
        self.after(0, lambda: self._status_var.set(msg))

    def _on_stop(self) -> None:
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        if self.server:
            self.server.stop()
            self.server = None
        if self.cfg.auto_adb_reverse and self.adb.is_available():
            try:
                self.adb.reverse_remove(self.cfg.tcp_port)
            except Exception:
                pass
        if self._playlist_path:
            try:
                self._playlist_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._playlist_path = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._status_var.set("idle")
        self._phone_yuv_var.set("phone yuv: —")

    def _on_close(self) -> None:
        try:
            self._on_stop()
        finally:
            self.destroy()

    # ── Hook Mode (Phase 4c) ───────────────────────────────────

    def _selected_serial(self) -> str | None:
        """The serial of the first online device, if any. Multi-device
        UX is out of scope — we just pick whoever is online."""
        for d in self.adb.devices():
            if d.online:
                return d.serial
        return None

    def _on_hook_encode_push(self) -> None:
        if not self.adb.is_available() or not self.adb.devices():
            messagebox.showwarning(
                "vcam-pc — Hook Mode",
                "No phone detected. Plug in via USB and try again.",
            )
            return
        videos = list_videos(self.cfg.videos_path)
        if not videos:
            messagebox.showwarning(
                "vcam-pc — Hook Mode",
                f"No videos in {self.cfg.videos_path}/.\n"
                f"Add some .mp4 files first.",
            )
            return
        prof = self.profiles.get(self.profile_var.get())
        if prof is None:
            messagebox.showerror("vcam-pc — Hook Mode", "Pick a device profile.")
            return

        # Apply current resolution / fps / bitrate from the form.
        try:
            self.cfg.resolution = self.res_var.get()
            self.cfg.fps = int(self.fps_var.get())
            self.cfg.video_bitrate = self.bitrate_var.get()
        except ValueError as e:
            messagebox.showerror("vcam-pc — Hook Mode",
                                 f"Invalid stream setting: {e}")
            return
        # Re-create the pipeline so it picks up the latest cfg.
        self.hook = HookModePipeline(self.cfg)

        playlist = write_playlist(videos)

        self.btn_hook_encode.config(state="disabled", text=T("Encoding…"))
        threading.Thread(
            target=self._do_hook_encode_push,
            args=(playlist, prof),
            daemon=True,
        ).start()

    def _do_hook_encode_push(self, playlist: Path, prof) -> None:
        try:
            self.after(0, lambda: self.btn_hook_encode.config(text=T("Encoding…")))
            r = self.hook.encode_playlist(
                playlist_file=playlist,
                profile=prof,
                output_path=self._hook_local_mp4,
            )
            if not r.ok:
                self.after(0, lambda: messagebox.showerror(
                    "vcam-pc — Hook Mode",
                    f"FFmpeg encode failed:\n\n{r.log_tail}",
                ))
                return
            self.after(0, lambda: self.btn_hook_encode.config(text=T("Pushing…")))
            push = self.hook.push_to_phone(
                local_mp4=self._hook_local_mp4,
                serial=self._selected_serial(),
            )
            if not push.ok:
                self.after(0, lambda: messagebox.showerror(
                    "vcam-pc — Hook Mode",
                    f"adb push failed:\n\n{push.error}",
                ))
                return
            mbps = (push.bytes / (1024 * 1024)) / push.elapsed_s if push.elapsed_s else 0
            self.after(0, lambda: messagebox.showinfo(
                "vcam-pc — Hook Mode",
                f"OK — pushed {human_bytes(push.bytes)} in {push.elapsed_s:.1f}s "
                f"({mbps:.1f} MB/s) to:\n  {push.target}\n\n"
                f"Encode took {r.duration_s:.1f}s · MP4 cached at\n"
                f"  {self._hook_local_mp4}\n\n"
                f"Next steps once you have root + LSPosed:\n"
                f"  1. Enable vcam-app in LSPosed scope under TikTok\n"
                f"  2. Click 'Activate hook' below\n"
                f"  3. Open TikTok → Live → camera replaced",
            ))
            self.after(0, self._refresh_hook_status)
        except Exception as e:
            log.exception("hook encode/push failed")
            self.after(0, lambda: messagebox.showerror(
                "vcam-pc — Hook Mode", f"Unexpected error:\n{e}",
            ))
        finally:
            self.after(0, lambda: self.btn_hook_encode.config(
                state="normal", text=T("Encode + push MP4"),
            ))

    def _on_live_stream_toggle(self, enabled: bool) -> None:
        """Switch CameraHook between live-TCP and MP4-loop modes.

        Implementation: the hook checks the existence of
        `/data/local/tmp/vcam_stream_url` to decide which path to take.
        Touch it (or remove it) via adb. We also try to set up
        `adb reverse tcp:8888 tcp:8888` so the StreamReceiver inside
        TikTok can `connect("127.0.0.1", 8888)` and reach this PC's
        FFmpeg server transparently.
        """
        adb = self.cfg.adb_path
        serial = self._selected_serial()
        try:
            if enabled:
                # Make sure the streamer is running first.
                if self.server is None or not self.server.is_running():
                    messagebox.showwarning(
                        "vcam-pc",
                        T("Start the streamer first (Section 4 → Start) "
                          "so port 8888 has data to send."),
                    )
                    return
                # Set up reverse port forward so 127.0.0.1:8888 on
                # the phone routes to this PC.
                cmd = [adb]
                if serial: cmd += ["-s", serial]
                cmd += ["reverse", f"tcp:{self.cfg.tcp_port}",
                        f"tcp:{self.cfg.tcp_port}"]
                subprocess.run(cmd, capture_output=True, timeout=5)
                # Touch the activation flag.
                cmd2 = [adb]
                if serial: cmd2 += ["-s", serial]
                cmd2 += ["shell", "touch",
                         "/data/local/tmp/vcam_stream_url"]
                subprocess.run(cmd2, capture_output=True, timeout=5)
                # Also force-stop TikTok so the new createInputSurface
                # picks up the live path next time it goes Live.
                cmd3 = [adb]
                if serial: cmd3 += ["-s", serial]
                cmd3 += ["shell", "am", "force-stop",
                         "com.ss.android.ugc.trill"]
                subprocess.run(cmd3, capture_output=True, timeout=5)
                self._live_stream_var.set(
                    T("Live-stream mode: ON (PC → phone over TCP)")
                )
            else:
                cmd = [adb]
                if serial: cmd += ["-s", serial]
                cmd += ["shell", "rm", "-f",
                        "/data/local/tmp/vcam_stream_url"]
                subprocess.run(cmd, capture_output=True, timeout=5)
                self._live_stream_var.set(
                    T("Live-stream mode: OFF (using MP4 loop)")
                )
        except Exception as e:
            messagebox.showerror("vcam-pc — Live stream", str(e))

    def _on_hook_set_enabled(self, enabled: bool) -> None:
        if not self.adb.is_available() or not self.adb.devices():
            messagebox.showwarning("vcam-pc — Hook Mode", "No phone detected.")
            return
        ok = self.hook.set_enabled(enabled, serial=self._selected_serial())
        if not ok:
            messagebox.showerror(
                "vcam-pc — Hook Mode",
                "Couldn't toggle the activation flag.\n\n"
                "If the phone isn't rooted yet, /data/local/tmp may be "
                "writable but the hook itself won't load. This still "
                "configures the flag correctly for the day root is in.",
            )
            return
        # Also fire a broadcast so a running hook picks up the change
        # immediately. Harmless on non-rooted phones.
        self.hook.set_mode_via_broadcast(
            mode=2 if enabled else 0,
            serial=self._selected_serial(),
        )
        self._refresh_hook_status()

    def _refresh_hook_status(self) -> None:
        s = self.hook.status(serial=self._selected_serial())
        if s.file_present:
            age = max(0, int(time.time()) - s.file_mtime)
            self._hook_status_var.set(
                f"hook file: {human_bytes(s.file_size)}  ·  age {age}s"
            )
        else:
            self._hook_status_var.set("hook file: not on phone")
        self._hook_flag_var.set(
            "enabled flag: ON" if s.enabled_flag else "enabled flag: off"
        )

    # ── LSPatch (Phase 4d) ─────────────────────────────────────

    def _refresh_lspatch_status(self) -> None:
        info = self.lspatch.installed_status(serial=self._selected_serial())
        if info["package"]:
            self._tiktok_var.set(
                f"TikTok: {info['package'].split('.')[-1]}  ·  v{info['version']}"
            )
        else:
            self._tiktok_var.set("TikTok: not installed")
        if info["patched"] == "yes":
            self._tiktok_patched_var.set(
                f"patched: yes ({info['fingerprint']})"
            )
        elif info["patched"] == "no":
            self._tiktok_patched_var.set(
                f"patched: NO ({info['fingerprint']})"
            )
        else:
            self._tiktok_patched_var.set("patched: —")

    def _on_lspatch_run(self) -> None:
        """Three-step pipeline behind one button: pull, patch, install.

        Each step is gated on the result of the previous one. Anything
        unusual surfaces in a messagebox so the user understands what
        happened on the phone side.
        """
        st = self.lspatch.probe_tools()
        if not st.ok:
            messagebox.showerror(
                "vcam-pc — LSPatch",
                "Toolchain not ready:\n\n" + "\n".join(st.errors),
            )
            return
        if not messagebox.askyesno(
            "vcam-pc — LSPatch",
            "This will:\n\n"
            "  1. Pull TikTok APKs from the phone\n"
            "  2. Patch them with LSPatch (embeds vcam-app)\n"
            "  3. Uninstall the original TikTok\n"
            "  4. Install the patched bundle\n\n"
            "You will be LOGGED OUT of TikTok (the signature changes).\n"
            "Stream + downloads remain intact.\n\n"
            "Continue?",
        ):
            return

        self.btn_lspatch_run.config(state="disabled", text=T("Working…"))
        self.btn_lspatch_status.config(state="disabled")
        threading.Thread(target=self._do_lspatch_run, daemon=True).start()

    def _do_lspatch_run(self) -> None:
        serial = self._selected_serial()
        try:
            log.info("LSPatch: pull")
            pull = self.lspatch.pull_tiktok(serial=serial)
            if not pull.ok:
                self._lspatch_finish_error("pull failed", pull.error)
                return
            log.info("LSPatch: patching %d APKs (TikTok %s v%s)",
                     len(pull.apks), pull.package, pull.version_name)
            patch = self.lspatch.patch(pull.apks)
            if not patch.ok:
                tail = patch.log_tail or patch.error
                self._lspatch_finish_error("patch failed", tail)
                return
            log.info("LSPatch: installing %d patched APKs",
                     len(patch.patched_apks))
            inst = self.lspatch.install(
                package=pull.package,
                patched_apks=patch.patched_apks,
                serial=serial,
                uninstall_first=True,
            )
            if not inst.ok:
                self._lspatch_finish_error("install failed", inst.error)
                return

            self.after(0, lambda: messagebox.showinfo(
                "vcam-pc — LSPatch",
                f"Patched TikTok installed.\n\n"
                f"package    : {pull.package}\n"
                f"version    : {pull.version_name}\n"
                f"signer     : {inst.fingerprint}\n"
                f"pull       : {pull.elapsed_s:.1f}s\n"
                f"patch      : {patch.elapsed_s:.1f}s ({len(patch.patched_apks)} APKs)\n"
                f"install    : {inst.elapsed_s:.1f}s\n\n"
                f"Open TikTok on the phone, log in, and try Live.\n"
                f"The vcam hook should fire automatically — watch:\n"
                f"  adb logcat -s LSPosed-Bridge:I",
            ))
        finally:
            self.after(0, self._lspatch_finish_reset)

    def _lspatch_finish_error(self, where: str, detail: str) -> None:
        log.error("LSPatch %s: %s", where, detail)
        self.after(0, lambda: messagebox.showerror(
            "vcam-pc — LSPatch",
            f"{where}\n\n{detail}",
        ))

    def _lspatch_finish_reset(self) -> None:
        self.btn_lspatch_run.config(state="normal", text=T("Patch & install TikTok"))
        self.btn_lspatch_status.config(state="normal")
        self._refresh_lspatch_status()

    # ── periodic tick ──────────────────────────────────────────

    DEVICE_RESCAN_EVERY_TICKS = 4  # 4 × 500 ms = 2 s
    _tick_n = 0
    _last_adb_label = "(no device)"

    def _tick(self) -> None:
        if self.server and self.server.is_running():
            mb = self.server.bytes_sent / (1024 * 1024)
            self._stat_var.set(
                f"{mb:6.2f} MB  ·  {self.server.uptime_s:4.0f}s  ·  "
                f"~{self.server.frames_sent} NAL  ·  client {self.server.client_addr}"
            )
        else:
            self._stat_var.set("—")

        # Surface the phone-side YUV freshness so the user can confirm
        # at a glance that the decoder is still consuming.
        snap = self.monitor.snapshot() if self.monitor else None
        if snap and snap.phone_yuv_size is not None:
            kib = snap.phone_yuv_size // 1024
            age = snap.phone_yuv_fresh_s if snap.phone_yuv_fresh_s is not None else 0
            badge = "✓ live" if age <= 2 else ("△ slow" if age < 8 else "✗ stalled")
            self._phone_yuv_var.set(
                f"phone yuv: {kib} KiB  ·  age {age:4.1f}s  ·  {badge}"
            )
        elif self.server and self.server.is_running():
            self._phone_yuv_var.set("phone yuv: probing…")

        self._tick_n += 1
        if self._tick_n % self.DEVICE_RESCAN_EVERY_TICKS == 0:
            self._auto_refresh_devices()

        self.after(self.REFRESH_MS, self._tick)

    def _auto_refresh_devices(self) -> None:
        """Re-poll adb for connected devices and auto-pick a profile by model."""
        if not self.adb.is_available():
            return
        devs = self.adb.devices()
        if not devs:
            label = "(no device)"
        else:
            label = "  |  ".join(
                f"{d.serial}{' ['+d.model+']' if d.model else ''} → {d.state}"
                for d in devs
            )
        if label != self._last_adb_label:
            self._last_adb_label = label
            self.adb_var.set(label)
            online = next((d for d in devs if d.online), None)
            if online and online.model:
                self._maybe_pick_profile_for(online.model)
            self._refresh_apk()

    _MODEL_HINTS: tuple[tuple[str, str], ...] = (
        ("23100", "Redmi 13C"),         # 23100RN82L etc.
        ("23106", "Redmi 14C"),         # 23106RN0DA etc.
        ("topaz", "Redmi 13C"),
        ("gale",  "Redmi 14C"),
        ("c75",   "Poco C75"),
        ("25078", "Redmi 15C"),
    )

    def _maybe_pick_profile_for(self, model: str) -> None:
        """Auto-set profile dropdown if a clear match exists and user
        hasn't manually changed it (still on the default)."""
        if self.profile_var.get() != self.cfg.default_profile:
            return  # user picked something — don't override
        m = model.lower()
        for needle, profile_name in self._MODEL_HINTS:
            if needle in m and profile_name in self.profiles.names():
                self.profile_var.set(profile_name)
                return


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    VcamApp().mainloop()


if __name__ == "__main__":
    main()
