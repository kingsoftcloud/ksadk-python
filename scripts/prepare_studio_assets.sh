#!/bin/sh
# Prepare studio assets for electron-builder.
# Outputs to build-resources/:
#   runtime/          — Python 3.13 venv with ksadk wheel + codex
#   node/             — Node.js + pnpm
#   plugin-toolchains/ — DSH toolchain
#   manifest.json     — build metadata
#   AgentKitStudio.icns — app icon
#
# electron-builder reads these as extraResources (not in asar) so native
# binaries (.dylib/.so/.node/.exe) stay on disk for dlopen + codesign.
set -eu

: "${STUDIO_APP_VERSION:?STUDIO_APP_VERSION is required}"
: "${STUDIO_APP_PYTHON:?STUDIO_APP_PYTHON is required (e.g. 3.13)}"

BUILD_RES="build-resources"
RUNTIME="$BUILD_RES/runtime"
NODE_ROOT="$BUILD_RES/node"
TOOLCHAINS="$BUILD_RES/plugin-toolchains"

wheel="$(ls -t dist/ksadk-*.whl 2>/dev/null | head -1 || true)"
test -n "$wheel" || {
  echo "ERROR: no KsADK wheel found in dist/" >&2
  exit 1
}

# Reject a stale or mixed wheel before touching the existing application bundle.
python3 scripts/reviewed_web_assets.py verify --wheel "$wheel" >/dev/null

rm -rf "$BUILD_RES"
mkdir -p "$RUNTIME" "$NODE_ROOT" "$TOOLCHAINS"

# ── Python 3.13 venv ──────────────────────────────────────────────────
uv venv --python "$STUDIO_APP_PYTHON" "$RUNTIME"

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
for interpreter in "$RUNTIME"/bin/python*; do
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

test -n "$python_home"
mkdir -p "$RUNTIME/lib/python3.13"
cp -R "$python_home/lib/python3.13/." "$RUNTIME/lib/python3.13/"
rm -f "$RUNTIME/lib/python3.13/EXTERNALLY-MANAGED"
for dylib in "$python_home"/lib/libpython*.dylib; do
  test -f "$dylib" && cp "$dylib" "$RUNTIME/lib/"
done

# Python.org's macOS `bin/python3` is a thin launcher. It starts the real
# interpreter at Resources/Python.app relative to the bundled libpython, so
# ship that app too; copying only bin/python3 and libpython makes the
# launcher resolve a path that does not exist inside the bundle.
if [ -d "$python_home/Resources/Python.app" ]; then
  mkdir -p "$RUNTIME/lib/Resources"
  cp -R "$python_home/Resources/Python.app" "$RUNTIME/lib/Resources/"
fi
# The framework interpreter's build-only library links target a top-level
# Python binary that is not shipped. Remove only these known broken links.
for build_link in "$RUNTIME"/lib/python3.13/config-*/libpython3.13.a \
                  "$RUNTIME"/lib/python3.13/config-*/libpython3.13.dylib; do
  if [ -L "$build_link" ] && [ ! -e "$build_link" ] && [ "$(readlink "$build_link")" = "../../../Python" ]; then
    rm "$build_link"
  fi
done

# A Python.org/framework interpreter records its framework path in the Mach-O
# load commands. On a clean Mac dyld would still look for the build host's
# /Library/Frameworks/Python.framework/... path and abort before Python
# starts. Relocate both sides of that pair before any code signing.
if command -v install_name_tool >/dev/null 2>&1 && command -v otool >/dev/null 2>&1; then
  python_library="$(find "$RUNTIME/lib" -maxdepth 1 -type f -name 'libpython*.dylib' -print -quit)"
  test -n "$python_library" || {
    echo "ERROR: bundled libpython dylib is missing" >&2
    exit 1
  }
  python_framework_path=""
  for interpreter in "$RUNTIME"/bin/python* \
                     "$RUNTIME/lib/Resources/Python.app/Contents/MacOS/Python"; do
    [ -f "$interpreter" ] || continue
    python_framework_path="$(otool -L "$interpreter" | awk '$1 ~ /Python\.framework\/Versions/ && $1 ~ /\/Python$/ { print $1; exit }')"
    [ -n "$python_framework_path" ] && break
  done
  if [ -n "$python_framework_path" ]; then
    bundled_python_name="$(basename "$python_library")"
    for interpreter in "$RUNTIME"/bin/python* \
                       "$RUNTIME/lib/Resources/Python.app/Contents/MacOS/Python"; do
      [ -f "$interpreter" ] || continue
      case "$interpreter" in
        */lib/Resources/Python.app/Contents/MacOS/Python)
          python_load_path="@loader_path/../../../../$bundled_python_name"
          ;;
        *)
          python_load_path="@loader_path/../lib/$bundled_python_name"
          ;;
      esac
      install_name_tool -change "$python_framework_path" \
        "$python_load_path" "$interpreter"
    done
    install_name_tool -id "@rpath/$bundled_python_name" "$python_library"
    # install_name_tool invalidates the Python.org signature. Re-sign the
    # modified interpreter so uv pip can execute it; the distribution
    # Developer ID pass replaces these ad-hoc signatures before notarization.
    if command -v codesign >/dev/null 2>&1; then
      for interpreter in "$RUNTIME"/bin/python* \
                         "$RUNTIME/lib/Resources/Python.app/Contents/MacOS/Python"; do
        [ -f "$interpreter" ] || continue
        codesign --force --sign - "$interpreter"
      done
      codesign --force --sign - "$python_library"
    fi
  fi
