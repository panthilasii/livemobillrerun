#!/usr/bin/env bash
# Phase 3 — pull the live boot.img off an unlocked phone so we can
# patch it with Magisk. Works on both A-only and A/B devices and
# auto-detects the active slot.
#
# Usage:  bash scripts/phase3_dump_boot.sh
#
# Output: dist/boot/boot.img  (ready to push to phone for Magisk patch)

set -eu

if [ -t 1 ]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'
                  B=$'\033[34m'; C=$'\033[36m'; N=$'\033[0m'
else R=; G=; Y=; B=; C=; N=; fi
ok()   { echo "  ${G}✓${N} $*"; }
warn() { echo "  ${Y}△${N} $*"; }
err()  { echo "  ${R}✗${N} $*"; }
step() { echo; echo "${B}── $1 ──${N}"; }

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist/boot"
mkdir -p "$OUT"

# ── 1. precondition: device visible + bootloader unlocked ────────────
step "1. preflight"
if ! command -v adb >/dev/null 2>&1; then
  err "adb missing — source .tools/env.sh first"; exit 1
fi
if [ "$(adb devices | awk 'NR>1 && /device$/ {n++} END {print n+0}')" -lt 1 ]; then
  err "no device on adb"; exit 1
fi
LOCKED=$(adb shell getprop ro.boot.flash.locked 2>/dev/null | tr -d '\r')
if [ "$LOCKED" != "0" ]; then
  err "ro.boot.flash.locked=$LOCKED — bootloader still locked!"
  err "we need an unlocked device to safely dump (cleartext) boot.img"
  err "complete §1–§5 of docs/PHASE3_UNLOCK_ROOT.md first"
  exit 1
fi
ok "device on adb, bootloader unlocked"

# ── 2. find active slot ─────────────────────────────────────────────
step "2. detect active slot"
SLOT=$(adb shell getprop ro.boot.slot_suffix 2>/dev/null | tr -d '\r')
if [ -z "$SLOT" ]; then
  ok "A-only device (no slot suffix) — will dump /dev/block/by-name/boot"
  PARTITION="boot"
else
  ok "A/B device, active slot: ${SLOT}"
  PARTITION="boot${SLOT}"
fi

# ── 3. find the partition's actual block device ─────────────────────
step "3. resolve block device"
# `by-name` is the canonical alias on every Xiaomi/MTK device we
# care about. Fall back to a full scan if it's hidden.
BY_NAME="/dev/block/by-name/${PARTITION}"
if adb shell "[ -e $BY_NAME ]"; then
  ok "$BY_NAME exists"
  BLK="$BY_NAME"
else
  warn "$BY_NAME not found, scanning…"
  BLK=$(adb shell "find /dev/block/platform -name '${PARTITION}' 2>/dev/null" | head -1 | tr -d '\r')
  if [ -z "$BLK" ]; then
    err "couldn't locate the boot partition"
    err "drop into adb shell and find it manually:"
    err "  adb shell ls -la /dev/block/by-name/ | grep boot"
    exit 1
  fi
  ok "found at $BLK"
fi

# ── 4. dump (need root since boot is restricted) ────────────────────
step "4. dump $BLK → boot.img"
# Two paths: try `su` first (after Magisk root); if that fails fall
# back to the slower exec-out + dd path that works on some devices
# even pre-root (boot is sometimes readable by adb shell uid=2000).
TMP_PHONE="/data/local/tmp/boot_dump.img"

if adb shell "su -c 'dd if=$BLK of=$TMP_PHONE bs=4M' 2>&1" \
   | tee /tmp/_boot_dump.log | grep -qE 'records (in|out)'; then
  ok "dumped via su"
  adb shell "su -c 'chmod 0644 $TMP_PHONE && chown shell:shell $TMP_PHONE'" >/dev/null
elif adb shell "dd if=$BLK of=$TMP_PHONE bs=4M 2>&1" \
   | tee /tmp/_boot_dump.log | grep -qE 'records (in|out)'; then
  ok "dumped via shell uid"
else
  err "dd failed — tail:"
  tail /tmp/_boot_dump.log | sed 's/^/    /'
  err "this device probably needs root first; install Magisk per §7-§8"
  exit 1
fi

# ── 5. pull to host ─────────────────────────────────────────────────
step "5. pull to host"
adb pull "$TMP_PHONE" "$OUT/boot.img" 2>&1 | tail -1
adb shell "rm -f $TMP_PHONE" 2>/dev/null || true
SIZE=$(stat -f%z "$OUT/boot.img" 2>/dev/null || stat -c%s "$OUT/boot.img")
ok "$OUT/boot.img  (${SIZE} bytes)"

# ── 6. validate AVB header (sanity) ─────────────────────────────────
step "6. validate"
HDR=$(head -c 8 "$OUT/boot.img" | xxd -p)
case "$HDR" in
  414e44524f494421*)  ok "valid Android boot magic ('ANDROID!')" ;;
  *)                  warn "unexpected header bytes: $HDR — file may be corrupted" ;;
esac

if [ "$SIZE" -lt 4194304 ]; then
  warn "boot.img is suspiciously small (<4MB) — re-check the partition"
fi

# ── 7. next steps ───────────────────────────────────────────────────
step "7. ${B}next steps${N}"
cat <<EOF

  ${C}adb push $OUT/boot.img /sdcard/Download/${N}

then on the phone:
  Open Magisk → Install → Select and Patch a File → boot.img
  Magisk writes magisk_patched-XXXXX.img into Download/

then back on the Mac:
  ${C}adb pull /sdcard/Download/magisk_patched-*.img dist/boot/${N}
  ${C}bash $ROOT/scripts/phase3_flash_magisk.sh${N}

EOF
