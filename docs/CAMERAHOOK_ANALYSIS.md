# CameraHook architecture — analysis of `UltimateRerun.rar`

**Date:** 2026-05-05
**Source:** `app-debug.apk` from `/Users/ii/Downloads/UltimateRerun.rar`
**Decompiler:** jadx 1.5.0 against `/tmp/UltimateRerun_inspect/jadx_out/`

This document captures *how* the reference Xposed module replaces
TikTok's live camera feed with a video file, so we can port the same
technique into `vcam-app` once Phase 3 (root) is unlocked.

It is not a copy-paste port. It is an architecture map.

---

## TL;DR

UltimateRerun does NOT fight the camera HAL. It hooks **above** the
camera, at the boundary where TikTok's app code feeds raw frames into
the H.264 encoder. By replacing the encoder's input Surface with a
MediaPlayer-driven Surface, the encoder happily encodes whatever video
file we want — and the upstream camera path becomes irrelevant.

This makes the technique:

* **Universal** — works on Camera1 (`android.hardware.Camera`) and
  Camera2 (`android.hardware.camera2.*`). TikTok's legacy paths and
  modern paths both end up funnelling through `MediaCodec`.
* **Stable across phones** — no SoC-specific HAL knowledge needed.
* **Stable across TikTok versions** — the hooks target *Android
  framework* APIs (`MediaCodec`, `AudioRecord`, `Camera`,
  `CaptureRequest`), which TikTok cannot change without breaking the
  whole platform.
* **Hard requirement: Xposed/LSPosed = root.**

Without root there is no Xposed and no way to inject a class into
TikTok's process. Phase 5 (Screen Share Live) is the only no-root
fallback.

---

## Hook points (in order of importance)

Each Java function in `CameraHook.kt` is summarised below with the
exact API signature it targets and what it does.

### 1. `MediaCodec.configure()` — encoder discovery

```kotlin
findAndHookMethod(MediaCodec.class, "configure",
    MediaFormat.class, Surface.class, MediaCrypto.class, Integer.TYPE,
    object : XC_MethodHook() { ... })
```

When TikTok calls `MediaCodec.configure(format, surface, crypto, flags)`
the hook inspects:

* `flags & CONFIGURE_FLAG_ENCODE` → only encoders.
* `format.getString("mime")`:
    * `video/...` → add to `videoEncoders` set.
    * `audio/...` → add to `audioEncoders` set, **also** start
      `AudioFeeder` if it isn't running yet.

Output: every encoder TikTok creates during a Live session is now
known to us.

### 2. `MediaCodec.createInputSurface()` — the killer hook

```kotlin
findAndHookMethod(MediaCodec.class, "createInputSurface",
    object : XC_MethodHook() {
        override fun afterHookedMethod(param) {
            val surface = param.result as? Surface ?: return
            val mode = resolvedMode()
            if (mode == 2 /* replace */) {
                val path = activeVideoPath ?: VideoFeeder.activeVideoPath
                VideoFeeder.feedToSurface(surface, path)
            }
        }
    })
```

The encoder returns a `Surface`. `MediaPlayer` can render directly to
a `Surface`. So we point a fresh `MediaPlayer` (loaded with our video)
at the encoder's input Surface. From the encoder's perspective, frames
are arriving as normal — only their *origin* changed.

`VideoFeeder.feedToSurface()` (see below) maintains a
`Map<Surface, MediaPlayer>` so multiple Surfaces can each play a video
in parallel. It also handles loop, seek-back-to-zero on EOS, and
graceful stop when the Surface is released.

### 3. `CaptureRequest.Builder.addTarget()` — Camera2 short-circuit

```kotlin
findAndHookMethod(CaptureRequest.Builder.class, "addTarget",
    Surface.class, object : XC_MethodHook() {
        override fun beforeHookedMethod(param) {
            val surface = param.args[0] as? Surface ?: return
            if (resolvedMode() == 2) {
                VideoFeeder.feedToSurface(surface, activeVideoPath)
            }
            param.result = null   // BLOCK the addTarget — camera doesn't get the surface
        }
    })
```

Some TikTok code paths (rare in Live, common in regular video
recording) plumb a `SurfaceTexture` straight from the
`CameraDevice` through a `CaptureRequest` — bypassing
`createInputSurface`. This hook catches that path: feed our video onto
the Surface ourselves *and* prevent the camera from being attached to
it. The result is the same — encoder sees our frames.

### 4. `Camera.setPreviewTexture()` — Camera1 legacy

```kotlin
findAndHookMethod(Camera.class, "setPreviewTexture",
    SurfaceTexture.class, object : XC_MethodHook() {
        override fun beforeHookedMethod(param) {
            val orig = param.args[0] as? SurfaceTexture ?: return
            if (resolvedMode() == 2) {
                VideoFeeder.feedToSurface(Surface(orig), activeVideoPath)
            }
            param.args[0] = SurfaceTexture(0)   // give the camera a dummy
        }
    })
```

For old Camera1 paths (Android < 5 era APIs that some TikTok modules
still use), the same trick applies. Render our video onto the original
SurfaceTexture, then hand the camera a *fresh empty* one so the
camera's frames go nowhere.

