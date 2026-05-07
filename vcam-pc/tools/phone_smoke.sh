#!/usr/bin/env bash
# Phone-side smoke test: prove the streamer reaches an unrooted Redmi 13C
# (or any Android device with adb enabled), without needing the APK.
#
# Flow:
#   1. Confirm `adb devices` shows exactly one online device.
#   2. Probe phone for a TCP grabber: `toybox nc` (preferred) → `nc`.
#   3. `adb reverse tcp:$PORT tcp:$PORT` so phone's localhost:$PORT
#      reaches the PC streamer.
#   4. Start the streamer (background).
#   5. On the phone, capture ~5 s of bytes to /sdcard/Download/vcam_capture.h264.
#   6. `adb pull` the capture back to PC, validate with `ffmpeg -f null`.
#   7. Cleanup: kill streamer, remove `adb reverse`.
#
# Output: 0 if the full pipe works, non-zero with a diagnostic message
# otherwise.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SELF/.." && pwd)"
PORT="${VCAM_PHONE_SMOKE_PORT:-8889}"
DURATION_S="${VCAM_PHONE_SMOKE_SECS:-5}"
PHONE_OUT="/sdcard/Download/vcam_capture.h264"
PC_OUT="${VCAM_PHONE_OUT:-/tmp/vcam_phone_capture.h264}"

die() { echo "[!] $*" >&2; exit 1; }
log() { echo "[+] $*"; }

# ── prerequisites ──────────────────────────────────────────────
command -v adb    >/dev/null 2>&1 || die "adb not on PATH (run: source $SELF/bin/env.sh)"
command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg not on PATH (run: source $SELF/bin/env.sh)"
command -v python3 >/dev/null 2>&1 || die "python3 not on PATH"

# Generate sample if videos folder is empty.
shopt -s nullglob
have=("$ROOT/videos"/*.mp4 "$ROOT/videos"/*.mov "$ROOT/videos"/*.mkv "$ROOT/videos"/*.webm)
if (( ${#have[@]} == 0 )); then
  log "no videos found, generating sample"
  bash "$SELF/make_sample_video.sh"
fi

# ── phone connectivity ────────────────────────────────────────
log "checking adb devices"
DEVICES_OUT="$(adb devices -l)"
echo "$DEVICES_OUT" | sed 's/^/    /'
ONLINE_COUNT=$(echo "$DEVICES_OUT" | awk 'NR>1 && $2=="device"' | wc -l | tr -d ' ')
if (( ONLINE_COUNT < 1 )); then
  die "no online adb device. Connect your phone, enable USB debugging, accept the prompt, and re-run."
fi
if (( ONLINE_COUNT > 1 )); then
  die "multiple devices online. Set ANDROID_SERIAL=<serial> and re-run, e.g. ANDROID_SERIAL=$(echo "$DEVICES_OUT" | awk 'NR==2{print $1}') bash $0"
fi

# Pick a TCP grabber on the phone.
log "probing phone for a TCP grabber"
PHONE_NC=""
for candidate in "toybox nc" "nc"; do
  if adb shell "command -v ${candidate%% *} >/dev/null 2>&1 && echo OK" 2>/dev/null | grep -q OK; then
    PHONE_NC="$candidate"
    log "  found: $candidate"
    break
  fi
done
if [[ -z "$PHONE_NC" ]]; then
  die "no nc/toybox-nc on phone. Build the APK in vcam-app/ and use the in-app TCP client instead."
fi

# ── set up adb reverse ────────────────────────────────────────
log "adb reverse tcp:$PORT tcp:$PORT"
adb reverse tcp:$PORT tcp:$PORT

cleanup() {
  log "cleanup"
  if [[ -n "${STREAMER_PID:-}" ]]; then
    kill -TERM "$STREAMER_PID" 2>/dev/null || true
    wait "$STREAMER_PID" 2>/dev/null || true
  fi
  adb reverse --remove tcp:$PORT 2>/dev/null || true
  adb shell "rm -f $PHONE_OUT" 2>/dev/null || true
}
trap cleanup EXIT

# ── start streamer ────────────────────────────────────────────
log "starting PC streamer on :$PORT"
( cd "$ROOT" && python3 -m src.main --cli --no-adb-reverse --port "$PORT" -v ) \
  >/tmp/vcam_phone_smoke_streamer.log 2>&1 &
STREAMER_PID=$!

for _ in $(seq 1 50); do
  if grep -q "listening :$PORT" /tmp/vcam_phone_smoke_streamer.log 2>/dev/null; then
    break
  fi
  sleep 0.1
done
if ! grep -q "listening :$PORT" /tmp/vcam_phone_smoke_streamer.log 2>/dev/null; then
  echo "----- streamer log -----"
  cat /tmp/vcam_phone_smoke_streamer.log
  die "streamer did not reach 'listening' state"
fi
log "streamer up"

# ── phone-side capture ────────────────────────────────────────
log "capturing $DURATION_S s on phone → $PHONE_OUT"
adb shell "rm -f $PHONE_OUT 2>/dev/null; \
  ($PHONE_NC 127.0.0.1 $PORT > $PHONE_OUT) & \
  NCPID=\$!; \
  sleep $DURATION_S; \
  kill -TERM \$NCPID 2>/dev/null; \
  wait \$NCPID 2>/dev/null; \
  ls -la $PHONE_OUT" || true

# ── pull + validate ───────────────────────────────────────────
log "adb pull → $PC_OUT"
rm -f "$PC_OUT"
adb pull "$PHONE_OUT" "$PC_OUT" || die "adb pull failed; capture may not exist on phone"

SIZE=$(wc -c < "$PC_OUT" | tr -d ' ')
log "capture size: $SIZE bytes"
if (( SIZE < 50000 )); then
  die "capture suspiciously small (< 50 KB). The TCP tunnel reached the phone but FFmpeg may have failed. Streamer log: /tmp/vcam_phone_smoke_streamer.log"
fi

log "validating with ffmpeg"
if ! ffmpeg -hide_banner -loglevel error -f h264 -i "$PC_OUT" -f null - 2>&1 | tail -3; then
  die "ffmpeg refused to decode capture"
fi

cat <<EOF

[ok] phone-side smoke test passed.

Pipe verified:  PC streamer → adb reverse :$PORT → phone $PHONE_NC → /sdcard
Capture saved:  $PC_OUT  ($SIZE bytes)

This proves Phase 2 + the adb tunnel work end-to-end.
Next: build vcam-app/ in Android Studio so the phone can DECODE the
stream (instead of just dumping bytes to a file).
EOF
