#!/bin/sh
# Sign all Mach-O binaries in managed resources, re-sign the app, notarize.
# Called by after-sign.cjs (delegates to shell to avoid Node async issues).
set -eu

APP_PATH="$1"
OUT_DIR="$2"
IDENTITY="${STUDIO_APP_CODESIGN_IDENTITY:?required}"
ENTITLEMENTS="$3"
NODE_ENTITLEMENTS="$4"
NOTARIZE="${STUDIO_APP_NOTARIZE:-0}"

RESOURCES="$APP_PATH/Contents/Resources"
RUNTIME="$RESOURCES/runtime"
NODE_ROOT="$RESOURCES/node"
TOOLCHAINS="$RESOURCES/plugin-toolchains"

echo "[after-sign] signing Mach-O binaries in managed resources..."
for dir in "$RUNTIME" "$NODE_ROOT" "$TOOLCHAINS"; do
  [ -d "$dir" ] || continue
  find "$dir" -type f -exec file {} \; 2>/dev/null | grep 'Mach-O' | cut -d: -f1 | while read -r f; do
    codesign --force --timestamp --options runtime --sign "$IDENTITY" "$f" 2>/dev/null || true
  done
  echo "[after-sign] done: $(basename "$dir")"
done

echo "[after-sign] re-signing node with JIT entitlements..."
[ -f "$NODE_ROOT/bin/node" ] && codesign --force --timestamp --options runtime \
  --entitlements "$NODE_ENTITLEMENTS" --sign "$IDENTITY" "$NODE_ROOT/bin/node" 2>/dev/null || true

echo "[after-sign] re-signing app seal..."
codesign --force --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$APP_PATH"

if [ "$NOTARIZE" = "1" ]; then
  echo "[after-sign] notarizing..."
  ZIP="$OUT_DIR/AgentKitStudio-notary.zip"
  ditto -c -k --keepParent "$APP_PATH" "$ZIP"
  xcrun notarytool submit "$ZIP" \
    --apple-id "$APPLE_ID" \
    --password "$APP_SPECIFIC_PASSWORD" \
    --team-id "$TEAM_ID" --wait
  echo "[after-sign] stapling..."
  xcrun stapler staple "$APP_PATH"
  xcrun stapler validate "$APP_PATH"
  rm -f "$ZIP"
fi

echo "[after-sign] complete"
