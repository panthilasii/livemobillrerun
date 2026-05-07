#!/usr/bin/env bash
# Phase 3 — flash a Magisk-patched boot.img and verify root.
#
# Usage:  bash scripts/phase3_flash_magisk.sh [path/to/magisk_patched.img]
#
# If no path is given, picks the newest dist/boot/magisk_patched-*.img.

set -eu

if [ -t 1 ]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'
                  B=$'\033[34m'; C=$'\033[36m'; BOLD=$'\033[1m'; N=$'\033[0m'
else R=; G=; Y=; B=; C=; BOLD=; N=; fi
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}△${N} $*"; }
err()  { echo "  ${R}✗${N} $*"; }
step() { echo; echo "${B}── $1 ──${N}"; }

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 1. find the patched image ───────────────────────────────────────
step "1. locate magisk_patched.img"
if [ $# -ge 1 ]; then
  IMG="$1"
else
  IMG=$(ls -t "$ROOT"/dist/boot/magisk_patched-*.img 2>/dev/null | head -1 || true)
fi
if [ -z "${IMG:-}" ] || [ ! -f "$IMG" ]; then
  err "no magisk_patched-*.img found in $ROOT/dist/boot/"
  err "did you run §7 (patch in Magisk) and pull the result?"
  echo "  ${C}adb pull /sdcard/Download/magisk_patched-*.img $ROOT/dist/boot/${N}"
  exit 1
fi
SIZE=$(stat -f%z "$IMG" 2>/dev/null || stat -c%s "$IMG")
ok "$IMG  ($SIZE bytes)"

# ── 2. preconditions ────────────────────────────────────────────────
step "2. preflight"
ADB_DEVS=$(adb devices 2>/dev/null | awk 'NR>1 && /device$/ {n++} END {print n+0}')
FB_DEVS=$(fastboot devices 2>/dev/null | wc -l | tr -d ' ')
if [ "$ADB_DEVS" -lt 1 ] && [ "$FB_DEVS" -lt 1 ]; then
  err "no device on adb or fastboot"; exit 1
fi
if [ "$ADB_DEVS" -ge 1 ]; then
  LOCKED=$(adb shell getprop ro.boot.flash.locked 2>/dev/null | tr -d '\r')
  if [ "$LOCKED" != "0" ]; then
    err "ro.boot.flash.locked=$LOCKED — bootloader still locked"
    err "you cannot flash a custom boot.img on a locked bootloader"
    exit 1
  fi
  ok "device on adb, bootloader unlocked"
fi

# ── 3. reboot to fastboot ───────────────────────────────────────────
step "3. reboot to fastboot"
if [ "$FB_DEVS" -lt 1 ]; then
  ok "asking adb to reboot bootloader…"
  adb reboot bootloader
  echo -n "  waiting for fastboot"
  for i in $(seq 1 30); do
    sleep 1
    n=$(fastboot devices 2>/dev/null | wc -l | tr -d ' ')
    if [ "$n" -ge 1 ]; then echo " — got it ($i s)"; break; fi
    echo -n "."
  done
  if [ "$(fastboot devices 2>/dev/null | wc -l | tr -d ' ')" -lt 1 ]; then
    echo
    err "device didn't show up in fastboot"
    err "manually power-cycle into fastboot (Vol Down + Power) and re-run"
    exit 1
  fi
fi
fastboot devices

# ── 4. confirm bootloader is actually unlocked ──────────────────────
step "4. verify fastboot says 'unlocked'"
UNL=$(fastboot getvar unlocked 2>&1 | grep -i '^unlocked:' | awk '{print $2}' | tr -d '\r')
case "$UNL" in
  yes|true) ok "fastboot reports unlocked=$UNL" ;;
  *)        err "fastboot reports unlocked=${UNL:-?}"
            err "abort — flashing a custom boot here would brick"
            exit 1 ;;
esac

# ── 5. detect slot, then flash ──────────────────────────────────────
step "5. flash boot"
SLOT=$(fastboot getvar current-slot 2>&1 | grep -i 'current-slot' | awk '{print $2}' | tr -d '\r' || true)
if [ -n "$SLOT" ] && [ "$SLOT" != "(none)" ]; then
  ok "active slot: $SLOT — flashing boot_$SLOT"
  fastboot flash boot_$SLOT "$IMG"
else
  ok "A-only device — flashing boot"
  fastboot flash boot "$IMG"
fi

# ── 6. reboot ───────────────────────────────────────────────────────
step "6. reboot"
fastboot reboot

# ── 7. wait for adb + verify root ──────────────────────────────────
step "7. wait for adb"
echo -n "  "
for i in $(seq 1 90); do
  sleep 2
  if adb devices 2>/dev/null | grep -q $'\tdevice$'; then
    echo " — back on adb after $((i*2)) s"
    break
  fi
  echo -n "."
done

step "8. ${BOLD}verify root${N}"
sleep 2
if adb shell 'su -c id' 2>/dev/null | grep -q 'uid=0'; then
  echo
  echo "  ${G}${BOLD}🎉  ROOT CONFIRMED.${N}"
  adb shell 'su -c id'
  echo
  echo "  Next: $(basename "$ROOT")/scripts/phase3_post_root.sh"
else
  warn "could not get uid=0 yet"
  warn "the first 'su' invocation pops a prompt on the phone — tap GRANT,"
  warn "then re-run this section: adb shell su -c id"
  echo
  echo "  if you don't see the Magisk app on the home screen, install it again"
  echo "  to finalise the install:"
  echo "    ${C}adb install Magisk-vXX.X.apk${N}"
fi
