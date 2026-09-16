#!/bin/sh
# Windows x64 Studio bundle builder. Mirrors package_studio_app.sh but uses
# the standard flat Electron layout (electron.exe renamed to AgentKitStudio.exe
# at the bundle root, Electron auto-discovers resources/app/) instead of the
# macOS nested Electron.app structure.
#
# Runs under Git Bash on a windows-latest GitHub Actions runner. Produces an
# UNSIGNED AgentKitStudio-Setup-x64.exe via NSIS; signing is delegated to
# SignPath in the release workflow.
set -eu

: "${STUDIO_APP_DIR:?STUDIO_APP_DIR is required}"
: "${STUDIO_APP_VERSION:?STUDIO_APP_VERSION is required}"

wheel="$(ls -t dist/ksadk-*.whl 2>/dev/null | head -1 || true)"
test -n "$wheel" || {
  echo "ERROR: no KsADK wheel found in dist/" >&2
  exit 1
}

bundle="$STUDIO_APP_DIR/AgentKitStudio"
resources="$bundle/resources"
app_dir="$resources/app"

rm -rf "$STUDIO_APP_DIR"
mkdir -p "$bundle" "$resources/runtime" "$app_dir" "$resources/node" \
  "$resources/plugin-toolchains/dsh"

# ── Python 3.13 embeddable (self-contained, relocatable) ──────────────
# The embeddable zip is the Windows equivalent of the macOS copy-python-stdlib
# step: it ships python.exe + python313.dll + Lib/ and is designed to be moved.
# It lacks pip/ensurepip, so we unlock site-packages via the ._pth file and
# bootstrap pip with get-pip.py.
python_version="${STUDIO_APP_PYTHON_VERSION:-3.13.1}"
python_cache=".cache/studio-python"
python_zip="$python_cache/python-$python_version-embed-amd64.zip"
if [ ! -f "$python_zip" ]; then
  mkdir -p "$python_cache"
  curl -fL --retry 3 --retry-delay 2 --retry-all-errors \
    "https://www.python.org/ftp/python/$python_version/python-$python_version-embed-amd64.zip" \
    -o "$python_zip"
fi
unzip -q -o "$python_zip" -d "$resources/runtime"
# Uncomment "import site" so pip/venv machinery works inside the embeddable.
pth_file="$resources/runtime/python313._pth"
if [ -f "$pth_file" ]; then
  sed -i 's/^#import site/import site/' "$pth_file"
fi
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$python_cache/get-pip.py"
"$resources/runtime/python.exe" "$python_cache/get-pip.py" --quiet
"$resources/runtime/python.exe" -m pip install --quiet "$wheel[codex]"
# Same slimming as the macOS build: drop OCR/opencv/onnx (~240 MiB) and
# orphaned numpy/shapely/pip. Best-effort; ignore if already absent.
"$resources/runtime/python.exe" -m pip uninstall -y \
  rapidocr-onnxruntime opencv-python onnxruntime \
  shapely numpy numpydoc pip 2>/dev/null || true
# litellm is not installed on Windows+py3.13 (pyproject.toml platform mark);
# if any consumer imports it lazily it falls back, but verify in CI.

# ── Electron win32-x64 (flat layout) ───────────────────────────────────
electron_cache=".cache/studio-electron"
electron_version="${ELECTRON_VERSION:-44.3.0}"
electron_zip="$electron_cache/electron-v${electron_version}-win32-x64.zip"
electron_dist="$electron_cache/electron-dist-win"
if [ ! -f "$electron_zip" ]; then
  mkdir -p "$electron_cache"
  curl -fL --retry 3 --retry-delay 2 --retry-all-errors \
    "https://github.com/electron/electron/releases/download/v${electron_version}/electron-v${electron_version}-win32-x64.zip" \
    -o "$electron_zip"
fi
if [ ! -x "$electron_dist/electron.exe" ]; then
  mkdir -p "$electron_dist"
  unzip -q -o "$electron_zip" -d "$electron_dist"
