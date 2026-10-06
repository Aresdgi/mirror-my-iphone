#!/bin/bash
# Builds "dist/Mirror my iPhone.app" (and a zip of it) from this checkout. Nothing is downloaded or published.
#
#   packaging/build_app.sh
#
# The bundle holds a small native launcher, the app's Python sources and its icon. Python and the
# dependencies live outside it, in a venv that bootstrap.sh creates on the first launch, in
# ~/Library/Application Support/Mirror my iPhone. Needs Xcode's command line tools.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGING="$ROOT/packaging"
DIST="$ROOT/dist"
APP="$DIST/Mirror my iPhone.app"
VERSION="$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" "$ROOT/version.py")"
BUNDLE_ID="$(sed -n "s/^BUNDLE_ID = '\(.*\)'$/\1/p" "$ROOT/paths.py")"
[ -n "$VERSION" ] && [ -n "$BUNDLE_ID" ] || { echo "error: can't read version or bundle ID" >&2; exit 1; }

echo "Building Mirror my iPhone $VERSION ($BUNDLE_ID)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app" "$APP/Contents/Resources/bin"

sed -e "s/__VERSION__/$VERSION/g" -e "s/__BUNDLE_ID__/$BUNDLE_ID/g" "$PACKAGING/Info.plist" > "$APP/Contents/Info.plist"
plutil -lint -s "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# Launcher, for Apple silicon and Intel
clang -O2 -Wall -Wextra -Werror -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 \
    -o "$APP/Contents/MacOS/Mirror my iPhone" "$PACKAGING/launcher.c"

# Icon
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ROOT/assets/AppIcon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$ROOT/assets/AppIcon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

# App sources and helper scripts
cp "$ROOT"/*.py "$ROOT/requirements.lock" "$ROOT/requirements-build.lock" "$APP/Contents/Resources/app/"
cp -R "$ROOT/wda-patches" "$APP/Contents/Resources/app/"
cp "$PACKAGING/bootstrap.sh" "$PACKAGING/setup.command" "$APP/Contents/Resources/"
cp "$PACKAGING/mirror-my-iphone" "$APP/Contents/Resources/bin/"
chmod +x "$APP/Contents/Resources/bootstrap.sh" "$APP/Contents/Resources/setup.command" \
    "$APP/Contents/Resources/bin/mirror-my-iphone"
xattr -cr "$APP"

# Ad-hoc signature: required for arm64 code. The app isn't notarized; built locally, it has no quarantine flag.
codesign --force --sign - --timestamp=none "$APP"
codesign --verify --strict "$APP"

ZIP="$DIST/Mirror-my-iPhone-$VERSION.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
echo "Built $APP"
echo "      $ZIP"
echo "sha256 $(shasum -a 256 "$ZIP" | cut -d' ' -f1)"
