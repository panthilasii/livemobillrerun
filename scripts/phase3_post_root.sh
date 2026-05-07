#!/usr/bin/env bash
# Phase 3 — post-root sanity check + flash the vcam Magisk module.
#
# Run AFTER phase3_flash_magisk.sh has succeeded. Verifies root,
# switches the YUV write path back to /data/local/tmp, builds + flashes
# the vcam Magisk module, and tails logcat for the symbol-probe lines.
#
# Usage:  bash scripts/phase3_post_root.sh

set -eu

if [ -t 1 ]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'
                  B=$'\033[34m'; C=$'\033[36m'; BOLD=$'\033[1m'; N=$'\033[0m'
else R=; G=; Y=; B=; C=; BOLD=; N=; fi
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}△${N} $*"; }
err()  { echo "  ${R}✗${N} $*"; }
step() { echo; echo "${B}── $1 ──${N}"; }

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_PKG="com.livemobillrerun.vcam"

# ── 1. preflight ────────────────────────────────────────────────────
step "1. preflight"
if ! adb devices 2>/dev/null | grep -q $'\tdevice$'; then
  err "no device on adb"; exit 1
fi
ok "adb online"

# ── 2. confirm root ────────────────────────────────────────────────
step "2. confirm Magisk root"
if adb shell 'su -c id' 2>/dev/null | grep -q 'uid=0'; then
  ok "su works, uid=0"
else
  err "su failed — Magisk install may not have taken"
  err "check Magisk app: must say 'Installed: <version>'"
  err "if it says 'Ramdisk: No', flash to the OTHER slot (boot_a vs boot_b)"
  exit 1
fi
MAGISK_VER=$(adb shell 'su -c "magisk -v"' 2>/dev/null | tr -d '\r' | head -1)
ok "Magisk: $MAGISK_VER"

# ── 3. confirm /data/local/tmp writable from app uid ───────────────
step "3. /data/local/tmp accessible from app uid"
if adb shell pm list packages "$APP_PKG" | grep -q "$APP_PKG"; then
  if adb shell run-as "$APP_PKG" sh -c \
        "touch /data/local/tmp/vcam.probe && rm /data/local/tmp/vcam.probe" \
        >/dev/null 2>&1; then
    ok "$APP_PKG can write /data/local/tmp/ — switch will happen on next service start"
  else
    warn "$APP_PKG cannot write /data/local/tmp/ as its uid"
    warn "this is normal pre-Magisk; after our module runs post-fs-data.sh"
    warn "the path will be relabeled and writable. Continue."
  fi
else
  warn "$APP_PKG not installed — install with:"
  echo "    ${C}adb install -r -g $ROOT/vcam-app/app/build/outputs/apk/debug/app-debug.apk${N}"
fi

# Force-stop the app so the next launch re-probes the YUV target.
adb shell am force-stop "$APP_PKG" 2>/dev/null || true

# ── 4. build + push the Magisk module ──────────────────────────────
step "4. build & push the vcam Magisk module"
ZIP="$ROOT/vcam-magisk/dist/vcam-magisk.zip"
if [ ! -f "$ZIP" ] || [ "$ZIP" -ot "$ROOT/vcam-magisk/src/zygisk/main.cpp" ]; then
  ok "rebuilding native (NDK)…"
  bash "$ROOT/vcam-magisk/build_native.sh" arm64-v8a >/dev/null
  bash "$ROOT/vcam-magisk/build_native.sh" armeabi-v7a >/dev/null
  ok "rebuilding zip…"
  bash "$ROOT/vcam-magisk/build.sh" >/dev/null
fi
ok "$ZIP ($(stat -f%z "$ZIP" 2>/dev/null || stat -c%s "$ZIP") bytes)"

adb push "$ZIP" /sdcard/Download/vcam-magisk.zip 2>&1 | tail -1
echo
echo "  ${BOLD}NOW ON THE PHONE:${N}"
echo "  Open Magisk → Modules → Install from storage → pick"
echo "  /sdcard/Download/vcam-magisk.zip → Reboot."
echo
read -r -p "  press Enter once you've rebooted…"

# ── 5. wait for reboot ──────────────────────────────────────────────
step "5. wait for adb after reboot"
echo -n "  "
for i in $(seq 1 60); do
  sleep 2
  if adb devices 2>/dev/null | grep -q $'\tdevice$'; then
    echo " — back on adb"; break
  fi
  echo -n "."
done

# ── 6. verify the module loaded ────────────────────────────────────
step "6. verify module loaded"
MOD_DIR="/data/adb/modules/livemobillrerun_vcam"
if adb shell "su -c 'ls -la $MOD_DIR'" 2>/dev/null | grep -q module.prop; then
  ok "module installed at $MOD_DIR"
else
  err "module not installed — re-flash via Magisk app"
  exit 1
fi

# ── 7. logcat snapshot ─────────────────────────────────────────────
step "7. early logcat (Zygisk + symbol probe)"
adb logcat -d -s vcam-zygisk:I vcam-yuv:I vcam-hook:I 2>&1 | tr -d '\r' | head -40
echo
echo "  ${BOLD}interpretation${N}"
echo "  - 'vcam-zygisk: module loaded'              → Zygisk part works"
echo "  - 'vcam-hook: resolved <symbol> @ 0x… in …' → symbol probe found a hook"
echo "                                                 target on this device"
echo "  - 'vcam-hook: Dobby not linked'             → drop Dobby into"
echo "    vcam-magisk/src/zygisk/third_party/dobby/, rebuild, reflash"

# ── 8. summary ─────────────────────────────────────────────────────
step "8. ${BOLD}where you are${N}"
echo
echo "  ${G}${BOLD}✓ Phase 3 complete:${N}"
echo "    bootloader unlocked, Magisk root works, vcam module loaded."
echo
echo "  ${BOLD}Phase 4b checklist:${N}"
echo "  [ ] git clone Dobby into vcam-magisk/src/zygisk/third_party/dobby/"
echo "  [ ] implement the inline hook in camera_hook.cpp (DobbyHook)"
echo "  [ ] rebuild + reflash module"
echo "  [ ] verify a target app (TikTok / IG) sees the streamed video"