fi
# Copy Electron's runtime files to the bundle root, renaming the launcher exe.
# Copy ALL runtime payloads (pak/dat/bin/dll) rather than an enumerated list:
# Electron ships a variable set across versions (vk_swiftshader.dll, vulkan-1.dll,
# chrome_elf.dll, etc.) and missing one causes silent render/startup failures.
cp "$electron_dist/electron.exe" "$bundle/AgentKitStudio.exe"
for f in "$electron_dist"/*.pak "$electron_dist"/*.dat "$electron_dist"/*.bin \
         "$electron_dist"/*.dll; do
  [ -f "$f" ] && cp "$f" "$bundle/"
done
[ -d "$electron_dist/locales" ] && cp -R "$electron_dist/locales" "$bundle/locales"

# ── Node win-x64 + pinned pnpm ─────────────────────────────────────────
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
cp "$node_src/node.exe" "$resources/node/"
cp -R "$node_src/node_modules" "$resources/node/" 2>/dev/null || true
rm -rf "$resources/node/include"
pnpm_root="${STUDIO_APP_PNPM_ROOT:-$HOME/.cache/node/corepack/v1/pnpm/11.7.0}"
test -f "$pnpm_root/bin/pnpm.cjs" || {
  echo "ERROR: pnpm 11.7.0 cache is missing: $pnpm_root" >&2
  exit 1
}
mkdir -p "$resources/node/node_modules/pnpm"
cp -R "$pnpm_root/." "$resources/node/node_modules/pnpm/"
cat > "$resources/node/pnpm.cmd" <<'PNPM'
@echo off
setlocal
set "ROOT=%~dp0"
"%ROOT%node.exe" "%ROOT%node_modules\pnpm\bin\pnpm.cjs" %*
PNPM
cat > "$resources/node/pnpx.cmd" <<'PNPX'
@echo off
setlocal
set "ROOT=%~dp0"
"%ROOT%node.exe" "%ROOT%node_modules\pnpm\bin\pnpx.cjs" %*
PNPX

# ── DSH toolchain (win32-x64 prebuilds) ────────────────────────────────
# On macOS the toolchain is pre-built on the host; on Windows the runner
# re-installs it so optionalDependencies (sharp, node-addon-require-builtin)
# resolve their win32-x64 variants from the npm registry.
dsh_source="${STUDIO_APP_DSH_ROOT:-$HOME/.agentengine/plugin-toolchains/dsh/0.1.5-rc.1}"
# Sanity-check the toolchain installed. npm on Windows creates dsh.cmd (not a
# Unix `dsh` shim), so accept either; the real presence signal is the package dir.
if [ ! -d "$dsh_source/node_modules/@deepseek-ai/dsh" ]; then
  echo "ERROR: pinned DSH toolchain is missing or not Windows-ready: $dsh_source" >&2
  echo "       run 'npm install @deepseek-ai/dsh@0.1.5-rc.1' on a Windows host first." >&2
  exit 1
fi
rm -rf "$resources/plugin-toolchains/dsh/0.1.5-rc.1"
cp -R "$dsh_source" "$resources/plugin-toolchains/dsh/0.1.5-rc.1"
# Prune non-win32-x64 prebuilds + debug/type/map files (runtime never reads them).
find "$resources/plugin-toolchains" \
  \( -name "*.pdb" -o -name "*.d.ts" -o -name "*.tsbuildinfo" -o -name "*.map" \) \
  -type f -delete 2>/dev/null || true
find "$resources/plugin-toolchains" -type d \
  \( -name "darwin-arm64" -o -name "darwin-x64" -o -name "linux-x64" \
     -o -name "linux-arm64" -o -name "win32-arm64" -o -name "win32-ia32" \) \
  -exec rm -rf {} + 2>/dev/null || true

# ── App code + icon + manifest ────────────────────────────────────────
cp electron-main.js "$app_dir/main.js"
cp desktop-runtime.js "$app_dir/desktop-runtime.js"
cp preload.js "$app_dir/preload.js"
cat > "$app_dir/package.json" <<JSON
{"name":"agentkit-studio-shell","version":"$STUDIO_APP_VERSION","main":"main.js","dependencies":{"electron-updater":"${STUDIO_APP_UPDATER_VERSION:-6.8.9}"}}
JSON
# Install electron-updater into the bundled app (autoUpdater runtime dep).
( cd "$app_dir" && \
  npm install --omit=dev --no-audit --no-fund --no-package-lock \
    "electron-updater@${STUDIO_APP_UPDATER_VERSION:-6.8.9}" )
cp scripts/studio_icon.ico "$resources/AgentKitStudio.ico"
cat > "$resources/manifest.json" <<MANIFEST
{
  "product": "AgentKit Studio",
  "ksadk_version": "$STUDIO_APP_VERSION",
  "codex_version": "0.147.0",
  "electron_version": "$electron_version",
  "node_version": "$node_version",
  "pnpm_version": "11.7.0",
  "python_version": "$python_version",
  "platform": "windows",
  "arch": "x64",
  "source_commit": "$(git rev-parse HEAD 2>/dev/null || echo unavailable)",
  "ksadk_web_version": "${KSADK_WEB_VERSION:-0.3.8}"
}
MANIFEST

echo "Windows bundle staged at $bundle"

# ── NSIS installer ────────────────────────────────────────────────────
# makensis must be on PATH (workflow installs NSIS via chocolatey). NSIS
# expects Windows-style paths; bash variables are MSYS-style, so convert
# with cygpath. The .nsi packages the staged bundle into one unsigned .exe.
setup_exe="$STUDIO_APP_DIR/AgentKitStudio-Setup-x64.exe"
if ! command -v cygpath >/dev/null 2>&1; then
  echo "ERROR: cygpath not found; this script must run under Git Bash on Windows" >&2
  exit 1
fi
bundle_win=$(cygpath -w "$bundle")
out_win=$(cygpath -w "$setup_exe")
makensis -V2 -DAPP_VERSION="$STUDIO_APP_VERSION" \
  -DBUNDLE_DIR="$bundle_win" \
  -DOUTPUT_FILE="$out_win" \
  scripts/studio_installer.nsi

test -f "$setup_exe" || {
  echo "ERROR: NSIS did not produce $setup_exe" >&2
  exit 1
}
echo "Unsigned installer: $setup_exe"
