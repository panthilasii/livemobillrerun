# Roadmap

## Phase 1 — Device inspection (30 min)

**Goal**: Know exactly what we're working with on the Redmi 13C.

Run `vcam-pc/scripts/check_device.sh` (Mac/Linux) or read
`docs/PHASE1_DEVICE_CHECK.md` for manual commands.

We need to record:
- SoC model (`ro.soc.model`) — expect `MT6769V/CU` (Helio G85) or similar
- Android version + HyperOS/MIUI version
- Bootloader status (`ro.boot.flash.locked`)
- Verified boot state
- ABI (`ro.product.cpu.abi`) — expect `arm64-v8a`

**Output**: `docs/device_state.txt` (manually saved)

---

## Phase 2 — PC Streamer (1–2 days) **← DONE (code), e2e on user box**

**Goal**: A working PC app that streams a looped video over TCP.

### Sub-tasks

- [x] Project scaffold
- [x] Config + device profiles JSON
- [x] `ffmpeg_streamer.py` — FFmpeg subprocess wrapper
- [x] `tcp_server.py` — accept phone connection, forward H.264 stream
- [x] `playlist.py` — multi-file playlist with loop
- [x] `adb.py` — subprocess wrapper, handles `adb reverse`
- [x] `ui/app.py` — Tkinter control panel
- [x] `main.py` — CLI + GUI entry points
- [x] Unit tests (`tests/`, 26 passing — config, playlist, ffmpeg cmd builder, tcp server with fake ffmpeg)
- [x] Fake-phone smoke test (`tools/fake_phone.py` + `tools/smoke_test.sh`)
- [ ] **You** run `tools/smoke_test.sh` once `ffmpeg` is installed locally

### Unit tests (no FFmpeg / ADB needed)

```bash
cd vcam-pc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

### End-to-end smoke test (needs ffmpeg)

```bash
brew install ffmpeg          # or: sudo apt install ffmpeg
cd vcam-pc
bash tools/smoke_test.sh
```

That script generates a 5 s SMPTE colour-bar mp4 (if `videos/` is empty),
launches the streamer, has `tools/fake_phone.py` capture ~8 s as raw H.264,
runs `ffprobe` on the result. Exits 0 on success.

### Test with a real video player

```bash
# Terminal A
python -m src.main --cli --no-adb-reverse --port 8888

# Terminal B
ffplay tcp://localhost:8888
```

If you see your video looping, Phase 2 is done.

---

## Phase 3 — Bootloader unlock + root (7–30 days, mostly waiting)

**Goal**: Redmi 13C running unlocked + Magisk + Zygisk.

### Steps (do NOT skip)

1. **Backup everything.** Photos, contacts, app data via Mi Cloud or local.
2. **Bind Mi Account** to the phone for at least 7 days
   (Settings → Mi Account → sign in → leave it).
3. Apply for unlock via **Mi Unlock app** on Windows
   (Xiaomi only ships Windows version — boot Windows or use a Win VM).
4. **Wait** for the approval timer. HyperOS 1: 7–30 days. HyperOS 2: longer.
5. Once approved: connect phone in Fastboot mode → `mi unlock` → **WIPES**.
6. Reflash stock fastboot ROM matching your region (CN/Global/EEA).
7. Get `boot.img` from the matching firmware → patch with **Magisk Manager**
   on the phone → flash via `fastboot flash boot magisk_patched.img`.
8. Install **Zygisk** + **Shamiko** + **Play Integrity Fix** modules (optional
   but needed for apps that block rooted devices).
9. Verify with **Magisk** (root status) and **Play Integrity Checker**
   (DEVICE + BASIC pass minimum).

See `docs/PHASE3_UNLOCK_ROOT.md` for command-by-command walkthrough.

---

## Phase 4 — Android receiver app + Magisk HAL (1–2 weeks)

### 4a. Android receiver (`vcam-app/`)  **← scaffold committed**

- [x] Gradle project (AGP 8.5.2, Kotlin 2.0.20, compileSdk 34, minSdk 33)
- [x] AndroidManifest + permissions + foreground-service-type=dataSync
- [x] `MainActivity.kt` — host/port + Start/Stop + log view
- [x] `VcamService.kt` — foreground service, wake-lock, notification w/ Stop action
- [x] `StreamPipeline.kt` — wires TcpClient → H264Decoder → YuvFileWriter
- [x] `TcpClient.kt` — auto-reconnect (1 s back-off) on dropped socket
- [x] `H264Decoder.kt` — async MediaCodec, packs `Image` → I420 with stride-aware copy
- [x] `YuvFileWriter.kt` — 16-byte LE header + atomic rename to `/data/local/tmp/vcam.yuv`
- [x] `AppLogger.kt` — multi-listener log fan-out for the UI tail
- [x] `tools/bootstrap_build_macos.sh` — portable JDK17 + Android SDK + Gradle 8.10.2
- [x] `gradle :app:assembleDebug` — produces `app/build/outputs/apk/debug/app-debug.apk` (~7.3 MB)
- [ ] Connect to a desktop streamer over `adb reverse tcp:8888`, watch logcat for `[Pipeline]` lines
- [ ] On a real device, write to `/data/local/tmp/vcam.yuv` (need root for cameraserver to read)

### 4b. Magisk HAL module (`vcam-magisk/`)

- C++ shared library that overlays
  `/vendor/lib64/hw/camera.<soc>.so`
- Implements `ICameraDevice` HIDL/AIDL
- For each capture request:
  - read `/data/local/tmp/vcam.yuv`
  - copy YUV → output buffer
  - if file missing → forward to real HAL
- Magisk template files in `module/`:
  - `module.prop`, `service.sh`, `post-fs-data.sh`
  - `system/vendor/lib64/hw/camera.mt6769.so` (replacement)

### Risks specific to MediaTek

MediaTek's `mtkcam` is closed-source. We have two strategies:

**Strategy A (preferred): Vendor binary hooking**
- Use **LSPosed Zygisk** to hook `android.hardware.camera2.CameraManager`
  at framework level, return a custom `CameraDevice` impl that reads our
  YUV file. Works without touching `/vendor/`.

**Strategy B (fallback): HAL overlay**
- Replace `camera.mt6769.so` with a wrapper that loads the original as
  `.real`, intercepts the relevant vtable entries.

We'll start with **A** because it's lower-risk and easier to revert.

---

## Phase 5 (optional) — Quality of life

- Multi-device support (run on 2+ phones in parallel)
- Audio routing (route PC audio → phone mic via virtual mic HAL)
- Hot-reload of playlist
- Remote control via local web UI
