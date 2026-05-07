# Quickstart — PC video → TikTok Live, no root needed

A **15-minute** path from "I just plugged in my Redmi" to a live
TikTok broadcast streaming a video file on your behalf. Tested on
**macOS Apple Silicon** with no Homebrew and no Android Studio. The
phone does **not** need to be rooted, and **not** need Mi Unlock.

How it works (the trick):

- The PC encodes H.264 with `ffmpeg` and serves it over TCP.
- `adb reverse` tunnels the TCP port from the phone back to the PC.
- The Android app pulls the stream, decodes it with `MediaCodec`,
  and renders it **fullscreen** in immersive Live Mode.
- TikTok's **Live → Screen Share** mode (a built-in TikTok feature)
  uses Android's `MediaProjection` API to capture the display.
  Because Live Mode hides every chrome element, MediaProjection only
  sees the streamed video — TikTok thinks it's a normal screen
  broadcast.
- The **🔴 Go Live on TikTok** button in the GUI drives all of the
  above plus the TikTok app's UI through `uiautomator`, leaving you
  one tap away from going live.

No root, no bootloader unlock, no flashing — every API used is part
of stock Android.

Optional advanced path (Phase 4b — for direct Camera HAL injection
to apps that *don't* have a Screen Share mode): see
`docs/PHASE3_UNLOCK_ROOT.md` and `vcam-magisk/README.md`. Most users
will never need it.

---

## 0. Phone setup (one-time, 5 min)

On the Redmi 13C / 14C / Poco C75:

1. Settings → About phone → tap **MIUI version** 7 times to enable
   Developer options.
2. Settings → Additional settings → Developer options → enable
   **USB debugging** *and* **USB debugging (security settings)**.
3. Connect to the Mac with a USB-C **data** cable.
4. When the "Allow USB debugging?" prompt appears, tick **Always allow
   from this computer** and tap **OK**.

Test it:

```bash
source /Users/ii/livemobillrerun/.tools/env.sh
adb devices -l
# → list of devices attached
#   abc123…  device  product:topaz model:Redmi_13C
```

If it says `unauthorized`, retick the prompt; if nothing shows up at
all, swap the cable (the bundled charge cables on most retail
adapters are charge-only) or run `adb kill-server && adb start-server`.

## 1. Start the PC GUI (1 click, 0 typing)

```bash
cd /Users/ii/livemobillrerun/vcam-pc
source tools/bin/env.sh
python3 -m src.main --gui
```

The window has three sections:

```
┌───────────────────────────────────────────────────────────┐
│ 1. Stream                                                 │
│    profile:  Redmi 13C (HyperOS / Android 14)  ▼          │
│    res:      1280x720    fps:  30   bitrate:  4M          │
│                                                           │
│ 2. Playlist + ADB                                         │
│    videos:   smpte_30s.mp4  (5.1 MB)                      │
│    adb:      abc123… [Redmi_13C]  →  device               │
│    apk:      app-debug.apk (5.6 MB) · installed on phone  │
│                                       [ Install vcam app ]│
│                                                           │
│ 3. Control                                                │
│    status:   running                                      │
│    phone yuv: 1350 KiB  ·  age 0.4s  ·  ✓ live            │
│    [ ▶ Start streamer + phone ]  [ ■ Stop ]  [ Open app ] │
└───────────────────────────────────────────────────────────┘
```

The right-most button on the bottom row, **"Open app on phone"**,
launches the receiver app on the phone *and* taps its Start button
for you. Use it on first run; after that the **▶ Start** button does
the same thing automatically.

If the **apk** row says `not installed`, click **Install vcam app on
phone** first.

## 2. Click ▶ Start

That's it. You should see, within ~2 seconds:

- PC GUI: `status: running`, `phone yuv: ✓ live`, MB counter rising.
- Phone: live colour-bars preview at the top, FPS overlay reading
  ~30 fps, `frames received` counter rising.

The "phone yuv" line is the most important confidence indicator —
it's polled via `adb stat` once per second. `✓ live` means the
phone's `YuvFileWriter` is actively rewriting the file faster than
2 s; `△ slow` means it's lagging; `✗ stalled` means decoding has
stopped despite the PC still sending bytes.

## 3. Loopback verify (optional, recommended once)

In the receiver app, tick **"Loopback verify (read vcam.yuv from
disk)"**. The preview now reads back the on-disk YUV file instead
of the in-memory bus. If both views look identical, the file
format is exactly what the Magisk HAL hook will consume in Phase 4b.

The overlay switches from `… fps · N total` to
`loopback W×H · idx N · M read` — the `idx` field is the frame
counter from the file header, which proves the writer is using the
canonical `'VCAM' magic + width + height + frame_counter` layout.

