#!/usr/bin/env bash
# Phase 1 helper — dump the Redmi 13C state we care about.
# Run with phone connected via USB and USB-debugging enabled.
#
# Usage:
#   bash scripts/check_device.sh
#   bash scripts/check_device.sh > docs/device_state.txt 2>&1

set -u

SECTION() {
  echo
  echo "=== $1 ==="
}

if ! command -v adb >/dev/null 2>&1; then
  echo "ERROR: adb not found in PATH" >&2
  echo "  macOS:   brew install --cask android-platform-tools" >&2
  echo "  Windows: https://developer.android.com/studio/releases/platform-tools" >&2
  exit 1
fi

SECTION "adb version"
adb version

SECTION "connected devices"
adb devices -l

DEVICE_COUNT=$(adb devices | awk 'NR>1 && /device$/ {n++} END {print n+0}')
if [ "${DEVICE_COUNT}" -eq 0 ]; then
  echo
  echo "no authorized device — accept the RSA prompt on the phone"
  exit 1
fi

SECTION "SoC / hardware"
for k in \
  ro.soc.model \
  ro.soc.manufacturer \
  ro.board.platform \
  ro.hardware \
  ro.product.device \
  ro.product.model \
  ro.product.brand \
  ro.product.cpu.abi \
  ro.vendor.product.cpu.abilist
do
  printf "%-40s %s\n" "$k" "$(adb shell getprop $k 2>/dev/null)"
done

SECTION "OS / build"
for k in \
  ro.build.version.release \
  ro.build.version.sdk \
  ro.build.version.security_patch \
  ro.miui.ui.version.name \
  ro.mi.os.version.name \
  ro.build.version.incremental \
  ro.build.fingerprint
do
  printf "%-40s %s\n" "$k" "$(adb shell getprop $k 2>/dev/null)"
done

SECTION "Bootloader / verified boot"
for k in \
  ro.boot.flash.locked \
  ro.boot.verifiedbootstate \
  ro.boot.veritymode \
  ro.boot.warranty_bit \
  ro.warranty_bit \
  ro.secure \
  ro.debuggable
do
  printf "%-40s %s\n" "$k" "$(adb shell getprop $k 2>/dev/null)"
done

SECTION "Camera HAL libs"
echo "/vendor/lib64/hw/:"
adb shell ls -la /vendor/lib64/hw/ 2>/dev/null | grep -iE 'camera|provider' || echo "  (none / locked)"
echo
echo "/vendor/lib/hw/:"
adb shell ls -la /vendor/lib/hw/ 2>/dev/null | grep -iE 'camera|provider' || echo "  (none / locked)"
echo
echo "/odm/lib64/hw/:"
adb shell ls -la /odm/lib64/hw/ 2>/dev/null | grep -iE 'camera|provider' || echo "  (none)"

SECTION "/data/local/tmp writability"
if adb shell touch /data/local/tmp/.vcam_probe 2>/dev/null && \
   adb shell rm /data/local/tmp/.vcam_probe 2>/dev/null; then
  echo "OK — adb can write to /data/local/tmp/"
else
  echo "FAILED — /data/local/tmp/ not writable from adb (unexpected)"
fi

SECTION "summary"
LOCKED=$(adb shell getprop ro.boot.flash.locked 2>/dev/null)
SOC=$(adb shell getprop ro.soc.model 2>/dev/null)
ANDROID=$(adb shell getprop ro.build.version.release 2>/dev/null)
HYPER=$(adb shell getprop ro.mi.os.version.name 2>/dev/null)
MIUI=$(adb shell getprop ro.miui.ui.version.name 2>/dev/null)

echo "  SoC:                ${SOC:-?}"
echo "  Android:            ${ANDROID:-?}"
echo "  HyperOS:            ${HYPER:-—}"
echo "  MIUI:               ${MIUI:-—}"
echo "  Bootloader locked:  ${LOCKED:-?} (1=locked, 0=unlocked)"
