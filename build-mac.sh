#!/usr/bin/env bash
# Build OverlayMusic.app + .dmg for macOS distribution.
# Bundles `media-control` (ungive) for runtime now-playing access on macOS 15.4+.
set -euo pipefail

APP_NAME="OverlayMusic"
BUNDLE_ID="com.melanholy.overlaymusic"
DIST_DIR="dist"
BUILD_DIR="build"
APP_PATH="$DIST_DIR/$APP_NAME.app"
DMG_PATH="$DIST_DIR/$APP_NAME-arm64.dmg"

# Detect media-control install (for bundling)
MC_CELLAR=""
for d in /opt/homebrew/Cellar/media-control/*; do
  [ -d "$d" ] && MC_CELLAR="$d"
done
if [ -z "$MC_CELLAR" ]; then
  echo "ERROR: media-control not found in Homebrew. Run: brew install media-control" >&2
  exit 1
fi
echo "Bundling media-control from: $MC_CELLAR"

# Activate venv if it exists (local dev). In CI, system Python is already active.
if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

# Clean previous build artifacts
rm -rf "$BUILD_DIR" "$APP_PATH" "$DIST_DIR/$APP_NAME" "$DMG_PATH"

# PyInstaller build
pyinstaller --noconfirm --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --hidden-import _platform_mac \
  --exclude-module _platform_win \
  overlay.py

# Bundle media-control runtime
RES_DIR="$APP_PATH/Contents/Resources/media-control"
mkdir -p "$RES_DIR"
cp -R "$MC_CELLAR/bin"        "$RES_DIR/"
cp -R "$MC_CELLAR/lib"        "$RES_DIR/"
cp -R "$MC_CELLAR/Frameworks" "$RES_DIR/"

# Mark as background tray-only app (no Dock icon)
plutil -insert LSUIElement -bool true "$APP_PATH/Contents/Info.plist" 2>/dev/null \
  || plutil -replace LSUIElement -bool true "$APP_PATH/Contents/Info.plist"

# Re-codesign (ad-hoc) after Info.plist + Resources mutation
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --verbose=2 "$APP_PATH"
echo "App built: $APP_PATH"

# Build DMG
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg \
    --volname "$APP_NAME" \
    --window-size 540 360 \
    --icon-size 110 \
    --icon "$APP_NAME.app" 140 175 \
    --app-drop-link 400 175 \
    --no-internet-enable \
    "$DMG_PATH" "$APP_PATH"
else
  echo "create-dmg not installed — falling back to hdiutil (no fancy layout)"
  hdiutil create -volname "$APP_NAME" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"
fi

echo "DMG built: $DMG_PATH"
ls -lh "$DMG_PATH"
