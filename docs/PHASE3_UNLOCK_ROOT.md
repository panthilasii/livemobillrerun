# Phase 3 — Bootloader Unlock + Magisk Root (Redmi 13C / 14C / Poco C75)

> **WARNING — read first.**
> Unlocking the bootloader on Xiaomi/HyperOS 2 is significantly harder
> than it was a year ago. Plan for **3–10 days minimum** of waiting,
> NOT counting the data wipe at the end.
>
> - The phone **WILL be wiped** when the unlock finishes. Back up
>   first (Mi Cloud, photos, app data).
> - Banking apps and some payment apps will refuse to run after this,
>   until you set up Play Integrity Fix + Shamiko (Step 9 below).
> - The Mi Unlock Tool runs **only on Windows**. Everything else in
>   this project runs on Mac. Plan to spend ~30 min on Windows total.

> **At any point**, run `bash scripts/phase3_status.sh` from the
> project root to see exactly which step you're on.

## Device matrix

| Phone     | codename  | SoC                      | typical fastboot ROM tag       |
| --------- | --------- | ------------------------ | ------------------------------ |
| Redmi 13C | `topaz`   | MediaTek Helio G85       | `topaz_global_images_V…`       |
| Redmi 14C | **`gale`**| MediaTek Helio G81-Ultra | `gale_global_images_V…`        |
| Poco C75  | `gale`    | MediaTek Helio G81-Ultra | rebrand of 14C; same ROM tag   |

Confirm with `adb shell getprop ro.product.device`. The reference build
for this guide is **Redmi 14C global, HyperOS 2 (OS2.0.x), Android 15.**

---

## §0  Pre-flight (15 min)

- [ ] Phone charged ≥ 60 %.
- [ ] Mi Account exists, signed in on the phone, **at least 30 days
      old.** (Newer accounts will silently fail at Step 4.)
- [ ] Insert a SIM card, turn on **Mobile Data**, turn off WiFi.
      Mi Unlock binding will refuse on WiFi.
- [ ] Photos/contacts backed up.
- [ ] One free evening for the timing-window step.
- [ ] Access to a Windows PC (a Windows VM on Mac with USB pass-through
      works — Parallels/UTM/VirtualBox).

Everything else (Mac toolchain, vcam project) stays where it is —
the only thing Windows is needed for is the official **Mi Unlock Tool**.

```bash
# Run this on the Mac at any point to see your status:
source /Users/ii/livemobillrerun/.tools/env.sh
bash /Users/ii/livemobillrerun/scripts/phase3_status.sh
```

---

## §1  Enable developer mode + USB debugging

On the phone:

1. Settings → About phone → tap **HyperOS version** 7 times.
2. Settings → Additional settings → Developer options → enable
   **USB debugging** *and* **USB debugging (security settings)**.

Verify on the Mac:

```bash
adb devices -l
# Should show the device, NOT "unauthorized".
```

---

## §2  Apply for unlock permission via Mi Community App  ⏳ HARD

This is the new gate that didn't exist before HyperOS 2. It's a
**daily quota** with a **single global midnight (China time)** opening
window. Plan to be at your phone for the timing.

1. Install **Xiaomi Community** from Play Store (or Mi GetApps).
2. Open it → if you see a **China-only** UI: Settings → clear app data,
   re-launch, choose **Global** region on first run.
3. Go to: Community → **Unlocking** section.
4. **Time the click.** China is UTC+8.
   - **Bangkok (UTC+7) → click at 23:00 sharp.**
   - The daily quota opens at **00:00:00 China time** and runs out in
     seconds. Open a "live China time" page in a floating browser
     window so you can watch the clock to the millisecond. Click
     **Apply** at `23:59:59.500` Bangkok time (≈500 ms before the
     window opens, to compensate for server processing).
5. Expected outcome:
   - **Best case**: success toast → permission granted (rare on
     first attempt).
   - **Common case**: "Daily quota exceeded" toast — *but the
     permission may have been granted anyway*. Always continue to
     Step 3 to check.
   - **Worst case**: rate-limited for 24 h. Try again tomorrow.

Many users report **failing for 5–10 nights in a row** before
getting through. This is normal.

> **Tip**: don't tap furiously — Xiaomi will rate-limit you faster.
> One click, exactly on the second.

---

## §3  Bind your Mi Account to the device

Whether or not you got a success toast, immediately:

1. Phone → Settings → Developer options → **Mi Unlock status**.
2. Mobile Data ON, WiFi OFF.
3. Tap **Add account and device.**

Outcomes:
- **"Added successfully"** → 🎉 you got permission. The 72-hour timer
  has started. Skip to §4.
- **"Couldn't verify account"** / **"please go to Mi Community to
  apply for unlock permission"** → permission was NOT granted.
  Go back to §2 tomorrow night.
