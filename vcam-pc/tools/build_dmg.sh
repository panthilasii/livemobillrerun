#!/usr/bin/env bash
# NP Create -- one-command macOS .dmg build.
#
# Run this on macOS with create-dmg installed:
#
#   brew install create-dmg
#   python tools/build_pyinstaller.py    # produces dist/pyinstaller/NP-Create.app
#   bash tools/build_dmg.sh
#
# Output:
#   vcam-pc/dist/installer/NP-Create-<version>.dmg
#
# Why .dmg, not just a .zip
# -------------------------
# macOS users are conditioned to drag-to-Applications via .dmg --
# it's the platform's "installer" UX (see Discord, OBS, Notion).
# A .zip works but customers often run the app from ~/Downloads
# and then macOS Gatekeeper quarantines it on every launch. Dropping
# into /Applications via .dmg clears the quarantine flag once and
# the customer never sees "is from the internet" warnings again.

set -euo pipefail

# Force C locale before any tool invocation. ``create-dmg`` greps
# the literal English string "Resource busy" out of ``hdiutil``
# stderr to decide whether to retry an unmount; on machines whose
# system locale is Thai (or any non-English one) ``hdiutil`` emits
# the localized phrase ("แหล่งข้อมูลไม่ว่าง" etc.), the grep
# misses, and the build aborts with exit 16 on the first transient
# busy mount. ``LC_ALL=C`` keeps both ``hdiutil`` and any of its
# child tools speaking English so the heuristic works.
export LC_ALL=C
export LANG=C

cd "$(dirname "$0")/.."
PROJECT="$(pwd)"

APP="$PROJECT/dist/pyinstaller/NP-Create.app"
OUT_DIR="$PROJECT/dist/installer"
VERSION="$(python3 -c 'import sys; sys.path.insert(0, "src"); from branding import BRAND; print(BRAND.version)')"
DMG="$OUT_DIR/NP-Create-${VERSION}.dmg"
VOL_NAME="NP Create ${VERSION}"

echo
echo " ============================================================"
echo "  NP Create -- macOS .dmg Build"
echo "  version: ${VERSION}"
echo " ============================================================"

if [[ ! -d "$APP" ]]; then
    echo "[!] $APP not found."
    echo "    Run: python3 tools/build_pyinstaller.py"
    exit 1
fi

if ! command -v create-dmg >/dev/null 2>&1; then
    echo "[!] create-dmg not installed."
    echo "    Run: brew install create-dmg"
    exit 1
fi

# ── Inject the tool payload into the .app (v1.8.27) ─────────────
#
# PyInstaller deliberately does NOT bundle .tools/ or apk/ (see
# build_pyinstaller.py) — the platform installer is responsible for
# placing them. Pre-1.8.27 this script never did, so every .dmg
# customer got a 28 MB app with NO adb/ffmpeg/JDK at all and the
# wizard hung on "รอเครื่อง…" with the "พบ adb แต่รันไม่ได้" dialog.
#
# The payload goes in Contents/Resources/ — NOT Contents/MacOS/ —
# because codesign refuses to seal a bundle with a foreign directory
# inside MacOS/ ("bundle format unrecognized ... In subcomponent:
# .tools"). platform_tools._extra_tools_roots() resolves
# Resources/.tools/<os>/ at runtime, and find_vcam_apk() checks
# Resources/apk/.
#
# cp -RL (dereference) so dev machines whose .tools/macos entries
# are symlinks into legacy layouts still produce a self-contained
# bundle; on CI the dirs are already real.
#
# We inject into a STAGED COPY, not dist/pyinstaller/NP-Create.app
# itself — build_release.py zips that same .app into the portable
# customer ZIP (which already ships .tools/ + apk/ at the bundle
# root), and mutating the original would double the payload there.
WORKSPACE="$(cd "$PROJECT/.." && pwd)"
TOOLS_SRC="$WORKSPACE/.tools/macos"
APK_SRC="$WORKSPACE/apk/vcam-app-release.apk"

