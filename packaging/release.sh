#!/bin/bash
# Publishes the version in version.py: builds the zip, points Casks/mirror-my-iphone.rb at it,
# commits and pushes that, and creates the GitHub release vX.Y.Z with the zip attached.
# After that, `brew upgrade --cask mirror-my-iphone` picks it up.
#
#   1. bump __version__ in version.py and commit
#   2. packaging/release.sh
#
# Needs the GitHub CLI (`brew install gh`, then `gh auth login`).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CASK="$ROOT/Casks/mirror-my-iphone.rb"
cd "$ROOT"

command -v gh >/dev/null || { echo "error: the GitHub CLI is missing: brew install gh" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "error: commit or stash your changes first" >&2; exit 1; }

"$ROOT/packaging/build_app.sh"
VERSION="$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" "$ROOT/version.py")"
ZIP="$ROOT/dist/Mirror-my-iPhone-$VERSION.zip"
SHA="$(shasum -a 256 "$ZIP" | cut -d' ' -f1)"

if gh release view "v$VERSION" >/dev/null 2>&1; then
    echo "error: release v$VERSION already exists — bump the version in version.py" >&2
    exit 1
fi

sed -i '' -e "s/^  version \".*\"/  version \"$VERSION\"/" -e "s/^  sha256 \".*\"/  sha256 \"$SHA\"/" "$CASK"
git diff --stat
read -r -p "Commit the cask, push, and publish release v$VERSION on GitHub? [y/N] " answer
[ "$answer" = y ] || [ "$answer" = Y ] || { git checkout -- "$CASK"; echo "Cancelled."; exit 1; }

git commit -m "Mirror my iPhone $VERSION" -- "$CASK"
git push
gh release create "v$VERSION" "$ZIP" --target "$(git rev-parse HEAD)" \
    --title "Mirror my iPhone $VERSION" --generate-notes
echo "Published. Install with: brew tap bhuwanadhikari/mirror-my-iphone https://github.com/bhuwanadhikari/mirror-my-iphone && brew trust bhuwanadhikari/mirror-my-iphone && brew install --cask mirror-my-iphone"
