#!/usr/bin/env bash
# End-to-end smoke test for the PC streamer:
#   1. Generate a sample mp4 if `videos/` is empty
#   2. Start the streamer (CLI, no adb reverse)
#   3. Run fake_phone for ~8 s, save raw H.264 → /tmp/vcam_capture.h264
#   4. Probe the captured file with ffprobe
#   5. Tear down the streamer
#
# Exit 0 on success.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SELF/.." && pwd)"
VIDEOS="$ROOT/videos"
CAP="${VCAM_SMOKE_OUT:-/tmp/vcam_capture.h264}"
PORT="${VCAM_SMOKE_PORT:-8889}"  # use non-default to avoid collisions

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[!] ffmpeg not found on PATH. Aborting smoke test." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "[!] python3 not found on PATH. Aborting." >&2
  exit 1
fi

# Ensure we have at least one video.
shopt -s nullglob
have=("$VIDEOS"/*.mp4 "$VIDEOS"/*.mov "$VIDEOS"/*.mkv "$VIDEOS"/*.webm)
if (( ${#have[@]} == 0 )); then
  echo "[+] no videos found, generating sample"
  bash "$SELF/make_sample_video.sh"
fi

cd "$ROOT"

echo "[+] starting streamer on port $PORT"
python3 -m src.main --cli --no-adb-reverse --port "$PORT" -v \
  >/tmp/vcam_smoke_streamer.log 2>&1 &
STREAMER_PID=$!
trap 'kill -TERM "$STREAMER_PID" 2>/dev/null || true; wait "$STREAMER_PID" 2>/dev/null || true' EXIT

# wait until the server is listening
for _ in $(seq 1 40); do
  if grep -q "listening :$PORT" /tmp/vcam_smoke_streamer.log 2>/dev/null; then
    break
  fi
  sleep 0.1
done

echo "[+] running fake_phone for 8 s → $CAP"
rm -f "$CAP"
python3 -m tools.fake_phone --port "$PORT" --duration 8 --out "$CAP" || true

if [[ ! -s "$CAP" ]]; then
  echo "[!] capture is empty, streamer log:"
  cat /tmp/vcam_smoke_streamer.log
  exit 2
fi

echo "[+] validating with ffmpeg (decode to /dev/null)"
if ! ffmpeg -hide_banner -loglevel error -f h264 -i "$CAP" -f null - 2>&1 | tail -3; then
  echo "[!] ffmpeg refused to decode capture" >&2
  exit 4
fi

SIZE=$(wc -c < "$CAP")
echo "[+] capture: $SIZE bytes"
if (( SIZE < 50000 )); then
  echo "[!] capture suspiciously small (< 50 KB)" >&2
  exit 3
fi

echo "[ok] smoke test passed."
