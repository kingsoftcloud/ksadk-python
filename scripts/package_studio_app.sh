#!/bin/sh
set -eu

: "${STUDIO_APP_DIR:?STUDIO_APP_DIR is required}"
: "${STUDIO_APP_RUNTIME:?STUDIO_APP_RUNTIME is required}"
: "${STUDIO_APP_BUNDLE:?STUDIO_APP_BUNDLE is required}"
: "${STUDIO_APP_PYTHON:?STUDIO_APP_PYTHON is required}"
: "${STUDIO_APP_VERSION:?STUDIO_APP_VERSION is required}"

wheel="$(ls -t dist/ksadk-*.whl 2>/dev/null | head -1 || true)"
test -n "$wheel" || {
  echo "ERROR: no KsADK wheel found in dist/" >&2
  exit 1
}

rm -rf "$STUDIO_APP_DIR"
mkdir -p "$STUDIO_APP_BUNDLE/Contents/MacOS" "$STUDIO_APP_RUNTIME"
python3 scripts/create_studio_icon.py
uv venv --python "$STUDIO_APP_PYTHON" "$STUDIO_APP_RUNTIME"

# uv may create interpreter symlinks into its user cache. Resolve and copy them
# into the bundle so the app remains self-contained on another Mac.
resolve_link() {
  link="$1"
  while [ -L "$link" ]; do
    target="$(readlink "$link")"
    case "$target" in
      /*) link="$target" ;;
      *) link="$(dirname "$link")/$target" ;;
    esac
  done
  printf '%s\n' "$link"
}
python_home=""
for interpreter in "$STUDIO_APP_RUNTIME"/bin/python*; do
  if [ -L "$interpreter" ]; then
    resolved="$(resolve_link "$interpreter")"
    if [ -z "$python_home" ]; then
      python_home="$(CDPATH= cd -- "$(dirname "$resolved")/.." && pwd)"
    fi
    rm "$interpreter"
    cp "$resolved" "$interpreter"
    chmod 0755 "$interpreter"
  fi
done

# Include the interpreter's shared library and standard library as well.
test -n "$python_home"
mkdir -p "$STUDIO_APP_RUNTIME/lib/python3.13"
cp -R "$python_home/lib/python3.13/." "$STUDIO_APP_RUNTIME/lib/python3.13/"
rm -f "$STUDIO_APP_RUNTIME/lib/python3.13/EXTERNALLY-MANAGED"
for dylib in "$python_home"/lib/libpython*.dylib; do
  test -f "$dylib" && cp "$dylib" "$STUDIO_APP_RUNTIME/lib/"
done

uv pip install --python "$STUDIO_APP_RUNTIME/bin/python" "$wheel[codex]"

cat > "$STUDIO_APP_BUNDLE/Contents/MacOS/AgentKitStudio" <<'LAUNCHER'
#!/bin/sh
set -eu
APP_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
WORKSPACE="${STUDIO_APP_WORKSPACE:-.}"
PORT="${STUDIO_APP_PORT:-8172}"
exec "$APP_ROOT/Contents/Resources/electron/Contents/MacOS/Electron" "$APP_ROOT/Contents/Resources/app" "$@"
LAUNCHER
chmod 0755 "$STUDIO_APP_BUNDLE/Contents/MacOS/AgentKitStudio"

# Bundle Electron itself from the official architecture-specific ZIP. This
# avoids npm postinstall policies and keeps the runtime reproducible offline.
electron_cache=".cache/studio-electron"
electron_version="${ELECTRON_VERSION:-44.3.0}"
electron_zip="$electron_cache/electron-v${electron_version}-darwin-arm64.zip"
electron_dist="$electron_cache/electron-dist"
if [ ! -f "$electron_zip" ]; then
  mkdir -p "$electron_cache"
  curl -fL --retry 3 --retry-delay 2 --retry-all-errors \
    "https://github.com/electron/electron/releases/download/v${electron_version}/electron-v${electron_version}-darwin-arm64.zip" \
    -o "$electron_zip"
fi
if [ ! -x "$electron_dist/Electron.app/Contents/MacOS/Electron" ]; then
  mkdir -p "$electron_dist"
  unzip -q -o "$electron_zip" -d "$electron_dist"
fi
mkdir -p "$STUDIO_APP_BUNDLE/Contents/Resources/electron" "$STUDIO_APP_BUNDLE/Contents/Resources/app"
cp -R "$electron_dist/Electron.app/." "$STUDIO_APP_BUNDLE/Contents/Resources/electron/"
cp electron-main.js "$STUDIO_APP_BUNDLE/Contents/Resources/app/main.js"
cat > "$STUDIO_APP_BUNDLE/Contents/Resources/app/package.json" <<'JSON'
{"name":"agentkit-studio-shell","version":"1.0.0","main":"main.js"}
JSON

cat > "$STUDIO_APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleDisplayName</key><string>AgentKit Studio</string>
<key>CFBundleExecutable</key><string>AgentKitStudio</string>
<key>CFBundleIconFile</key><string>AgentKitStudio.icns</string>
<key>CFBundleIconName</key><string>AgentKitStudio</string>
<key>CFBundleIdentifier</key><string>com.kingsoftcloud.agentkit.studio</string>
<key>CFBundleName</key><string>AgentKit Studio</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$STUDIO_APP_VERSION</string>
<key>CFBundleVersion</key><string>$STUDIO_APP_VERSION</string>
</dict></plist>
PLIST

# Keep the generated branded PNG available to the Finder bundle even when
# iconutil is unavailable on a CI host.
if [ -f "dist/studio-app/.iconset/icon_512x512.png" ]; then
  sips -s format icns "dist/studio-app/.iconset/icon_512x512.png" --out "$STUDIO_APP_BUNDLE/Contents/Resources/AgentKitStudio.icns" >/dev/null
fi
cat > "$STUDIO_APP_BUNDLE/Contents/Resources/manifest.json" <<MANIFEST
{
  "product": "AgentKit Studio",
  "ksadk_version": "$STUDIO_APP_VERSION",
  "codex_version": "0.147.0",
  "electron_version": "$electron_version",
  "platform": "macos",
  "arch": "arm64",
  "source_commit": "$(git rev-parse HEAD 2>/dev/null || echo unavailable)",
  "ksadk_web_version": "0.3.7"
}
MANIFEST

echo "Studio bundle staged at $STUDIO_APP_BUNDLE"
