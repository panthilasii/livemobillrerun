#!/usr/bin/env bash
# Phase 3 — bootloader unlock status checker for Xiaomi/Redmi.
#
# Tells you exactly which step of the (very annoying) HyperOS 2 unlock
# procedure you're at, and what to do next.
#
# Usage:  bash scripts/phase3_status.sh
#
# Run this both BEFORE and AFTER each Mi Unlock attempt — it's idempotent.

set -u

# ── colours ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
  R=$'\033[31m'  G=$'\033[32m'  Y=$'\033[33m'
  B=$'\033[34m'  M=$'\033[35m'  C=$'\033[36m'
  BOLD=$'\033[1m'  N=$'\033[0m'
else
  R=  G=  Y=  B=  M=  C=  BOLD=  N=
fi

step() { echo "${B}── $1 ──${N}"; }
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}△${N} $*"; }
err()  { echo "  ${R}✗${N} $*"; }
hint() { echo "    ${C}↳${N} $*"; }

# ── 0. tooling ────────────────────────────────────────────────────────
step "0. host tooling"
if ! command -v adb >/dev/null 2>&1; then
  err "adb not on PATH"
  hint "source /Users/ii/livemobillrerun/.tools/env.sh"
  exit 1
fi
ok "adb $(adb --version | head -1 | awk '{print $5}')"
if command -v fastboot >/dev/null 2>&1; then
  ok "fastboot $(fastboot --version 2>&1 | head -1 | awk '{print $3}')"
else
  warn "fastboot missing"
  hint "Should ship with platform-tools — re-source env.sh"
fi

# ── 1. mode (adb / fastboot / off) ────────────────────────────────────
step "1. device mode"
ADB_DEVS=$(adb devices 2>/dev/null | awk 'NR>1 && /device$/ {n++} END {print n+0}')
FB_DEVS=$(fastboot devices 2>/dev/null | wc -l | tr -d ' ')

if [ "$ADB_DEVS" -ge 1 ]; then
  MODE=adb
  ok "in ADB mode (Android booted)"
elif [ "$FB_DEVS" -ge 1 ]; then
  MODE=fastboot
  ok "in FASTBOOT mode"
else
  MODE=none
  err "no device detected"
  hint "plug in via data USB-C cable; tick 'Allow USB debugging?' on phone"
  exit 1
fi

# Branch: fastboot mode is short — we just print state and exit.
if [ "$MODE" = fastboot ]; then
  step "2. fastboot state"
  fastboot getvar all 2>&1 | grep -iE 'unlocked|secure|product|variant|version-bootloader|slot-count|current-slot' | head -20
  echo
  echo "${BOLD}NEXT:${N} once unlocked, run:"
  echo "  ${C}fastboot flash boot magisk_patched.img${N}     # patched boot.img"
  echo "  ${C}fastboot reboot${N}"
  exit 0
fi

# ── 2. device identity ────────────────────────────────────────────────
step "2. device identity"
DEVICE=$(adb shell getprop ro.product.device | tr -d '\r')
MODEL=$(adb shell getprop ro.product.model | tr -d '\r')
NAME=$(adb shell getprop ro.product.name | tr -d '\r')
ANDROID=$(adb shell getprop ro.build.version.release | tr -d '\r')
SDK=$(adb shell getprop ro.build.version.sdk | tr -d '\r')
HYPER=$(adb shell getprop ro.mi.os.version.name | tr -d '\r')
HYPER_INC=$(adb shell getprop ro.mi.os.version.incremental | tr -d '\r')
FP=$(adb shell getprop ro.build.fingerprint | tr -d '\r')
REGION=$(adb shell getprop ro.boot.hwc | tr -d '\r')
ok "${BOLD}$MODEL${N} (codename: $DEVICE, ROM: $NAME, region: ${REGION:-?})"
ok "Android $ANDROID (SDK $SDK)"
[ -n "$HYPER" ] && ok "HyperOS: $HYPER  ($HYPER_INC)"
hint "fingerprint: $FP"

# ── 3. bootloader lock state ──────────────────────────────────────────
step "3. bootloader lock state"
LOCKED=$(adb shell getprop ro.boot.flash.locked | tr -d '\r')
VBSTATE=$(adb shell getprop ro.boot.verifiedbootstate | tr -d '\r')
VBMETA=$(adb shell getprop ro.boot.vbmeta.device_state | tr -d '\r')

case "$LOCKED" in
  0) ok    "ro.boot.flash.locked=0  (UNLOCKED — you've done it!)" ;;
  1) err   "ro.boot.flash.locked=1  (still LOCKED)" ;;
  *) warn  "ro.boot.flash.locked=${LOCKED:-?}  (unknown)" ;;
esac
case "$VBSTATE" in
  green)   warn  "verifiedbootstate=green  (stock signed boot — locked path)" ;;
  yellow)  ok    "verifiedbootstate=yellow (custom boot — unlocked w/ user key)" ;;
  orange)  ok    "verifiedbootstate=orange (UNLOCKED, custom boot)" ;;
  red)     err   "verifiedbootstate=red    (boot verification FAILED — recover via fastboot)" ;;
  *)       warn  "verifiedbootstate=${VBSTATE:-?}" ;;
