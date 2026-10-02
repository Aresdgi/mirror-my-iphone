#!/bin/bash
# Creates or updates the Python environment (a venv) iPhone Mirror runs in.
#
#   bootstrap.sh [VENV]
#
# The Homebrew cask runs it after installing, with a venv in its Caskroom directory. Without an
# argument (setup.command, when the app finds no environment) it uses $IPHONE_MIRROR_VENV or
# ~/Library/Application Support/iPhone Mirror/venv.
set -euo pipefail

RESOURCES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="$RESOURCES/app/requirements.txt"
VENV="${1:-${IPHONE_MIRROR_VENV:-$HOME/Library/Application Support/iPhone Mirror/venv}}"
STAMP="$VENV/.requirements.txt"

find_python() {
    local candidate name
    # Homebrew's python@3.12 (a dependency of the cask) first, then other recent versions
    for candidate in \
        /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 \
        /opt/homebrew/opt/python@3.13/bin/python3.13 /usr/local/opt/python@3.13/bin/python3.13 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13; do
        [ -x "$candidate" ] && { echo "$candidate"; return 0; }
    done
    for name in python3.12 python3.13 python3.11 python3.10 python3; do
        candidate="$(command -v "$name" 2>/dev/null)" || continue
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null && { echo "$candidate"; return 0; }
    done
    return 1
}

if [ -x "$VENV/bin/python3" ] && cmp -s "$REQUIREMENTS" "$STAMP" \
    && "$VENV/bin/python3" -c 'import PyQt6, pymobiledevice3' 2>/dev/null; then
    echo "iPhone Mirror: Python environment is up to date ($VENV)"
    exit 0
fi

PYTHON="$(find_python)" || {
    echo "error: Python 3.10 or newer is required. Install it with: brew install python@3.12" >&2
    exit 1
}

# Start over if the environment's interpreter is gone (e.g. its Python was uninstalled)
if [ -d "$VENV" ] && ! "$VENV/bin/python3" -c '' 2>/dev/null; then
    rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
    echo "iPhone Mirror: creating a Python environment with $("$PYTHON" --version) in $VENV"
    mkdir -p "$(dirname "$VENV")"
    "$PYTHON" -m venv "$VENV"
fi

echo "iPhone Mirror: installing dependencies (about a minute)…"
"$VENV/bin/python3" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$VENV/bin/python3" -m pip install --disable-pip-version-check --quiet -r "$REQUIREMENTS"

# Where libpython is, so the app can run Python in its own process (see launcher.c).
# Empty if this Python has no shared library; the app then runs the interpreter directly.
"$VENV/bin/python3" - > "$VENV/.libpython" <<'PYTHON'
import os
import sysconfig

library = sysconfig.get_config_var('LDLIBRARY') or ''
prefix = sysconfig.get_config_var('PYTHONFRAMEWORKPREFIX') or sysconfig.get_config_var('LIBDIR') or ''
path = os.path.join(prefix, library)
print(path if library and not library.endswith('.a') and os.path.exists(path) else '')
PYTHON

cp "$REQUIREMENTS" "$STAMP"
echo "iPhone Mirror: Python environment ready ($VENV)"