- **"Mi Unlock service unreachable"** → server-side glitch. Try a
  Singapore/HK VPN, retry in 5 min.

Confirm via `phase3_status.sh` Section 5 — should show your account.

---

## §4  Wait 72 hours

This is enforced server-side; nothing you can do speeds it up.
The HyperOS 2 timer was reduced from 7 days (HyperOS 1) to 72 h.
On the phone you can monitor: Settings → Developer → Mi Unlock status
→ "You can unlock the device on …(date)".

While you wait, you can do everything in §6–§8 of this doc as a dry
run, since they don't actually flash anything yet.

---

## §5  Unlock the bootloader (Windows-only, ~10 min)

1. **Download Mi Unlock Tool** (≥ v7.6.727.43, current as of mid-2025):
   - Official: <https://en.miui.com/unlock/download_en.html>
     (sometimes 404s — Xiaomi's own page is unreliable)
   - Mirror: search XDA "Mi Unlock Tool latest" → grab a fresh upload
2. **Extract** to `C:\MiUnlock\` (Chinese path traversal bug — keep
   it short).
3. **Reboot phone to fastboot:**
   - Power off phone.
   - Hold **Volume Down + Power** until you see the Fastboot bunny.
4. **Plug into Windows.** First connection installs the MTK USB driver;
   wait for that to finish.
5. **Run `MiFlashUnlock.exe`** as administrator.
6. Sign in with the same Mi Account as on the phone.
7. Click **Unlock**. Either:
   - **"Unlocked successfully"** → the phone wipes & reboots into
     setup wizard. Done.
   - **"Couldn't verify account, please retry in N hours"** → the
     timer hasn't elapsed. Wait, retry.
   - **"Couldn't connect to server"** → use a Singapore VPN on the
     PC, retry.
8. Phone reboots into setup wizard. **Skip everything**, including
   Wi-Fi (it tries to push an OTA that re-locks). Get to the home
   screen as fast as possible.

When the phone is back on, on the Mac:

```bash
adb devices -l                  # re-allow USB debug if prompted
bash /Users/ii/livemobillrerun/scripts/phase3_status.sh
# §3 should now show: ro.boot.flash.locked=0 (UNLOCKED)
# §7 verdict: "🎉 Bootloader is UNLOCKED."
```

---

## §6  Get the matching `boot.img`

There are two paths. **Use Path A unless OTA isn't available.**

### Path A — dump from the live device (zero downloads)

After unlock, the phone is still running the same HyperOS build, so
the live boot partition is what you want. From the Mac:

```bash
source /Users/ii/livemobillrerun/.tools/env.sh
bash /Users/ii/livemobillrerun/scripts/phase3_dump_boot.sh
# → drops boot.img into /Users/ii/livemobillrerun/dist/boot/
```

This script is wired up to detect your slot (`_a` vs `_b`), pull
the right partition, and validate the AVB header.

### Path B — official fastboot ROM zip

For when the dump fails (locked partition, weird vendor) or when you
want to revert to a known-good base.

1. Browse <https://xiaomifirmwareupdater.com/firmware/gale/> and pick
   the **fastboot ROM** matching `ro.build.fingerprint`. For the
   reference Redmi 14C the file looks like
   `gale_global_images_V*_OS2.0.206.0.VGPMIXM.tgz`.
2. ~2.5 GB download. Extract `images/boot.img`.
3. Copy to phone's `Download/`:
   ```bash
   adb push boot.img /sdcard/Download/
   ```

---

## §7  Patch boot.img with Magisk (on the phone, ~2 min)

1. Install Magisk APK on phone (latest stable):
   <https://github.com/topjohnwu/Magisk/releases>
   ```bash
   adb install Magisk-v27.x.apk
   ```
2. Open Magisk → **Install** → **Select and Patch a File** →
   pick `boot.img` from `Download/`.
3. Magisk writes `magisk_patched-XXXXX.img` to `Download/`.
4. Pull it back to the Mac:
   ```bash
   adb pull /sdcard/Download/magisk_patched-*.img dist/boot/
   ```

---

## §8  Flash patched boot from the Mac (~30 s)

```bash
source /Users/ii/livemobillrerun/.tools/env.sh
adb reboot bootloader
# wait for phone to enter fastboot
fastboot devices                       # confirm visible
fastboot flash boot dist/boot/magisk_patched-*.img
fastboot reboot
```

Phone boots — open Magisk app — should say **"Installed: <version>"**.

```bash
# Verify root from the Mac:
adb shell su -c id
# uid=0(root) gid=0(root) groups=0(root) context=u:r:su:s0
```

If `su` prompt appears on the phone, tap **Grant**.

---

## §9  Hide root from picky apps (optional but recommended)

Without this, TikTok / banking / Google Wallet refuse to run.

1. Magisk app → **Settings** → enable **Zygisk** → reboot.
2. Magisk → **Modules** → install:
   - **Shamiko** (LSPosed releases) — silent denylist enforcement
     <https://github.com/LSPosed/LSPosed.github.io/releases>
   - **Play Integrity Fix** (chiteroman fork) — passes BASIC + DEVICE
     <https://github.com/chiteroman/PlayIntegrityFix/releases>
3. Reboot.
4. Magisk → **Configure DenyList** → tick TikTok, Instagram, banking
   apps, Google Wallet — anything you don't want detecting root.
5. Verify with **Play Integrity API Checker** from Play Store.
   Goal: **MEETS_DEVICE_INTEGRITY**, ideally **MEETS_STRONG_INTEGRITY**.

---

## §10  Switch vcam-app's YUV path back to canonical

Until now, `YuvFileWriter` falls back to the app-private directory
because unrooted apps can't write to `/data/local/tmp/`. After root,
switch back to the canonical path the Magisk module expects:

```bash
# From the Mac — confirm /data/local/tmp is writable as the app uid:
adb shell run-as com.livemobillrerun.vcam touch /data/local/tmp/vcam.probe
adb shell run-as com.livemobillrerun.vcam rm /data/local/tmp/vcam.probe
```

If both succeed silently, restart the receiver service in the app —
`YuvFileWriter` re-probes its target on every `init()` and will
auto-detect the now-writable canonical path. The Magisk module's
`post-fs-data.sh` will then chcon it to a context the camera HAL
can read.

The PC GUI's **phone yuv** line will start showing `/data/local/tmp/
vcam.yuv` instead of `/data/data/.../files/vcam.yuv` — confirmation
that you're now feeding the path the HAL hook reads from.

---

## §11  Flash the vcam Magisk module

```bash
source /Users/ii/livemobillrerun/.tools/env.sh
cd /Users/ii/livemobillrerun/vcam-magisk

# Build (NDK + cmake — already installed under .tools/):
bash build_native.sh arm64-v8a
bash build.sh
# → dist/vcam-magisk.zip  (~280 KB, includes both ABIs)

adb push dist/vcam-magisk.zip /sdcard/Download/
```

In Magisk app → Modules → **Install from storage** → pick the zip →
**Reboot.**

After reboot, verify the module loaded:

```bash
adb shell ls -la /data/adb/modules/livemobillrerun_vcam/
adb shell logcat -d -s vcam-zygisk:I vcam-yuv:I vcam-hook:I | head -30
```

Expected first line: `vcam-zygisk: module loaded`. Subsequent lines
will show one `preAppSpecialize` per spawned target app, and (if
`libcameraservice.so` exposes the symbols we expect) a
`vcam-hook: resolved <symbol> @ 0x… in <lib>` line.

If you see that, the symbol probe works. The actual inline hook
needs Dobby — see `vcam-magisk/src/zygisk/third_party/README.md`.

---

## Troubleshooting

| Symptom                                        | Fix                                                              |
| ---------------------------------------------- | ---------------------------------------------------------------- |
| Community App: "quota exceeded" every night    | Be earlier; you have ≤ 1 s window. Try a different night.        |
| Settings → Mi Unlock status: "couldn't verify" | Re-apply via Community App; try Mobile Data only (no WiFi).      |
| Mi Unlock Tool: "couldn't connect"             | VPN to Singapore/HK on the Windows PC, retry.                    |
| Mi Unlock Tool: "wait N hours"                 | The 72 h timer hasn't elapsed yet.                               |
| `fastboot devices` empty after unlock          | Reinstall Google USB driver / Mi Flash driver on Windows.        |
| Bootloop after flashing magisk_patched.img     | `fastboot flash boot` the original `boot.img` from §6 — recover. |
| Magisk app: "Ramdisk: No"                      | Wrong slot; flash to `boot_b` if your active slot is `_b`.       |
| TikTok detects root despite Shamiko            | Update Play Integrity Fix; reboot; re-tick TikTok in DenyList.   |
| `adb shell su` says "su: not found"            | Magisk wasn't installed (boot wasn't actually patched). Re-do §7–§8. |
| `phase3_status.sh` still shows locked          | The flash didn't take — verify with `fastboot getvar unlocked`.  |

---

## What's next

You're now at **Phase 4b**. The Magisk module from §11 is loaded but
the camera hook is still a skeleton — it probes for symbols but
doesn't patch them yet. To finish:

1. Drop Dobby into `vcam-magisk/src/zygisk/third_party/dobby/`
   (single `git clone`, see `third_party/README.md`).
2. Implement the trampoline in `camera_hook.cpp` that mutates the
   captured `CameraMetadata` to splice in YUV bytes from
   `YuvReader::ReadLatest()`.
3. Rebuild + reflash module.
4. Test with TikTok / IG — they should now see the streamed video as
   the front camera.
