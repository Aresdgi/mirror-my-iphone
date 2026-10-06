#!/bin/bash
# Creates or updates the Python environment (a venv) Mirror my iPhone runs in.
#
#   bootstrap.sh [VENV]
#
# The Homebrew cask runs it after installing, with a venv in its Caskroom directory. Without an
# argument (setup.command, when the app finds no environment) it uses $MIRROR_MY_IPHONE_VENV or
# ~/Library/Application Support/Mirror my iPhone/venv.
set -euo pipefail

RESOURCES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="$RESOURCES/app/requirements.lock"
BUILD_REQUIREMENTS="$RESOURCES/app/requirements-build.lock"
VENV="${1:-${MIRROR_MY_IPHONE_VENV:-$HOME/Library/Application Support/Mirror my iPhone/venv}}"
STAMP="$VENV/.requirements.lock"

find_python() {
    local candidate name
    # requirements.lock is resolved for Python 3.12, so only 3.12 will do
    for candidate in \
        /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 \
        /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
        "$(command -v python3.12 2>/dev/null)"; do
        [ -n "$candidate" ] && [ -x "$candidate" ] \
            && "$candidate" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null \
            && { echo "$candidate"; return 0; }
    done
    return 1
}

if [ -x "$VENV/bin/python3" ] && cmp -s "$REQUIREMENTS" "$STAMP" \
    && "$VENV/bin/python3" -c 'import PyQt6, pymobiledevice3' 2>/dev/null; then
    echo "Mirror my iPhone: Python environment is up to date ($VENV)"
    exit 0
fi

PYTHON="$(find_python)" || {
    echo "error: Python 3.12 is required. Install it with: brew install python@3.12" >&2
    exit 1
}

# Start over if the environment's interpreter is gone (e.g. its Python was uninstalled)
if [ -d "$VENV" ] && ! "$VENV/bin/python3" -c '' 2>/dev/null; then
    rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
    echo "Mirror my iPhone: creating a Python environment with $("$PYTHON" --version) in $VENV"
    mkdir -p "$(dirname "$VENV")"
    "$PYTHON" -m venv "$VENV"
fi

# Only the exact files pinned in the lock files (by SHA-256) and nothing beyond them. Everything is a
# prebuilt wheel except hexdump, which exists only as source: it's built without build isolation, with
# the hash-checked setuptools from requirements-build.lock, so pip never fetches unpinned build tools.
echo "Mirror my iPhone: installing dependencies (about a minute)…"
"$VENV/bin/python3" -m pip install --disable-pip-version-check --quiet --require-hashes --no-deps --only-binary=:all: -r "$BUILD_REQUIREMENTS"
"$VENV/bin/python3" -m pip install --disable-pip-version-check --quiet --require-hashes --no-deps --only-binary=:all: --no-binary=hexdump \
    --no-build-isolation -r "$REQUIREMENTS"

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
echo "Mirror my iPhone: Python environment ready ($VENV)"