### 5. Audio replacement — three call sites

TikTok reads microphone PCM in three different ways. The hook covers
all of them:

```kotlin
// (a) AudioRecord.read(byte[], int, int)
// (b) AudioRecord.read(byte[], int, int, int)
// (c) AudioRecord.read(ByteBuffer, int)
findAndHookMethod(AudioRecord.class, "read", ...)

// (d) MediaCodec.queueInputBuffer(int idx, int off, int size, long pts, int flags)
// — substitute the buffer contents for known audio encoders only
findAndHookMethod(MediaCodec.class, "queueInputBuffer", ...)
```

In each case `AudioFeeder.read()` decodes the audio track of the
active video file with `MediaExtractor` + `MediaCodec` (audio
decoder), buffers PCM, and hands it back as if it came from the
microphone.

To prevent TikTok from removing music/echo from the substituted audio,
two more hooks zero out post-processing:

```kotlin
findAndHookMethod("android.media.audiofx.AcousticEchoCanceler",
    "create", Integer.TYPE, ...)   // result = null
findAndHookMethod("android.media.audiofx.NoiseSuppressor",
    "create", Integer.TYPE, ...)   // result = null
```

### 6. Mode plumbing

```kotlin
class VCamModeReceiver : BroadcastReceiver() {
    fun onReceive(ctx, intent) {
        CameraHook.currentMode = intent.getIntExtra("mode", 0)
        CameraHook.activeVideoPath = intent.getStringExtra("videoPath") ?: <auto>
        VideoFeeder.loopEnabled = intent.getBooleanExtra("loop", true)
        VideoFeeder.rotationDegrees = intent.getFloatExtra("rotation", 0f)
        VideoFeeder.zoomLevel = intent.getFloatExtra("zoom", 1f)
        VideoFeeder.flipX = intent.getBooleanExtra("flipX", false)
        VideoFeeder.flipY = intent.getBooleanExtra("flipY", false)
        ...
    }
}
```

Modes:

| `mode` value | meaning                                            |
|--------------|----------------------------------------------------|
| `0`          | passthrough (real camera reaches encoder normally) |
| `1`          | block (encoder gets nothing → black frames)        |
| `2`          | **replace** with `activeVideoPath`                  |

Auto-fallback in `resolvedMode()` — if `currentMode == 0` but
`/data/local/tmp/vcam_enabled` exists on disk, mode becomes `2`. This
lets a shell command flip the switch:

```bash
adb shell touch /data/local/tmp/vcam_enabled       # ON
adb shell rm    /data/local/tmp/vcam_enabled       # OFF
```

Auto-discovery of the video file. `VideoFeeder.getActiveVideoPath()`
walks this list (first hit wins):

```
/data/local/tmp/vcam_hook_playlist.txt   ← line-delimited list, file://, http://
/sdcard/vcam_hook_playlist.txt
/sdcard/Android/data/com.livemobile.vcam/files/vcam_hook_playlist.txt
…
/sdcard/Android/data/com.ss.android.ugc.trill/files/vcam.mp4
/data/local/tmp/vcam_final.mp4
/storage/emulated/0/vcam_final.mp4
/sdcard/vcam_final.mp4
…
```

So just dropping a file at `/data/local/tmp/vcam_final.mp4` plus
touching `/data/local/tmp/vcam_enabled` is enough to activate the
module — no broadcast required.

---

## What the package ships beyond hooks

The `com.livemobile.vcam` APK contains, in order of relevance to us:

| Class                         | Purpose                                                |
|-------------------------------|--------------------------------------------------------|
| `hook/CameraHook.kt`          | Xposed entry — installs all the hooks above            |
| `hook/VideoFeeder.kt`         | Surface ↔ MediaPlayer pool, loop/rotate/zoom/flip      |
| `hook/AudioFeeder.kt`         | Audio decode + PCM ring buffer                         |
| `hook/FlipRenderer.kt`        | GLES helper for live mirroring                         |
| `hook/VCamModeReceiver.kt`    | Broadcast control surface                              |
| `hook/CaptchaBypassHook.kt`   | Bypass TikTok captcha popups (best-effort)             |
| `MainActivity.java`           | Settings UI                                            |
| `LoginActivity.java`          | **License gate** — refuses to open without a paid key  |
| `AuthManager.kt`              | Talks to `https://ultimateinfinity.tech/api/vcam/*`    |
| `VideoServerService.kt`       | HTTP server for in-LAN video pushes                    |
| `VideoTranscoder.kt`          | Re-encode videos to TikTok-friendly H.264              |
| `AutoAppealService.kt`        | Accessibility service: auto-appeal banned accounts     |
| `KeepAliveService.kt`         | Foreground service to survive Doze                     |

**Commercial parts** (Login/Auth/Telegram-notify) are not interesting
to us — we'll re-implement only the hook portion in our own `vcam-app`
and keep our existing PC streamer + adb-reverse pipeline.

---

## Porting plan into our project

Once Phase 3 (Mi Unlock + Magisk + LSPosed) is complete:

