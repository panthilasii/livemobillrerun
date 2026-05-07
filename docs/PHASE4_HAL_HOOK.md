# Phase 4 — Android Receiver + Camera HAL Hook

## Two-strategy approach

Because MediaTek's `mtkcam` is closed-source and fragile to patch, we attack
the camera path from **two layers** and pick the one that works on Redmi 13C.

### Strategy A — Framework hook (LSPosed / Zygisk)

Hook `android.hardware.camera2.CameraManager.openCamera()` at the framework
level. When any app opens the camera, we return a `CameraDevice` impl that
serves frames from our YUV file instead of the real sensor.

- ✅ No `/vendor/` modification needed
- ✅ Works regardless of SoC
- ✅ Easy to disable (uninstall the LSPosed module)
- ❌ Only intercepts apps using Camera2; legacy Camera1 apps unaffected
- ❌ Apps that detect Xposed/LSPosed via signature can refuse

### Strategy B — HAL overlay (Magisk module replacing `.so`)

Replace `/vendor/lib64/hw/camera.mt6769.so` with a wrapper that loads the
original as `.real` and intercepts `ICameraDevice` vtable entries.

- ✅ Catches every camera client, no exceptions
- ✅ Invisible to userland apps — looks like a real camera
- ❌ SoC-specific binary work (MTK HIDL ABI varies by Android version)
- ❌ Risk of bootloop until flags are right
- ❌ Hard to debug when it doesn't work

## Recommended order

1. Build the **Android receiver app** (`vcam-app/`) first — needed by both.
2. Try **Strategy A** (LSPosed module). Fast iteration.
3. If Strategy A blocked by a target app, fall back to **Strategy B**.

---

## Android receiver app — `vcam-app/`

### Module layout

```
vcam-app/
├── settings.gradle.kts
├── build.gradle.kts
├── app/
│   ├── build.gradle.kts
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── kotlin/com/livemobillrerun/vcam/
│       │   ├── MainActivity.kt
│       │   ├── VcamService.kt           ← foreground service
│       │   ├── net/TcpClient.kt
│       │   ├── codec/H264Decoder.kt
│       │   ├── io/YuvWriter.kt
│       │   └── util/Logger.kt
│       └── res/...
└── README.md
```

### Key code paths

**TcpClient.kt**

```kotlin
class TcpClient(private val host: String, private val port: Int) {
    fun connect(onPacket: (ByteArray) -> Unit) {
        Socket(host, port).use { sock ->
            val input = DataInputStream(sock.getInputStream())
            while (!Thread.currentThread().isInterrupted) {
                val len = input.readInt()
                val buf = ByteArray(len)
                input.readFully(buf)
                onPacket(buf)
            }
        }
    }
}
```

**H264Decoder.kt** uses `MediaCodec` in async mode, output format
`COLOR_FormatYUV420Flexible`, and feeds each output buffer to `YuvWriter`.

**YuvWriter.kt** writes 16-byte header + YUV planes to
`/data/local/tmp/vcam.yuv.tmp`, then `rename()` for atomic update.

### Permissions

```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_CAMERA"/>
<uses-permission android:name="android.permission.WAKE_LOCK"/>
<uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE"
    tools:ignore="ScopedStorage"/>
```

For `/data/local/tmp/` write, use `Os.open()` with `O_WRONLY | O_CREAT`.
Works with shell-mode permissions; no extra capability needed once
the app is launched via `am start-foreground-service` from Magisk
post-fs-data, OR you grant SELinux exception.

### Run

```bash
adb install vcam-app/app/build/outputs/apk/debug/app-debug.apk
adb shell am start-foreground-service -n com.livemobillrerun.vcam/.VcamService
adb reverse tcp:8888 tcp:8888
```

---

## Strategy A — LSPosed framework hook

### Module layout (added under `vcam-app/`)

```
app/src/main/kotlin/com/livemobillrerun/vcam/xposed/
├── XposedEntry.kt          ← IXposedHookLoadPackage entry point
├── CameraManagerHook.kt
└── VirtualCameraDevice.kt
```

### Hook target

```kotlin
class CameraManagerHook : IXposedHookLoadPackage {
    override fun handleLoadPackage(lpparam: LoadPackageParam) {
        // skip our own process
        if (lpparam.packageName == BuildConfig.APPLICATION_ID) return
        if (lpparam.packageName == "com.android.systemui") return

        XposedHelpers.findAndHookMethod(
            "android.hardware.camera2.CameraManager",
            lpparam.classLoader,
            "openCamera",
            String::class.java,
            CameraDevice.StateCallback::class.java,
            Handler::class.java,
            object : XC_MethodHook() {
                override fun beforeHookedMethod(param: MethodHookParam) {
                    // construct VirtualCameraDevice that reads /data/local/tmp/vcam.yuv
                    // call callback.onOpened(virtualDevice)
                    // setResult(null) to skip real openCamera
                }
            }
        )
    }
}
```

### Build

Add in `app/build.gradle.kts`:

```kotlin
dependencies {
    compileOnly("de.robv.android.xposed:api:82")
}
```

`AndroidManifest.xml` add:

```xml
<meta-data
    android:name="xposedmodule"
    android:value="true" />
<meta-data
    android:name="xposedminversion"
    android:value="93" />
<meta-data
    android:name="xposeddescription"
    android:value="livemobillrerun virtual camera" />
```

Install via Magisk → LSPosed → Modules → enable for the apps you want to
spoof camera in (per-app scope, not system-wide).

---

## Strategy B — HAL overlay (only if A fails)

### Module skeleton (`vcam-magisk/`)

```
vcam-magisk/
├── module.prop
├── post-fs-data.sh
├── service.sh
├── system/
│   └── vendor/lib64/hw/
│       └── camera.mt6769.so       ← our replacement
└── src/
    ├── CMakeLists.txt
    ├── camera_wrapper.cpp         ← loads original .so as `.real`
    ├── ICameraDeviceWrap.cpp      ← intercepts createCaptureSession etc.
    └── yuv_reader.cpp             ← reads /data/local/tmp/vcam.yuv
```

### Build approach

Use Android NDK + a build container with the right HIDL/AIDL headers for the
target Android version. Approximate flow:

```
mkdir build && cd build
cmake -DCMAKE_TOOLCHAIN_FILE=$ANDROID_NDK/build/cmake/android.toolchain.cmake \
      -DANDROID_ABI=arm64-v8a \
      -DANDROID_PLATFORM=android-33 \
      ..
make -j
```

### Module install

```bash
cd vcam-magisk
zip -r ../vcam-magisk.zip ./*
adb push ../vcam-magisk.zip /sdcard/Download/
# In Magisk app: Modules → Install from storage
```

### Why this is risky on MTK

MediaTek's `mtkcam` provider does not implement the AOSP HAL3 interface
verbatim — there are vendor-specific extensions (`IMtkCamera`, custom
metadata tags). A naive vtable hook will crash `cameraserver`. Need to:

1. Dump the symbol table of the original `camera.mt6769.so`:
   `nm --dynamic /vendor/lib64/hw/camera.mt6769.so | head -100`
2. Identify the `ICameraProvider::getCameraDeviceInterface_V3_X` entry.
3. Wrap that one specifically; pass everything else to `.real`.

This is week 2 of Phase 4 — start here only after Strategy A is exhausted.

---

## Acceptance test

The phase is "done" when, on the phone:

```bash
adb shell am start -n com.android.camera2/.CameraActivity
```

opens the stock camera app and the **preview shows your video**, not the
real sensor. Repeat with another app (any video-conf app, recorder, etc.)
to verify it's system-wide.
