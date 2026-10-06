#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "=== Mirror my iPhone - Setup ==="
echo ""

# Check Python version
PYTHON=""
# requirements.lock is resolved for Python 3.12, so only 3.12 will do
for cmd in /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 python3.12; do
    if command -v "$cmd" &> /dev/null \
        && "$cmd" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2> /dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.12 is required but not found."
    echo "Install it via: brew install python@3.12"
    exit 1
fi

echo "[1/3] Using $($PYTHON --version)"

# Create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "[2/3] Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "[2/3] Virtual environment already exists."
fi

# Install dependencies
echo "[3/3] Installing dependencies..."
# Only the exact files pinned in the lock files (by SHA-256); see packaging/bootstrap.sh
"$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check --quiet --require-hashes --no-deps --only-binary=:all: -r "$SCRIPT_DIR/requirements-build.lock"
"$VENV_DIR/bin/python3" -m pip install --disable-pip-version-check --quiet --require-hashes --no-deps --only-binary=:all: --no-binary=hexdump \
    --no-build-isolation -r "$SCRIPT_DIR/requirements.lock"

echo ""
echo "=== Setup complete! ==="
echo ""
echo "--- iPhone Setup ---"
echo "1. Enable Developer Mode on your iPhone:"
echo "   Settings > Privacy & Security > Developer Mode > ON"
echo "   (Requires restart)"
echo ""
echo "2. Connect iPhone via USB and trust this computer"
echo ""
echo "--- Optional: Touch Control (WebDriverAgent) ---"
echo "For touch/tap/swipe control, install Xcode from the App Store and sign in with"
echo "your Apple ID (Xcode > Settings > Accounts). The app's Doctor downloads the pinned,"
echo "verified WebDriverAgent; the app builds and installs it on the iPhone."
echo "Then trust the developer: Settings > General > VPN & Device Management"
echo ""
echo "Without WDA, the app works in view-only mode."
echo ""
echo "--- Run the app ---"
echo "  $VENV_DIR/bin/python3 $SCRIPT_DIR/main.py"
