# vcam-magisk — Magisk module (Phase 4b)

> **Status: builds & flashes; symbol-probe live; hook engine pending.**
>
> What works today:
> * Native skeleton compiles cleanly for both `arm64-v8a` and
>   `armeabi-v7a` (verified end-to-end with NDK r26 + cmake 3.22).
> * `dist/vcam-magisk.zip` is a valid Magisk module — flashes, installs
>   under `/data/adb/modules/livemobillrerun_vcam/`, registers the
>   Zygisk module.
> * On every spawned `cameraserver`/target-app process, `InstallHooks()`
>   walks `/system/lib*/libcameraservice.so`, dlopens it, and probes a
>   set of mangled C++ symbols (`CameraDeviceClient::onResultReceived`,
>   `CameraService::Client::sendCommandFrom`, …). Any match is logged
>   to `logcat -s vcam-hook:I` so you can confirm the device exposes
>   the symbols we expect on Android 13/14 / Redmi 14C.
> * The on-disk YUV file format (`vcam.yuv`) is byte-for-byte readable
>   from both the C++ side (`yuv_reader.cpp`) and the Kotlin side
>   (`io/YuvFileReader.kt` — used by the app's "Loopback verify" mode).
>   The header layout is now consistent across both.
>
> What is **not yet** wired:
> * The actual inline patch — we still need an inline-hook engine
>   (Dobby or shadowhook) under `src/zygisk/third_party/`. See
>   `src/zygisk/third_party/README.md` for one-shot drop-in
>   instructions; the CMake build picks it up automatically.
>
> This phase is gated on **Phase 3** (bootloader unlock + Magisk root).
> See `docs/PHASE3_UNLOCK_ROOT.md`.

## What this module does

When fully implemented, this module intercepts the Android Camera HAL
and serves the YUV file written by `vcam-app` (the receiver) instead of
real sensor output, for a curated list of target apps.

```
videos/*.mp4        ←  PC streamer (vcam-pc, ffmpeg)
       │ TCP / adb reverse
       ▼
vcam-app (receiver) → /data/local/tmp/vcam.yuv
       │
       ▼
vcam-magisk (Zygisk hook) ── replaces camera capture buffer ──→  TikTok / IG / etc.
```

## Layout

```
vcam-magisk/
├── README.md                       ← this file
├── build.sh                        ← packages module/ + .so → dist/vcam-magisk.zip
├── build_native.sh                 ← compiles src/zygisk/ via NDK
├── module/                         ← payload that lives at /data/adb/modules/<id>/
│   ├── module.prop                 ← Magisk metadata
│   ├── customize.sh                ← runs at install time (ui_print)
│   ├── post-fs-data.sh             ← runs early-boot, relabels SELinux on YUV file
│   ├── service.sh                  ← runs late-boot (no-op for now)
│   └── sepolicy.rule               ← lets cameraserver read the YUV file
└── src/
    └── zygisk/                     ← Strategy A: Zygisk + framework hook
        ├── CMakeLists.txt
        ├── zygisk.h                ← minimal subset of Magisk's Zygisk API
        ├── main.cpp                ← onLoad / preAppSpecialize / postAppSpecialize
        ├── camera_hook.{h,cpp}     ← libcameraservice.so symbol probe
        ├── yuv_reader.{h,cpp}      ← reads /data/local/tmp/vcam.yuv frames
        └── third_party/
            └── README.md           ← drop-in instructions for Dobby
```

## Strategy A — Zygisk framework hook (skeleton scaffolded here)

`src/zygisk/main.cpp` sets up a `VcamModule : zygisk::ModuleBase` that
runs in every spawned zygote child. We filter to a hardcoded set of
process names (`cameraserver`, target apps) and, on a match, install
inline hooks against `libcameraservice.so` symbols.

The actual symbol patching is **left as a stub**:

```cpp
// src/zygisk/camera_hook.cpp
bool InstallHooks() {
    // TODO(phase4b): integrate dobby / shadowhook to:
    //   1. dlopen("/system/lib64/libcameraservice.so") and find
    //      `_ZN7android20CameraDeviceClient16onResultReceived...`.
    //   2. Replace with trampoline that mutates the resulting
    //      android::CameraMetadata + binder data so the captured
    //      Image's planes point at YuvReader::ReadLatest()'s buffers.
    //   3. For Camera1 apps, hook
    //      `android.hardware.Camera.takePicture` at the JNI layer.
    return false;
}
```

Pick **one** of:

- **[Dobby](https://github.com/jmpews/Dobby)** — small, MIT, drop-in
  for arm64; needs `dobby_hook(addr, replace, &original)`. Add the
  prebuilt `libdobby.a` under `src/zygisk/third_party/dobby/` and link
  it in `CMakeLists.txt`.
- **[shadowhook](https://github.com/bytedance/shadowhook)** — used by
  major apps in the wild; better stability on MTK SoCs.

## Strategy B — HAL overlay (MediaTek), fallback only

If a target hardens against framework hooks, drop a wrapper at
`/vendor/lib64/hw/camera.mt6769.so` that loads the original as
`.real` and intercepts `getCameraDeviceInterface_V3_X`. This is
SoC-specific (mt6769 = Helio G81-Ultra/G85). Skeleton intentionally
not provided — if you get this far, see `docs/PHASE4_HAL_HOOK.md`
for the reverse-engineering checklist.

## Build

```bash
# 1. Compile native (only needed when src/zygisk changes).
ANDROID_NDK=$HOME/Library/Android/sdk/ndk/26.1.10909125 \
  bash build_native.sh arm64-v8a

# 2. Package flashable zip.
bash build.sh
# → dist/vcam-magisk.zip
```

If `build_native.sh` hasn't been run, `build.sh` still produces a valid
zip — it just contains only the shell scripts and SELinux rules.
Useful for sanity-checking the install pipeline before the hook is
ready.

## Install (on a phone with Magisk + Zygisk enabled)

```bash
adb push dist/vcam-magisk.zip /sdcard/Download/
# Magisk app → Modules → Install from storage → pick the zip → Reboot
```

After reboot, verify it's loaded:

```bash
adb shell ls -la /data/adb/modules/livemobillrerun_vcam/
adb shell logcat -d -s vcam-zygisk:V vcam-yuv:V vcam-hook:V | head -20
```

You should see `module loaded` from `vcam-zygisk` once and one
`preAppSpecialize` line per spawned target app.

## Recovery

If the phone bootloops, reboot to recovery (Vol Up + Power on Xiaomi)
or hold Vol Down for ~10s during boot to enter Magisk safe mode (which
disables every module). From shell:

```bash
adb shell rm -rf /data/adb/modules/livemobillrerun_vcam
adb reboot
```
