# context.md

Deep architecture reference for **NP Create** (`livemobillrerun` monorepo).
Where `AGENTS.md` is the quick "how to work here" cheat-sheet, this file
explains **how the system is wired** — the data flow, the moving parts,
and how a frame of video travels from a PC file onto a TikTok Live feed.

Read `AGENTS.md` first for conventions; come here when you need the
mental model.

---

## 1. Product in one paragraph

A Thai-market desktop app that makes a pre-recorded video file appear as
the phone's live camera inside TikTok Live, so a single operator can run
many "live selling" sessions from one PC. The customer is non-technical;
the whole UX is Thai, USB-cable-first, and tuned around cheap Android
phones (Redmi/Realme/OPPO) on flaky Windows machines.

---

## 2. Monorepo submodules

| Path           | Role                                                        | Stack                         |
| -------------- | ----------------------------------------------------------- | ----------------------------- |
| `vcam-pc/`     | Desktop GUI + encoder + device manager + LSPatch wizard     | Python 3.10+, customtkinter   |
| `vcam-app/`    | Android receiver app + Xposed camera hook module            | Kotlin, AGP, min SDK 33       |
| `vcam-server/` | License issuance + admin dashboard API                      | FastAPI, SQLite               |
| `vcam-magisk/` | Magisk/Zygisk native Camera-HAL hook (older path)           | C++/CMake, NDK                |

99% of work is in `vcam-pc/`. `vcam-app/` changes only when the on-phone
hook behavior changes (e.g. the v1.8.16 camera-switch freeze fix).

---

## 3. The two delivery paths (how video reaches TikTok)

There are **two independent mechanisms** that ship in the same app. They
solve the same problem for different phone capabilities.

### Path A — Phase 5 "Screen Share" (current main path, stock Android)

No root, no patching. The phone runs the `vcam-app` receiver.

```
PC video file(s)
   │  ffmpeg_streamer / hook_mode encode
   ▼
H.264+AAC frames
   │  tcp_server  (PC listens)
   ▼
adb reverse  tcp:PORT → phone
   │
   ▼
vcam-app receiver (Android)  ── fullscreen Surface
   │
   ▼
TikTok Live  ←  MediaProjection "Share screen"
```

### Path B — LSPatch (legacy / opt-in, for locked-down OEMs)

The phone has no working MediaProjection, so we patch TikTok itself.

```
lspatch_pipeline:
  1. adb pull  the installed TikTok split APKs
  2. fuse vcam-app as an embedded Xposed module via LSPatch (JDK 21)
  3. adb install-multiple  the patched splits
        (re-sign ⇒ TikTok session is reset; customer re-logs in)
  4. push  vcam_final.mp4  →  /sdcard/
  5. hook's VideoFeeder loops that MP4 as the camera buffer
```

`hook_mode.py` produces the MP4 for Path B (`vcam_final.mp4`).
`set_mode_via_broadcast` / the `vcam_enabled` flag file toggles the hook
on/off without re-pushing.

---

## 4. `vcam-pc/` component map

### Entry + lifecycle
- `main.py` — argparse entry. `python -m src.main` → Studio GUI (default);
  `--cli` headless streamer; `--legacy` diagnostic UI. Also runs the
  macOS read-only-mount (.dmg) boot guard.
- `ui/studio_app.py` — the `StudioApp` Tk root. Owns shared services
  (`cfg`, `adb`, `hook`, `lspatch`, `devices_lib`, `license`), the ADB
  poller thread, page routing, and shutdown (incl. install-on-close).
- `ui/studio_pages.py` — every Thai UI page (Dashboard, Wizard, Settings).
  ~7.5k lines, hard-coded Thai (no `i18n.T`). Biggest file in the repo.

### Config + state
- `config.py` — `StreamConfig` (encode dims, bitrate, CRF, fps, paths)
  + `ProfileLibrary` (per-device rotation/crop) + `PROJECT_ROOT` /
  `DATA_ROOT` resolution (handles frozen PyInstaller + cloud-sync dirs).
- `customer_devices.py` — `DeviceEntry` + `DeviceLibrary`. Per-device
  state: label, model, patched TikTok version/signature, `clip_showing`,
  transport. **License cap lives here** (`try_admit_new` vs `upsert`).
- `branding.py` — `BRAND` single source of truth (name, version, theme).

### Encode + transport
- `hook_mode.py` — `HookModePipeline`: re-encode playlist → MP4 for the
  LSPatch hook. BT.709 color pipeline + CRF rate control (v1.8.19).
- `ffmpeg_streamer.py` — low-latency H.264 stream for the Screen-Share
  path (libx264 veryfast zerolatency).
- `encode_push_runner.py` / `encode_push_tasks.py` — per-device,
  parallel encode→push state machine (one daemon thread per device,
  no shared mutable state, two-phase 0..0.5 encode / 0.5..1.0 push).
- `playlist.py` — build the ffmpeg concat playlist from `videos/`.
- `tcp_server.py` — PC-side TCP server feeding the phone over
  `adb reverse`.
