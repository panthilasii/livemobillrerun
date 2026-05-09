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
