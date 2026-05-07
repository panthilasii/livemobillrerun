# LSPatch breakthrough — replacing TikTok's camera with no root

**Date:** May 5–6, 2026
**Phone under test:** Redmi 14C, HyperOS 2 (Android 15), bootloader **locked**, **no root**.
**Outcome:** ✅ vcam-app's CameraHook successfully loaded into TikTok International (`com.ss.android.ugc.trill` v44.5.3) running on a stock, fully-locked phone. Smoke-test logs:

```
LSPosed-Bridge: Loading class com.livemobillrerun.vcam.hook.CameraHook
LSPosed-Bridge: [VCAM_HOOK] ✅ loaded into com.ss.android.ugc.trill — installing hooks
LSPosed-Bridge: [VCAM_HOOK] MediaCodec.queueInputBuffer audio hook installed
LSPosed-Bridge: [VCAM_HOOK] AudioRecord hooks installed
LSPosed-Bridge: [VCAM_HOOK] 🚀 Application.onCreate fired in TikTok process
```

Bootloader status at the time of success:

```
ro.boot.flash.locked         : 1
ro.boot.verifiedbootstate    : green
ro.boot.vbmeta.device_state  : locked
sys.oem_unlock_allowed       : 1   (toggle is ON, but never used)
magisk binary                : (absent)
su binary                    : (absent)
```

## What we used to think (wrong)

Earlier analysis of the dealer's `UltimateRerun.rar` concluded:

> "UltimateRerun is an Xposed module. Xposed needs LSPosed in the Zygote.
> LSPosed needs Magisk. Magisk needs an unlocked bootloader. Therefore
> the user must Mi-Unlock the phone to use any version of this technique."

This was wrong about the *mechanism*. The correct picture is:

## What's actually happening

UltimateRerun's setup guide (decompiled from MainActivity.java line 800) reveals the trick:

```
✅ Required apps
1. UntimateLive (this app)
2. TikTok Patch for {variant}                    ← pre-built per-device
                                                   patched APK shipped
                                                   with the kit

⚠️ Notes
• Use TikTok Patch 15C only
• Do NOT update TikTok from Play Store           ← signature would change
                                                   back to Google's
```

