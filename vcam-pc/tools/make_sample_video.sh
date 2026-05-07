#!/usr/bin/env bash
# Generate a small synthetic test video so we can smoke-test the streamer
# without needing real footage.
#
# Output: vcam-pc/videos/_smoketest.mp4   (~5 s, 480x270, color bars)
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SELF/.." && pwd)"
OUT="$ROOT/videos/_smoketest.mp4"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[!] ffmpeg not found on PATH. Install it:"
  echo "    macOS:  brew install ffmpeg"
  echo "    Linux:  sudo apt install ffmpeg"
  exit 1
fi

mkdir -p "$ROOT/videos"
echo "[+] generating $OUT"

ffmpeg -y -hide_banner -loglevel warning \
  -f lavfi -i "smptebars=size=480x270:rate=30" \
  -f lavfi -i "sine=frequency=440:duration=5" \
  -t 5 \
  -c:v libx264 -preset veryfast -pix_fmt yuv420p \
  -c:a aac -shortest \
  "$OUT"

ls -la "$OUT"
echo "[+] done. Run:"
echo "    python -m src.main --cli --no-adb-reverse"
echo "    python -m tools.fake_phone --duration 8 --out /tmp/vcam_capture.h264"
