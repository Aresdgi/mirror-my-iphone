#!/bin/bash
# One command to ship: if the app's code changed since the latest release, it commits your pending
# changes, bumps the patch version, builds, updates the cask, pushes and publishes the GitHub
# release. Then `brew upgrade --cask mirror-my-iphone` gets it. Does nothing if only the README,
# assets or other non-code files changed.
#
#   packaging/publish.sh ["commit message"]
#   packaging/publish.sh --minor | --major ["commit message"]     (bump more than the patch)
#
# Needs the GitHub CLI (`brew install gh`, then `gh auth login`).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BUMP=patch
case "${1:-}" in
    --minor) BUMP=minor; shift ;;
    --major) BUMP=major; shift ;;
esac
MESSAGE="${1:-}"

command -v gh >/dev/null || { echo "error: the GitHub CLI is missing: brew install gh" >&2; exit 1; }
[ "$(git branch --show-current)" = main ] || { echo "error: publish from the main branch" >&2; exit 1; }

# What ships in the app: the Python sources, requirements and packaging (not the cask file itself)
CODE=(':(glob)*.py' requirements.txt packaging ':!packaging/publish.sh' ':!packaging/release.sh')

TAG="$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || true)"
if [ -n "$TAG" ]; then
    CHANGED="$(git diff --name-only "$TAG" -- "${CODE[@]}"; git ls-files --others --exclude-standard -- "${CODE[@]}")"
else
    CHANGED=yes
fi
# version.py alone (bumped by hand) doesn't count as a change
CHANGED="$(printf '%s\n' "$CHANGED" | grep -v -x -e version.py -e '' || true)"
[ -n "$CHANGED" ] || { echo "No code changes since ${TAG:-the start}: nothing to publish."; exit 0; }
echo "Code changed since ${TAG:-the start}:"; printf '%s\n' "$CHANGED" | sed 's/^/  /'

# Commit pending changes (code and anything else, e.g. docs) before bumping
if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -m "${MESSAGE:-Update Mirror my iPhone}"
fi

# Bump version.py
CURRENT="$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" version.py)"
IFS=. read -r MAJOR MINOR PATCH <<< "$CURRENT"
case "$BUMP" in
    major) NEW="$((MAJOR + 1)).0.0" ;;
    minor) NEW="$MAJOR.$((MINOR + 1)).0" ;;
    patch) NEW="$MAJOR.$MINOR.$((PATCH + 1))" ;;
esac
# If a release for the current version doesn't exist yet, publish it as is
if gh release view "v$CURRENT" >/dev/null 2>&1; then
    sed -i '' "s/^__version__ = '.*'$/__version__ = '$NEW'/" version.py
    git commit -m "Bump version to $NEW" -- version.py
fi

RELEASE_YES=1 "$ROOT/packaging/release.sh"
