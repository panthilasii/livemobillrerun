#!/usr/bin/env bash
# Lightweight 5-second check: is the phone connected, authorized, and
# ready to talk to us?
#
# Run BEFORE phone_smoke.sh / check_device.sh. Tells you exactly which
# step is failing if anything is wrong.
#
# Usage:
#   source tools/bin/env.sh   # so adb is on PATH
#   bash tools/check_phone.sh

# Note: do NOT use `set -e` — we want to continue past failed checks and
# show the full report even if the early steps fail.
set -u

# ── colours ────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; BLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=""; GRN=""; YEL=""; DIM=""; BLD=""; RST=""
fi

PASS=0
FAIL=0
WARN=0

ok()   { echo "  ${GRN}✓${RST} $*"; PASS=$((PASS+1)); }
bad()  { echo "  ${RED}✗${RST} $*"; FAIL=$((FAIL+1)); }
warn() { echo "  ${YEL}!${RST} $*"; WARN=$((WARN+1)); }
hint() { echo "    ${DIM}↳ $*${RST}"; }
section() { echo; echo "${BLD}── $1 ──${RST}"; }

# ── 1. adb on PATH ─────────────────────────────────────────────
section "1. adb available"
if command -v adb >/dev/null 2>&1; then
  ok "adb at $(command -v adb)"
  ADB_VER=$(adb version 2>/dev/null | head -1 || echo "?")
  hint "$ADB_VER"
else
  bad "adb not found in PATH"
  hint "run: source vcam-pc/tools/bin/env.sh"
  hint "or:  bash vcam-pc/tools/bootstrap_macos.sh"
  echo; echo "${RED}stop: install adb first.${RST}"
  exit 2
fi

# ── 2. USB device visible? ────────────────────────────────────
section "2. USB device detection"
DEV_RAW="$(adb devices -l 2>&1)"
echo "${DIM}$DEV_RAW${RST}" | sed 's/^/    /'

DEV_LINES="$(echo "$DEV_RAW" | awk 'NR>1 && NF>=2 {print}')"
if [[ -z "$DEV_LINES" ]]; then
  bad "no devices reported by adb"
  hint "checks: USB cable supports DATA (not charge-only)"
  hint "checks: phone unlocked, plugged into Mac"
  hint "checks: in dev options, 'USB Debugging' toggle is ON"
  hint "try   : adb kill-server && adb start-server"
  EARLY_EXIT=1
else
  COUNT_DEVICE=$(echo "$DEV_LINES" | awk '$2=="device"' | wc -l | tr -d ' ')
  COUNT_UNAUTH=$(echo "$DEV_LINES" | awk '$2=="unauthorized"' | wc -l | tr -d ' ')
  COUNT_OFFLINE=$(echo "$DEV_LINES" | awk '$2=="offline"' | wc -l | tr -d ' ')

  if (( COUNT_UNAUTH > 0 )); then
    bad "device shows up but is ${YEL}unauthorized${RST}"
    hint "look at the phone screen — accept the 'Allow USB debugging?' prompt"
    hint "tick 'Always allow from this computer' to make it permanent"
    EARLY_EXIT=1
  elif (( COUNT_OFFLINE > 0 )); then
    bad "device is ${YEL}offline${RST}"
    hint "unplug, replug, then re-run"
    EARLY_EXIT=1
  elif (( COUNT_DEVICE == 0 )); then
    bad "device visible but in unknown state"
    EARLY_EXIT=1
  elif (( COUNT_DEVICE > 1 )); then
    warn "$COUNT_DEVICE online devices — multi-device pipelines need ANDROID_SERIAL"
    SERIAL=$(echo "$DEV_LINES" | awk '$2=="device"' | head -1 | awk '{print $1}')
    hint "ANDROID_SERIAL=$SERIAL bash tools/phone_smoke.sh"
    EARLY_EXIT=0
  else
    ok "1 device online (state=device)"
    SERIAL=$(echo "$DEV_LINES" | awk '$2=="device"' | head -1 | awk '{print $1}')
    hint "serial: $SERIAL"
    EARLY_EXIT=0
  fi
