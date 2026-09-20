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
# Python.org's macOS `bin/python3` is a thin launcher.  It starts the real
# interpreter at Resources/Python.app relative to the bundled libpython, so
# ship that app too; copying only bin/python3 and libpython makes the launcher
# resolve a path that does not exist inside the bundle.
if [ -d "$python_home/Resources/Python.app" ]; then
  mkdir -p "$STUDIO_APP_RUNTIME/lib/Resources"
  cp -R "$python_home/Resources/Python.app" "$STUDIO_APP_RUNTIME/lib/Resources/"
fi
# The framework interpreter's build-only library links target a top-level
# Python binary that is not shipped. Remove only these known broken links;
# strict verification below must reject other malformed bundle resources.
for build_link in "$STUDIO_APP_RUNTIME"/lib/python3.13/config-*/libpython3.13.a \
                  "$STUDIO_APP_RUNTIME"/lib/python3.13/config-*/libpython3.13.dylib; do
  if [ -L "$build_link" ] && [ ! -e "$build_link" ] && [ "$(readlink "$build_link")" = "../../../Python" ]; then
    rm "$build_link"
  fi
done
for dylib in "$python_home"/lib/libpython*.dylib; do
  test -f "$dylib" && cp "$dylib" "$STUDIO_APP_RUNTIME/lib/"
done

# A Python.org/framework interpreter records its framework path in the Mach-O
# load commands.  Copying the executable and libpython is not enough: on a
# clean Mac dyld would still look for the build host's
# /Library/Frameworks/Python.framework/... path and abort before Python starts.
# Relocate both sides of that pair before any code signing so the bundled
# interpreter can start without a system Python installation.
if command -v install_name_tool >/dev/null 2>&1 && command -v otool >/dev/null 2>&1; then
  python_library="$(find "$STUDIO_APP_RUNTIME/lib" -maxdepth 1 -type f -name 'libpython*.dylib' -print -quit)"
  test -n "$python_library" || {
    echo "ERROR: bundled libpython dylib is missing" >&2
    exit 1
  }
  python_framework_path=""
  for interpreter in "$STUDIO_APP_RUNTIME"/bin/python* \
                     "$STUDIO_APP_RUNTIME/lib/Resources/Python.app/Contents/MacOS/Python"; do
    [ -f "$interpreter" ] || continue
    python_framework_path="$(otool -L "$interpreter" | awk '$1 ~ /Python\.framework\/Versions/ && $1 ~ /\/Python$/ { print $1; exit }')"
    [ -n "$python_framework_path" ] && break
  done
  if [ -n "$python_framework_path" ]; then
    bundled_python_name="$(basename "$python_library")"
    for interpreter in "$STUDIO_APP_RUNTIME"/bin/python* \
                       "$STUDIO_APP_RUNTIME/lib/Resources/Python.app/Contents/MacOS/Python"; do
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
    # The first entry reported by otool -L for a dylib is its install ID.
    # Give the copied library a bundle-local identity instead of retaining the
    # framework path from the build machine.
    install_name_tool -id "@rpath/$bundled_python_name" "$python_library"
    # install_name_tool invalidates the Python.org signature.  Re-sign the
    # modified interpreter just long enough for uv pip to execute it; the
    # distribution Developer ID pass below replaces these ad-hoc signatures
    # before notarization.
    if command -v codesign >/dev/null 2>&1; then
      for interpreter in "$STUDIO_APP_RUNTIME"/bin/python* \
                         "$STUDIO_APP_RUNTIME/lib/Resources/Python.app/Contents/MacOS/Python"; do
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
if [ -f "$STUDIO_APP_RUNTIME/pyvenv.cfg" ]; then
  pyvenv_tmp="$STUDIO_APP_RUNTIME/pyvenv.cfg.tmp"
  awk 'BEGIN { replaced = 0 } /^home[[:space:]]*=/ { print "home = ."; replaced = 1; next } { print } END { if (!replaced) print "home = ." }' \
    "$STUDIO_APP_RUNTIME/pyvenv.cfg" > "$pyvenv_tmp"
  mv "$pyvenv_tmp" "$STUDIO_APP_RUNTIME/pyvenv.cfg"
  cp "$STUDIO_APP_RUNTIME/pyvenv.cfg" "$STUDIO_APP_RUNTIME/lib/pyvenv.cfg"
