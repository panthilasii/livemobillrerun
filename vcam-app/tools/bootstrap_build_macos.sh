#!/usr/bin/env bash
# Portable Android-build toolchain installer for macOS Apple Silicon.
#
# Downloads (all into ../../.tools/ relative to vcam-app/, so ~600 MB total):
#   - Temurin JDK 17 (arm64)
#   - Android Command-Line Tools (latest)
#   - Android SDK packages: platform-tools, platforms;android-34, build-tools;34.0.0
#   - Gradle 8.10.2 distribution
#
# After running, source the env file:
#   source .tools/env.sh
# Then `./gradlew :app:assembleDebug` will work in vcam-app/.
set -euo pipefail

VCAM_APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_ROOT="$(cd "$VCAM_APP_DIR/.." && pwd)"
TOOLS="$PROJECT_ROOT/.tools"
DL="$TOOLS/_downloads"
mkdir -p "$TOOLS" "$DL"

step() { echo; echo "==> $*"; }
log()  { echo "    $*"; }

UNAME_M=$(uname -m)
if [[ "$UNAME_M" != "arm64" ]]; then
  echo "WARN: this script is tuned for arm64 (Apple Silicon). uname=$UNAME_M" >&2
fi

# ── 1. JDK 17 ──────────────────────────────────────────────────
JDK_DIR="$TOOLS/jdk-17"
if [[ -x "$JDK_DIR/Contents/Home/bin/javac" ]]; then
  step "JDK 17 already installed: $JDK_DIR"
else
  step "downloading Temurin JDK 17 (arm64, ~200 MB)"
  JDK_URL="https://api.adoptium.net/v3/binary/latest/17/ga/mac/aarch64/jdk/hotspot/normal/eclipse?project=jdk"
  curl -fL --progress-bar -o "$DL/jdk17.tar.gz" "$JDK_URL"
  step "unpacking JDK"
  rm -rf "$JDK_DIR"
  mkdir -p "$JDK_DIR"
  tar -xzf "$DL/jdk17.tar.gz" -C "$JDK_DIR" --strip-components=1
  log "$($JDK_DIR/Contents/Home/bin/javac --version 2>&1 || true)"
fi
export JAVA_HOME="$JDK_DIR/Contents/Home"
export PATH="$JAVA_HOME/bin:$PATH"

# ── 2. Android cmdline-tools ──────────────────────────────────
SDK="$TOOLS/android-sdk"
CMDLINE_HOME="$SDK/cmdline-tools/latest"
if [[ -x "$CMDLINE_HOME/bin/sdkmanager" ]]; then
  step "Android cmdline-tools already installed"
else
  step "downloading Android cmdline-tools (~150 MB)"
  CMD_URL="https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip"
  curl -fL --progress-bar -o "$DL/cmdline-tools.zip" "$CMD_URL"
  step "unpacking cmdline-tools to $CMDLINE_HOME"
  rm -rf "$CMDLINE_HOME"
  mkdir -p "$CMDLINE_HOME"
  TMP_EXTRACT="$DL/_cmdline_extract"
  rm -rf "$TMP_EXTRACT"
  mkdir -p "$TMP_EXTRACT"
  unzip -q -o "$DL/cmdline-tools.zip" -d "$TMP_EXTRACT"
  # the zip extracts to `cmdline-tools/`; move its contents to .../latest/
  mv "$TMP_EXTRACT/cmdline-tools/"* "$CMDLINE_HOME/"
  rm -rf "$TMP_EXTRACT"
fi
export ANDROID_HOME="$SDK"
export ANDROID_SDK_ROOT="$SDK"
export PATH="$CMDLINE_HOME/bin:$SDK/platform-tools:$PATH"

# ── 3. Accept licenses + install SDK packages ─────────────────
step "accepting Android SDK licenses (yes-spam)"
yes 2>/dev/null | "$CMDLINE_HOME/bin/sdkmanager" --licenses >/dev/null 2>&1 || true

PKG_LIST=("platform-tools" "platforms;android-34" "build-tools;34.0.0")
need_install=0
for pkg in "${PKG_LIST[@]}"; do
  case "$pkg" in
    "platform-tools")          [[ -x "$SDK/platform-tools/adb" ]] || need_install=1 ;;
    "platforms;android-34")    [[ -d "$SDK/platforms/android-34" ]] || need_install=1 ;;
    "build-tools;34.0.0")      [[ -d "$SDK/build-tools/34.0.0" ]] || need_install=1 ;;
  esac
done

if (( need_install )); then
  step "installing SDK packages (~300 MB total): ${PKG_LIST[*]}"
  "$CMDLINE_HOME/bin/sdkmanager" --install "${PKG_LIST[@]}" 2>&1 | \
    grep -vE '^\[=' | tail -20 || true
else
  step "SDK packages already present"
fi
log "platform-tools: $($SDK/platform-tools/adb --version 2>&1 | head -1 || echo missing)"
log "build-tools 34: $(ls $SDK/build-tools/34.0.0 2>/dev/null | head -1 || echo missing)"

# ── 4. Gradle 8.10.2 ──────────────────────────────────────────
GRADLE_DIR="$TOOLS/gradle-8.10.2"
if [[ -x "$GRADLE_DIR/bin/gradle" ]]; then
  step "Gradle already installed"
else
  step "downloading Gradle 8.10.2 (~150 MB)"
  GRADLE_URL="https://services.gradle.org/distributions/gradle-8.10.2-bin.zip"
  curl -fL --progress-bar -o "$DL/gradle.zip" "$GRADLE_URL"
  step "unpacking gradle to $GRADLE_DIR"
  rm -rf "$GRADLE_DIR"
  unzip -q -o "$DL/gradle.zip" -d "$TOOLS"
fi
export PATH="$GRADLE_DIR/bin:$PATH"
log "gradle: $(gradle --version 2>&1 | grep -E '^Gradle' | head -1)"

# ── 5. Wire vcam-app to local SDK ─────────────────────────────
step "configuring vcam-app/local.properties"
cd "$VCAM_APP_DIR"
echo "sdk.dir=$SDK" > local.properties
log "wrote $VCAM_APP_DIR/local.properties (sdk.dir=$SDK)"

# We deliberately DO NOT run `gradle wrapper`. The wrapper task hits a
# bind/network step that fails in some sandboxed environments. Instead,
# users run `gradle :app:assembleDebug` directly — gradle 8.10.2 is
# already on PATH after sourcing .tools/env.sh.

# ── 6. env.sh for sourcing ────────────────────────────────────
cat > "$TOOLS/env.sh" <<EOF
# Source this to put the portable Android build toolchain on PATH.
# Usage:  source .tools/env.sh
export JAVA_HOME="$JDK_DIR/Contents/Home"
export ANDROID_HOME="$SDK"
export ANDROID_SDK_ROOT="$SDK"
export PATH="\$JAVA_HOME/bin:$CMDLINE_HOME/bin:$SDK/platform-tools:$GRADLE_DIR/bin:\$PATH"

# Force English+Gregorian locale so that AGP's MS-DOS zip date check
# (1980 ≤ year ≤ 2107) doesn't fall over on Thai macOS, where the JVM
# would otherwise default to BuddhistCalendar and report year 2569.
export LANG=C
export LC_ALL=C
EOF
log "wrote $TOOLS/env.sh"

cat <<EOF

==> done. Build the APK:
    source .tools/env.sh           # from project root
    cd vcam-app
    gradle :app:assembleDebug

APK output: vcam-app/app/build/outputs/apk/debug/app-debug.apk
EOF
