#!/usr/bin/env bash
# Build a flashable Magisk module zip from `module/` + native libs in `src/build/`.
#
# Run after building the native pieces with CMake:
#   ( cd src/zygisk-hook && mkdir -p build && cd build && cmake .. && make )
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST="$ROOT/dist"
mkdir -p "$DIST"

STAGE="$(mktemp -d)"
trap "rm -rf $STAGE" EXIT

cp -R "$ROOT/module/." "$STAGE/"

# Optional: copy built Zygisk module .so if present.
# `build_native.sh` produces src/zygisk/build/<abi>/libvcam_zygisk.so —
# the Magisk Zygisk loader expects them under module/zygisk/<abi>.so.
HAS_ZYGISK=0
for ABI in arm64-v8a armeabi-v7a x86_64 x86; do
  SO="$ROOT/src/zygisk/build/$ABI/libvcam_zygisk.so"
  if [ -f "$SO" ]; then
    mkdir -p "$STAGE/zygisk"
    cp "$SO" "$STAGE/zygisk/$ABI.so"
    echo "  + $ABI : $(stat -f%z "$SO" 2>/dev/null || stat -c%s "$SO") bytes"
    HAS_ZYGISK=1
  fi
done
if [ $HAS_ZYGISK -eq 0 ]; then
  echo "  (no libvcam_zygisk.so found — module zip will install but do nothing.)"
  echo "   Run vcam-magisk/build_native.sh first to bake in the hook."
fi

# Optional: copy built HAL overlay.
if [ -f "$ROOT/src/hal-overlay/build/libcamera.mt6769.so" ]; then
  mkdir -p "$STAGE/system/vendor/lib64/hw"
  cp "$ROOT/src/hal-overlay/build/libcamera.mt6769.so" \
     "$STAGE/system/vendor/lib64/hw/camera.mt6769.so"
fi

OUT="$DIST/vcam-magisk.zip"
( cd "$STAGE" && zip -r9 "$OUT" . -x "*.DS_Store" )

echo "wrote: $OUT"
ls -la "$OUT"