fi

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

# Studio never runs Python's bundled test harness, interactive IDLE, Tk demo,
# or ensurepip bootstrap.  Drop these development-only stdlib trees after all
# package installation is complete; they account for roughly 35 MiB in the
# shipped image without changing application imports.
for stdlib_tree in test idlelib turtledemo tkinter ensurepip; do
  rm -rf "$STUDIO_APP_RUNTIME/lib/python3.13/$stdlib_tree"
done

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

# Bundle Electron itself from the official architecture-specific ZIP. This
# avoids npm postinstall policies and keeps the runtime reproducible offline.
# We integrate Electron INTO our own .app (not nest it under Resources/) so
# that app.isPackaged is true and electron-updater runs. The shell launcher
# approach set process.defaultApp, which broke isPackaged and auto-update.
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
# Copy Electron's runtime payload into OUR bundle, renaming the main binary.
# Keep Frameworks (Electron Framework), Resources (locales), but drop
# Electron's own Info.plist and default app — we write our own below.
cp "$electron_dist/Electron.app/Contents/MacOS/Electron" "$STUDIO_APP_BUNDLE/Contents/MacOS/AgentKitStudio"
chmod 0755 "$STUDIO_APP_BUNDLE/Contents/MacOS/AgentKitStudio"
cp -R "$electron_dist/Electron.app/Contents/Frameworks" "$STUDIO_APP_BUNDLE/Contents/Frameworks"
cp -R "$electron_dist/Electron.app/Contents/Resources" "$STUDIO_APP_BUNDLE/Contents/Resources/electron-resources"
# Move locales to where Electron expects them (Contents/Resources/*.lproj).
if [ -d "$STUDIO_APP_BUNDLE/Contents/Resources/electron-resources" ]; then
  for lproj in "$STUDIO_APP_BUNDLE/Contents/Resources/electron-resources"/*.lproj; do
    [ -d "$lproj" ] && cp -R "$lproj" "$STUDIO_APP_BUNDLE/Contents/Resources/" 2>/dev/null || true
  done
  rm -rf "$STUDIO_APP_BUNDLE/Contents/Resources/electron-resources"
fi

# Stage app code under Contents/Resources/app and pack into app.asar.
# app.asar presence is part of what makes app.isPackaged true. We keep the
# unpacked tree too so electron-updater can write a new bundle on update,
# but Electron loads from app.asar when present.
app_dir="$STUDIO_APP_BUNDLE/Contents/Resources/app"
mkdir -p "$app_dir"
cp electron-main.js "$app_dir/main.js"
cp desktop-runtime.js "$app_dir/desktop-runtime.js"
cp preload.js "$app_dir/preload.js"

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
# Install pnpm into the bundled node using the node's own npm. Previously we
# copied from a corepack cache (~/.cache/node/corepack/...), but that path is
# only populated on developer machines — CI runners are clean and the copy
# failed. `npm install -g pnpm@<ver>` works everywhere node + npm exist.
pnpm_version="${STUDIO_APP_PNPM_VERSION:-11.7.0}"
"$node_root/bin/npm" install -g "pnpm@${pnpm_version}" --prefix "$node_root" --no-audit --no-fund --loglevel=error 2>&1 | tail -3
test -f "$node_root/lib/node_modules/pnpm/bin/pnpm.cjs" || {
  echo "ERROR: pnpm install failed" >&2
  exit 1
}
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
cat > "$STUDIO_APP_BUNDLE/Contents/Resources/app/package.json" <<JSON
{"name":"agentkit-studio-shell","version":"$STUDIO_APP_VERSION","main":"main.js","dependencies":{"electron-updater":"${STUDIO_APP_UPDATER_VERSION:-6.8.9}"}}
JSON

# Install electron-updater into the bundled app so autoUpdater is available at
# runtime. This is the app's own dependency, not the host's. --omit=dev keeps
# it lean; the bundle's node_modules ships alongside main.js.
( cd "$STUDIO_APP_BUNDLE/Contents/Resources/app" && \
  npm install --omit=dev --no-audit --no-fund --no-package-lock \
    "electron-updater@${STUDIO_APP_UPDATER_VERSION:-6.8.9}" )

# Pack app/ into app.asar. Electron checks for app.asar (not just app/) when
# deciding app.isPackaged, so this is required for electron-updater to run.
# Keep the unpacked app/ as well — electron-updater replaces the .app on
# update, and some tooling reads package.json from the unpacked tree.
( cd "$STUDIO_APP_BUNDLE/Contents/Resources" && \
  npx --yes @electron/asar pack app app.asar )

# app-update.yml is read by electron-updater when the app is packaged (the
# publish config normally injected by electron-builder). We hand-write it
# since we don't use electron-builder. STUDIO_APP_UPDATE_REPO can override the
# provider for internal distribution; default is GitHub.
update_repo_owner="kingsoftcloud"
update_repo_name="ksadk-python"
cat > "$app_dir/app-update.yml" <<YAML
provider: github
owner: ${STUDIO_APP_UPDATE_OWNER:-$update_repo_owner}
repo: ${STUDIO_APP_UPDATE_REPO_NAME:-$update_repo_name}
YAML

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
  "codex_version": "0.154.0",
  "electron_version": "$electron_version",
  "node_version": "$node_version",
  "pnpm_version": "11.7.0",
  "platform": "macos",
  "arch": "arm64",
  "source_commit": "$(git rev-parse HEAD 2>/dev/null || echo unavailable)",
  "ksadk_web_version": "${KSADK_WEB_VERSION:-0.3.10}"
}
MANIFEST

ENTITLEMENTS="${STUDIO_APP_ENTITLEMENTS:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/scripts/studio.entitlements}"

# Bytecode caches generated after signing break the sealed resources; purge
# them and keep the runtime from writing new ones past this point.
find "$STUDIO_APP_BUNDLE" -name '__pycache__' -type d -prune -exec rm -rf {} +
export PYTHONDONTWRITEBYTECODE=1

if [ -n "${STUDIO_APP_CODESIGN_IDENTITY:-}" ]; then
  echo "==> codesign (identity: $STUDIO_APP_CODESIGN_IDENTITY)"
  test -f "$ENTITLEMENTS" || { echo "ERROR: entitlements missing: $ENTITLEMENTS" >&2; exit 1; }
  # Sign Electron's nested helpers and frameworks explicitly, innermost-first.
  # codesign --deep is unreliable for Electron + Python: it misses dylibs
  # (libffmpeg, libpython) and skips secure timestamps / hardened runtime,
  # which Apple notarization rejects. Sign each binary with --timestamp and
  # --options runtime, then the top-level app last. Entitlements only apply to
  # main executable + helpers, not pure libraries.
  sign_with() {
    codesign --force --timestamp --options runtime --sign "$STUDIO_APP_CODESIGN_IDENTITY" "$@"
  }
  # Sign every Mach-O binary in the bundle, innermost-first. Notarization
  # rejects ANY unsigned Mach-O: the Python interpreter + libpython, 100+ C
  # extension .so under site-packages, pnpm/DSH .node modules, bundled CLI
  # binaries (codex, rg), and framework internals (libffmpeg.dylib). Detect by
  # magic number so the extension (.so/.dylib/.node/none) doesn't matter. This
  # runs before the helper/framework/app passes below so each bundle-level seal
  # covers already-signed contents.
  find "$STUDIO_APP_BUNDLE" -type f -size +0 -exec sh -c '
    identity="$1"; shift
    for f do
      magic=$(od -An -N4 -tx1 "$f" 2>/dev/null | tr -d " \n")
      case "$magic" in
        cffaedfe|cefaedfe|cafebabe|cafebabf|feedfacf|feedface)
          codesign --force --timestamp --options runtime --sign "$identity" "$f" || exit 1
          ;;
      esac
    done
  ' sh "$STUDIO_APP_CODESIGN_IDENTITY" {} +
  # Re-sign the bundled node with JIT entitlements. The generic pass above
  # signs it without entitlements, but node spawns from the hardened-runtime
  # Python process inherit the restricted flag — without allow-jit V8 cannot
  # mmap executable memory and dies with SIGTRAP before dsh --version runs.
  # Verified 2026-09-18: entitled node runs V8 fine under the app process tree.
  NODE_ENTITLEMENTS="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/node.entitlements"
  if [ -f "$STUDIO_APP_BUNDLE/Contents/Resources/node/bin/node" ] && [ -f "$NODE_ENTITLEMENTS" ]; then
    codesign --force --timestamp --options runtime \
      --entitlements "$NODE_ENTITLEMENTS" \
      --sign "$STUDIO_APP_CODESIGN_IDENTITY" \
      "$STUDIO_APP_BUNDLE/Contents/Resources/node/bin/node" || exit 1
  fi
  # Electron helper apps — apply entitlements so JIT/x86 work in sandbox.
  for helper in "$STUDIO_APP_BUNDLE/Contents/Frameworks/Electron Helper.app" \
                "$STUDIO_APP_BUNDLE/Contents/Frameworks/Electron Helper (GPU).app" \
                "$STUDIO_APP_BUNDLE/Contents/Frameworks/Electron Helper (Renderer).app" \
                "$STUDIO_APP_BUNDLE/Contents/Frameworks/Electron Helper (Plugin).app"; do
    [ -d "$helper" ] && sign_with --entitlements "$ENTITLEMENTS" "$helper"
  done
  # Frameworks — sign the framework version dir; no entitlements for pure libs.
  for fw in "$STUDIO_APP_BUNDLE/Contents/Frameworks/Electron Framework.framework" \
            "$STUDIO_APP_BUNDLE/Contents/Frameworks/Squirrel.framework" \
            "$STUDIO_APP_BUNDLE/Contents/Frameworks/Mantle.framework" \
            "$STUDIO_APP_BUNDLE/Contents/Frameworks/ReactiveObjC.framework"; do
    [ -d "$fw/Versions/Current" ] && sign_with "$fw/Versions/Current"
  done
  # Top-level app last, with entitlements.
  sign_with --entitlements "$ENTITLEMENTS" "$STUDIO_APP_BUNDLE"
  # Verify nested code and resource links before spending time on notarization.
  codesign --verify --deep --strict --verbose=2 "$STUDIO_APP_BUNDLE"
else
  echo "==> STUDIO_APP_CODESIGN_IDENTITY not set; leaving bundle unsigned" >&2
fi

if [ "${STUDIO_APP_NOTARIZE:-0}" = "1" ]; then
  : "${STUDIO_APP_CODESIGN_IDENTITY:?STUDIO_APP_NOTARIZE=1 requires STUDIO_APP_CODESIGN_IDENTITY}"
  zip_path="$STUDIO_APP_DIR/AgentKitStudio.zip"
  rm -f "$zip_path"
  ditto -c -k --keepParent "$STUDIO_APP_BUNDLE" "$zip_path"
  # Match Wegent's notarytool credential precedence: App Store Connect API Key
  # first (team-level, no personal Apple ID), then Apple ID + app-specific
  # password. The API Key path is what the shared kcwork team already uses.
  if [ -n "${APPLE_API_KEY:-}" ] && [ -n "${APPLE_API_KEY_ID:-}" ] && [ -n "${APPLE_API_ISSUER:-}" ]; then
    xcrun notarytool submit "$zip_path" \
      --key "$APPLE_API_KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER" \
      --wait
  else
    : "${APPLE_ID:?notarize requires APPLE_ID or APPLE_API_KEY/ID/ISSUER}"
    : "${APP_SPECIFIC_PASSWORD:?notarize requires APP_SPECIFIC_PASSWORD or API Key}"
    : "${TEAM_ID:?notarize requires TEAM_ID or API Key}"
    xcrun notarytool submit "$zip_path" \
      --apple-id "$APPLE_ID" \
      --password "$APP_SPECIFIC_PASSWORD" \
      --team-id "$TEAM_ID" --wait
  fi
  xcrun stapler staple "$STUDIO_APP_BUNDLE"
  xcrun stapler validate "$STUDIO_APP_BUNDLE"
fi

echo "Studio bundle staged at $STUDIO_APP_BUNDLE"
