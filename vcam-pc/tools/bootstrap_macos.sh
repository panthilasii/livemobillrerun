#!/usr/bin/env bash
# Set up portable adb + ffmpeg in vcam-pc/tools/bin/.
#
# - adb: official Google platform-tools (arm64 + intel universal)
# - ffmpeg: arm64-native via the `imageio-ffmpeg` pip package
#   (works on Apple Silicon without Rosetta; intel-only fallbacks
#    like evermeet.cx do NOT run on plain Apple Silicon)
#
# After running, source the env file:
#   source vcam-pc/tools/bin/env.sh
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SELF/.." && pwd)"
BIN="$SELF/bin"
mkdir -p "$BIN"

step() { echo; echo "==> $*"; }

# Ensure venv exists with imageio-ffmpeg (used to provide a native ffmpeg).
if [[ ! -x "$ROOT/../.venv/bin/python" ]]; then
  step "creating .venv (project root) and installing pip deps"
  python3 -m venv "$ROOT/../.venv"
fi
VENV_PY="$ROOT/../.venv/bin/python"
"$VENV_PY" -m pip install --quiet --disable-pip-version-check \
  -r "$ROOT/requirements.txt" imageio-ffmpeg

# ── adb (Google Platform Tools) ───────────────────────────────
if [[ -x "$BIN/adb" ]]; then
  echo "==> adb already present"
else
  step "downloading adb (Google platform-tools, ~6 MB)"
  cd "$BIN"
  curl -fL --progress-bar \
    -o platform-tools.zip \
    https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
  unzip -q -o platform-tools.zip
  for t in adb fastboot; do
    cp -f "platform-tools/$t" "$BIN/$t"
    chmod +x "$BIN/$t"
  done
  rm -rf platform-tools platform-tools.zip
  cd - >/dev/null
fi

# ── ffmpeg (arm64-native via imageio-ffmpeg) ──────────────────
step "wiring ffmpeg from imageio-ffmpeg"
FFMPEG_BIN="$("$VENV_PY" -c 'import imageio_ffmpeg as f; print(f.get_ffmpeg_exe())')"
ln -sf "$FFMPEG_BIN" "$BIN/ffmpeg"
file "$BIN/ffmpeg" | sed 's/^/    /'
"$BIN/ffmpeg" -version 2>&1 | head -1 | sed 's/^/    /'

# ── env.sh for sourcing ───────────────────────────────────────
cat > "$BIN/env.sh" <<'EOF'
# Source this to put portable adb + ffmpeg on PATH for this shell.
# Usage:  source vcam-pc/tools/bin/env.sh
__VCAM_BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
case ":$PATH:" in
  *":$__VCAM_BIN_DIR:"*) ;;
  *) export PATH="$__VCAM_BIN_DIR:$PATH" ;;
esac
unset __VCAM_BIN_DIR
EOF

cat <<EOF

==> done.

Activate this shell:
    source vcam-pc/tools/bin/env.sh
    adb --version
    ffmpeg -version | head -1

Note: ffprobe is NOT installed. Smoke tests use \`ffmpeg -f null\` to
validate captures, which works without ffprobe.
EOF
