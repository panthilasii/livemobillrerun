#!/usr/bin/env bash
# Build the Zygisk module .so files for arm64-v8a and (optionally)
# armeabi-v7a using the Android NDK CMake toolchain.
#
# Prerequisites:
#   1. Android NDK installed locally. Set `$ANDROID_NDK` to the
#      directory containing `build/cmake/android.toolchain.cmake`.
#   2. CMake ≥ 3.18 on the host.
#
# Output:
#   src/zygisk/build/<abi>/libvcam_zygisk.so
#
# These are picked up automatically by build.sh when packaging the
# flashable Magisk zip.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src/zygisk"
ANDROID_PLATFORM="${ANDROID_PLATFORM:-android-26}"

if [ -z "${ANDROID_NDK:-}" ]; then
  echo "ERROR: \$ANDROID_NDK is not set." >&2
  echo "       Install the NDK (sdkmanager 'ndk;26.1.10909125' for example)" >&2
  echo "       and rerun: ANDROID_NDK=\$HOME/Library/Android/sdk/ndk/26.1.10909125 bash $0" >&2
  exit 1
fi

if [ ! -f "$ANDROID_NDK/build/cmake/android.toolchain.cmake" ]; then
  echo "ERROR: \$ANDROID_NDK ($ANDROID_NDK) doesn't look like an NDK install" >&2
  exit 1
fi

ABIS=("${@:-arm64-v8a}")

for ABI in "${ABIS[@]}"; do
  BUILD_DIR="$SRC/build/$ABI"
  rm -rf "$BUILD_DIR"
  mkdir -p "$BUILD_DIR"
  echo "==> building $ABI in $BUILD_DIR"
  cmake \
    -S "$SRC" \
    -B "$BUILD_DIR" \
    -DCMAKE_TOOLCHAIN_FILE="$ANDROID_NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI="$ABI" \
    -DANDROID_PLATFORM="$ANDROID_PLATFORM" \
    -DCMAKE_BUILD_TYPE=Release
  cmake --build "$BUILD_DIR" --parallel 4
  echo "==> $BUILD_DIR/libvcam_zygisk.so"
  ls -la "$BUILD_DIR/libvcam_zygisk.so"
done
