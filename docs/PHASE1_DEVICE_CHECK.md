# Phase 1 — Device inspection (Redmi 13C)

## What we need to know

| Property                       | Why                                       |
| ------------------------------ | ----------------------------------------- |
| `ro.soc.model`                 | which Camera HAL `.so` to overlay         |
| `ro.board.platform`            | confirm SoC family (mt6769, etc.)         |
| `ro.product.device`            | choose correct Xiaomi firmware            |
| `ro.product.model`             | CN vs Global variant                      |
| `ro.build.version.release`     | Android version                           |
| `ro.build.version.sdk`         | API level                                 |
| `ro.miui.ui.version.name`      | MIUI version (legacy)                     |
| `ro.mi.os.version.name`        | HyperOS version                           |
| `ro.boot.flash.locked`         | bootloader locked? (0 = unlocked)         |
| `ro.boot.verifiedbootstate`    | green / yellow / orange                   |
| `ro.product.cpu.abi`           | expect `arm64-v8a`                        |
| `ro.vendor.product.cpu.abilist`| supported ABIs                            |

## Setup

1. On the phone:
   - Settings → About phone → tap **MIUI version** / **HyperOS version**
     7 times to enable Developer Options
   - Settings → Additional Settings → Developer Options →
     enable **USB debugging**
2. Plug into PC, accept the RSA fingerprint prompt on the phone.
3. Make sure ADB is installed:
   - macOS:   `brew install --cask android-platform-tools`
   - Windows: download from <https://developer.android.com/studio/releases/platform-tools>

## Inspection commands

Either run the helper script:

```bash
bash vcam-pc/scripts/check_device.sh
```

Or manually:

```bash
adb devices

# SoC / hardware
adb shell getprop ro.soc.model
adb shell getprop ro.soc.manufacturer
adb shell getprop ro.board.platform
adb shell getprop ro.hardware
adb shell getprop ro.product.device
adb shell getprop ro.product.model
adb shell getprop ro.product.cpu.abi
adb shell getprop ro.vendor.product.cpu.abilist

# OS
adb shell getprop ro.build.version.release
adb shell getprop ro.build.version.sdk
adb shell getprop ro.build.version.security_patch
adb shell getprop ro.miui.ui.version.name
adb shell getprop ro.mi.os.version.name
adb shell getprop ro.build.version.incremental

# Bootloader / verified boot
adb shell getprop ro.boot.flash.locked
adb shell getprop ro.boot.verifiedbootstate
adb shell getprop ro.boot.veritymode
adb shell getprop ro.boot.warranty_bit
adb shell getprop ro.warranty_bit

# Camera HAL files (read-only on locked devices, but we can list)
adb shell ls -la /vendor/lib64/hw/ | grep -i camera
adb shell ls -la /vendor/lib/hw/      | grep -i camera
adb shell ls -la /odm/lib64/hw/       | grep -i camera 2>/dev/null
```

## Saving the output

```bash
bash vcam-pc/scripts/check_device.sh > docs/device_state.txt 2>&1
```

Then commit / share that file when planning Phase 3.

## Expected results for Redmi 13C (Global)

| Property                | Likely value                |
| ----------------------- | --------------------------- |
| `ro.soc.model`          | `MT6769V/CU` or `MT6769T`   |
| `ro.board.platform`     | `mt6768` / `mt6769`         |
| `ro.product.device`     | `gale` / `earth`            |
| `ro.product.model`      | `23100RN82L` (Global)       |
| `ro.product.cpu.abi`    | `arm64-v8a`                 |
| Android                 | 13 (T) or 14 (U)            |
| HyperOS                 | 1.x or 2.x                  |
| `ro.boot.flash.locked`  | `1` (locked, expected)      |
| Camera HAL              | `camera.mt6769.so`          |

## Deciding the next step

Based on the result we either:

- **`ro.boot.flash.locked = 1` and HyperOS 2.x:**
  Long unlock wait expected. Consider Phase 2 first to prove the streamer
  works, then start the unlock paperwork in parallel.
- **`ro.boot.flash.locked = 0`:**
  Already unlocked. Skip directly to Magisk install in Phase 3.
- **`ro.soc.model` is *not* `MT6769*`:**
  Update `vcam-magisk/module.prop` and target the correct HAL filename.