STAGE="$PROJECT/dist/dmg-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
echo "[*] Staging .app copy for dmg ..."
cp -R "$APP" "$STAGE/NP-Create.app"
APP="$STAGE/NP-Create.app"
RES="$APP/Contents/Resources"

if [[ ! -d "$TOOLS_SRC" ]]; then
    echo "[!] $TOOLS_SRC not found."
    echo "    Run: python3 tools/setup_ci_tools.py --os macos"
    exit 1
fi

echo "[*] Injecting .tools/macos + apk into the .app ..."
rm -rf "$RES/.tools" "$RES/apk"
mkdir -p "$RES/.tools" "$RES/apk"
cp -RL "$TOOLS_SRC" "$RES/.tools/macos"
if [[ -f "$APK_SRC" ]]; then
    cp "$APK_SRC" "$RES/apk/vcam-app-release.apk"
else
    echo "[!] $APK_SRC not found — dmg will lack the vcam APK."
    exit 1
fi

# Hard guard: never ship an empty payload again. Each of these is a
# hard runtime dependency (device polling / encode / patch). chmod
# first — zip extraction on CI has historically dropped Unix mode
# bits (the v1.8.27 dmg shipped a non-exec adb) — then require -x.
for required in \
    "$RES/.tools/macos/platform-tools/adb" \
    "$RES/.tools/macos/ffmpeg" \
    "$RES/.tools/macos/jdk-21/Contents/Home/bin/java"; do
    if [[ ! -f "$required" ]]; then
        echo "[!] payload incomplete: missing $required"
        exit 1
    fi
    chmod +x "$required"
    if [[ ! -x "$required" ]]; then
        echo "[!] payload broken: $required is not executable"
        exit 1
    fi
done

# Adding files invalidated PyInstaller's ad-hoc seal; re-sign so
# Gatekeeper doesn't report the app as damaged. Plain (non-deep)
# ad-hoc signing re-seals the bundle while leaving the vendors'
# signatures on nested tool binaries (adb/java) untouched.
echo "[*] Re-signing .app (ad-hoc) ..."
codesign --force -s - "$APP"
codesign --verify --strict "$APP"
echo "[*] Payload injected: $(du -sh "$APP" | awk '{print $1}') total"

mkdir -p "$OUT_DIR"
rm -f "$DMG"

# Optional background image (logo on light/dark gradient). Falls
# back to plain white if the asset hasn't been authored yet --
# create-dmg accepts a missing --background gracefully via the
# --no-internet-enable trick we use below.
BG_ARGS=()
if [[ -f "$PROJECT/assets/dmg-background.png" ]]; then
    BG_ARGS=(--background "$PROJECT/assets/dmg-background.png")
fi

# create-dmg wraps `hdiutil` with a sane DSL. Window geometry
# values below place the .app icon to the left of the Applications
# alias so the customer's natural left-to-right read = "drag NP
# Create -> Applications".
create-dmg \
    --volname "$VOL_NAME" \
    --volicon "$PROJECT/assets/logo.icns" \
    --window-pos 200 120 \
    --window-size 720 400 \
    --icon-size 128 \
    --icon "NP-Create.app" 180 200 \
    --hide-extension "NP-Create.app" \
    --app-drop-link 540 200 \
    "${BG_ARGS[@]+"${BG_ARGS[@]}"}" \
    --no-internet-enable \
    "$DMG" \
    "$APP"
# The ``${BG_ARGS[@]+...}`` indirection above is the standard
# bash-3.2 idiom for "expand only if the array has elements".
# Without it, ``set -u`` plus an empty BG_ARGS triggers an
# "unbound variable" abort BEFORE create-dmg even starts —
# painful because the asset (assets/dmg-background.png) is
# optional by design.

echo
echo " DONE."
SIZE=$(du -h "$DMG" | awk '{print $1}')
echo "  Output: $DMG"
echo "  Size:   $SIZE"
echo