fi

if [[ "${EARLY_EXIT:-0}" == "1" ]]; then
  echo
  echo "${RED}stop: fix the device-state error above, then re-run.${RST}"
  exit 3
fi

# ── 3. shell + identity ───────────────────────────────────────
section "3. shell sanity"
WHO="$(adb shell whoami 2>/dev/null | tr -d '\r')"
if [[ -n "$WHO" ]]; then
  ok "adb shell works (uid=$WHO)"
else
  bad "adb shell whoami returned empty"
fi

ID_OUT="$(adb shell id 2>/dev/null | tr -d '\r')"
hint "$ID_OUT"

# ── 4. device identity ─────────────────────────────────────────
section "4. device identity"
MODEL=$(adb shell getprop ro.product.model | tr -d '\r')
BRAND=$(adb shell getprop ro.product.brand | tr -d '\r')
SOC=$(adb shell getprop ro.soc.model | tr -d '\r')
ABI=$(adb shell getprop ro.product.cpu.abi | tr -d '\r')
ANDR=$(adb shell getprop ro.build.version.release | tr -d '\r')
HYPER=$(adb shell getprop ro.mi.os.version.name | tr -d '\r')
MIUI=$(adb shell getprop ro.miui.ui.version.name | tr -d '\r')

ok "${BRAND:-?} ${MODEL:-?}  (Android ${ANDR:-?}, ${HYPER:-${MIUI:-—}})"
hint "SoC: ${SOC:-?}   ABI: ${ABI:-?}"

# ── 5. /data/local/tmp writable ───────────────────────────────
section "5. /data/local/tmp write test"
if adb shell 'touch /data/local/tmp/.vcam_probe && rm /data/local/tmp/.vcam_probe' 2>/dev/null; then
  ok "/data/local/tmp/ is writable from adb shell"
else
  bad "cannot write /data/local/tmp/"
  hint "this would block Phase 4 (vcam.yuv writes)"
fi

# ── 6. tcp grabber present? ───────────────────────────────────
section "6. TCP grabber for phone_smoke.sh"
NC_PATH=""
for cand in "toybox nc" "nc"; do
  bin="${cand%% *}"
  if adb shell "command -v $bin >/dev/null 2>&1 && echo OK" 2>/dev/null | tr -d '\r' | grep -q OK; then
    NC_PATH="$cand"
    ok "phone has '$cand' — phone_smoke.sh will work"
    break
  fi
done
if [[ -z "$NC_PATH" ]]; then
  warn "no nc / toybox-nc on phone"
  hint "phone_smoke.sh won't work; build the vcam-app/ APK instead"
fi

# ── 7. adb reverse capability (privilege-free probe) ──────────
section "7. adb reverse + tcp loopback"
PORT=$((30000 + RANDOM % 5000))
if adb reverse "tcp:$PORT" "tcp:$PORT" >/dev/null 2>&1; then
  ok "adb reverse tcp:$PORT installed"
  if adb reverse --remove "tcp:$PORT" >/dev/null 2>&1; then
    ok "adb reverse --remove cleaned up"
  else
    warn "could not remove reverse — manual cleanup may be needed"
  fi
else
  bad "adb reverse failed"
  hint "this is required for the phone to reach the PC streamer"
fi

# ── 8. summary ────────────────────────────────────────────────
section "summary"
echo "  ${GRN}pass: $PASS${RST}    ${YEL}warn: $WARN${RST}    ${RED}fail: $FAIL${RST}"
echo
if (( FAIL == 0 && WARN == 0 )); then
  echo "${GRN}${BLD}all good.${RST} you can run:"
  echo "    bash tools/phone_smoke.sh"
elif (( FAIL == 0 )); then
  echo "${YEL}${BLD}ready, with caveats above.${RST}"
  echo "    bash tools/phone_smoke.sh"
else
  echo "${RED}${BLD}fix the failed checks above before going further.${RST}"
  exit 1
fi
