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
# Do not depend on the host's global/user-site Pillow installation. A stale
# native Pillow extension there can make an otherwise self-contained package
# fail before the bundled runtime is created.
uv run --isolated --no-project --with pillow python scripts/create_studio_icon.py
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

# The wheel intentionally keeps the complete SDK dependency set for published
# library installs.  The desktop Studio has a narrower UI contract: Harness
# and Codex are the only built-in runtimes.  google-adk is still required by
# the shared MCP ToolContext compatibility layer, but ADK is not exposed as a
# selectable Studio runtime.  Remove packages that are only used by optional
# attachment OCR.  The OCR module is
# imported lazily and falls back to the system OCR path when it is unavailable;
# this removes roughly 240 MiB of native OpenCV/ONNX payload from the app.
uv pip uninstall --python "$STUDIO_APP_RUNTIME/bin/python" -y \
  rapidocr-onnxruntime opencv-python onnxruntime \
  2>/dev/null || true

# rapidocr 卸载后遗留的孤儿传递依赖（本地 Studio 无任何消费方），连同
# 冻结 runtime 里无用的 pip 一起移除；ksadk/google 全量源码扫描确认无
# "import numpy/shapely"（唯一引用点 google vertex code executor 不在
# Studio 路径上）。shapely 自身是唯一声明依赖 numpy 的包。
uv pip uninstall --python "$STUDIO_APP_RUNTIME/bin/python" -y \
  shapely numpy numpydoc pip \
  2>/dev/null || true

# DSH toolchain node_modules 只服务本机 arm64 运行：清理其他平台预编译
# 产物、调试符号与类型/源码映射（运行时永不读取），约减 ~100 MiB。
if [ -d "$STUDIO_APP_BUNDLE/Contents/Resources/plugin-toolchains" ]; then
  find "$STUDIO_APP_BUNDLE/Contents/Resources/plugin-toolchains" \
    \( -name "*.pdb" -o -name "*.d.ts" -o -name "*.tsbuildinfo" \
       -o -name "*.map" \) -type f -delete 2>/dev/null || true
  find "$STUDIO_APP_BUNDLE/Contents/Resources/plugin-toolchains" -type d \
    \( -name "win32-x64" -o -name "win32-arm64" -o -name "linux-x64" -o -name "linux-arm64" \
       -o -name "win32-ia32" -o -name "darwin-x64" \) \
    -exec rm -rf {} + 2>/dev/null || true
fi

cat > "$STUDIO_APP_BUNDLE/Contents/MacOS/AgentKitStudio" <<'LAUNCHER'
#!/bin/sh
set -eu
APP_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
export AGENTENGINE_PLUGIN_TOOLCHAIN_HOME="$APP_ROOT/Contents/Resources/plugin-toolchains"
export PATH="$APP_ROOT/Contents/Resources/node/bin:$PATH"
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
cp desktop-runtime.js "$STUDIO_APP_BUNDLE/Contents/Resources/app/desktop-runtime.js"
cp preload.js "$STUDIO_APP_BUNDLE/Contents/Resources/app/preload.js"

# Bundle a portable Node runtime and the exact pnpm version used by DSH.
# The Homebrew Node binary is not portable because it links to Homebrew
# libraries; the official release binary only uses macOS system frameworks.
node_version="${STUDIO_APP_NODE_VERSION:-22.19.0}"
node_cache=".cache/studio-node"
node_archive="$node_cache/node-v${node_version}-darwin-arm64.tar.gz"
node_root="$STUDIO_APP_BUNDLE/Contents/Resources/node"
if [ ! -f "$node_archive" ]; then
  mkdir -p "$node_cache"
  curl -fL --retry 3 --retry-delay 2 --retry-all-errors \
    "https://nodejs.org/dist/v${node_version}/node-v${node_version}-darwin-arm64.tar.gz" \
    -o "$node_archive"
fi
mkdir -p "$node_root"
tar -xzf "$node_archive" --strip-components=1 -C "$node_root"
# Headers, examples, and documentation are development-only and are not
# needed by the bundled DSH CLI.  Keep the portable node executable and npm
# runtime libraries only.
rm -rf "$node_root/include" "$node_root/share" \
  "$node_root/CHANGELOG.md" "$node_root/LICENSE" "$node_root/README.md"
pnpm_root="${STUDIO_APP_PNPM_ROOT:-$HOME/.cache/node/corepack/v1/pnpm/11.7.0}"
test -f "$pnpm_root/bin/pnpm.cjs" || {
  echo "ERROR: pnpm 11.7.0 cache is missing: $pnpm_root" >&2
  exit 1
}
mkdir -p "$node_root/lib/node_modules/pnpm"
cp -R "$pnpm_root/." "$node_root/lib/node_modules/pnpm/"
cat > "$node_root/bin/pnpm" <<'PNPM'
#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "$ROOT/bin/node" "$ROOT/lib/node_modules/pnpm/bin/pnpm.cjs" "$@"
PNPM
chmod 0755 "$node_root/bin/pnpm"
cat > "$node_root/bin/pnpx" <<'PNPX'
#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec "$ROOT/bin/node" "$ROOT/lib/node_modules/pnpm/bin/pnpx.cjs" "$@"
PNPX
chmod 0755 "$node_root/bin/pnpx"

# Bundle the exact pinned DSH CLI/Core runtime for offline provider registration.
dsh_source="${STUDIO_APP_DSH_ROOT:-$HOME/.agentengine/plugin-toolchains/dsh/0.1.5-rc.1}"
test -x "$dsh_source/node_modules/.bin/dsh" || {
  echo "ERROR: pinned DSH toolchain is missing: $dsh_source" >&2
  exit 1
}
mkdir -p "$STUDIO_APP_BUNDLE/Contents/Resources/plugin-toolchains/dsh"
rm -rf "$STUDIO_APP_BUNDLE/Contents/Resources/plugin-toolchains/dsh/0.1.5-rc.1"
cp -R "$dsh_source" "$STUDIO_APP_BUNDLE/Contents/Resources/plugin-toolchains/dsh/0.1.5-rc.1"
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
  "node_version": "$node_version",
  "pnpm_version": "11.7.0",
  "platform": "macos",
  "arch": "arm64",
  "source_commit": "$(git rev-parse HEAD 2>/dev/null || echo unavailable)",
  "ksadk_web_version": "${KSADK_WEB_VERSION:-0.3.8}"
}
MANIFEST

echo "Studio bundle staged at $STUDIO_APP_BUNDLE"