1. **Add Xposed scaffolding to `vcam-app`**

    * Add `assets/xposed_init` with the line:

      ```
      com.livemobillrerun.vcam.hook.CameraHook
      ```

    * Add the meta-data block to `AndroidManifest.xml`:

      ```xml
      <meta-data android:name="xposedmodule" android:value="true" />
      <meta-data android:name="xposeddescription"
                 android:value="VCam — replace TikTok camera with streamed video" />
      <meta-data android:name="xposedminversion" android:value="93" />
      ```

    * Add the Xposed API to `app/build.gradle.kts`:

      ```kotlin
      compileOnly("de.robv.android.xposed:api:82")
      ```

2. **Re-implement these classes in `com.livemobillrerun.vcam.hook`**

    | New file                               | Mirrors UltimateRerun's …  |
    |----------------------------------------|-----------------------------|
    | `CameraHook.kt`                        | `hook/CameraHook.kt`        |
    | `VideoFeeder.kt`                       | `hook/VideoFeeder.kt`       |
    | `AudioFeeder.kt`                       | `hook/AudioFeeder.kt`       |
    | `FlipRenderer.kt`                      | `hook/FlipRenderer.kt`      |
    | `VCamModeReceiver.kt`                  | `hook/VCamModeReceiver.kt`  |

    Total target: ~1900 lines of Kotlin, mostly mechanical translation
    from the decompiled jadx output.

3. **Wire our existing streamer to feed the hook**

    * Phase 4a writes `vcam.yuv` continuously. The hook expects an
      `.mp4` file. Two clean approaches:
        - Drop YUV mode entirely and have the PC FFmpeg encode an
          `.mp4` rolling segment to disk → `adb push` to
          `/sdcard/vcam_final.mp4`. Simpler.
        - Keep YUV mode and write a tiny container shim that wraps
          our raw I420 stream into an MP4 for the hook.

    * Add a helper Magisk Module post-install step:

      ```bash
      su -c 'touch /data/local/tmp/vcam_enabled'
      ```

      so the hook auto-engages on TikTok cold start.

4. **Sanity-check on the device**

    * After install, in TikTok logcat:

      ```
      adb logcat -s 'Xposed':*  | grep VCAM_HOOK
      ```

      expect lines like:

      ```
      [VCAM_HOOK] ✅ loaded in TikTok — installing hooks
      [VCAM_HOOK] detected video encoder: video/avc
      [VCAM_HOOK] 🎬 attaching video to encoder surface path=demo.mp4
      ```

    * Open TikTok → start Live → flip front/rear camera. Whichever you
      pick, viewers see the streamed video.

---

## Risk register / things they got right

* **Hook level.** Hooking at `MediaCodec.createInputSurface` instead
  of fighting the camera HAL means the same hook works on every chip,
  every Android version 8+, regardless of vendor camera weirdness.
* **Audio path is treated separately.** Camera replacement without
  audio replacement means viewers hear room sound while seeing your
  video — instant tell. The 3 `AudioRecord.read` overloads + the
  `MediaCodec.queueInputBuffer` path together cover every audio
  capture method an Android app can plausibly use.
* **AEC/NS off.** Without disabling the audio post-processing, the
  injected audio gets gated/filtered to silence. Their hooks return
  `null` from `AcousticEchoCanceler.create()` and
  `NoiseSuppressor.create()` so TikTok can't apply them.
* **Mode flag on disk.** `/data/local/tmp/vcam_enabled` is a perfect
  switch — `adb shell touch / rm` from the PC enables/disables without
  needing the app to be running.

## Risk register / what to be careful of when we port

* **MediaPlayer is not a great injector for low latency.** It works,
  but if our PC streamer is producing 30fps and the encoder Surface
  is being driven by our MediaPlayer at the same rate, any decode
  hiccup shows up as a frozen broadcast. Long-term, write our own
  `MediaCodec` decoder that pushes frames onto the Surface at the
  encoder's pull rate — same code we already have in
  `vcam-app/.../core/H264Decoder.kt`.
* **License gate behaviour.** If we decompile their APK and ship its
  hooks under our package name, that's a copyright issue — even
  though Xposed code is small, it is theirs. We translate the
  technique, not the bytes; rewrite all of it in our own style.
* **Captcha bypass.** TikTok shows a captcha if it suspects bot
  behaviour from a Live session. UltimateRerun has
  `CaptchaBypassHook.kt` for this. We can ignore captcha at first and
  only add a bypass if real testing shows it firing.

---

## Phase status update (after this analysis)

| Phase | Status        | Notes                                                      |
|-------|---------------|------------------------------------------------------------|
| 1     | DONE          | device matrix                                              |
| 2     | DONE          | PC streamer                                                |
| 4a    | DONE          | preview + YUV writer                                       |
| 5     | DONE          | TikTok Live Screen Share auto-pilot — main path no-root    |
| **3** | **gated**     | Mi Unlock for HyperOS 2 — required for Phase 4c            |
| **4c**| **scoped**    | This analysis. Port hook design once root is in hand.      |
| 4b    | descoped      | Native HAL hook abandoned in favour of 4c (Xposed).        |