fi

# CPython resolves its standard-library prefix from the nearest pyvenv.cfg.
# Keep a second config beside libpython (the framework launcher resolves the
# real executable from that directory) and make both configs bundle-relative;
# otherwise a clean Mac falls back to the build host's Python.framework even
# after all Mach-O load commands have been relocated.
if [ -f "$RUNTIME/pyvenv.cfg" ]; then
  pyvenv_tmp="$RUNTIME/pyvenv.cfg.tmp"
  awk 'BEGIN { replaced = 0 } /^home[[:space:]]*=/ { print "home = ."; replaced = 1; next } { print } END { if (!replaced) print "home = ." }' \
    "$RUNTIME/pyvenv.cfg" > "$pyvenv_tmp"
  mv "$pyvenv_tmp" "$RUNTIME/pyvenv.cfg"
  cp "$RUNTIME/pyvenv.cfg" "$RUNTIME/lib/pyvenv.cfg"
fi

uv pip install --python "$RUNTIME/bin/python" "$wheel[codex]"

# Remove OCR + orphan deps (~240 MiB).
uv pip uninstall --python "$RUNTIME/bin/python" -y \
  rapidocr-onnxruntime opencv-python onnxruntime \
  shapely numpy numpydoc pip \
  2>/dev/null || true

# ── Node.js + pnpm ────────────────────────────────────────────────────
node_version="${STUDIO_APP_NODE_VERSION:-22.19.0}"
node_cache=".cache/studio-node"
node_archive="$node_cache/node-v${node_version}-darwin-arm64.tar.gz"
if [ ! -f "$node_archive" ]; then
  mkdir -p "$node_cache"
  curl -fL --retry 3 --retry-delay 2 --retry-all-errors \
    "https://nodejs.org/dist/v${node_version}/node-v${node_version}-darwin-arm64.tar.gz" \
    -o "$node_archive"
fi
tar -xzf "$node_archive" --strip-components=1 -C "$NODE_ROOT"
rm -rf "$NODE_ROOT/include" "$NODE_ROOT/share" \
  "$NODE_ROOT/CHANGELOG.md" "$NODE_ROOT/LICENSE" "$NODE_ROOT/README.md"

pnpm_version="${STUDIO_APP_PNPM_VERSION:-11.7.0}"
"$NODE_ROOT/bin/npm" install -g "pnpm@${pnpm_version}" --prefix "$NODE_ROOT" \
  --no-audit --no-fund --loglevel=error 2>&1 | tail -3

# ── DSH toolchain ─────────────────────────────────────────────────────
dsh_source="${STUDIO_APP_DSH_ROOT:-$HOME/.agentengine/plugin-toolchains/dsh/0.1.5-rc.1}"
test -d "$dsh_source/node_modules/@deepseek-ai/dsh" || {
  echo "ERROR: pinned DSH toolchain is missing: $dsh_source" >&2
  echo "       run 'uv run agentengine plugin toolchain install' first" >&2
  exit 1
}
mkdir -p "$TOOLCHAINS/dsh"
rm -rf "$TOOLCHAINS/dsh/0.1.5-rc.1"
cp -R "$dsh_source" "$TOOLCHAINS/dsh/0.1.5-rc.1"
# Prune non-darwin-arm64 prebuilds + debug/type/map files.
find "$TOOLCHAINS" \
  \( -name "*.pdb" -o -name "*.d.ts" -o -name "*.tsbuildinfo" -o -name "*.map" \) \
  -type f -delete 2>/dev/null || true
find "$TOOLCHAINS" -type d \
  \( -name "win32-x64" -o -name "win32-arm64" -o -name "linux-x64" \
     -o -name "linux-arm64" -o -name "darwin-x64" -o -name "win32-ia32" \) \
  -exec rm -rf {} + 2>/dev/null || true

# ── App code (electron-updater) ──────────────────────────────────────
npm install --omit=dev --no-audit --no-fund --no-package-lock \
  "electron-updater@${STUDIO_APP_UPDATER_VERSION:-6.8.9}" 2>&1 | tail -3

# ── Icon ──────────────────────────────────────────────────────────────
python3 scripts/create_studio_icon.py
cp dist/studio-app/.iconset/icon_256x256@2x.png "$BUILD_RES/" 2>/dev/null || true
# Generate .icns from the iconset if iconutil is available.
if command -v iconutil >/dev/null 2>&1; then
  iconutil -c icns dist/studio-app/.iconset -o "$BUILD_RES/AgentKitStudio.icns" 2>/dev/null || true
fi

# ── Manifest ──────────────────────────────────────────────────────────
cat > "$BUILD_RES/manifest.json" <<MANIFEST
{
  "product": "AgentKit Studio",
  "ksadk_version": "$STUDIO_APP_VERSION",
  "codex_version": "0.154.0",
  "node_version": "$node_version",
  "pnpm_version": "$pnpm_version",
  "python_version": "$STUDIO_APP_PYTHON",
  "platform": "macos",
  "arch": "arm64",
  "source_commit": "$(git rev-parse HEAD 2>/dev/null || echo unavailable)",
  "ksadk_web_version": "$(python3 scripts/reviewed_web_assets.py identity --field package_version)",
  "ksadk_web_source_commit": "$(python3 scripts/reviewed_web_assets.py identity --field source_commit)",
  "ksadk_web_artifact_sha256": "$(python3 scripts/reviewed_web_assets.py identity --field artifact_sha256)"
}
MANIFEST

echo "Studio assets prepared at $BUILD_RES"
