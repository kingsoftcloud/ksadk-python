#!/bin/sh
# Prepare studio assets for electron-builder (Windows x64).
# Outputs to build-resources/ — same layout as the macOS prepare script but
# with Windows-specific binaries: Python embeddable, Node win-x64, DSH win32.
set -eu

: "${STUDIO_APP_VERSION:?STUDIO_APP_VERSION is required}"
: "${STUDIO_APP_PYTHON:?STUDIO_APP_PYTHON is required (e.g. 3.13.1)}"

BUILD_RES="build-resources"
RUNTIME="$BUILD_RES/runtime"
NODE_ROOT="$BUILD_RES/node"
TOOLCHAINS="$BUILD_RES/plugin-toolchains"

wheel="$(ls -t dist/ksadk-*.whl 2>/dev/null | head -1 || true)"
test -n "$wheel" || {
  echo "ERROR: no KsADK wheel found in dist/" >&2
  exit 1
}

rm -rf "$BUILD_RES"
mkdir -p "$RUNTIME" "$NODE_ROOT" "$TOOLCHAINS/dsh"

# ── Python embeddable (Windows) ────────────────────────────────────────
python_version="${STUDIO_APP_PYTHON:-3.13.1}"
python_cache=".cache/studio-python"
python_zip="$python_cache/python-$python_version-embed-amd64.zip"
if [ ! -f "$python_zip" ]; then
  mkdir -p "$python_cache"
  curl -fL --retry 3 --retry-delay 2 --retry-all-errors \
    "https://www.python.org/ftp/python/$python_version/python-$python_version-embed-amd64.zip" \
    -o "$python_zip"
fi
unzip -q -o "$python_zip" -d "$RUNTIME"
# Unlock site-packages for pip.
pth_file="$RUNTIME/python313._pth"
if [ -f "$pth_file" ]; then
  sed -i 's/^#import site/import site/' "$pth_file"
fi
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$python_cache/get-pip.py"
"$RUNTIME/python.exe" "$python_cache/get-pip.py" --quiet
# Embeddable Python lacks setuptools; install it before wheel (some deps
# have build dependencies that need setuptools.build_meta).
"$RUNTIME/python.exe" -m pip install --quiet setuptools wheel
"$RUNTIME/python.exe" -m pip install --quiet "$wheel[codex]"
"$RUNTIME/python.exe" -m pip uninstall -y \
  rapidocr-onnxruntime opencv-python onnxruntime \
  shapely numpy numpydoc pip 2>/dev/null || true

# ── Node.js win-x64 + pnpm ─────────────────────────────────────────────
node_version="${STUDIO_APP_NODE_VERSION:-22.19.0}"
node_cache=".cache/studio-node"
node_archive="$node_cache/node-v${node_version}-win-x64.zip"
if [ ! -f "$node_archive" ]; then
  mkdir -p "$node_cache"
  curl -fL --retry 3 --retry-delay 2 --retry-all-errors \
    "https://nodejs.org/dist/v${node_version}/node-v${node_version}-win-x64.zip" \
    -o "$node_archive"
fi
unzip -q -o "$node_archive" -d "$node_cache/node-extract"
node_src="$node_cache/node-extract/node-v${node_version}-win-x64"
cp "$node_src/node.exe" "$NODE_ROOT/"
cp -R "$node_src/node_modules" "$NODE_ROOT/" 2>/dev/null || true
pnpm_version="${STUDIO_APP_PNPM_VERSION:-11.7.0}"
"$NODE_ROOT/node.exe" "$NODE_ROOT/node_modules/npm/bin/npm-cli.js" install -g "pnpm@${pnpm_version}" --prefix "$NODE_ROOT" --no-audit --no-fund --loglevel=error 2>&1 | tail -3

# ── DSH toolchain (win32-x64 prebuilds) ────────────────────────────────
dsh_source="${STUDIO_APP_DSH_ROOT:-$HOME/.agentengine/plugin-toolchains/dsh/0.1.5-rc.1}"
if [ ! -d "$dsh_source/node_modules/@deepseek-ai/dsh" ]; then
  echo "ERROR: pinned DSH toolchain is missing or not Windows-ready: $dsh_source" >&2
  exit 1
fi
rm -rf "$TOOLCHAINS/dsh/0.1.5-rc.1"
cp -R "$dsh_source" "$TOOLCHAINS/dsh/0.1.5-rc.1"
find "$TOOLCHAINS" \
  \( -name "*.pdb" -o -name "*.d.ts" -o -name "*.tsbuildinfo" -o -name "*.map" \) \
  -type f -delete 2>/dev/null || true
find "$TOOLCHAINS" -type d \
  \( -name "darwin-arm64" -o -name "darwin-x64" -o -name "linux-x64" \
     -o -name "linux-arm64" -o -name "win32-arm64" -o -name "win32-ia32" \) \
  -exec rm -rf {} + 2>/dev/null || true

# ── electron-updater ───────────────────────────────────────────────────
npm install --omit=dev --no-audit --no-fund --no-package-lock \
  "electron-updater@${STUDIO_APP_UPDATER_VERSION:-6.8.9}" 2>&1 | tail -3

# ── Icon + manifest ────────────────────────────────────────────────────
python3 scripts/create_studio_icon.py
cp scripts/studio_icon.ico "$BUILD_RES/AgentKitStudio.ico" 2>/dev/null || true

cat > "$BUILD_RES/manifest.json" <<MANIFEST
{
  "product": "AgentKit Studio",
  "ksadk_version": "$STUDIO_APP_VERSION",
  "codex_version": "0.154.0",
  "node_version": "$node_version",
  "pnpm_version": "$pnpm_version",
  "python_version": "$python_version",
  "platform": "windows",
  "arch": "x64",
  "source_commit": "$(git rev-parse HEAD 2>/dev/null || echo unavailable)",
  "ksadk_web_version": "${KSADK_WEB_VERSION:-0.3.13}"
}
MANIFEST

echo "Windows studio assets prepared at $BUILD_RES"