esac
ok "vbmeta.device_state=$VBMETA"

# ── 4. Mi Unlock prerequisites ────────────────────────────────────────
step "4. Mi Unlock prerequisites"
OEM=$(adb shell settings get global oem_unlock_supported 2>/dev/null | tr -d '\r')
OEM_ENABLED=$(adb shell settings get global oem_unlock_allowed 2>/dev/null | tr -d '\r')
USB_DEBUG=$(adb shell settings get global adb_enabled 2>/dev/null | tr -d '\r')
DEV=$(adb shell settings get global development_settings_enabled 2>/dev/null | tr -d '\r')

if [ "$DEV" = "1" ]; then
  ok "Developer options enabled"
else
  err "Developer options NOT enabled"
  hint "Settings → About phone → tap MIUI version 7 times"
fi
if [ "$USB_DEBUG" = "1" ]; then
  ok "USB debugging enabled"
else
  err "USB debugging NOT enabled"
fi
case "$OEM_ENABLED" in
  1) ok   "OEM unlocking allowed"  ;;
  0) err  "OEM unlocking NOT allowed yet"
     hint "Settings → Developer options → toggle ${BOLD}OEM unlocking${N} ON"
     ;;
  *) warn "OEM unlock state unknown ($OEM_ENABLED)" ;;
esac

# ── 5. Mi Unlock account binding ──────────────────────────────────────
step "5. Mi Unlock account binding"
# Two heuristics:
# (a) Any registered Xiaomi account in AccountManager
# (b) Mi Unlock service shows the account-bound flag (varies by HyperOS)
ACCOUNTS=$(adb shell dumpsys account 2>/dev/null | grep -i -E 'xiaomi|com\.xiaomi' | head -3 | tr -d '\r')
if [ -n "$ACCOUNTS" ]; then
  ok "Xiaomi account present:"
  echo "$ACCOUNTS" | sed 's/^/      /'
else
  warn "no Xiaomi account detected via dumpsys account"
  hint "Settings → Mi Account → sign in"
fi

# Look for the Mi Unlock service / com.xiaomi.unlock package
if adb shell pm list packages com.xiaomi.unlock 2>/dev/null | grep -q '^package'; then
  ok "Mi Unlock service installed (com.xiaomi.unlock)"
elif adb shell pm list packages com.xiaomi.bootloader 2>/dev/null | grep -q '^package'; then
  ok "Mi Unlock service installed (com.xiaomi.bootloader)"
else
  warn "Mi Unlock service package not found"
fi

# Detect whether the "Add account and device" timer has been started.
# This is stored differently across MIUI/HyperOS versions; try a few
# common keys. Even when we can't read it, the user can check by hand
# (Settings → Developer → Mi Unlock status).
TIMER_HINT=""
for prop in persist.sys.miui.bind_status persist.sys.unlock.bind sys.unlock.timer; do
  v=$(adb shell getprop $prop 2>/dev/null | tr -d '\r')
  [ -n "$v" ] && TIMER_HINT="$TIMER_HINT $prop=$v"
done
[ -n "$TIMER_HINT" ] && ok "binding hints:$TIMER_HINT"

# ── 6. /data/local/tmp writability (for Magisk side-channel) ─────────
step "6. /data/local/tmp writability"
if adb shell touch /data/local/tmp/.vcam_probe >/dev/null 2>&1; then
  ok "/data/local/tmp/ writable from adb shell"
  adb shell rm /data/local/tmp/.vcam_probe 2>/dev/null
else
  err "/data/local/tmp/ not writable (unexpected)"
fi

# ── 7. final verdict ─────────────────────────────────────────────────
step "7. ${BOLD}where you are${N}"
if [ "$LOCKED" = "0" ]; then
  echo
  echo "  ${G}${BOLD}🎉  Bootloader is UNLOCKED.${N}"
  echo "  Next: patch boot.img with Magisk, then ${C}fastboot flash boot${N}."
  echo "  See ${C}docs/PHASE3_UNLOCK_ROOT.md  §6 onward${N}."
elif [ "$OEM_ENABLED" = "1" ]; then
  echo
  echo "  ${Y}${BOLD}⏳  OEM unlock allowed but bootloader still locked.${N}"
  echo "  → run Mi Unlock Tool on Windows (after the 72h timer expires)."
  echo "  See ${C}docs/PHASE3_UNLOCK_ROOT.md  §3–§5${N}."
else
  echo
  echo "  ${R}${BOLD}🚧  Pre-requisites not met yet.${N}"
  echo "  → enable Developer options + USB debugging + OEM unlocking,"
  echo "    then bind your Mi account in Settings → Mi Unlock Status,"
  echo "    then wait 72h before running Mi Unlock Tool."
  echo "  See ${C}docs/PHASE3_UNLOCK_ROOT.md  §0–§2${N}."
fi
echo
