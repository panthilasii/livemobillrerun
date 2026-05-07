#!/usr/bin/env bash
# Download a portable Adoptium Temurin JDK 21 into .tools/jdk-21/.
# LSPatch v0.8+ is built against Java 21 class files, so anything older
# fails with `UnsupportedClassVersionError`.
#
# Idempotent: if jdk-21/ already exists with a working `bin/java`, exit
# fast.

set -euo pipefail

cd "$(dirname "$0")/../.." || exit 1
TOOLS_DIR="$(pwd)/.tools"
mkdir -p "$TOOLS_DIR"
JDK_DIR="$TOOLS_DIR/jdk-21"

case "$(uname -s)/$(uname -m)" in
    Darwin/arm64)  PLATFORM="mac/aarch64";  STRIP=1; HOME_REL="Contents/Home" ;;
    Darwin/x86_64) PLATFORM="mac/x64";      STRIP=1; HOME_REL="Contents/Home" ;;
    Linux/aarch64) PLATFORM="linux/aarch64"; STRIP=1; HOME_REL="" ;;
    Linux/x86_64)  PLATFORM="linux/x64";    STRIP=1; HOME_REL="" ;;
    *) echo "Unsupported platform: $(uname -s)/$(uname -m)" >&2; exit 2 ;;
esac

if [[ -n "$HOME_REL" ]]; then
    JAVA_BIN="$JDK_DIR/$HOME_REL/bin/java"
else
    JAVA_BIN="$JDK_DIR/bin/java"
fi

if [[ -x "$JAVA_BIN" ]]; then
    if "$JAVA_BIN" -version 2>&1 | grep -q '"21'; then
        echo "JDK 21 already present at $JAVA_BIN"
        exit 0
    fi
fi

URL="https://api.adoptium.net/v3/binary/latest/21/ga/${PLATFORM}/jdk/hotspot/normal/eclipse"
TARBALL="$TOOLS_DIR/jdk21.tar.gz"

echo "Downloading JDK 21 from $URL ..."
curl -fL --progress-bar -o "$TARBALL" "$URL"

mkdir -p "$JDK_DIR"
echo "Extracting into $JDK_DIR ..."
tar xzf "$TARBALL" -C "$JDK_DIR" --strip-components=$STRIP

rm -f "$TARBALL"

if [[ ! -x "$JAVA_BIN" ]]; then
    echo "Extraction succeeded but $JAVA_BIN is missing." >&2
    ls -la "$JDK_DIR" >&2
    exit 3
fi

"$JAVA_BIN" -version 2>&1 | head -3
echo "JDK 21 ready at $JAVA_BIN"