- `rtmp_server.py` + `virtual_cam_apps.py` — Mode B "no-USB" path via
  MediaMTX (phone's CameraFi/Larix pulls RTMP over WiFi).

### Device / ADB layer
- `adb.py` — `AdbController`. **All ADB goes through here.** Owns
  `restart_server` + `last_restart_error` + `_find_port_5037_holder`.
- `wifi_adb.py` — wireless ADB (tcpip/connect, IP:port ↔ serial folding).
- `platform_tools.py` — resolves bundled `.tools/<os>/...` binaries (adb,
  ffmpeg, java, lspatch, scrcpy) + driver locators
  (`find_adb_driver_dir`, `find_universal_adb_driver_msi`).
- `scrcpy_mirror.py` / `scrcpy_installer.py` — on-PC screen mirror.
- `live_control.py` — volume/home/rotate/screenshot device controls.

### Patching
- `lspatch_pipeline.py` — pull TikTok APKs → LSPatch fuse → re-install.
  Contains the pull-fallback ladder, Java-probe quarantine handling,
  and cloud-sync placeholder detection. The gnarliest file; read its
  top docstring before touching.

### Licensing
- `license_key.py` — offline Ed25519-signed key verify
  (`customer|max_devices|expiry|nonce`). `_pubkey.py` is generated.
- `license_server.py` — optional online check; **fail-open** (server
  down must never block the customer).
- `license_history.py` — admin-side issued-key audit log (never shipped).

### Updates + ops
- `auto_update.py` — manifest poll, resumable signed-patch download +
  SHA256, persistent prefetch cache, install-on-close.
- `update_prefs.py` — persisted toggles (`install_on_close`,
  `auto_prefetch`, `last_check_ts`).
- `announcements.py` — in-app news feed.
- `log_setup.py` — rotating logs + **secret redaction** (license key,
  signing key, OAuth tokens must never land in logs).
- `backup_restore.py` — settings backup (must exclude `.private_key`).
- `_startup_diagnostic.py` — boot diagnostic file for support tickets.

### Admin webapp (separate from customer UI)
- `webapp/` — FastAPI session dashboard + TikTok Shop integration
  (`server.py`, `db.py`, `tiktok_shop.py`). Not part of the customer
  desktop flow.

---

## 5. Threading model (vcam-pc)

- **Tk main thread** — all UI; never block it.
- **ADB poller thread** — `studio_app` polls `adb devices` every ~2 s,
  folds WiFi rows onto USB serials, calls back onto the Tk thread via
  `self.after(...)`. License-cap admission happens in this callback.
- **Per-device encode/push threads** — `encode_push_runner.run_encode_push`
  spawned daemon-per-click. Fully independent: device 1 can encode while
  device 2 pushes. Persistence back to `devices.json` is marshalled to
  the Tk thread.
- **Update poller thread** — `auto_update.UpdatePoller`; `kick()` to wake
  early, `poll_now()` for synchronous manual checks.

Rule: worker threads never touch Tk widgets directly — always
`self.after(0, ...)` to hop back.

---

## 6. Filesystem & bundled tools

```
<workspace>/
  .tools/<os>/                 ← bundled runtime (resolver: platform_tools)
    platform-tools/adb(.exe)
    jdk-21/...                 ← LSPatch needs Java 21
    lspatch/lspatch.jar
    ffmpeg(.exe)
    scrcpy/  mediamtx/
    windows/adb-driver/        ← Google INF + Universal ADB Driver MSI
  apk/vcam-app-release.apk     ← Xposed module fused into TikTok
  vcam-pc/videos/              ← customer source clips
  vcam-pc/logs/                ← npcreate.log + startup-diagnostic.txt
  cache/                       ← ffmpeg encode cache + patch prefetch
```

`DATA_ROOT` (writable) is split from `INSTALL_ROOT` (may be read-only,
e.g. macOS `.dmg` or Windows `Program Files`) — see `config.py`.

---

## 7. Build & release flow

```
branding.py version bump
   │
git tag v<X.Y.Z>  &&  git push origin v<X.Y.Z>
   │
.github/workflows/release.yml  (Python 3.13)
   ├─ Windows runner: build_pyinstaller.py → NP-Create.exe
   │                  ISCC installer.iss   → NP-Create-Setup-<v>.exe
   │                  build_release.py      → ...customer-windows-<v>.zip
   └─ macOS runner:   build_pyinstaller.py → NP-Create.app
                      build_dmg.sh          → NP-Create-<v>.dmg
                      build_release.py      → ...customer-macos-<v>.zip
   │
GitHub Release  ← 4 artifacts attached
```

Tools are populated in CI by `setup_ci_tools.py` + `setup_scrcpy.py`.
Local Windows ZIP build (no CI): `setup_ci_tools.py --os windows` then
`build_release.py --target customer --os windows`.

---

## 8. Where bodies are buried (start-here pointers)

| If you're touching…             | Read first                                  |
| ------------------------------- | ------------------------------------------- |
| Anything ADB                    | `adb.py` top docstring                      |
| Pull / patch / install          | `lspatch_pipeline.py` top docstring         |
| Encode quality / color          | `hook_mode.py` `encode_playlist` (v1.8.19)  |
| License limits                  | `customer_devices.py` `try_admit_new`       |
| Windows "phone not found"       | `platform_tools` driver locators + AGENTS gotchas 9-10 |
| Per-device parallel encode      | `encode_push_runner.py` concurrency contract|
| Update system                   | `auto_update.py` + `update_prefs.py`        |

See `AGENTS.md` → "Recent significant changes" for the v1.8.16-1.8.20
changelog and the anti-patterns list before editing.
