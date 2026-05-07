#!/usr/bin/env bash
# Download the latest LSPatch CLI jar from JingMatrix/LSPatch into .tools/.
# LSPatch is the open-source patcher we use to embed vcam-app into
# TikTok's APK without root.
#
# https://github.com/JingMatrix/LSPatch

set -euo pipefail

cd "$(dirname "$0")/../.." || exit 1
TOOLS_DIR="$(pwd)/.tools"
LS_DIR="$TOOLS_DIR/lspatch"
mkdir -p "$LS_DIR"

JAR="$LS_DIR/lspatch.jar"

FORCE="${1:-}"
if [[ -f "$JAR" ]] && [[ "$FORCE" != "-f" ]]; then
    echo "lspatch.jar already present ($(du -h "$JAR" | cut -f1))"
    echo "  pass -f to force re-download"
    exit 0
fi

# Resolve the latest non-debug release.
URL=$(curl -fsSL https://api.github.com/repos/JingMatrix/LSPatch/releases/latest \
        | python3 -c "
import json, sys
data = json.load(sys.stdin)
for a in data.get('assets', []):
    n = a['name']
    if n == 'lspatch.jar':
        print(a['browser_download_url'])
        break
")

if [[ -z "${URL:-}" ]]; then
    echo "Could not resolve lspatch.jar download URL." >&2
    exit 2
fi

echo "Downloading $URL ..."
curl -fL --progress-bar -o "$JAR" "$URL"

ls -la "$JAR"
echo "lspatch ready at $JAR"
