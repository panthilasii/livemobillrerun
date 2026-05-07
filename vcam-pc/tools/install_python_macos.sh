#!/usr/bin/env bash
# Install Python 3.13 universal2 from python.org so the GUI uses
# Tk 8.6.14 (which renders correctly under macOS Dark Mode on
# Apple Silicon). System Python 3.9 ships with Tk 8.5 which has a
# broken `aqua` theme — every ttk widget renders transparent.
#
# Idempotent: if /Library/Frameworks/Python.framework/Versions/3.13
# already exists we just print where it is and exit.
#
# Usage:
#   bash tools/install_python_macos.sh

set -euo pipefail

PY_VER="3.13.5"
PY_PKG="python-${PY_VER}-macos11.pkg"
PY_URL="https://www.python.org/ftp/python/${PY_VER}/${PY_PKG}"
PY_PREFIX="/Library/Frameworks/Python.framework/Versions/3.13"
PY_BIN="${PY_PREFIX}/bin/python3"

echo "──────────────────────────────────────────────"
echo "  Installing Python ${PY_VER} for vcam-pc GUI"
echo "──────────────────────────────────────────────"

if [ -x "${PY_BIN}" ]; then
    echo "✓ Python ${PY_VER} is already installed at:"
    echo "  ${PY_BIN}"
    "${PY_BIN}" --version
    "${PY_BIN}" -c "import tkinter; print('  Tk', tkinter.TkVersion)"
    echo
    echo "Run the GUI with:"
    echo "  ${PY_BIN} -m src.main --gui"
    exit 0
fi

DOWNLOAD_DIR="${TMPDIR:-/tmp}"
PKG_PATH="${DOWNLOAD_DIR}/${PY_PKG}"

if [ ! -f "${PKG_PATH}" ]; then
    echo "→ Downloading ${PY_PKG} (~30 MB)..."
    curl -fL --progress-bar -o "${PKG_PATH}" "${PY_URL}"
fi

echo
echo "→ Installing (will prompt for your Mac admin password)..."
sudo installer -pkg "${PKG_PATH}" -target /

if [ ! -x "${PY_BIN}" ]; then
    echo "✗ Install seems to have failed — ${PY_BIN} is missing."
    exit 1
fi

echo
echo "✓ Installed:"
"${PY_BIN}" --version
"${PY_BIN}" -c "import tkinter; print('  Tk', tkinter.TkVersion, '(should be 8.6+)')"

echo
echo "──────────────────────────────────────────────"
echo "  Done. Now run the GUI with:"
echo "    cd /Users/ii/livemobillrerun/vcam-pc"
echo "    source tools/bin/env.sh"
echo "    ${PY_BIN} -m src.main --gui"
echo "──────────────────────────────────────────────"
