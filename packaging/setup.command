#!/bin/bash
# Opened in Terminal by the app when its Python environment is missing or out of date.
# Builds it with bootstrap.sh, then starts the app again.

RESOURCES="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$RESOURCES/../.." && pwd)"

clear
echo "iPhone Mirror needs to finish setting up (one time, about a minute)."
echo
if "$RESOURCES/bootstrap.sh"; then
    echo
    echo "Done — starting iPhone Mirror. You can close this window."
    open "$APP"
else
    echo
    echo "Setup failed — see the messages above. Python 3.10+ is required:"
    echo "  brew install python@3.12"
    echo
    read -n 1 -s -r -p "Press any key to close."
    echo
fi
