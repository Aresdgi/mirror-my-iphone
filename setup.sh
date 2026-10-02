#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "=== iPhone Mirror Tool - Setup ==="
echo ""

# Check Python version
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &> /dev/null; then
        version=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10+ is required but not found."
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
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q

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
echo "3. For iOS 17+, start the developer tunnel (required):"
echo "   sudo $VENV_DIR/bin/python3 -m pymobiledevice3 remote tunneld"
echo "   (Keep this running in a separate terminal)"
echo ""
echo "--- Optional: Touch Control (WebDriverAgent) ---"
echo "For touch/tap/swipe control, you need WebDriverAgent:"
echo "1. Install Xcode from the App Store"
echo "2. git clone https://github.com/appium/WebDriverAgent.git"
echo "3. Open WebDriverAgent.xcodeproj in Xcode"
echo "4. Select 'WebDriverAgentRunner' target"
echo "5. Set your Apple ID as signing team"
echo "6. Change bundle ID to something unique"
echo "7. Build & run on your iPhone"
echo "8. Trust the certificate: Settings > General > VPN & Device Management"
echo ""
echo "Without WDA, the app works in view-only mode."
echo ""
echo "--- Run the app ---"
echo "  $VENV_DIR/bin/python3 $SCRIPT_DIR/main.py"