The "TikTok Patch" is a **standalone pre-patched TikTok APK**: the dealer
ran [LSPatch](https://github.com/JingMatrix/LSPatch) once on their server,
embedded their CameraHook module, and shipped the resulting APK as a
separate file. The customer just installs it — no root, no Mi-Unlock,
no LSPosed-on-Magisk.

LSPatch works by *injecting the Xposed framework runtime into the target
APK itself*, then re-signing. Anatomy of the patched APK:

```
TikTok-patched.apk
├── original TikTok contents (preserved as assets/lspatch/origin.apk)
├── assets/lspatch/loader.dex                ← Xposed loader for this process
├── assets/lspatch/modules/<module>.apk      ← OUR CameraHook embedded here
├── assets/lspatch/so/{abi}/liblspatch.so    ← native shim for class injection
├── classes.dex modified                     ← AppComponentFactory replaced
│                                              with LSPAppComponentFactoryStub
└── re-signed with LSPatch's debug keystore
```

When the patched TikTok boots, `LSPAppComponentFactoryStub.<clinit>` runs
*before* TikTok's own Application class — it loads `loader.dex`, which
loads our embedded module, which calls `IXposedHookLoadPackage.handleLoadPackage`,
which is where our `CameraHook.kt` installs the MediaCodec / Camera2 /
AudioRecord hooks.

The genius of LSPatch: it never touches `system_server`, never modifies
`/system`, and never needs `init` to do anything. The injection runs
entirely inside one app's own UID.

## Setup matrix

| Requirement                  | Phase 4b (HAL hook) | Phase 4c (LSPosed) | **Phase 4d (LSPatch)** |
| ---------------------------- | ------------------- | ------------------ | ---------------------- |
| Mi Community App approval    | ✅ required         | ✅ required        | ❌ not needed          |
| Bootloader unlocked          | ✅ required         | ✅ required        | ❌ not needed          |
| Magisk installed             | ✅ required         | ✅ required        | ❌ not needed          |
| LSPosed installed            | ❌ unused           | ✅ required        | ❌ not needed          |
| USB debugging                | ✅                  | ✅                 | ✅                     |
| Install via USB              | ✅                  | ✅                 | ✅                     |
| OEM unlock toggle            | ✅                  | ✅                 | ❌ not needed          |
| Wipe phone during setup      | ✅                  | ✅                 | ❌                     |
| Phone permanently modifiable | ✅                  | ✅                 | ❌                     |
| Smoke test elapsed           | weeks               | days               | **~1 minute**          |

The only thing Phase 4d takes from the user is their TikTok login
session — re-installing TikTok with a different signing key forces
re-login, but that's it.

## Pipeline (one button in the GUI)

```
┌─────────────┐
│ vcam-app.apk│        Section 7 in vcam-pc GUI:
│ (debug)     │        "Patch & install TikTok"
└──────┬──────┘
       │
       │       ┌───────────────────────────────────────┐
       └──────►│  1. adb pull base + every split.apk   │
               │     from /data/app/...com.ss.android  │
               │     .ugc.trill-*/                     │
               │                                       │
               │  2. java -jar lspatch.jar             │
               │       *.apk                           │
               │       -m vcam-app-debug.apk           │
               │       -l 2     # PM + openat sig bypass
               │       -f       # overwrite output     │
               │       -o ./out/                       │
               │                                       │
               │  3. adb uninstall com.ss.android.     │
               │     ugc.trill                         │
               │                                       │
               │  4. adb install-multiple              │
               │       out/base-{ver}-lspatched.apk    │
               │       out/split_*-lspatched.apk       │
               │       (every split must come along    │
               │        or pm rejects the install with │
               │        INSTALL_FAILED_MISSING_SPLIT)  │
               └───────────────────┬───────────────────┘
                                   │
                                   ▼
                ┌─────────────────────────────────────┐
                │ Phone now has com.ss.android.ugc    │
                │ .trill installed but signed by      │
                │ LSPatch's debug keystore.           │
                │ fingerprint prefix: e0b8d3e5        │
                │                                     │
                │ Every TikTok launch:                │
                │   LSPatch-MetaLoader bootstraps     │
                │      → loader.dex                   │
                │      → liblspatch.so                │
                │      → vcam-app's CameraHook.kt     │
                │      → installs MediaCodec /        │
                │        Camera2 / AudioRecord hooks  │
                │      → reads /data/local/tmp/       │
                │        vcam_enabled flag            │
                │      → if ON, replaces camera       │
                │        frames with /sdcard/         │
                │        vcam_final.mp4               │
                └─────────────────────────────────────┘
```

Pipeline is implemented in
[`vcam-pc/src/lspatch_pipeline.py`](../vcam-pc/src/lspatch_pipeline.py)
and surfaced in the GUI as Section 7. Each step is independently callable
from the Python REPL for debugging.

## Tooling bootstrap

Both helpers are idempotent:

```bash
# One-time setup on a fresh checkout:
./vcam-pc/tools/install_jdk21.sh        # JDK 21 → .tools/jdk-21/
./vcam-pc/tools/install_lspatch.sh      # lspatch.jar → .tools/lspatch/
cd vcam-app && ./gradlew assembleDebug  # vcam-app-debug.apk
```

After that, the GUI's Section 7 *"Patch & install TikTok"* button does
the rest end-to-end and reports timing for every stage:

```
package    : com.ss.android.ugc.trill
version    : 44.5.3
signer     : e0b8d3e5
pull       :  9.6s
patch      :  4.9s (53 APKs)
install    : 27.5s
```

## Limitations & known caveats

1. **TikTok updates from Play Store will overwrite the patched APK.** The
   user should disable auto-update for TikTok (or just remember to re-run
   Section 7 after every TikTok update).
2. **Sig-bypass level 2 (`-l 2`) is required.** TikTok performs runtime
   signature self-checks via both `PackageManager.getPackageInfo` and
   `openat`/JAR parsing. Lower levels leave one or the other observable.
3. **Some TikTok background services may crash on first run.** We see
   `LSPAppComponentFactoryStub` reported as `ExceptionInInitializerError`
   from the `:push` and `:jobInfoSchedulerService` processes. The main
   activity is unaffected and Live works. This is benign and matches
   reports from other LSPatch users on TikTok 44.x.
4. **The TikTok session is logged out** because the signing key changes.
   The user must log back in once after install.
5. **MD5/SHA fingerprint of vcam-app must match what's embedded.** If you
   rebuild vcam-app, you must re-run Section 7 — the embedded copy is
   the one LSPatch fused in last time.
6. **TikTok variant matters.** This was tested on `com.ss.android.ugc.trill`
   (TikTok International, the build distributed in TH/SEA). The same
   pipeline should work on `com.zhiliaoapp.musically` (US/EU) without
   change — the LSPatch CLI is generic — but only `trill` v44.5.3 has
   been smoke-tested end-to-end.

## What this means for the project plan

| Phase                                        | Status            |
| -------------------------------------------- | ----------------- |
| Phase 1 (device probe)                       | done              |
| Phase 2 (PC streamer + Android receiver)     | done              |
| Phase 3 (Mi Unlock + Magisk + LSPosed)       | **deprecated**    |
| Phase 4a (loopback verification)             | done              |
| Phase 4b (native HAL hook via Zygisk)        | **deprecated**    |
| Phase 4c (Xposed module via system LSPosed)  | superseded by 4d  |
| **Phase 4d (LSPatch + embedded module)**     | **shipping**      |
| Phase 5 (TikTok screen-share fallback)       | still useful as belt-and-suspenders |
| Phase 4e (FlipRenderer for live rotate/zoom) | pending (in CameraHook) |

Phase 3's prerequisites — applying for Mi Community approval, the 7-day
Xiaomi unlock waiting period, the wipe — **never need to happen** for
the user. Their phone stays exactly as it shipped.