## 4. Tilt your head (optional)

The captured video is rotated to match the device profile's expected
camera orientation, which means it shows up sideways on the preview.
Tick **"Show preview as portrait"** to un-rotate it for human viewing
— it does *not* affect the bytes that get written to disk.

## 5. 🔴 Go Live on TikTok (the actual point of all this)

This is the new flow that replaces Phase 3 entirely.

Pre-flight:

- Streamer is running (`status: running`, MB counter rising).
- Receiver app is launched on the phone (the **▶ Start** button does
  this for you).
- TikTok is **installed** on the phone and you're **logged in**.
- Your TikTok account is **eligible for Live**: in Thailand this is
  usually 18+ verification + a few followers. If you're not eligible,
  no automation in the world fixes that — TikTok blocks the Live
  button server-side.

Click **🔴 Go Live on TikTok** in the GUI. It will:

1. Send an Intent to the receiver app: `--ez vcam_live true` →
   the app switches to immersive fullscreen, hides every UI element,
   and locks orientation portrait.
2. Wait ~2 s for the first decoded frame to land in the Live overlay.
3. Launch TikTok and walk its UI:
   * tap the **Live** tab on the Create screen,
   * tap the **Go Live** / **เริ่มไลฟ์** button,
   * tap the **Screen Share** / **แชร์หน้าจอ** mode toggle,
   * stop *one tap before* "Start Now" — you give the final go-ahead
     yourself, on the phone.

The receiver app is now showing your video fullscreen with no chrome.
When you tap **Start Now** in TikTok, MediaProjection captures the
display and broadcasts it. Viewers see only the video.

To stop, swipe down to expose the system bar, tap TikTok's stop
button, then tap the receiver app's preview to exit Live Mode.

## 6. Stop

Click **■ Stop** in the GUI, or close the window. The streamer cleans
up the FFmpeg subprocess, removes the `adb reverse` tunnel, and the
phone-side service shuts down (you can also tap the app's own Stop
button).

---

## Demoing this to a friend

1. Drop their video into `vcam-pc/videos/`. Anything ffmpeg can read
   — `.mp4`, `.mov`, `.mkv`. Multiple files loop back-to-back.
2. Click **▶ Start streamer + phone** in the GUI.
3. Hand them the phone with the receiver app already running.
4. (Optional) Click **🔴 Go Live on TikTok** if their account is
   live-eligible.

The whole pipeline runs at ~30 fps on Apple Silicon → Redmi 13C with
no thermal throttling visible across a 30-minute soak. The MediaCodec
stall watchdog inside the app means a hung decoder recovers in ≤8 s
without dropping the connection.

---

## What's next

| Phase | Status                 | What it gives you                           |
| ----- | ---------------------- | ------------------------------------------- |
| 1     | ✅ done                | device matrix in `docs/`                    |
| 2     | ✅ done                | PC GUI, FFmpeg, TCP server                  |
| 4a    | ✅ done                | live preview on phone (this guide)          |
| **5** | **✅ done**            | **TikTok Live Screen Share auto-pilot — main path** |
| 3     | 🛑 not on the main path | bootloader unlock, only needed for Phase 4b |
| 4b    | ⏳ scaffolded          | direct camera HAL hook (root-only, advanced)|

**Phase 5 is the main path now**: it doesn't need root, it doesn't
need Mi Unlock, and it works on stock HyperOS 2 / Android 15.

The Phase 3 / 4b path is still scaffolded for users who specifically
need to fool an app that *doesn't* have a Screen Share mode (e.g. a
banking KYC verifier, a video conferencing app). For TikTok Live,
Phase 5 is strictly better.

---

## Power-user CLI

If you'd rather skip the GUI:

```bash
cd /Users/ii/livemobillrerun/vcam-pc
source tools/bin/env.sh
python3 -m src.main --cli --profile "Redmi 14C"
# launches PC streamer + adb reverse + HealthMonitor in one shot
# logs include: [stat] kbps, frames sent, phone yuv freshness, ...
```

To rebuild the APK after editing `vcam-app/`:

```bash
source /Users/ii/livemobillrerun/.tools/env.sh
cd /Users/ii/livemobillrerun/vcam-app
gradle :app:assembleDebug
adb install -r -g app/build/outputs/apk/debug/app-debug.apk
```

To rebuild the Magisk module (for flashing once Phase 3 is unlocked):

```bash
source /Users/ii/livemobillrerun/.tools/env.sh
cd /Users/ii/livemobillrerun/vcam-magisk
bash build_native.sh arm64-v8a
bash build_native.sh armeabi-v7a
bash build.sh
# → dist/vcam-magisk.zip ready to flash via Magisk app
```
