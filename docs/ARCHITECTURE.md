# Architecture

## Overview

`livemobillrerun` consists of three independent components that talk to each
other over well-defined interfaces:

```
┌──────────────┐  H.264/TCP  ┌──────────────┐  YUV file  ┌──────────────┐
│  PC Streamer │ ──────────► │  Android App │ ─────────► │ Magisk HAL   │
│  (Python)    │             │  (Kotlin)    │            │  (C++)       │
└──────────────┘             └──────────────┘            └──────────────┘
                                                                │
                                                                ▼
                                                         Camera2 API
                                                         (any app on
                                                          the phone)
```

## Component contracts

### 1. PC Streamer → Android App

- **Transport**: TCP over `adb reverse tcp:8888 tcp:8888`
- **Payload**: Annex-B H.264 elementary stream
- **Framing**: Length-prefixed NAL units
  ```
  uint32 (big-endian)  length of NAL unit
  bytes[length]        NAL unit (without start code)
  ```
- **Resolution**: must match what the HAL reports to clients (default 1280x720)
- **FPS**: 30 (constant)
- **Reconnect**: app retries every 1s if TCP drops

### 2. Android App → Magisk HAL

- **Path**: `/data/local/tmp/vcam.yuv`  (writable by `shell` group, readable
  by `cameraserver` user via SELinux exception in module)
- **Format**: raw YUV420 (I420), `width * height * 3 / 2` bytes per frame
- **Header (16 bytes, little-endian)** prepended on every write:
  ```
  uint32  magic = 0x564D4143  ("VCAM")
  uint32  width
  uint32  height
  uint32  frame_counter
  ```
- **Update model**: app overwrites the file atomically (write to
  `vcam.yuv.tmp` then `rename(2)`); HAL reads on each `dequeueBuffer` call.

### 3. Magisk HAL → Camera2 client

- HAL implements the standard `ICameraDevice` HIDL/AIDL interface.
- For each request, reads `/data/local/tmp/vcam.yuv`, validates magic,
  copies YUV plane to the request's output buffer.
- If file is missing/stale, falls back to the original camera HAL (loaded
  from `/vendor/lib64/hw/camera.<soc>.so.real`).

## Process model on PC

```
┌─────────────────────────────────────────────────────────┐
│ main.py                                                 │
│ ├─ ConfigManager  (config.json + device_profiles.json)  │
│ ├─ AdbController  (subprocess wrapper)                  │
│ ├─ Playlist       (file list, loop, current index)      │
│ ├─ FFmpegStreamer (subprocess: video → H.264 → stdout)  │
│ ├─ TcpServer      (forwards FFmpeg stdout → phone)      │
│ └─ Tkinter GUI    (controls all of the above)           │
└─────────────────────────────────────────────────────────┘
```

Each subsystem is independent — you can run the streamer **headless** via
`--cli`, useful for automation later.

## Process model on phone

```
com.livemobillrerun.vcam (foreground service)
├─ TcpClient         (connects to localhost:8888)
├─ H264Decoder       (MediaCodec, async mode)
├─ YuvWriter         (writes /data/local/tmp/vcam.yuv)
└─ ForegroundService (notification, keeps process alive)
```

## Why this approach?

- **No third-party APK is touched.** Every running app on the phone uses the
  same OS-level Camera HAL — by replacing the HAL on a device you own, every
  app automatically sees the virtual camera. We never modify or distribute
  third-party software.
- **TCP framing instead of named pipe**: ADB reverse already exists; named
  pipes on rooted Android are a pain across SELinux contexts.
- **YUV file instead of shared memory**: simpler to debug; HAL reads at
  ~30 fps so file I/O overhead is negligible.
- **Magisk module instead of forking AOSP**: keeps the user's stock ROM
  intact, easy to disable (just remove the module).

## Limitations

1. Phone must be **rooted** with Magisk installed.
2. `Play Integrity` will fail by default — apps that strictly enforce it may
   refuse to run. Workarounds (Shamiko, PIF) are documented but not bundled.
3. **MediaTek HAL is not as well-documented as Qualcomm.** The Phase 4
   implementation needs to be tailored per SoC. We start with `mt6769` (G85).
4. Audio is **not** routed; only video. Apps that record audio will get the
   real microphone.
