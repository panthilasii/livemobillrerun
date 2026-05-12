# livemobillrerun

Personal project — pipe a video file from PC to a rooted Android phone, expose
it as the system camera so any app on the phone (camera apps, video conference,
streaming apps) sees the video as live camera input.

> **Personal use only.** Use only on your own device. You are responsible for
> following the Terms of Service of any third-party app you use this with.

## What this project is *not*

- It is **not** a TikTok APK patcher. We do **not** decompile, modify, or
  redistribute any third-party app.
- It does **not** ship `tiktok_patched.apk`, `lspatch_*.jar`, or any
  reverse-engineered binaries.

The approach here is **OS-level Camera HAL replacement on a rooted device you
own** — the phone exposes a virtual camera to *every* app via the standard
Android Camera2 / CameraX API. No third-party app is modified.

## Architecture

```
┌────────────────── PC (Windows / macOS) ──────────────────┐
│                                                          │
│  videos/*.mp4                                            │
│       │                                                  │
│       ▼                                                  │
│  FFmpeg (loop, rotate per device profile, H.264 enc.)    │
│       │                                                  │
│       ▼                                                  │
│  TCP server :8888  ◄──────────  control_panel.py (UI)    │
└──────────────────────────┬───────────────────────────────┘
                           │ ADB reverse: tcp:8888
                           ▼
┌─────────────────── Android (rooted) ─────────────────────┐
│                                                          │
│  com.livemobillrerun.vcam (your APK)                     │
│   ├─ TCP receiver (localhost:8888)                       │
│   ├─ MediaCodec H.264 decoder                            │
│   └─ Writes YUV420 frames → /data/local/tmp/vcam.yuv     │
│                                                          │
│  Magisk module: vcam-hal                                 │
│   └─ Overlay /vendor/lib64/hw/camera.<soc>.so            │
│       Intercepts ICameraDevice::open()                   │
│       Returns frames from /data/local/tmp/vcam.yuv       │
│       Falls back to real camera if file missing          │
│                                                          │
│  Result: every app calling Camera2 / CameraX sees the    │
│  video stream as live camera input.                      │
└──────────────────────────────────────────────────────────┘
```

## Project layout

```
livemobillrerun/
├── README.md                    # this file
├── .gitignore
├── docs/
│   ├── ARCHITECTURE.md          # detailed system design
│   ├── ROADMAP.md               # 4-phase build plan
│   ├── PHASE1_DEVICE_CHECK.md   # how to inspect Redmi 13C state
│   ├── PHASE3_UNLOCK_ROOT.md    # bootloader unlock + Magisk root
│   └── PHASE4_HAL_HOOK.md       # MediaTek camera HAL hook design
│
├── vcam-pc/                     # ← Phase 2: DONE (Python)
│   ├── README.md
│   ├── requirements.txt         # pytest only (runtime is stdlib)
│   ├── config.json              # global config
│   ├── device_profiles.json     # rotation/resolution per device
│   ├── videos/                  # drop your .mp4 files here
│   ├── scripts/
│   │   └── check_device.sh      # Phase 1 helper
│   ├── src/
│   │   ├── main.py              # entry point
│   │   ├── config.py
│   │   ├── adb.py
│   │   ├── ffmpeg_streamer.py
│   │   ├── playlist.py
│   │   ├── tcp_server.py
│   │   └── ui/app.py            # Tkinter control panel
│   ├── tests/                   # 26 unit tests (pytest, no ffmpeg needed)
│   └── tools/
│       ├── fake_phone.py        # TCP client that pretends to be the phone
│       ├── make_sample_video.sh # generates a 5 s SMPTE mp4 with ffmpeg
│       └── smoke_test.sh        # full e2e: streamer + fake_phone + ffprobe
│
├── vcam-app/                    # ← Phase 4a: SKELETON (Android Studio)
│   └── README.md                # what to implement
│
└── vcam-magisk/                 # ← Phase 4b: SKELETON (Magisk module)
    ├── README.md
    ├── module/
    │   └── module.prop
    └── src/
        └── (HAL hook C++ source goes here)
```

## Roadmap

| Phase | Status     | Description                                      | Time      |
| ----- | ---------- | ------------------------------------------------ | --------- |
| 1     | **DONE**   | Inspect Redmi 13C / 14C / Poco C75 device state  | done      |
| 2     | **DONE**   | PC streamer (FFmpeg + TCP + GUI)                 | done      |
| 4a    | **DONE**   | Android receiver app (Kotlin, MediaCodec)        | done      |
| **5** | **DONE**   | **TikTok Live Screen Share auto-pilot — main path, no root** | **done** |
| 3     | optional   | Bootloader unlock + Magisk root *(only Phase 4b needs this)* | 7–30 days |
| 4b    | scaffolded | Magisk HAL module — direct camera injection      | 1 week    |

**Phase 5 is the main supported path now.** It uses TikTok's
built-in *Live → Screen Share* mode (MediaProjection capture of an
immersive-fullscreen receiver), which means it works on stock,
sealed, never-touched HyperOS 2 / Android 15 devices.

Phase 3 + 4b remain scaffolded for the niche case of fooling an app
that lacks a Screen Share equivalent (banking KYC, certain enterprise
video tools). For TikTok specifically, Phase 5 is strictly easier and
strictly better.

See `docs/ROADMAP.md` for details.

## Quick start

For a 30-minute end-to-end run-through (PC tools install → adb device check
→ phone connectivity smoke test), see **[QUICKSTART.md](QUICKSTART.md)**.

Bare-minimum form:

```bash
cd vcam-pc
bash tools/bootstrap_macos.sh        # downloads adb + arm64 ffmpeg into tools/bin/
source tools/bin/env.sh
bash tools/smoke_test.sh             # PC-only (no phone needed) — 15 s
bash tools/phone_smoke.sh            # phone-side — needs USB-debug + cable
```

Drop `.mp4` files into `vcam-pc/videos/` to use real footage instead of
the auto-generated SMPTE bars sample.

## Full source on another machine

To clone or archive the whole workspace, build **`vcam-app`**, sign **`apk/vcam-app-release.apk`**, and run **`vcam-pc`**, see **[docs/BUILD_FROM_SOURCE.md](docs/BUILD_FROM_SOURCE.md)**.

## License

Private project. Not for redistribution.
