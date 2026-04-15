#!/usr/bin/env bash
set -euo pipefail

# OpenClaw 运行时启动脚本
# 这个脚本主要负责四件事：
# 1. 把镜像内置的插件 / skills 同步到用户挂载的 ~/.openclaw
# 2. 应用我们对上游 OpenClaw 的兼容补丁
# 3. 生成并修正 openclaw.json / exec-approvals.json / .env
# 4. 最后以当前环境启动 gateway
#
# 设计原则：
# - 用户目录优先：如果用户已经手动修改过挂载目录，尽量不覆盖
# - 国内优先：运行时默认给 npm / pip / Playwright 配置国内镜像
# - 启动可观测：关键阶段输出中文日志，方便排查卡点

STATE_DIR="${OPENCLAW_STATE_DIR:-${HOME:-/root}/.openclaw}"
CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${STATE_DIR}/openclaw.json}"
BOOTSTRAP_MARKER="${STATE_DIR}/.bootstrapped"
PUBLIC_PORT="${OPENCLAW_PUBLIC_PORT:-19089}"
BIND_MODE="${OPENCLAW_GATEWAY_BIND:-lan}"
AUTH_MODE="trusted-proxy"
BROWSER_EXECUTABLE_DEFAULT="/usr/bin/chromium"
WORKSPACE_DIR="${OPENCLAW_WORKSPACE_DIR:-${STATE_DIR}/workspace}"
SAFE_BIN_DIR="${OPENCLAW_SAFE_BIN_DIR:-/opt/openclaw/safe-bin}"
PRESET_SKILLS_DIR="${OPENCLAW_PRESET_SKILLS_DIR:-/opt/openclaw/preset-skills}"
DEFAULT_EXTENSIONS_DIR="${OPENCLAW_DEFAULT_EXTENSIONS_DIR:-/opt/openclaw/default-extensions}"
WORKSPACE_TEMPLATE_DIR="${OPENCLAW_WORKSPACE_TEMPLATE_DIR:-/opt/openclaw/workspace-template}"
RUNTIME_DIST_DIR="${OPENCLAW_DIST_DIR:-/app/dist}"
BOOTSTRAP_CACHE_DIR="${OPENCLAW_BOOTSTRAP_CACHE_DIR:-${STATE_DIR}/.bootstrap-cache}"
DIST_PATCH_MARKER_VERSION="${OPENCLAW_DIST_PATCH_MARKER_VERSION:-2026.3.19.2}"
DIST_PATCH_MARKER="${OPENCLAW_DIST_PATCH_MARKER:-${RUNTIME_DIST_DIR}/.agentengine-dist-patched-${DIST_PATCH_MARKER_VERSION}}"
OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_PACKAGE_NAME="${OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_PACKAGE_NAME:-@tencent-weixin/openclaw-weixin}"
OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_MIN_VERSION="${OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_MIN_VERSION:-${OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_VERSION:-2.1.7}}"
export PATH="${HOME:-/root}/.local/bin:${PATH}"

# -----------------------------------------------------------------------------
# 基础工具函数
# -----------------------------------------------------------------------------

is_truthy() {
  local raw
  raw="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "${raw}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

bootstrap_now_seconds() {
  date +%s
}

bootstrap_log() {
  printf '[bootstrap] %s\n' "$*" >&2
}

bootstrap_phase() {
  bootstrap_log "=== $* ==="
}

start_gateway_process() {
  node openclaw.mjs gateway run --allow-unconfigured --bind "${BIND_MODE}" --port "${GATEWAY_PORT}" --auth "${AUTH_MODE}" &
  GATEWAY_CHILD_PID=$!
  set +e
  wait "${GATEWAY_CHILD_PID}"
  local exit_code=$?
  set -e
  GATEWAY_CHILD_PID=""
  return "${exit_code}"
}

forward_gateway_shutdown() {
  GATEWAY_SHUTDOWN_REQUESTED="true"
  if [[ -n "${GATEWAY_CHILD_PID:-}" ]]; then
    kill -TERM "${GATEWAY_CHILD_PID}" 2>/dev/null || true
    wait "${GATEWAY_CHILD_PID}" 2>/dev/null || true
    GATEWAY_CHILD_PID=""
  fi
}

# -----------------------------------------------------------------------------
# 浏览器与命令路径探测
# -----------------------------------------------------------------------------

configure_browser_executable() {
  local resolved=""
  local source_label=""

  if [[ -n "${OPENCLAW_BROWSER_EXECUTABLE_PATH:-}" && -x "${OPENCLAW_BROWSER_EXECUTABLE_PATH}" ]]; then
    resolved="${OPENCLAW_BROWSER_EXECUTABLE_PATH}"
    source_label="OPENCLAW_BROWSER_EXECUTABLE_PATH"
  elif [[ -n "${OPENCLAW_BROWSER_EXECUTABLE:-}" && -x "${OPENCLAW_BROWSER_EXECUTABLE}" ]]; then
    resolved="${OPENCLAW_BROWSER_EXECUTABLE}"
    source_label="OPENCLAW_BROWSER_EXECUTABLE"
  elif [[ -n "${AGENT_BROWSER_EXECUTABLE_PATH:-}" && -x "${AGENT_BROWSER_EXECUTABLE_PATH}" ]]; then
    resolved="${AGENT_BROWSER_EXECUTABLE_PATH}"
    source_label="AGENT_BROWSER_EXECUTABLE_PATH"
  else
    resolved="$(resolve_system_browser_executable || true)"
    source_label="system-browser"
  fi

  if [[ -n "${resolved}" && -x "${resolved}" ]]; then
    export OPENCLAW_BROWSER_EXECUTABLE_PATH="${resolved}"
    export AGENT_BROWSER_EXECUTABLE_PATH="${AGENT_BROWSER_EXECUTABLE_PATH:-${resolved}}"
    bootstrap_log "已解析浏览器可执行文件: ${resolved} (来源: ${source_label})"
    return 0
  fi

  bootstrap_log "未解析到浏览器可执行文件；agent-browser 仍可使用远端 provider，或后续显式传入 --executable-path"
  return 1
}

resolve_system_browser_executable() {
  resolve_allowlisted_command_path \
    chromium \
    "${BROWSER_EXECUTABLE_DEFAULT}" \
    /usr/bin/chromium-browser \
    /usr/bin/google-chrome \
    /usr/bin/google-chrome-stable \
    /usr/local/bin/chromium \
    /usr/local/bin/chromium-browser \
    /usr/local/bin/google-chrome \
    /usr/local/bin/google-chrome-stable
}

resolve_allowlisted_command_path() {
  local command_name="$1"
  shift || true
  local candidate

  for candidate in "$@"; do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  candidate="$(command -v "${command_name}" 2>/dev/null || true)"
  if [[ -n "${candidate}" && "${candidate}" == /* && -x "${candidate}" ]]; then
    printf '%s\n' "${candidate}"
    return 0
  fi

  return 1
}

resolve_preset_skills_allowlist() {
  local allowlist_raw="${OPENCLAW_PRESET_SKILLS_ALLOWLIST:-}"

  if [[ -n "${allowlist_raw}" ]]; then
    printf '%s\n' "${allowlist_raw}"
    return 0
  fi

  allowlist_raw="clawhub-store,agent-browser-clawdbot,kdocs,wps365-skill"
  # self-improving-agent：仅严格模式
  is_truthy "${OPENCLAW_EXEC_STRICT_MODE:-false}" && allowlist_raw="${allowlist_raw},self-improving-agent"
  # tuanziguardianclaw：仅严格模式保留，宽松模式默认不内置
  is_truthy "${OPENCLAW_EXEC_STRICT_MODE:-false}" && allowlist_raw="${allowlist_raw},tuanziguardianclaw"
  printf '%s\n' "${allowlist_raw}"
}

# -----------------------------------------------------------------------------
# 预置 skills / 插件同步
# -----------------------------------------------------------------------------

sync_preset_skills() {
  local src_dir="${PRESET_SKILLS_DIR}"
  local dst_dir="${STATE_DIR}/skills"
  local sig_dir="${BOOTSTRAP_CACHE_DIR}/preset-skills"
  local item
  local skill_name
  local skill_dst
  local allowlist_raw
  allowlist_raw="$(resolve_preset_skills_allowlist)"
  local existing
  local src_signature
  local dst_signature
  local previous_signature
  local signature_file
  local legacy_src_signature

  [[ -d "${src_dir}" ]] || return 0

  mkdir -p "${BOOTSTRAP_CACHE_DIR}" "${dst_dir}" "${sig_dir}"

  for existing in "${dst_dir}"/*; do
    [[ -d "${existing}" ]] || continue
    skill_name="$(basename "${existing}")"
    case ",${allowlist_raw}," in
      *,"${skill_name}",*)
        ;;
      *)
        signature_file="${sig_dir}/${skill_name}.sig"
        previous_signature=""
        legacy_src_signature=""
        if [[ -f "${signature_file}" ]]; then
          previous_signature="$(cat "${signature_file}" 2>/dev/null || true)"
        fi
        dst_signature="$(compute_directory_content_signature "${existing}" 2>/dev/null || true)"
        if [[ -n "${previous_signature}" && -n "${dst_signature}" && "${dst_signature}" == "${previous_signature}" ]]; then
          rm -rf "${existing}"
          rm -f "${signature_file}"
          bootstrap_log "removed deprecated bundled skill ${skill_name}"
          continue
        fi
        if [[ -z "${previous_signature}" && -d "${src_dir}/${skill_name}" ]]; then
          legacy_src_signature="$(compute_directory_content_signature "${src_dir}/${skill_name}" 2>/dev/null || true)"
        fi
        if [[ -n "${legacy_src_signature}" && -n "${dst_signature}" && "${dst_signature}" == "${legacy_src_signature}" ]]; then
          rm -rf "${existing}"
          bootstrap_log "removed deprecated bundled skill ${skill_name} (legacy source match)"
          continue
        fi
        bootstrap_log "preserved user-managed skill ${skill_name}"
        ;;
    esac
  done

  for item in "${src_dir}"/*; do
    [[ -d "${item}" ]] || continue
    skill_name="$(basename "${item}")"
    case ",${allowlist_raw}," in
      *,"${skill_name}",*)
        ;;
      *)
        continue
        ;;
    esac
    skill_dst="${dst_dir}/${skill_name}"
    signature_file="${sig_dir}/${skill_name}.sig"
    src_signature="$(compute_directory_content_signature "${item}" || true)"
    previous_signature=""
    if [[ -f "${signature_file}" ]]; then
      previous_signature="$(cat "${signature_file}" 2>/dev/null || true)"
    fi

    if [[ ! -e "${skill_dst}" ]]; then
      replace_directory_with_copy "${item}" "${skill_dst}"
      if [[ -n "${src_signature}" ]]; then
        printf '%s\n' "${src_signature}" > "${signature_file}"
      fi
      bootstrap_log "seeded bundled skill ${skill_name}"
      continue
    fi

    dst_signature="$(compute_directory_content_signature "${skill_dst}" 2>/dev/null || true)"
    if [[ -z "${dst_signature}" ]]; then
      replace_directory_with_copy "${item}" "${skill_dst}"
      if [[ -n "${src_signature}" ]]; then
        printf '%s\n' "${src_signature}" > "${signature_file}"
      fi
      bootstrap_log "synced bundled skill ${skill_name}"
      continue
    fi

    if [[ -n "${src_signature}" && "${src_signature}" == "${dst_signature}" ]]; then
      printf '%s\n' "${src_signature}" > "${signature_file}"
      continue
    fi

    if [[ -n "${previous_signature}" && "${dst_signature}" == "${previous_signature}" ]]; then
      replace_directory_with_copy "${item}" "${skill_dst}"
      if [[ -n "${src_signature}" ]]; then
        printf '%s\n' "${src_signature}" > "${signature_file}"
      fi
      bootstrap_log "upgraded bundled skill ${skill_name}"
      continue
    fi

    bootstrap_log "preserved user-managed skill ${skill_name}"
  done
  bootstrap_log "reconciled preset skills allowlist: ${allowlist_raw}"
}

compute_directory_content_signature() {
  local dir_path="$1"
  local file_path
  local rel_path

  [[ -d "${dir_path}" ]] || return 1

  find "${dir_path}" \( -type f -o -type l \) | LC_ALL=C sort | while IFS= read -r file_path; do
    rel_path="${file_path#"${dir_path}/"}"
    if [[ -L "${file_path}" ]]; then
      printf 'link\t%s\t%s\n' "${rel_path}" "$(readlink "${file_path}")"
      continue
    fi
    printf 'file\t%s\t' "${rel_path}"
    cksum "${file_path}" | awk '{print $1 "\t" $2}'
  done | cksum | awk '{print $1 ":" $2}'
}

read_embedded_directory_signature() {
  local dir_path="$1"
  local sig_path="${dir_path}/.content.sig"

  [[ -f "${sig_path}" ]] || return 1

  tr -d '\r\n' < "${sig_path}"
}

directory_has_changes_since() {
  local reference_path="$1"
  local dir_path="$2"
  local changed_path

  [[ -f "${reference_path}" && -d "${dir_path}" ]] || return 0

  changed_path="$(find "${dir_path}" \( -type f -o -type l \) -newer "${reference_path}" -print -quit 2>/dev/null || true)"
  [[ -n "${changed_path}" ]]
}

replace_directory_with_copy() {
  local src_dir="$1"
  local dst_dir="$2"
  local dst_parent
  local tmp_dir

  dst_parent="$(dirname "${dst_dir}")"
  mkdir -p "${dst_parent}"
  tmp_dir="$(mktemp -d "${dst_parent}/.sync-$(basename "${dst_dir}").XXXXXX")"
  rmdir "${tmp_dir}"
  cp -R "${src_dir}" "${tmp_dir}"
  rm -rf "${dst_dir}"
  mv "${tmp_dir}" "${dst_dir}"
}

sync_default_extensions() {
  local src_dir="${DEFAULT_EXTENSIONS_DIR}"
  local dst_dir="${STATE_DIR}/extensions"
  local sig_dir="${BOOTSTRAP_CACHE_DIR}/extensions"
  local item
  local extension_name
  local extension_dst
  local src_signature
  local dst_signature
  local previous_signature
  local signature_file

  [[ -d "${src_dir}" ]] || return 0

  mkdir -p "${dst_dir}" "${sig_dir}"
  for item in "${src_dir}"/*; do
    [[ -d "${item}" ]] || continue
    extension_name="$(basename "${item}")"
    extension_dst="${dst_dir}/${extension_name}"
    signature_file="${sig_dir}/${extension_name}.sig"
    src_signature="$(read_embedded_directory_signature "${item}" 2>/dev/null || true)"
    if [[ -z "${src_signature}" ]]; then
      src_signature="$(compute_directory_content_signature "${item}" || true)"
    fi
    previous_signature=""
    if [[ -f "${signature_file}" ]]; then
      previous_signature="$(cat "${signature_file}" 2>/dev/null || true)"
    fi

    if [[ ! -e "${extension_dst}" ]]; then
      replace_directory_with_copy "${item}" "${extension_dst}"
      if [[ -n "${src_signature}" ]]; then
        printf '%s\n' "${src_signature}" > "${signature_file}"
      fi
      bootstrap_log "seeded bundled extension ${extension_name}"
      continue
    fi

    if [[ -n "${src_signature}" && -n "${previous_signature}" ]]; then
      if ! directory_has_changes_since "${signature_file}" "${extension_dst}"; then
        if [[ "${src_signature}" == "${previous_signature}" ]]; then
          printf '%s\n' "${src_signature}" > "${signature_file}"
          continue
        fi
        replace_directory_with_copy "${item}" "${extension_dst}"
        printf '%s\n' "${src_signature}" > "${signature_file}"
        bootstrap_log "upgraded bundled extension ${extension_name}"
        continue
      fi

      bootstrap_log "preserved user-managed extension ${extension_name}"
      continue
    fi

    dst_signature="$(compute_directory_content_signature "${extension_dst}" 2>/dev/null || true)"
    if [[ -z "${dst_signature}" ]]; then
      replace_directory_with_copy "${item}" "${extension_dst}"
      if [[ -n "${src_signature}" ]]; then
        printf '%s\n' "${src_signature}" > "${signature_file}"
      fi
      bootstrap_log "synced bundled extension ${extension_name}"
      continue
    fi

    if [[ -n "${src_signature}" && "${src_signature}" == "${dst_signature}" ]]; then
      printf '%s\n' "${src_signature}" > "${signature_file}"
      continue
    fi

    if [[ -n "${previous_signature}" && "${dst_signature}" == "${previous_signature}" ]]; then
      replace_directory_with_copy "${item}" "${extension_dst}"
      if [[ -n "${src_signature}" ]]; then
        printf '%s\n' "${src_signature}" > "${signature_file}"
      fi
      bootstrap_log "upgraded bundled extension ${extension_name}"
      continue
    fi

    bootstrap_log "preserved user-managed extension ${extension_name}"
  done
}

# -----------------------------------------------------------------------------
# 渠道插件最小补丁
# 这里只保留对官方稳定版微信插件（2.1.7+）的一个极小 shim：
# - 最新 upstream 已兼容新版 plugin-sdk
# - 但在 OpenClaw 2026.3.23-1 下 `plugins inspect` 仍不会暴露
#   `web.login.start/web.login.wait`
# - 我们统一入口 `agentengine openclaw channel connect --channel weixin`
#   依赖这两个 gateway methods 来触发远端扫码登录
# -----------------------------------------------------------------------------

patch_bundled_channel_plugins() {
  OPENCLAW_PATCH_ROOTS="${OPENCLAW_PATCH_ROOTS:-${STATE_DIR}/extensions}" \
  OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_PACKAGE_NAME="${OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_PACKAGE_NAME:-@tencent-weixin/openclaw-weixin}" \
  OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_MIN_VERSION="${OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_MIN_VERSION:-${OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_VERSION:-2.1.7}}" \
  node <<'NODE'
const fs = require('fs');
const path = require('path');

const rawRoots = String(process.env.OPENCLAW_PATCH_ROOTS || '')
  .split(':')
  .map((item) => item.trim())
  .filter(Boolean);
const uniqueRoots = [...new Set(rawRoots)];
const targetPackageName = String(process.env.OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_PACKAGE_NAME || '@tencent-weixin/openclaw-weixin').trim();
const minimumSupportedVersion = String(
  process.env.OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_MIN_VERSION ||
    process.env.OPENCLAW_WEIXIN_REMOTE_LOGIN_PATCH_TARGET_VERSION ||
    '2.1.7'
).trim();
const inlinePatches = [
  {
    label: 'weixin remote login methods',
    relativePath: path.join('openclaw-weixin', 'src', 'channel.ts'),
    marker: 'gatewayMethods: ["web.login.start", "web.login.wait"],',
    needle: '  status: {\n',
    replacement: '  gatewayMethods: ["web.login.start", "web.login.wait"],\n  status: {\n',
  },
  {
    label: 'weixin remote login methods compact status',
    relativePath: path.join('openclaw-weixin', 'src', 'channel.ts'),
    marker: 'gatewayMethods: ["web.login.start", "web.login.wait"],',
    needle: '  status: {},\n',
    replacement: '  gatewayMethods: ["web.login.start", "web.login.wait"],\n  status: {},\n',
  },
];

const patched = new Set();
const skipped = new Set();

function parseStableSemver(rawVersion) {
  const match = String(rawVersion || '').trim().match(/^(\d+)\.(\d+)\.(\d+)$/);
  if (!match) return null;
  return match.slice(1).map((item) => Number.parseInt(item, 10));
}

function compareSemver(left, right) {
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
    const leftPart = Number(left[index] || 0);
    const rightPart = Number(right[index] || 0);
    if (leftPart > rightPart) return 1;
    if (leftPart < rightPart) return -1;
  }
  return 0;
}

const minimumSupportedVersionParsed = parseStableSemver(minimumSupportedVersion);

function resolvePatchDecision(pluginRoot) {
  const packageJsonPath = path.join(pluginRoot, 'package.json');
  if (!fs.existsSync(packageJsonPath)) {
    return { ok: false, reason: 'missing package.json' };
  }

  try {
    const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'));
    const packageName = typeof packageJson.name === 'string' ? packageJson.name.trim() : '';
    const packageVersion = typeof packageJson.version === 'string' ? packageJson.version.trim() : '';
    if (packageName !== targetPackageName) {
      return {
        ok: false,
        reason: `package ${packageName || 'unknown'} does not match ${targetPackageName}`,
        packageName,
        packageVersion,
      };
    }
    if (minimumSupportedVersionParsed) {
      const parsedPackageVersion = parseStableSemver(packageVersion);
      if (!parsedPackageVersion) {
        return {
          ok: false,
          reason: `version ${packageVersion || 'unknown'} is not a stable semver >= ${minimumSupportedVersion}`,
          packageName,
          packageVersion,
        };
      }
      if (compareSemver(parsedPackageVersion, minimumSupportedVersionParsed) < 0) {
        return {
          ok: false,
          reason: `version ${packageVersion || 'unknown'} is below supported floor ${minimumSupportedVersion}`,
          packageName,
          packageVersion,
        };
      }
    }
    return {
      ok: true,
      packageName,
      packageVersion,
    };
  } catch (error) {
    return {
      ok: false,
      reason: `invalid package.json: ${error instanceof Error ? error.message : String(error)}`,
    };
  }
}

for (const rootDir of uniqueRoots) {
  if (!rootDir || !fs.existsSync(rootDir)) continue;
  const pluginRoot = path.join(rootDir, 'openclaw-weixin');
  if (!fs.existsSync(pluginRoot)) continue;
  const patchDecision = resolvePatchDecision(pluginRoot);
  if (!patchDecision.ok) {
    skipped.add(`openclaw-weixin (${patchDecision.reason})`);
    continue;
  }
  for (const patch of inlinePatches) {
    const targetPath = path.join(rootDir, patch.relativePath);
    if (!fs.existsSync(targetPath)) continue;
    const source = fs.readFileSync(targetPath, 'utf8');
    if (source.includes(patch.marker) || !source.includes(patch.needle)) continue;
    fs.writeFileSync(targetPath, source.replace(patch.needle, patch.replacement), 'utf8');
    patched.add(path.relative(rootDir, targetPath));
  }
}

if (patched.size > 0) {
  console.error(`[bootstrap] patched bundled channel plugins: ${[...patched].join(', ')}`);
}
if (skipped.size > 0) {
  console.error(`[bootstrap] skipped bundled channel plugin compat patch: ${[...skipped].join(', ')}`);
}
NODE
}

register_kdocs_skill() {
  local skill_dir="${STATE_DIR}/skills/kdocs"
  local setup_script="${skill_dir}/setup.sh"

  [[ -d "${skill_dir}" ]] || return 0

  if [[ -z "${KDOCS_TOKEN:-}" ]]; then
    return 0
  fi

  if [[ ! -f "${setup_script}" ]]; then
    echo "WARN: bundled kdocs skill found but setup.sh is missing; skipping auto-registration." >&2
    return 0
  fi

  chmod 755 "${setup_script}" 2>/dev/null || true
  echo "INFO: auto-registering bundled kdocs skill"
  if ! OPENCLAW_KDOCS_BOOTSTRAP=1 bash "${setup_script}"; then
    echo "WARN: bundled kdocs skill auto-registration failed; continuing startup." >&2
  fi
}

sync_workspace_security_templates() {
  local src_dir="${WORKSPACE_TEMPLATE_DIR}"
  local dst_dir="${WORKSPACE_DIR}"
  local file_name

  [[ -d "${src_dir}" ]] || return 0

  mkdir -p "${dst_dir}"
  if is_truthy "${OPENCLAW_EXEC_STRICT_MODE:-false}"; then
    set -- AGENTS.md MEMORY.md USER.MD SOUL.md TOOLS.md
  else
    set --
  fi

  for file_name in "$@"; do
    if [[ -f "${src_dir}/${file_name}" && ! -f "${dst_dir}/${file_name}" ]]; then
      cp "${src_dir}/${file_name}" "${dst_dir}/${file_name}"
    fi
  done
}

cleanup_relaxed_workspace_security_templates() {
  local src_dir="${WORKSPACE_TEMPLATE_DIR}"
  local dst_dir="${WORKSPACE_DIR}"
  local file_name

  is_truthy "${OPENCLAW_EXEC_STRICT_MODE:-false}" && return 0
  [[ -d "${src_dir}" && -d "${dst_dir}" ]] || return 0

  for file_name in AGENTS.md MEMORY.md USER.MD SOUL.md TOOLS.md; do
    if [[ -f "${src_dir}/${file_name}" && -f "${dst_dir}/${file_name}" ]] && cmp -s "${src_dir}/${file_name}" "${dst_dir}/${file_name}"; then
      rm -f "${dst_dir}/${file_name}"
    fi
  done
}

enable_self_improvement_workspace() {
  local skill_dir="${STATE_DIR}/skills/self-improving-agent"
  local learnings_src_dir="${skill_dir}/.learnings"
  local learnings_dst_dir="${WORKSPACE_DIR}/.learnings"
  local file_name

  [[ -d "${skill_dir}" ]] || return 0

  mkdir -p "${learnings_dst_dir}"
  for file_name in LEARNINGS.md ERRORS.md FEATURE_REQUESTS.md; do
    if [[ -f "${learnings_src_dir}/${file_name}" && ! -f "${learnings_dst_dir}/${file_name}" ]]; then
      cp "${learnings_src_dir}/${file_name}" "${learnings_dst_dir}/${file_name}"
    fi
  done
}

patch_gateway_client_loopback_trusted_proxy_identity() {
  local dist_dir="${RUNTIME_DIST_DIR}"
  local marker_file="${DIST_PATCH_MARKER}"

  [[ -d "${dist_dir}" ]] || return 0
  if [[ -n "${marker_file}" && -f "${marker_file}" ]]; then
    return 0
  fi

  OPENCLAW_DIST_PATCH_MARKER="${marker_file}" node <<'NODE'
const fs = require('fs');
const path = require('path');

const distDir = process.env.OPENCLAW_DIST_DIR || '/app/dist';
const markerFile = process.env.OPENCLAW_DIST_PATCH_MARKER || '';
const requiredLabels = new Set([
  'gateway client loopback trusted-proxy identity',
  'gateway backend self-pairing trusted-proxy bypass',
  'gateway trusted-proxy loopback internal auth compatibility',
  'gateway local override explicit-auth bypass',
  // NOTE: 以下两个 patch 已在 openclaw >= 2026.4.5 中由上游原生修复
  //   - 'gateway loopback device-identity bypass'  → resolveDeviceIdentityForGatewayCall() + try/catch
  //   - 'gateway loopback device-identity null sentinel' → catch 返回 null 而非 void 0
]);
const replacements = [
  {
    label: 'control-ui websocket reconnect gap handling',
    marker: 'this.ws.addEventListener(`open`,()=>{this.lastSeq=null,this.queueConnect()})',
    needle: 'this.ws.addEventListener(`open`,()=>this.queueConnect())',
    replacement: 'this.ws.addEventListener(`open`,()=>{this.lastSeq=null,this.queueConnect()})',
  },
  {
    // Container image code under /app/dist is immutable; hide upstream self-update affordance.
    label: 'gateway container self-update availability disabled',
    marker: 'function getUpdateAvailable() {\n\treturn null;\n}',
    needle: `function getUpdateAvailable() {
\treturn updateAvailableCache;
}`,
    replacement: `function getUpdateAvailable() {
\treturn null;
}`,
  },
  {
    label: 'gateway container self-update scheduler disabled',
    marker: 'function scheduleGatewayUpdateCheck(params) {\n\treturn () => {};\n}',
    needle: `function scheduleGatewayUpdateCheck(params) {
\tlet stopped = false;
\tlet timer = null;
\tlet running = false;
\tconst tick = async () => {
\t\tif (stopped || running) return;
\t\trunning = true;
\t\ttry {
\t\t\tawait runGatewayUpdateCheck(params);
\t\t} catch {} finally {
\t\t\trunning = false;
\t\t}
\t\tif (stopped) return;
\t\tconst intervalMs = resolveCheckIntervalMs(params.cfg);
\t\ttimer = setTimeout(() => {
\t\t\ttick();
\t\t}, intervalMs);
\t};
\ttick();
\treturn () => {
\t\tstopped = true;
\t\tif (timer) {
\t\t\tclearTimeout(timer);
\t\t\ttimer = null;
\t\t}
\t};
}`,
    replacement: `function scheduleGatewayUpdateCheck(params) {
\treturn () => {};
}`,
  },
  {
    // openclaw 2026.3.28+: trusted-proxy loopback now rides on trustedProxies
    // and no longer emits the legacy trusted_proxy_loopback_source branch.
    label: 'gateway trusted-proxy loopback internal auth compatibility',
    marker: 'if (!remoteAddr || !isTrustedProxyAddress$1(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };',
    needle: 'if (!remoteAddr || !isTrustedProxyAddress$1(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };',
    replacement: 'if (!remoteAddr || !isTrustedProxyAddress$1(remoteAddr, trustedProxies)) return { reason: "trusted_proxy_untrusted_source" };',
  },
  {
    // openclaw 2026.3.28 source-like bundles may keep the helper name
    // unaliased and expand the early return into a block.
    label: 'gateway trusted-proxy loopback internal auth compatibility',
    marker: 'if (!remoteAddr || !isTrustedProxyAddress(remoteAddr, trustedProxies)) {',
    needle: 'if (!remoteAddr || !isTrustedProxyAddress(remoteAddr, trustedProxies)) {',
    replacement: 'if (!remoteAddr || !isTrustedProxyAddress(remoteAddr, trustedProxies)) {',
  },
  {
    label: 'gateway trusted-proxy loopback internal auth compatibility',
    marker: 'const internalLoopbackUserHeader = String(process.env.OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER || process.env.OPENCLAW_TRUSTED_PROXY_USER_HEADER || "x-forwarded-user").trim().toLowerCase();',
    needle: 'if (isLoopbackAddress(remoteAddr)) return { reason: "trusted_proxy_loopback_source" };',
    replacement: `if (isLoopbackAddress(remoteAddr)) {
\tconst internalLoopbackUserHeader = String(process.env.OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER || process.env.OPENCLAW_TRUSTED_PROXY_USER_HEADER || "x-forwarded-user").trim().toLowerCase();
\tconst internalLoopbackUser = String(process.env.OPENCLAW_INTERNAL_TRUSTED_PROXY_USER || "openclaw-backend").trim();
\tconst loopbackUser = headerValue(req.headers[internalLoopbackUserHeader || "x-forwarded-user"]);
\tif (!internalLoopbackUser || !loopbackUser || loopbackUser.trim() !== internalLoopbackUser) return { reason: "trusted_proxy_loopback_source" };
}`,
  },
  {
    label: 'gateway client loopback trusted-proxy identity',
    marker: 'const internalTrustedProxyUser = String(process.env.OPENCLAW_INTERNAL_TRUSTED_PROXY_USER || "openclaw-backend").trim();',
    needle: 'const wsOptions = { maxPayload: 25 * 1024 * 1024 };',
    replacement: `const wsOptions = { maxPayload: 25 * 1024 * 1024 };
		const internalTrustedProxyUser = String(process.env.OPENCLAW_INTERNAL_TRUSTED_PROXY_USER || "openclaw-backend").trim();
		const internalTrustedProxyUserHeader = String(process.env.OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER || process.env.OPENCLAW_TRUSTED_PROXY_USER_HEADER || "x-forwarded-user").trim().toLowerCase();
		try {
			const parsedGatewayUrl = new URL(url);
			if (internalTrustedProxyUser && ["127.0.0.1", "::1", "localhost"].includes(parsedGatewayUrl.hostname)) wsOptions.headers = {
				...wsOptions.headers,
				[internalTrustedProxyUserHeader || "x-forwarded-user"]: internalTrustedProxyUser
			};
		} catch {}`,
  },
  {
    // openclaw 2026.3.28+: pairing/device-identity bypass moved into
    // shouldSkipControlUiPairing(... trustedProxyAuthOk = false ...)
    label: 'gateway backend self-pairing trusted-proxy bypass',
    marker: 'function shouldSkipControlUiPairing(policy, role, trustedProxyAuthOk = false, authMode) {',
    needle: 'function shouldSkipControlUiPairing(policy, role, trustedProxyAuthOk = false, authMode) {',
    replacement: 'function shouldSkipControlUiPairing(policy, role, trustedProxyAuthOk = false, authMode) {',
  },
  {
    // openclaw >= 2026.4.5: 函数重命名为 shouldSkipLocalBackendSelfPairing, isLocalClient → locality
    label: 'gateway backend self-pairing trusted-proxy bypass',
    marker: 'const usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";',
    needle: `function shouldSkipLocalBackendSelfPairing(params) {
	if (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;
	const usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";
	const usesDeviceTokenAuth = params.authMethod === "device-token";
	return params.locality === "direct_local" && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);
}`,
    replacement: `function shouldSkipLocalBackendSelfPairing(params) {
	if (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;
	const usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";
	const usesDeviceTokenAuth = params.authMethod === "device-token";
	const usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";
	return params.locality === "direct_local" && !params.hasBrowserOriginHeader && (usesLoopbackTrustedProxyAuth || params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);
}`,
  },
  {
    // openclaw < 2026.4.5: 旧函数名 shouldSkipBackendSelfPairing, 旧参数 isLocalClient
    label: 'gateway backend self-pairing trusted-proxy bypass',
    marker: 'const usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";',
    needle: `function shouldSkipBackendSelfPairing(params) {
\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;
\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";
\tconst usesDeviceTokenAuth = params.authMethod === "device-token";
\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);
}`,
    replacement: `function shouldSkipBackendSelfPairing(params) {
\tif (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;
\tconst usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";
\tconst usesDeviceTokenAuth = params.authMethod === "device-token";
\tconst usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";
\treturn params.isLocalClient && !params.hasBrowserOriginHeader && (usesLoopbackTrustedProxyAuth || params.sharedAuthOk && usesSharedSecretAuth || usesDeviceTokenAuth);
}`,
  },
  {
    label: 'gateway local override explicit-auth bypass',
    marker: 'if (["127.0.0.1", "::1", "localhost"].includes(parsed.hostname)) return;',
    needle: `function ensureExplicitGatewayAuth(params) {
	if (!params.urlOverride) return;
`,
    replacement: `function ensureExplicitGatewayAuth(params) {
	if (!params.urlOverride) return;
	try {
		const parsed = new URL(params.urlOverride);
		if (["127.0.0.1", "::1", "localhost"].includes(parsed.hostname)) return;
	} catch {}
`,
  },
  {
    label: 'gateway loopback device-identity bypass',
    marker: 'function shouldAttachDeviceIdentityForGatewayCall(params) {\n\ttry {\n\t\tconst parsed = new URL(params.url);\n\t\tif ([\n\t\t\t"127.0.0.1",\n\t\t\t"::1",\n\t\t\t"localhost"\n\t\t].includes(parsed.hostname)) return false;',
    needle: `function shouldAttachDeviceIdentityForGatewayCall(params) {
	return true;
}`,
    replacement: `function shouldAttachDeviceIdentityForGatewayCall(params) {
	try {
		const parsed = new URL(params.url);
		if ([
			"127.0.0.1",
			"::1",
			"localhost"
		].includes(parsed.hostname)) return false;
	} catch {
		return true;
	}
	return true;
}`,
  },
  {
    label: 'gateway loopback device-identity null sentinel',
    marker: 'deviceIdentity: shouldAttachDeviceIdentityForGatewayCall({\n\t\t\t\turl,\n\t\t\t\ttoken,\n\t\t\t\tpassword\n\t\t\t}) ? loadOrCreateDeviceIdentity() : null,',
    needle: `deviceIdentity: shouldAttachDeviceIdentityForGatewayCall({
				url,
				token,
				password
			}) ? loadOrCreateDeviceIdentity() : void 0,`,
    replacement: `deviceIdentity: shouldAttachDeviceIdentityForGatewayCall({
				url,
				token,
				password
			}) ? loadOrCreateDeviceIdentity() : null,`,
  },
  {
    label: 'gateway local trusted-proxy scope retention',
    marker: 'const keepUnboundScopes = !device && decision.kind === "allow" && authMethod === "trusted-proxy" && !hasBrowserOriginHeader;',
    needle: 'if (!device && (!isControlUi || decision.kind !== "allow")) clearUnboundScopes();',
    replacement: `const keepUnboundScopes = !device && decision.kind === "allow" && authMethod === "trusted-proxy" && !hasBrowserOriginHeader;
					if (!device && (!isControlUi || decision.kind !== "allow") && !keepUnboundScopes) clearUnboundScopes();`,
  },
];

const jsFiles = [];
const walk = (dir) => {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(fullPath);
      continue;
    }
    if (entry.isFile() && entry.name.endsWith('.js')) {
      jsFiles.push(fullPath);
    }
  }
};

walk(distDir);

const patchedLabels = new Set();
const satisfiedLabels = new Set();
const skippedLabels = new Set();
console.error(`[bootstrap] 开始扫描 dist 目录: ${distDir} (共 ${jsFiles.length} 个 JS 文件)`);
console.error(`[bootstrap] 需验证的必需补丁: ${[...requiredLabels].join(', ')}`);
console.error(`[bootstrap] 共 ${replacements.length} 个候选补丁规则`);
for (const filePath of jsFiles) {
  let source = fs.readFileSync(filePath, 'utf8');
  let changed = false;
  for (const patch of replacements) {
    if (source.includes(patch.marker)) {
      satisfiedLabels.add(patch.label);
      continue;
    }
    if (!source.includes(patch.needle)) {
      skippedLabels.add(patch.label);
      continue;
    }
    source = source.replaceAll(patch.needle, patch.replacement);
    patchedLabels.add(patch.label);
    satisfiedLabels.add(patch.label);
    changed = true;
  }
  if (!changed) continue;
  fs.writeFileSync(filePath, source);
}

if (patchedLabels.size > 0) {
  console.error(`[bootstrap] 已应用补丁: ${[...patchedLabels].join(', ')}`);
}
const alreadySatisfied = [...satisfiedLabels].filter((l) => !patchedLabels.has(l));
if (alreadySatisfied.length > 0) {
  console.error(`[bootstrap] 已由上游原生满足（无需补丁）: ${alreadySatisfied.join(', ')}`);
}

const missingRequiredLabels = [...requiredLabels].filter((label) => !satisfiedLabels.has(label));
if (missingRequiredLabels.length > 0) {
  console.error(`[bootstrap] ❌ 缺失的必需补丁: ${missingRequiredLabels.join(', ')}`);
  console.error(`[bootstrap] 已满足: ${[...satisfiedLabels].join(', ') || '无'}`);
  console.error(`[bootstrap] 未匹配（needle 和 marker 均未命中）: ${[...skippedLabels].filter((l) => !satisfiedLabels.has(l)).join(', ') || '无'}`);
  console.error('[bootstrap] 提示: 这通常是因为基础镜像版本更新导致上游代码结构变化，需要更新 bootstrap.sh 中的 patch 定义');
  throw new Error(`必需的 dist 补丁缺失: ${missingRequiredLabels.join(', ')}`);
}

console.error(`[bootstrap] ✅ 所有必需补丁验证通过 (${satisfiedLabels.size}/${requiredLabels.size})`);

if (markerFile) {
  fs.writeFileSync(markerFile, `version=${path.basename(markerFile)}\n`, 'utf8');
}
NODE
}

# -----------------------------------------------------------------------------
# 运行时环境文件与国内优先默认源
# -----------------------------------------------------------------------------

configure_runtime_network_defaults() {
  local runtime_npm_registry="${OPENCLAW_RUNTIME_NPM_REGISTRY:-https://registry.npmmirror.com}"
  local runtime_pip_index_url="${OPENCLAW_RUNTIME_PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
  local runtime_pip_trusted_host="${OPENCLAW_RUNTIME_PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
  local runtime_uv_index_url="${OPENCLAW_RUNTIME_UV_INDEX_URL:-${runtime_pip_index_url}}"
  local runtime_playwright_download_host="${OPENCLAW_RUNTIME_PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"
  local runtime_puppeteer_download_base_url="${OPENCLAW_RUNTIME_PUPPETEER_DOWNLOAD_BASE_URL:-https://npmmirror.com/mirrors/chrome-for-testing}"
  local runtime_puppeteer_download_host="${OPENCLAW_RUNTIME_PUPPETEER_DOWNLOAD_HOST:-https://npmmirror.com/mirrors}"
  local runtime_clawhub_site="${OPENCLAW_RUNTIME_CLAWHUB_SITE:-https://cn.clawhub-mirror.com}"
  local runtime_clawhub_registry="${OPENCLAW_RUNTIME_CLAWHUB_REGISTRY:-${runtime_clawhub_site}}"

  export NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY:-${runtime_npm_registry}}"
  export npm_config_registry="${npm_config_registry:-${NPM_CONFIG_REGISTRY}}"
  export YARN_NPM_REGISTRY_SERVER="${YARN_NPM_REGISTRY_SERVER:-${NPM_CONFIG_REGISTRY}}"
  export PIP_INDEX_URL="${PIP_INDEX_URL:-${runtime_pip_index_url}}"
  export PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-${runtime_pip_trusted_host}}"
  export UV_INDEX_URL="${UV_INDEX_URL:-${runtime_uv_index_url}}"
  export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-${runtime_playwright_download_host}}"
  export PUPPETEER_DOWNLOAD_BASE_URL="${PUPPETEER_DOWNLOAD_BASE_URL:-${runtime_puppeteer_download_base_url}}"
  export PUPPETEER_DOWNLOAD_HOST="${PUPPETEER_DOWNLOAD_HOST:-${runtime_puppeteer_download_host}}"
  export CLAWHUB_SITE="${CLAWHUB_SITE:-${runtime_clawhub_site}}"
  export CLAWHUB_REGISTRY="${CLAWHUB_REGISTRY:-${runtime_clawhub_registry}}"

  bootstrap_log "已启用国内优先运行时源: npm=${NPM_CONFIG_REGISTRY}, pip=${PIP_INDEX_URL}, clawhub=${CLAWHUB_REGISTRY}, playwright=${PLAYWRIGHT_DOWNLOAD_HOST}"
}

upsert_env_var() {
  local key="$1"
  local value="$2"
  local file_path="$3"
  local tmp_file

  [[ -n "${key}" ]] || return 0
  [[ -n "${value}" ]] || return 0

  touch "${file_path}"
  tmp_file="$(mktemp)"

  awk -v k="${key}" -v v="${value}" '
    BEGIN { done = 0 }
    index($0, k "=") == 1 {
      print k "=" v
      done = 1
      next
    }
    { print }
    END {
      if (!done) print k "=" v
    }
  ' "${file_path}" > "${tmp_file}"

  mv "${tmp_file}" "${file_path}"
}

sync_runtime_env_file() {
  local env_file="${STATE_DIR}/.env"
  local tavily_key="${OPENCLAW_TAVILY_API_KEY:-${TAVILY_API_KEY:-}}"

  if [[ -n "${tavily_key}" ]]; then
    upsert_env_var "tavily_api_key" "${tavily_key}" "${env_file}"
  fi

  upsert_env_var "CLAWHUB_SITE" "${CLAWHUB_SITE:-}" "${env_file}"
  upsert_env_var "CLAWHUB_REGISTRY" "${CLAWHUB_REGISTRY:-}" "${env_file}"
  upsert_env_var "NPM_CONFIG_REGISTRY" "${NPM_CONFIG_REGISTRY:-}" "${env_file}"
  upsert_env_var "npm_config_registry" "${npm_config_registry:-}" "${env_file}"
  upsert_env_var "YARN_NPM_REGISTRY_SERVER" "${YARN_NPM_REGISTRY_SERVER:-}" "${env_file}"
  upsert_env_var "PIP_INDEX_URL" "${PIP_INDEX_URL:-}" "${env_file}"
  upsert_env_var "PIP_TRUSTED_HOST" "${PIP_TRUSTED_HOST:-}" "${env_file}"
  upsert_env_var "UV_INDEX_URL" "${UV_INDEX_URL:-}" "${env_file}"
  upsert_env_var "PLAYWRIGHT_DOWNLOAD_HOST" "${PLAYWRIGHT_DOWNLOAD_HOST:-}" "${env_file}"
  upsert_env_var "PUPPETEER_DOWNLOAD_BASE_URL" "${PUPPETEER_DOWNLOAD_BASE_URL:-}" "${env_file}"
  upsert_env_var "PUPPETEER_DOWNLOAD_HOST" "${PUPPETEER_DOWNLOAD_HOST:-}" "${env_file}"
}

# -----------------------------------------------------------------------------
# Exec allowlist 生成
# -----------------------------------------------------------------------------

build_exec_default_allowlist() {
  local -a wrapped_bins=(pwd ls whoami id uname date ps df du stat find cat head tail wc git mcporter sh-safe bash-safe web-safe)
  local -a direct_bins=(curl jq openclaw agent-browser clawhub)
  local -a patterns=()
  local bin
  local resolved
  local joined=""

  for bin in "${wrapped_bins[@]}"; do
    resolved="${SAFE_BIN_DIR}/${bin}"
    if [[ -z "${resolved}" || "${resolved}" != /* || ! -x "${resolved}" ]]; then
      continue
    fi
    patterns+=("${resolved}")
  done

  for bin in "${direct_bins[@]}"; do
    case "${bin}" in
      curl)
        resolved="$(resolve_allowlisted_command_path "${bin}" /usr/bin/curl /usr/local/bin/curl /bin/curl || true)"
        ;;
      jq)
        resolved="$(resolve_allowlisted_command_path "${bin}" /usr/bin/jq /usr/local/bin/jq /bin/jq || true)"
        ;;
      openclaw)
        resolved="$(resolve_allowlisted_command_path "${bin}" /usr/local/bin/openclaw /usr/bin/openclaw /bin/openclaw || true)"
        ;;
      agent-browser)
        resolved="$(resolve_allowlisted_command_path "${bin}" /usr/local/bin/agent-browser /usr/bin/agent-browser /bin/agent-browser || true)"
        ;;
      clawhub)
        resolved="$(resolve_allowlisted_command_path "${bin}" /home/node/.local/bin/clawhub /root/.local/bin/clawhub /usr/local/bin/clawhub /usr/bin/clawhub || true)"
        ;;
      *)
        resolved=""
        ;;
    esac
    if [[ -z "${resolved}" || "${resolved}" != /* || ! -x "${resolved}" ]]; then
      continue
    fi
    patterns+=("${resolved}")
  done

  for resolved in "${patterns[@]}"; do
    if [[ -n "${joined}" ]]; then
      joined+=","
    fi
    joined+="${resolved}"
  done

  printf '%s\n' "${joined}"
}

DIST_PATCH_ONLY_RAW="$(printf '%s' "${OPENCLAW_DIST_PATCH_ONLY:-}" | tr '[:upper:]' '[:lower:]')"
if [[ "${DIST_PATCH_ONLY_RAW}" == "1" || "${DIST_PATCH_ONLY_RAW}" == "true" || "${DIST_PATCH_ONLY_RAW}" == "yes" || "${DIST_PATCH_ONLY_RAW}" == "on" ]]; then
  bootstrap_phase "仅执行镜像内置资源补丁"
  echo "INFO: [dist-patch-only] 运行时目录: ${RUNTIME_DIST_DIR:-/app/dist}"
  echo "INFO: [dist-patch-only] 开始应用 gateway 运行时补丁..."
  patch_gateway_client_loopback_trusted_proxy_identity
  echo "INFO: [dist-patch-only] gateway 补丁完成，开始处理渠道插件补丁..."
  OPENCLAW_PATCH_ROOTS="${DEFAULT_EXTENSIONS_DIR}" patch_bundled_channel_plugins
  echo "INFO: [dist-patch-only] ✅ 已完成 dist-patch-only 模式，所有镜像内置运行时资源补丁已就绪。"
  exit 0
fi

BOOTSTRAP_STARTED_AT="$(bootstrap_now_seconds)"

bootstrap_phase "开始初始化 OpenClaw 运行时"
bootstrap_log "状态目录: ${STATE_DIR}"
bootstrap_log "配置文件: ${CONFIG_PATH}"
bootstrap_log "工作目录: ${WORKSPACE_DIR}"

mkdir -p "${STATE_DIR}"
configure_runtime_network_defaults

bootstrap_phase "同步内置插件与兼容补丁"
OPENCLAW_PATCH_ROOTS="${DEFAULT_EXTENSIONS_DIR}" patch_bundled_channel_plugins
sync_default_extensions
patch_bundled_channel_plugins

configure_browser_executable || true

export OPENCLAW_INTERNAL_TRUSTED_PROXY_USER="${OPENCLAW_INTERNAL_TRUSTED_PROXY_USER:-openclaw-backend}"
export OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER="${OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER:-${OPENCLAW_TRUSTED_PROXY_USER_HEADER:-x-forwarded-user}}"

# 默认宽松执行模式；仅在 OPENCLAW_EXEC_STRICT_MODE=true 时收紧到安全模式。
EXEC_STRICT_MODE_RAW="$(printf '%s' "${OPENCLAW_EXEC_STRICT_MODE:-${OPENCLAW_EXEC_SAFE_MODE:-}}" | tr '[:upper:]' '[:lower:]')"
if is_truthy "${EXEC_STRICT_MODE_RAW}"; then
  export OPENCLAW_EXEC_STRICT_MODE="true"
  export OPENCLAW_EXEC_UNSAFE_MODE="${OPENCLAW_EXEC_UNSAFE_MODE:-false}"
else
  # 宽松模式：最大程度还原原版 OpenClaw 体验
  export OPENCLAW_EXEC_STRICT_MODE="false"
  export OPENCLAW_EXEC_UNSAFE_MODE="${OPENCLAW_EXEC_UNSAFE_MODE:-true}"
  export OPENCLAW_EXEC_SECURITY="${OPENCLAW_EXEC_SECURITY:-full}"
  export OPENCLAW_EXEC_ASK="${OPENCLAW_EXEC_ASK:-off}"
  export OPENCLAW_EXEC_ASK_FALLBACK="${OPENCLAW_EXEC_ASK_FALLBACK:-full}"
  export OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED="${OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED:-false}"
  export OPENCLAW_FS_WORKSPACE_ONLY="${OPENCLAW_FS_WORKSPACE_ONLY:-false}"
  # 宽松模式下不注入 safe-bin，让命令走原生 PATH
  export OPENCLAW_SKIP_SAFE_BIN_PATH="true"
fi

if is_truthy "${OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED:-1}"; then
  if [[ -z "${OPENCLAW_EXEC_DEFAULT_ALLOWLIST:-}" ]]; then
    export OPENCLAW_EXEC_DEFAULT_ALLOWLIST="$(build_exec_default_allowlist)"
  fi
fi

export OPENCLAW_RESOLVED_PRESET_SKILLS_ALLOWLIST="$(resolve_preset_skills_allowlist)"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo '{}' > "${CONFIG_PATH}"
fi

bootstrap_phase "生成并校正 Gateway 配置"

# Always reconcile runtime config from environment variables.
# NOTE:
# - We intentionally apply env -> config on every startup so deployment-time
#   env updates (e.g. OPENCLAW_ALLOWED_ORIGINS / OPENCLAW_DISABLE_DEVICE_AUTH)
#   can take effect for existing instances.
export STATE_DIR CONFIG_PATH PUBLIC_PORT BIND_MODE AUTH_MODE
CONFIG_RECONCILE_STARTED_AT="$(bootstrap_now_seconds)"
node <<'NODE'
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const configPath = process.env.CONFIG_PATH;
const publicPort = process.env.PUBLIC_PORT;
const bind = process.env.BIND_MODE;
const authMode = process.env.AUTH_MODE;
const parseBool = (raw, fallback) => {
  const text = String(raw ?? '').trim().toLowerCase();
  if (!text) return fallback;
  if (['1', 'true', 'yes', 'on'].includes(text)) return true;
  if (['0', 'false', 'no', 'off'].includes(text)) return false;
  return fallback;
};
const parseEnum = (raw, allowed, fallback) => {
  const text = String(raw ?? '').trim().toLowerCase();
  if (!text) return fallback;
  return allowed.includes(text) ? text : fallback;
};
const parsePositiveInt = (raw, fallback) => {
  const parsed = Number.parseInt(String(raw ?? '').trim(), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};
const firstNonBlank = (...values) => {
  for (const value of values) {
    const text = String(value ?? '').trim();
    if (text) return text;
  }
  return '';
};
const uniqueStrings = (items) => {
  const seen = new Set();
  const result = [];
  for (const item of items) {
    const text = String(item ?? '').trim();
    if (!text) continue;
    const key = text.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(text);
  }
  return result;
};
const parseStringList = (raw) => {
  const text = String(raw ?? "").trim();
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed.map((x) => String(x).trim()).filter(Boolean);
    }
  } catch {
    // fallback
  }
  return text
    .split(/[,\s;]+/)
    .map((x) => x.trim())
    .filter(Boolean);
};
const normalizeAllowlistEntries = (entries) => {
  if (!Array.isArray(entries)) return [];
  return entries
    .map((entry) => {
      if (typeof entry === 'string') {
        return { pattern: entry.trim() };
      }
      if (!entry || typeof entry !== 'object') {
        return null;
      }
      const pattern = String(entry.pattern ?? '').trim();
      if (!pattern) {
        return null;
      }
      return { ...entry, pattern };
    })
    .filter(Boolean);
};
const mergeAllowlistEntries = (existingEntries, patterns) => {
  const merged = [];
  const seen = new Set();
  for (const entry of normalizeAllowlistEntries(existingEntries)) {
    const key = entry.pattern.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(entry);
  }
  for (const pattern of uniqueStrings(patterns)) {
    const key = pattern.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push({ pattern });
  }
  return merged;
};
const readJsonFile = (filePath, fallback = {}) => {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch {
    return fallback;
  }
};
const ensurePluginEntry = (pluginId) => {
  cfg.plugins = cfg.plugins || {};
  cfg.plugins.entries = cfg.plugins.entries || {};
  cfg.plugins.entries[pluginId] = cfg.plugins.entries[pluginId] || {};
  return cfg.plugins.entries[pluginId];
};
const enablePlugin = (pluginId) => {
  cfg.plugins = cfg.plugins || {};
  cfg.plugins.allow = uniqueStrings([
    ...(Array.isArray(cfg.plugins.allow) ? cfg.plugins.allow : []),
    pluginId,
  ]);
  ensurePluginEntry(pluginId).enabled = true;
};
const isPlainObject = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const cloneJsonValue = (value) => {
  if (Array.isArray(value)) {
    return value.map(cloneJsonValue);
  }
  if (isPlainObject(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [key, cloneJsonValue(child)]),
    );
  }
  return value;
};
const deepMergeObjects = (base, overlay) => {
  const result = isPlainObject(base) ? cloneJsonValue(base) : {};
  for (const [key, value] of Object.entries(overlay || {})) {
    if (isPlainObject(value)) {
      result[key] = deepMergeObjects(result[key], value);
      continue;
    }
    result[key] = cloneJsonValue(value);
  }
  return result;
};
const normalizeBrowserSsrfPolicy = (rawPolicy) => {
  if (!isPlainObject(rawPolicy)) {
    return null;
  }
  const allowPrivateNetwork = rawPolicy.allowPrivateNetwork;
  const dangerouslyAllowPrivateNetwork = rawPolicy.dangerouslyAllowPrivateNetwork;
  const allowedHostnames = uniqueStrings(
    Array.isArray(rawPolicy.allowedHostnames) ? rawPolicy.allowedHostnames : [],
  );
  const hostnameAllowlist = uniqueStrings(
    Array.isArray(rawPolicy.hostnameAllowlist) ? rawPolicy.hostnameAllowlist : [],
  );
  const hasExplicitPrivateSetting =
    allowPrivateNetwork !== undefined || dangerouslyAllowPrivateNetwork !== undefined;
  const resolvedAllowPrivateNetwork =
    dangerouslyAllowPrivateNetwork !== false && allowPrivateNetwork !== false;

  if (
    resolvedAllowPrivateNetwork &&
    !hasExplicitPrivateSetting &&
    allowedHostnames.length === 0 &&
    hostnameAllowlist.length === 0
  ) {
    return { dangerouslyAllowPrivateNetwork: true };
  }

  const policy = {};
  if (
    resolvedAllowPrivateNetwork ||
    dangerouslyAllowPrivateNetwork === false ||
    allowPrivateNetwork === false
  ) {
    policy.dangerouslyAllowPrivateNetwork = resolvedAllowPrivateNetwork;
  }
  if (allowedHostnames.length > 0) {
    policy.allowedHostnames = allowedHostnames;
  }
  if (hostnameAllowlist.length > 0) {
    policy.hostnameAllowlist = hostnameAllowlist;
  }
  return policy;
};
const AGENTSPACE_DEFAULT_KEY_SOURCE = 'openclaw_agentspace';
const OPENCLAW_CHANNEL_SPECS = {
  weixin: {
    pluginId: 'openclaw-weixin',
    channelKey: 'openclaw-weixin',
    defaultAccountId: 'default',
  },
  feishu: {
    pluginId: 'openclaw-lark',
    channelKey: 'feishu',
  },
  agentspace: {
    pluginId: 'agentspace',
    channelKey: 'agentspace',
    defaultAccountId: 'default',
  },
};
const encryptAgentspaceToken = (wpsSid, appId = '') => {
  const token = String(wpsSid ?? '').trim();
  if (!token) return '';
  const keySource = String(appId ?? '').trim() || AGENTSPACE_DEFAULT_KEY_SOURCE;
  const salt = crypto.randomBytes(16);
  const iv = crypto.randomBytes(12);
  const key = crypto.scryptSync(keySource, salt, 32, { N: 16384, r: 8, p: 1 });
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const cipherBytes = Buffer.concat([cipher.update(token, 'utf8'), cipher.final()]);
  const tagBytes = cipher.getAuthTag();
  return `${salt.toString('hex')}:${iv.toString('hex')}:${tagBytes.toString('hex')}:${cipherBytes.toString('hex')}`;
};
const normalizeAgentspaceBootstrapPayload = (payload, existingChannelCfg) => {
  const rawWpsSid = firstNonBlank(payload.wps_sid, payload.wpsSid);
  if (!rawWpsSid) {
    return cloneJsonValue(payload);
  }

  const appId = firstNonBlank(payload.app_id, payload.appId);
  const currentUser = firstNonBlank(payload.current_user, payload.currentUser);
  const deviceUuid =
    firstNonBlank(
      payload.device_uuid,
      payload.deviceUuid,
      existingChannelCfg?.accounts?.default?.device_uuid,
      typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : '',
    ) || crypto.randomBytes(16).toString('hex');
  const extraPayload = cloneJsonValue(payload);
  for (const key of ['wps_sid', 'wpsSid', 'app_id', 'appId', 'current_user', 'currentUser', 'device_uuid', 'deviceUuid']) {
    delete extraPayload[key];
  }

  return deepMergeObjects(extraPayload, {
    accounts: {
      default: {
        enabled: true,
        token: encryptAgentspaceToken(rawWpsSid, appId),
        currentUser,
        app_id: appId,
        device_uuid: deviceUuid,
      },
    },
    dmPolicy: 'open',
    allowFrom: ['*'],
  });
};
const normalizeFeishuBootstrapPayload = (payload) => {
  const normalized = cloneJsonValue(payload);
  const appId = firstNonBlank(normalized.appId);
  const appSecret = firstNonBlank(normalized.appSecret);
  if (appId || appSecret) {
    normalized.enabled = normalized.enabled == null ? true : normalized.enabled;
    normalized.domain = firstNonBlank(normalized.domain, 'feishu');
    normalized.connectionMode = firstNonBlank(normalized.connectionMode, 'websocket');
    if (normalized.requireMention == null) {
      normalized.requireMention = true;
    }
    if (!['pairing', 'allowlist', 'open'].includes(String(normalized.dmPolicy || '').trim())) {
      normalized.dmPolicy = 'pairing';
    }
    if (!firstNonBlank(normalized.groupPolicy)) {
      normalized.groupPolicy = 'open';
    }
  }
  return normalized;
};
const normalizeChannelBootstrapPayload = (channelName, payload, existingChannelCfg) => {
  if (channelName === 'agentspace') {
    return normalizeAgentspaceBootstrapPayload(payload, existingChannelCfg);
  }
  if (channelName === 'feishu') {
    return normalizeFeishuBootstrapPayload(payload);
  }
  return cloneJsonValue(payload);
};
const applyChannelBootstrapDefaults = (channelName, spec, channelCfg) => {
  const defaultAccountId = spec.defaultAccountId;
  if (defaultAccountId && isPlainObject(channelCfg.accounts?.[defaultAccountId])) {
    if (channelCfg.accounts[defaultAccountId].enabled == null) {
      channelCfg.accounts[defaultAccountId].enabled = true;
    }
  }
  if (channelName === 'agentspace') {
    if (!String(channelCfg.dmPolicy || '').trim()) {
      channelCfg.dmPolicy = 'open';
    }
    if (!Array.isArray(channelCfg.allowFrom) || channelCfg.allowFrom.length === 0) {
      channelCfg.allowFrom = ['*'];
    }
  }
};
const normalizePersistedChannelConfig = (cfg) => {
  const feishuCfg = isPlainObject(cfg?.channels?.feishu) ? cfg.channels.feishu : null;
  if (feishuCfg && String(feishuCfg.dmPolicy || '').trim() === 'open') {
    const allowFrom = uniqueStrings(
      (Array.isArray(feishuCfg.allowFrom) ? feishuCfg.allowFrom : [])
        .map((item) => String(item || '').trim())
        .filter(Boolean),
    );
    if (!allowFrom.includes('*')) {
      allowFrom.push('*');
    }
    feishuCfg.allowFrom = allowFrom;
  }
};
const applyChannelBootstrapFromEnv = () => {
  const rawBootstrapJson = firstNonBlank(process.env.OPENCLAW_CHANNEL_BOOTSTRAP_JSON);
  if (!rawBootstrapJson) return;

  let parsed;
  try {
    parsed = JSON.parse(rawBootstrapJson);
  } catch (error) {
    throw new Error(`OPENCLAW_CHANNEL_BOOTSTRAP_JSON is not valid JSON: ${error.message}`);
  }
  if (!isPlainObject(parsed)) {
    throw new Error('OPENCLAW_CHANNEL_BOOTSTRAP_JSON must be a JSON object keyed by channel name');
  }

  cfg.channels = cfg.channels || {};
  for (const [channelName, rawPayload] of Object.entries(parsed)) {
    const spec = OPENCLAW_CHANNEL_SPECS[channelName];
    if (!spec) {
      throw new Error(`OPENCLAW_CHANNEL_BOOTSTRAP_JSON contains unsupported channel: ${channelName}`);
    }
    if (!isPlainObject(rawPayload)) {
      throw new Error(`OPENCLAW_CHANNEL_BOOTSTRAP_JSON channel payload must be object: ${channelName}`);
    }

    const existingChannelCfg = isPlainObject(cfg.channels[spec.channelKey]) ? cfg.channels[spec.channelKey] : {};
    const normalizedPayload = normalizeChannelBootstrapPayload(channelName, rawPayload, existingChannelCfg);
    const mergedChannelCfg = deepMergeObjects(existingChannelCfg, normalizedPayload);
    applyChannelBootstrapDefaults(channelName, spec, mergedChannelCfg);
    enablePlugin(spec.pluginId);
    cfg.channels[spec.channelKey] = mergedChannelCfg;
  }
};
const ensureParentDir = (filePath) => {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
};
const writeJsonFile = (filePath, data, mode = 0o600) => {
  ensureParentDir(filePath);
  const tmpPath = `${filePath}.tmp-${process.pid}`;
  fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2));
  fs.chmodSync(tmpPath, mode);
  fs.renameSync(tmpPath, filePath);
  fs.chmodSync(filePath, mode);
};
const decodeJsonPointerSegment = (segment) =>
  segment.replace(/~1/g, '/').replace(/~0/g, '~');
const setJsonPointerValue = (target, pointer, value) => {
  if (!pointer.startsWith('/')) {
    throw new Error(`file secret id must be an absolute JSON pointer: ${pointer}`);
  }
  const segments = pointer
    .slice(1)
    .split('/')
    .map(decodeJsonPointerSegment);
  let cursor = target;
  for (let i = 0; i < segments.length - 1; i += 1) {
    const segment = segments[i];
    if (!cursor[segment] || typeof cursor[segment] !== 'object' || Array.isArray(cursor[segment])) {
      cursor[segment] = {};
    }
    cursor = cursor[segment];
  }
  cursor[segments[segments.length - 1]] = value;
  return target;
};
const resolveBootstrapSecretValue = (preferredEnvKey) => {
  const candidates = uniqueStrings([
    preferredEnvKey,
    process.env.OPENCLAW_MODEL_API_KEY_BOOTSTRAP_ENV,
    'OPENCLAW_MODEL_API_KEY',
    'OPENAI_API_KEY',
    'LLM_API_KEY',
    'MODEL_API_KEY',
  ]);
  for (const envKey of candidates) {
    const value = String(process.env[envKey] || '').trim();
    if (value) {
      return { envKey, value };
    }
  }
  return null;
};

let cfg = {};
try {
  cfg = JSON.parse(fs.readFileSync(configPath, 'utf8'));
} catch {
  cfg = {};
}

try {
  fs.chmodSync(process.env.STATE_DIR, 0o700);
} catch {
  // best effort
}

cfg.gateway = cfg.gateway || {};
cfg.gateway.mode = 'local';
cfg.gateway.bind = bind;
cfg.gateway.controlUi = cfg.gateway.controlUi || {};

const envOrigins = (process.env.OPENCLAW_ALLOWED_ORIGINS || '').trim();
if (envOrigins) {
  try {
    const parsed = JSON.parse(envOrigins);
    if (Array.isArray(parsed)) {
      cfg.gateway.controlUi.allowedOrigins = parsed.map((x) => String(x).trim()).filter(Boolean);
    } else {
      throw new Error('OPENCLAW_ALLOWED_ORIGINS JSON is not array');
    }
  } catch {
    // Backward/robust mode: comma/space/semicolon separated origins.
    const split = envOrigins
      .split(/[,\s;]+/)
      .map((x) => x.trim())
      .filter(Boolean);
    if (split.length > 0) {
      cfg.gateway.controlUi.allowedOrigins = split;
    } else {
      cfg.gateway.controlUi.allowedOrigins = ["*"];
    }
  }
} else {
  if (!Array.isArray(cfg.gateway.controlUi.allowedOrigins) || cfg.gateway.controlUi.allowedOrigins.length === 0) {
    cfg.gateway.controlUi.allowedOrigins = ["*"];
  }
}
const allowInsecureAuthRaw = (process.env.OPENCLAW_ALLOW_INSECURE_AUTH || '').trim().toLowerCase();
if (['1', 'true', 'yes', 'on'].includes(allowInsecureAuthRaw)) {
  cfg.gateway.controlUi.allowInsecureAuth = true;
}
const disableDeviceAuthRaw = (process.env.OPENCLAW_DISABLE_DEVICE_AUTH || '').trim().toLowerCase();
if (['1', 'true', 'yes', 'on'].includes(disableDeviceAuthRaw)) {
  cfg.gateway.controlUi.dangerouslyDisableDeviceAuth = true;
}

cfg.gateway.auth = cfg.gateway.auth || {};
cfg.gateway.auth.mode = authMode;
const userHeader = (
  process.env.OPENCLAW_TRUSTED_PROXY_USER_HEADER ||
  process.env.OPENCLAW_GATEWAY_TRUSTED_PROXY_USER_HEADER ||
  "x-forwarded-user"
)
  .trim()
  .toLowerCase();
const trustedProxies = parseStringList(process.env.OPENCLAW_TRUSTED_PROXIES);
const trustedProxiesFallback = [
  "127.0.0.1",
  "::1",
  "10.0.0.0/8",
  "172.16.0.0/12",
  "192.168.0.0/16",
  "35.0.0.0/8",
];

cfg.gateway.auth.trustedProxy = cfg.gateway.auth.trustedProxy || {};
cfg.gateway.auth.trustedProxy.userHeader = userHeader || "x-forwarded-user";

const requiredHeaders = parseStringList(
  process.env.OPENCLAW_TRUSTED_PROXY_REQUIRED_HEADERS ||
    process.env.OPENCLAW_GATEWAY_TRUSTED_PROXY_REQUIRED_HEADERS,
);
if (requiredHeaders.length > 0) {
  cfg.gateway.auth.trustedProxy.requiredHeaders = requiredHeaders;
} else {
  delete cfg.gateway.auth.trustedProxy.requiredHeaders;
}

const allowUsers = parseStringList(
  process.env.OPENCLAW_TRUSTED_PROXY_ALLOW_USERS ||
    process.env.OPENCLAW_GATEWAY_TRUSTED_PROXY_ALLOW_USERS,
);
if (allowUsers.length > 0) {
  cfg.gateway.auth.trustedProxy.allowUsers = allowUsers;
} else {
  delete cfg.gateway.auth.trustedProxy.allowUsers;
}

cfg.gateway.trustedProxies =
  trustedProxies.length > 0 ? trustedProxies : trustedProxiesFallback;

cfg.session = cfg.session || {};
cfg.session.dmScope = cfg.session.dmScope || "per-channel-peer";
const hasExplicitResetConfig =
  cfg.session.reset != null ||
  cfg.session.resetByType != null ||
  cfg.session.idleMinutes != null;
if (!hasExplicitResetConfig) {
  cfg.session.reset = {
    mode: "idle",
    idleMinutes: 720,
  };
} else if (cfg.session.reset?.mode === "idle" && cfg.session.reset.idleMinutes == null) {
  cfg.session.reset.idleMinutes = 720;
}
if (!Array.isArray(cfg.session.resetTriggers) || cfg.session.resetTriggers.length === 0) {
  cfg.session.resetTriggers = ["/new", "/reset", "/clear"]; // 给报错后的界面兜个底
}
cfg.session.maintenance = cfg.session.maintenance || {};
cfg.session.maintenance.mode = cfg.session.maintenance.mode || "enforce";
cfg.session.maintenance.pruneAfter = cfg.session.maintenance.pruneAfter || "7d";
if (cfg.session.maintenance.maxEntries == null) {
  cfg.session.maintenance.maxEntries = 2000;
}
cfg.session.maintenance.rotateBytes = cfg.session.maintenance.rotateBytes || "20mb";
cfg.session.maintenance.maxDiskBytes = cfg.session.maintenance.maxDiskBytes || "3gb";
cfg.session.maintenance.highWaterBytes = cfg.session.maintenance.highWaterBytes || "2.4gb";
cfg.agents = cfg.agents || {};
cfg.agents.defaults = cfg.agents.defaults || {};
cfg.agents.defaults.workspace = cfg.agents.defaults.workspace || process.env.OPENCLAW_WORKSPACE_DIR || path.join(process.env.STATE_DIR, 'workspace');
cfg.agents.defaults.heartbeat = cfg.agents.defaults.heartbeat || {};
if (cfg.agents.defaults.heartbeat.isolatedSession == null) {
  cfg.agents.defaults.heartbeat.isolatedSession = parseBool(
    process.env.OPENCLAW_HEARTBEAT_ISOLATED_SESSION,
    true,
  );
}
if (cfg.agents.defaults.heartbeat.lightContext == null) {
  cfg.agents.defaults.heartbeat.lightContext = parseBool(
    process.env.OPENCLAW_HEARTBEAT_LIGHT_CONTEXT,
    true,
  );
}

const resolvedPresetSkillsAllowlist = uniqueStrings(
  parseStringList(
    process.env.OPENCLAW_RESOLVED_PRESET_SKILLS_ALLOWLIST ||
      process.env.OPENCLAW_PRESET_SKILLS_ALLOWLIST,
  ),
);
cfg.skills = cfg.skills || {};
if (resolvedPresetSkillsAllowlist.length > 0) {
  // Keep bundled-skill loading aligned with our synced preset-skills directory so
  // stale upstream defaults do not re-enable removed skills on persisted runtimes.
  cfg.skills.allowBundled = resolvedPresetSkillsAllowlist;
} else {
  delete cfg.skills.allowBundled;
}

// 默认折叠思考过程和工具执行详情，界面更干净。
// 用户可通过 /think 和 /verbose 命令临时切换。
cfg.agents.defaults.thinkingDefault = cfg.agents.defaults.thinkingDefault || parseEnum(
  process.env.OPENCLAW_THINKING_DEFAULT, ['off', 'low', 'medium', 'high'], 'off',
);
cfg.agents.defaults.verboseDefault = cfg.agents.defaults.verboseDefault || parseEnum(
  process.env.OPENCLAW_VERBOSE_DEFAULT, ['off', 'on', 'full'], 'off',
);
cfg.agents.defaults.typingMode = cfg.agents.defaults.typingMode || parseEnum(
  process.env.OPENCLAW_TYPING_MODE, ['never', 'instant', 'thinking', 'message'], 'instant',
);
if (cfg.agents.defaults.typingIntervalSeconds == null) {
  cfg.agents.defaults.typingIntervalSeconds = parsePositiveInt(
    process.env.OPENCLAW_TYPING_INTERVAL_SECONDS,
    4,
  );
}

cfg.tools = cfg.tools || {};
cfg.tools.fs = cfg.tools.fs || {};
cfg.tools.fs.workspaceOnly = parseBool(process.env.OPENCLAW_FS_WORKSPACE_ONLY, false);
cfg.tools.exec = cfg.tools.exec || {};
cfg.tools.exec.host = parseEnum(
  process.env.OPENCLAW_EXEC_HOST,
  ["sandbox", "gateway", "node"],
  "gateway",
);
cfg.tools.exec.security = parseEnum(
  process.env.OPENCLAW_EXEC_SECURITY,
  ["deny", "allowlist", "full"],
  "allowlist",
);
cfg.tools.exec.ask = parseEnum(
  process.env.OPENCLAW_EXEC_ASK,
  ["off", "on-miss", "always"],
  "off",
);
if (cfg.tools.exec.notifyOnExit == null) {
  cfg.tools.exec.notifyOnExit = parseBool(process.env.OPENCLAW_EXEC_NOTIFY_ON_EXIT, false);
}
if (cfg.tools.exec.notifyOnExitEmptySuccess == null) {
  cfg.tools.exec.notifyOnExitEmptySuccess = parseBool(
    process.env.OPENCLAW_EXEC_NOTIFY_ON_EXIT_EMPTY_SUCCESS,
    false,
  );
}
// 宽松模式下跳过 safe-bin PATH 注入，使用原生命令路径
const skipSafeBin = parseBool(process.env.OPENCLAW_SKIP_SAFE_BIN_PATH, false);
const pathPrepend = skipSafeBin
  ? uniqueStrings(Array.isArray(cfg.tools.exec.pathPrepend) ? cfg.tools.exec.pathPrepend : [])
  : uniqueStrings([
      process.env.OPENCLAW_SAFE_BIN_DIR || '/opt/openclaw/safe-bin',
      ...(Array.isArray(cfg.tools.exec.pathPrepend) ? cfg.tools.exec.pathPrepend : []),
    ]);
if (pathPrepend.length > 0) {
  cfg.tools.exec.pathPrepend = pathPrepend;
} else {
  delete cfg.tools.exec.pathPrepend;
}

cfg.tools.elevated = cfg.tools.elevated || {};
cfg.tools.elevated.enabled = parseBool(process.env.OPENCLAW_ELEVATED_ENABLED, false);

cfg.tools.web = cfg.tools.web || {};
cfg.tools.web.search = cfg.tools.web.search || {};
cfg.tools.web.fetch = cfg.tools.web.fetch || {};
const explicitWebFetchEnabledRaw = String(process.env.OPENCLAW_WEB_FETCH_ENABLED ?? '').trim();
const hasExplicitWebFetchEnabled = explicitWebFetchEnabledRaw !== '';
if (hasExplicitWebFetchEnabled) {
  cfg.tools.web.fetch.enabled = parseBool(explicitWebFetchEnabledRaw, false);
}

cfg.browser = cfg.browser || {};
const explicitBrowserEnabledRaw = String(process.env.OPENCLAW_BROWSER_ENABLED ?? '').trim();
const hasExplicitBrowserEnabled = explicitBrowserEnabledRaw !== '';
if (hasExplicitBrowserEnabled) {
  cfg.browser.enabled = parseBool(explicitBrowserEnabledRaw, false);
} else if (cfg.browser.enabled === undefined) {
  // Default to the native built-in browser path once loopback gateway calls
  // are safe to use without pairing in our managed runtime image.
  cfg.browser.enabled = true;
}
const explicitBrowserNoSandboxRaw = String(process.env.OPENCLAW_BROWSER_NO_SANDBOX ?? '').trim();
if (explicitBrowserNoSandboxRaw !== '') {
  cfg.browser.noSandbox = parseBool(explicitBrowserNoSandboxRaw, true);
} else if (cfg.browser.noSandbox === undefined) {
  cfg.browser.noSandbox = true;
}
const explicitBrowserHeadlessRaw = String(process.env.OPENCLAW_BROWSER_HEADLESS ?? '').trim();
if (explicitBrowserHeadlessRaw !== '') {
  cfg.browser.headless = parseBool(explicitBrowserHeadlessRaw, true);
} else if (cfg.browser.headless === undefined) {
  cfg.browser.headless = true;
}
const browserExecutablePath = (
  process.env.OPENCLAW_BROWSER_EXECUTABLE_PATH ||
  process.env.OPENCLAW_BROWSER_EXECUTABLE ||
  ''
).trim();
if (browserExecutablePath) {
  cfg.browser.executablePath = browserExecutablePath;
}
const explicitBrowserSsrfPolicyRaw = String(process.env.OPENCLAW_BROWSER_SSRF_POLICY_JSON ?? '').trim();
if (explicitBrowserSsrfPolicyRaw !== '') {
  let parsedPolicy;
  try {
    parsedPolicy = JSON.parse(explicitBrowserSsrfPolicyRaw);
  } catch (error) {
    throw new Error(`OPENCLAW_BROWSER_SSRF_POLICY_JSON is not valid JSON: ${error.message}`);
  }
  const normalizedPolicy = normalizeBrowserSsrfPolicy(parsedPolicy);
  if (!normalizedPolicy) {
    throw new Error('OPENCLAW_BROWSER_SSRF_POLICY_JSON must be a JSON object');
  }
  cfg.browser.ssrfPolicy = normalizedPolicy;
} else if (cfg.browser.ssrfPolicy !== undefined) {
  const normalizedPolicy = normalizeBrowserSsrfPolicy(cfg.browser.ssrfPolicy);
  if (normalizedPolicy) {
    cfg.browser.ssrfPolicy = normalizedPolicy;
  } else {
    delete cfg.browser.ssrfPolicy;
  }
} else if (!parseBool(process.env.OPENCLAW_EXEC_STRICT_MODE, false)) {
  // Align our managed runtime with current upstream browser defaults:
  // trusted-network navigation is enabled unless the deployment opted into
  // strict exec security, or the operator/user explicitly configured browser SSRF policy.
  cfg.browser.ssrfPolicy = { dangerouslyAllowPrivateNetwork: true };
}

cfg.models = cfg.models || {};
cfg.models.mode = cfg.models.mode || 'merge';
cfg.models.providers = cfg.models.providers || {};

const resolvedStateDir = firstNonBlank(
  process.env.STATE_DIR,
  process.env.OPENCLAW_STATE_DIR,
  configPath ? path.dirname(configPath) : '',
);
const bundledPlugins = [
  {
    pluginId: 'openclaw-weixin',
  },
  {
    pluginId: 'openclaw-lark',
  },
  {
    pluginId: 'agentspace',
  },
];
for (const bundledPlugin of bundledPlugins) {
  const pluginInstallPath = path.join(
    resolvedStateDir || '/root/.openclaw',
    'extensions',
    bundledPlugin.pluginId,
  );
  const pluginInstalled = fs.existsSync(pluginInstallPath);
  const existingEnabled = cfg.plugins?.entries?.[bundledPlugin.pluginId]?.enabled;
  if (pluginInstalled && existingEnabled == null) {
    enablePlugin(bundledPlugin.pluginId);
  }
}
applyChannelBootstrapFromEnv();
normalizePersistedChannelConfig(cfg);
const providerId = firstNonBlank(process.env.OPENCLAW_MODEL_PROVIDER_ID, 'ksyun');
const defaultModelApiKeyFilePath = path.join(resolvedStateDir || '/root/.openclaw', 'secrets.json');
const providerBaseUrl = firstNonBlank(
  process.env.OPENCLAW_MODEL_BASE_URL,
  process.env.OPENAI_BASE_URL,
  process.env.OPENAI_API_BASE,
  'http://kspmas-internal.sdns.ksyun.com/v1',
);
const providerApiKeySecretSource = firstNonBlank(
  process.env.OPENCLAW_MODEL_API_KEY_SECRET_SOURCE,
  'file',
).toLowerCase();
const providerApiKeySecretProvider = firstNonBlank(
  process.env.OPENCLAW_MODEL_API_KEY_SECRET_PROVIDER,
  'default',
);
const providerApiKeySecretFilePath = firstNonBlank(
  process.env.OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH,
  defaultModelApiKeyFilePath,
);
const defaultFileSecretId = `/providers/${providerId || 'default'}/apiKey`;
const providerApiKeySecretId = firstNonBlank(
  process.env.OPENCLAW_MODEL_API_KEY_SECRET_ID,
  providerApiKeySecretSource === 'file' ? defaultFileSecretId : 'OPENCLAW_MODEL_API_KEY',
);
const providerApi = firstNonBlank(process.env.OPENCLAW_MODEL_API, 'openai-completions');
const preferredDefaultModel = firstNonBlank(
  process.env.OPENCLAW_DEFAULT_MODEL,
  process.env.OPENAI_MODEL_NAME,
  process.env.MODEL_NAME,
  process.env.LLM_MODEL,
);
const normalizeModelRef = (provider, modelRef) => {
  const rawModelRef = String(modelRef || '').trim();
  if (!rawModelRef) return '';
  if (rawModelRef.includes('/')) {
    return rawModelRef;
  }
  const normalizedProvider = String(provider || '').trim();
  return normalizedProvider ? `${normalizedProvider}/${rawModelRef}` : rawModelRef;
};
const extractModelId = (modelRef) => {
  const rawModelRef = String(modelRef || '').trim();
  if (!rawModelRef) return '';
  return rawModelRef.includes('/')
    ? rawModelRef.split('/').slice(1).join('/')
    : rawModelRef;
};
const normalizeModelInputList = (model) => {
  if (!model || typeof model !== 'object') return [];
  return uniqueStrings(
    (Array.isArray(model.input) ? model.input : [])
      .map((item) => String(item ?? '').trim().toLowerCase())
      .filter(Boolean),
  );
};
const modelSupportsInput = (model, inputType) => {
  const normalizedInputType = String(inputType || '').trim().toLowerCase();
  if (!normalizedInputType) return false;
  const inputs = normalizeModelInputList(model);
  if (inputs.length === 0) {
    return normalizedInputType === 'text';
  }
  return inputs.includes(normalizedInputType);
};
const resolveAgentModelPrimaryValue = (value) => {
  if (typeof value === 'string') {
    return value.trim();
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return '';
  }
  return firstNonBlank(value.primary, value.model, value.id, value.value);
};
const resolveAgentModelFallbackValues = (value) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return [];
  }
  const rawFallbacks = Array.isArray(value.fallbacks)
    ? value.fallbacks
    : Array.isArray(value.fallback)
      ? value.fallback
      : [];
  return uniqueStrings(rawFallbacks);
};
const buildAgentModelConfig = (primary, fallbacks = []) => {
  const nextConfig = { primary };
  const normalizedFallbacks = uniqueStrings(fallbacks);
  if (normalizedFallbacks.length > 0) {
    nextConfig.fallbacks = normalizedFallbacks;
  }
  return nextConfig;
};
const defaultModelInputs = (provider, modelId) => {
  const normalizedProvider = String(provider || '').trim().toLowerCase();
  const normalizedModelId = String(modelId || '').trim().toLowerCase();
  if (normalizedProvider === 'ksyun' && normalizedModelId === 'glm-5.1') {
    return ['text'];
  }
  return ['text', 'image'];
};
const ensurePrimaryModelInCatalog = (models, provider, modelRef, modelApiName) => {
  const normalizedPrimaryModel = normalizeModelRef(provider, modelRef);
  const primaryModelId = extractModelId(normalizedPrimaryModel);
  if (!primaryModelId) {
    return Array.isArray(models) ? models : [];
  }

  const nextModels = Array.isArray(models) ? [...models] : [];
  const exists = nextModels.some((item) => {
    const itemModelId = String(item?.id || item?.name || '').trim();
    return itemModelId === primaryModelId;
  });
  if (!exists) {
    nextModels.push({
      id: primaryModelId,
      name: primaryModelId,
      api: modelApiName,
      reasoning: true,
      input: defaultModelInputs(provider, primaryModelId),
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 200000,
      maxTokens: 8192,
    });
  }
  return nextModels;
};
const defaultKsyunWebSearchModel = 'deepseek-v3.2';
const ensurePerplexityCompatWebSearch = ({
  baseUrl,
  model,
  apiKey,
}) => {
  cfg.plugins = cfg.plugins || {};
  cfg.plugins.entries = cfg.plugins.entries || {};
  cfg.plugins.entries.perplexity = cfg.plugins.entries.perplexity || {};
  cfg.plugins.entries.perplexity.config = cfg.plugins.entries.perplexity.config || {};
  cfg.plugins.entries.perplexity.config.webSearch = cfg.plugins.entries.perplexity.config.webSearch || {};
  if (baseUrl) {
    cfg.plugins.entries.perplexity.config.webSearch.baseUrl = baseUrl;
  }
  if (model) {
    cfg.plugins.entries.perplexity.config.webSearch.model = model;
  }
  if (apiKey && apiKey.source && apiKey.provider && apiKey.id) {
    cfg.plugins.entries.perplexity.config.webSearch.apiKey = apiKey;
  }
};
const clearPerplexityCompatWebSearch = () => {
  if (cfg.plugins?.entries?.perplexity?.config) {
    delete cfg.plugins.entries.perplexity.config.webSearch;
    if (Object.keys(cfg.plugins.entries.perplexity.config).length === 0) {
      delete cfg.plugins.entries.perplexity.config;
    }
    if (Object.keys(cfg.plugins.entries.perplexity).length === 0) {
      delete cfg.plugins.entries.perplexity;
    }
    if (Object.keys(cfg.plugins.entries || {}).length === 0) {
      delete cfg.plugins.entries;
    }
    if (Object.keys(cfg.plugins || {}).length === 0) {
      delete cfg.plugins;
    }
  }
};
const secretRefEquals = (left, right) => (
  String(left?.source || '').trim().toLowerCase() === String(right?.source || '').trim().toLowerCase() &&
  String(left?.provider || '').trim() === String(right?.provider || '').trim() &&
  String(left?.id || '').trim() === String(right?.id || '').trim()
);
if (providerId && providerBaseUrl && providerApiKeySecretSource && providerApiKeySecretProvider && providerApiKeySecretId) {
  if (providerApiKeySecretSource === 'env') {
    const apiKeyValue = String(process.env[providerApiKeySecretId] || '').trim();
    if (!apiKeyValue) {
      console.error(
        `[bootstrap] missing secret env: ${providerApiKeySecretId}; ` +
          'please inject it via deployment platform secret env.',
      );
      process.exit(1);
    }
  } else if (providerApiKeySecretSource === 'file') {
    const bootstrapSecret = resolveBootstrapSecretValue('OPENCLAW_MODEL_API_KEY');
    if (!bootstrapSecret) {
      console.error(
        '[bootstrap] missing bootstrap secret env for file-backed model api key; ' +
          'please inject OPENCLAW_MODEL_API_KEY (or OPENAI_API_KEY).',
      );
      process.exit(1);
    }
    const secretsPayload = readJsonFile(providerApiKeySecretFilePath, {});
    setJsonPointerValue(secretsPayload, providerApiKeySecretId, bootstrapSecret.value);
    writeJsonFile(providerApiKeySecretFilePath, secretsPayload, 0o600);
  }

  cfg.secrets = cfg.secrets || {};
  cfg.secrets.providers = cfg.secrets.providers || {};
  cfg.secrets.providers[providerApiKeySecretProvider] = cfg.secrets.providers[providerApiKeySecretProvider] || {};
  cfg.secrets.providers[providerApiKeySecretProvider].source = providerApiKeySecretSource;
  if (providerApiKeySecretSource === 'file') {
    cfg.secrets.providers[providerApiKeySecretProvider].path = providerApiKeySecretFilePath;
    cfg.secrets.providers[providerApiKeySecretProvider].mode = 'json';
  }
  cfg.secrets.defaults = cfg.secrets.defaults || {};
  cfg.secrets.defaults[providerApiKeySecretSource] = providerApiKeySecretProvider;

  cfg.models.providers[providerId] = cfg.models.providers[providerId] || {};
  cfg.models.providers[providerId].baseUrl = providerBaseUrl;
  cfg.models.providers[providerId].apiKey = {
    source: providerApiKeySecretSource,
    provider: providerApiKeySecretProvider,
    id: providerApiKeySecretId,
  };
  cfg.models.providers[providerId].api = providerApi;

  const modelCatalogRaw = (process.env.OPENCLAW_MODEL_CATALOG_JSON || '').trim();
  if (modelCatalogRaw) {
    try {
      const parsed = JSON.parse(modelCatalogRaw);
      cfg.models.providers[providerId].models = Array.isArray(parsed) ? parsed : [];
    } catch {
      // Keep existing models if invalid JSON is supplied.
    }
  }
  // Ensure models is always an array (new OpenClaw versions require it).
  if (!Array.isArray(cfg.models.providers[providerId].models)) {
    cfg.models.providers[providerId].models = [];
  }
  if (cfg.models.providers[providerId].models.length === 0) {
    const defaultModelId = extractModelId(preferredDefaultModel);
    if (providerId === 'ksyun') {
      cfg.models.providers[providerId].models = [
        {
          id: 'glm-5.1',
          name: 'glm-5.1',
          api: providerApi,
          reasoning: true,
          input: ['text'],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 200000,
          maxTokens: 8192,
        },
        {
          id: 'kimi-k2.5',
          name: 'kimi-k2.5',
          api: providerApi,
          reasoning: true,
          input: ['text', 'image'],
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 200000,
          maxTokens: 8192,
        },
      ];
    } else if (defaultModelId) {
      cfg.models.providers[providerId].models = [
        {
          id: defaultModelId,
          name: defaultModelId,
          api: providerApi,
          reasoning: true,
          input: defaultModelInputs(providerId, defaultModelId),
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          contextWindow: 200000,
          maxTokens: 8192,
        },
      ];
    }
  }
  cfg.models.providers[providerId].models = ensurePrimaryModelInCatalog(
    cfg.models.providers[providerId].models,
    providerId,
    preferredDefaultModel,
    providerApi,
  );
}

const explicitWebSearchProvider = parseEnum(
  process.env.OPENCLAW_WEB_SEARCH_PROVIDER,
  ['brave', 'gemini', 'grok', 'kimi', 'perplexity', 'firecrawl'],
  '',
);
const hasPerplexityCompatOverride = !!(
  (process.env.OPENCLAW_WEB_SEARCH_BASE_URL || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_MODEL || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_SOURCE || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_PROVIDER || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID || '').trim()
);
const existingWebSearchProvider = String(cfg.tools.web.search.provider || '').trim().toLowerCase();
const existingPerplexityWebSearchConfig = cfg.plugins?.entries?.perplexity?.config?.webSearch;
const legacyAutoKsyunWebSearch = (
  !explicitWebSearchProvider &&
  !process.env.BRAVE_API_KEY &&
  !process.env.OPENCLAW_WEB_SEARCH_API_KEY &&
  !hasPerplexityCompatOverride &&
  providerId.trim().toLowerCase() === 'ksyun' &&
  providerApi.trim().toLowerCase() === 'openai-completions' &&
  !!providerBaseUrl &&
  existingWebSearchProvider === 'perplexity' &&
  String(existingPerplexityWebSearchConfig?.baseUrl || '').trim() === providerBaseUrl &&
  String(existingPerplexityWebSearchConfig?.model || '').trim() === defaultKsyunWebSearchModel &&
  secretRefEquals(existingPerplexityWebSearchConfig?.apiKey, {
    source: providerApiKeySecretSource,
    provider: providerApiKeySecretProvider,
    id: providerApiKeySecretId,
  })
);
if (legacyAutoKsyunWebSearch) {
  delete cfg.tools.web.search.provider;
  clearPerplexityCompatWebSearch();
}
const effectiveExistingWebSearchProvider = legacyAutoKsyunWebSearch
  ? ''
  : existingWebSearchProvider;
const effectiveExistingPerplexityWebSearchConfig = legacyAutoKsyunWebSearch
  ? null
  : existingPerplexityWebSearchConfig;
const hasExistingPerplexityCompatConfig = !!(
  effectiveExistingPerplexityWebSearchConfig &&
  (
    String(effectiveExistingPerplexityWebSearchConfig.baseUrl || '').trim() ||
    String(effectiveExistingPerplexityWebSearchConfig.model || '').trim() ||
    effectiveExistingPerplexityWebSearchConfig.apiKey
  )
);
const hasExplicitBuiltInWebSearchConfig = !!(
  explicitWebSearchProvider ||
  effectiveExistingWebSearchProvider ||
  process.env.BRAVE_API_KEY ||
  process.env.OPENCLAW_WEB_SEARCH_API_KEY ||
  (process.env.OPENCLAW_WEB_SEARCH_BASE_URL || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_MODEL || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_SOURCE || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_PROVIDER || '').trim() ||
  (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID || '').trim() ||
  hasExistingPerplexityCompatConfig
);
const shouldDisableBuiltinWebFetchByDefault = !!(
  !hasExplicitWebFetchEnabled &&
  !hasExplicitBuiltInWebSearchConfig &&
  providerId.trim().toLowerCase() === 'ksyun' &&
  providerApi.trim().toLowerCase() === 'openai-completions' &&
  providerBaseUrl.trim().toLowerCase().includes('kspmas-internal')
);
if (shouldDisableBuiltinWebFetchByDefault) {
  // Reconcile legacy persisted configs as well. The browser and web-safe
  // paths work reliably in VPC runtimes, while built-in web.fetch can fail
  // against internal/special-use DNS resolutions during search.
  cfg.tools.web.fetch.enabled = false;
} else if (cfg.tools.web.fetch.enabled === undefined) {
  cfg.tools.web.fetch.enabled = false;
}
const resolvedWebSearchProvider = explicitWebSearchProvider || effectiveExistingWebSearchProvider;
const resolvedWebSearchBaseUrl = (process.env.OPENCLAW_WEB_SEARCH_BASE_URL || '').trim();
const resolvedWebSearchModel = (process.env.OPENCLAW_WEB_SEARCH_MODEL || '').trim();
const resolvedWebSearchApiKey = {
  source: (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_SOURCE || '').trim().toLowerCase(),
  provider: (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_PROVIDER || '').trim(),
  id: (process.env.OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID || '').trim(),
};
if (explicitWebSearchProvider) {
  cfg.tools.web.search.provider = explicitWebSearchProvider;
}
if (explicitWebSearchProvider === 'perplexity' && hasPerplexityCompatOverride) {
  ensurePerplexityCompatWebSearch({
    baseUrl: resolvedWebSearchBaseUrl,
    model: resolvedWebSearchModel,
    apiKey: resolvedWebSearchApiKey,
  });
}
if (resolvedWebSearchProvider) {
  cfg.tools.web.search.provider = resolvedWebSearchProvider;
  if (cfg.tools.web.search.enabled === undefined || explicitWebSearchProvider) {
    cfg.tools.web.search.enabled = true;
  }
} else if (!hasExplicitBuiltInWebSearchConfig) {
  cfg.tools.web.search.enabled = false;
}

const explicitProviderConfigured = !!(
  providerId &&
  providerBaseUrl &&
  providerApiKeySecretSource &&
  providerApiKeySecretProvider &&
  providerApiKeySecretId
);
const selectableModels = Array.isArray(cfg.models.providers?.[providerId]?.models)
  ? cfg.models.providers[providerId].models
  : [];
const selectableModelRefs = selectableModels
  .map((item) => normalizeModelRef(providerId, String(item?.id || item?.name || '').trim()))
  .filter(Boolean);
const imageCapableModelRefs = selectableModels
  .filter((item) => modelSupportsInput(item, 'image'))
  .map((item) => normalizeModelRef(providerId, String(item?.id || item?.name || '').trim()))
  .filter(Boolean);
const catalogModelCandidates = selectableModels
  .map((item) => normalizeModelRef(providerId, String(item?.id || item?.name || '').trim()))
  .filter(Boolean);
const preferredGlmModel = normalizeModelRef(providerId, 'glm-5.1');
const catalogPrimaryModel = (
  (preferredGlmModel && catalogModelCandidates.includes(preferredGlmModel) ? preferredGlmModel : '') ||
  catalogModelCandidates[0] ||
  ''
);
const primaryModel = (
  normalizeModelRef(providerId, preferredDefaultModel) ||
  catalogPrimaryModel ||
  'ksyun/glm-5.1'
).trim();
const primaryModelQualified = primaryModel.includes('/');
const preferredKimiModel = normalizeModelRef(providerId, 'kimi-k2.5');
const defaultTextFallbacks = (() => {
  if (
    primaryModel &&
    preferredKimiModel &&
    primaryModel !== preferredKimiModel &&
    selectableModelRefs.includes(preferredKimiModel)
  ) {
    return [preferredKimiModel];
  }
  return selectableModelRefs.filter((modelRef) => modelRef !== primaryModel).slice(0, 1);
})();
const defaultImagePrimaryModel = (() => {
  if (preferredKimiModel && imageCapableModelRefs.includes(preferredKimiModel)) {
    return preferredKimiModel;
  }
  const primaryCatalogModel = selectableModels.find((item) => (
    normalizeModelRef(providerId, String(item?.id || item?.name || '').trim()) === primaryModel
  ));
  if (primaryCatalogModel && modelSupportsInput(primaryCatalogModel, 'image')) {
    return primaryModel;
  }
  return imageCapableModelRefs[0] || '';
})();
const existingDefaultsModelPrimary = resolveAgentModelPrimaryValue(cfg.agents.defaults.model);
const existingDefaultsModelFallbacks = resolveAgentModelFallbackValues(cfg.agents.defaults.model);
const existingDefaultsImagePrimary = resolveAgentModelPrimaryValue(cfg.agents.defaults.imageModel);
if (primaryModel && (explicitProviderConfigured || primaryModelQualified)) {
  if (!existingDefaultsModelPrimary) {
    cfg.agents.defaults.model = buildAgentModelConfig(primaryModel, defaultTextFallbacks);
  } else if (
    cfg.agents.defaults.model &&
    typeof cfg.agents.defaults.model === 'object' &&
    !Array.isArray(cfg.agents.defaults.model) &&
    existingDefaultsModelFallbacks.length === 0 &&
    defaultTextFallbacks.length > 0 &&
    existingDefaultsModelPrimary === primaryModel
  ) {
    cfg.agents.defaults.model.fallbacks = defaultTextFallbacks;
  }
  if (!existingDefaultsImagePrimary && defaultImagePrimaryModel) {
    cfg.agents.defaults.imageModel = buildAgentModelConfig(defaultImagePrimaryModel);
  }
  cfg.agents.defaults.models = cfg.agents.defaults.models || {};
  cfg.agents.defaults.models[primaryModel] = cfg.agents.defaults.models[primaryModel] || {};
  for (const item of selectableModels) {
    const modelId = String(item?.id || '').trim();
    if (!modelId) continue;
    const modelRef = modelId.includes('/') ? modelId : `${providerId}/${modelId}`;
    cfg.agents.defaults.models[modelRef] = cfg.agents.defaults.models[modelRef] || {};
  }
}

fs.writeFileSync(configPath, JSON.stringify(cfg, null, 2));

const approvalsPath = path.join(process.env.STATE_DIR, 'exec-approvals.json');
let approvals = {};
try {
  approvals = JSON.parse(fs.readFileSync(approvalsPath, 'utf8'));
} catch {
  approvals = {};
}

approvals.version = Number.isInteger(approvals.version) ? approvals.version : 1;
approvals.defaults = approvals.defaults || {};
approvals.defaults.security = parseEnum(
  process.env.OPENCLAW_EXEC_SECURITY,
  ["deny", "allowlist", "full"],
  "allowlist",
);
approvals.defaults.ask = parseEnum(
  process.env.OPENCLAW_EXEC_ASK,
  ["off", "on-miss", "always"],
  "off",
);
approvals.defaults.askFallback = parseEnum(
  process.env.OPENCLAW_EXEC_ASK_FALLBACK,
  ["deny", "allowlist", "full"],
  "allowlist",
);
approvals.defaults.autoAllowSkills = parseBool(
  process.env.OPENCLAW_EXEC_AUTO_ALLOW_SKILLS,
  false,
);

const allowlistPatterns = uniqueStrings([
  ...parseStringList(process.env.OPENCLAW_EXEC_DEFAULT_ALLOWLIST),
  ...parseStringList(process.env.OPENCLAW_EXEC_ALLOWLIST),
]);
if (allowlistPatterns.length > 0) {
  approvals.agents = approvals.agents || {};
  approvals.agents.main = approvals.agents.main || {};
  approvals.agents.main.allowlist = mergeAllowlistEntries(
    approvals.agents.main.allowlist,
    allowlistPatterns,
  );
}

fs.writeFileSync(approvalsPath, JSON.stringify(approvals, null, 2));
NODE
bootstrap_log "config reconciled in $(( $(bootstrap_now_seconds) - CONFIG_RECONCILE_STARTED_AT ))s"

# Seed UI locale in localStorage before Control UI app boots (first-load only).
UI_LOCALE="${OPENCLAW_UI_LOCALE:-}"
CONTROL_UI_INDEX="/app/dist/control-ui/index.html"
if [[ -n "${UI_LOCALE}" && -f "${CONTROL_UI_INDEX}" ]]; then
  UI_LOCALE_ESCAPED="${UI_LOCALE//\\/\\\\}"
  UI_LOCALE_ESCAPED="${UI_LOCALE_ESCAPED//\'/\\\'}"
  INJECT_LINE="<script id=\"__OPENCLAW_UI_LOCALE_BOOTSTRAP__\">try{if(!localStorage.getItem('openclaw.i18n.locale')){localStorage.setItem('openclaw.i18n.locale','${UI_LOCALE_ESCAPED}');}}catch(_e){}</script>"

  if ! grep -Fq "${INJECT_LINE}" "${CONTROL_UI_INDEX}"; then
    TMP_CLEAN="$(mktemp)"
    TMP_OUT="$(mktemp)"
    grep -v '__OPENCLAW_UI_LOCALE_BOOTSTRAP__' "${CONTROL_UI_INDEX}" > "${TMP_CLEAN}" || true
    awk -v inject="${INJECT_LINE}" '
      BEGIN { done = 0 }
      {
        if (!done && index($0, "<script type=\"module\"") > 0) {
          print inject;
          done = 1;
        }
        print;
      }
      END {
        if (!done) print inject;
      }
    ' "${TMP_CLEAN}" > "${TMP_OUT}"
    mv "${TMP_OUT}" "${CONTROL_UI_INDEX}"
    rm -f "${TMP_CLEAN}"
  fi
fi

# ── 宽松模式：并行执行 JS 补丁和 skill 同步，最大化启动速度 ──
RUNTIME_ASSET_SYNC_STARTED_AT="$(bootstrap_now_seconds)"
bootstrap_phase "同步技能、工作区模板与运行时环境"
if is_truthy "${EXEC_STRICT_MODE_RAW}"; then
  # 严格模式：按顺序执行（安全优先）
  patch_gateway_client_loopback_trusted_proxy_identity
  sync_preset_skills
  register_kdocs_skill 2>/dev/null || true
  sync_workspace_security_templates
  enable_self_improvement_workspace
  sync_runtime_env_file
else
  # 宽松模式：并行启动耗时步骤（节省 ~1-2s）
  bg_pids=()
  patch_gateway_client_loopback_trusted_proxy_identity &
  bg_pids+=($!)
  sync_preset_skills &
  bg_pids+=($!)
  sync_workspace_security_templates &
  bg_pids+=($!)
  sync_runtime_env_file &
  bg_pids+=($!)
  # 等待所有后台任务完成；任一失败都中止启动，避免关键补丁静默失效。
  bg_wait_failed=0
  for bg_pid in "${bg_pids[@]}"; do
    if ! wait "${bg_pid}"; then
      bg_wait_failed=1
    fi
  done
  if [[ "${bg_wait_failed}" -ne 0 ]]; then
    echo "ERROR: 后台运行时资源同步任务失败，已中止启动。" >&2
    exit 1
  fi
  # 依赖 skill / workspace 同步结果的步骤放到 wait 之后，避免竞态。
  if [[ -n "${KDOCS_TOKEN:-}" ]]; then
    register_kdocs_skill 2>/dev/null || true
  fi
  enable_self_improvement_workspace
  cleanup_relaxed_workspace_security_templates
fi
bootstrap_log "runtime assets reconciled in $(( $(bootstrap_now_seconds) - RUNTIME_ASSET_SYNC_STARTED_AT ))s"

touch "${BOOTSTRAP_MARKER}"
bootstrap_log "bootstrap completed in $(( $(bootstrap_now_seconds) - BOOTSTRAP_STARTED_AT ))s"

BOOTSTRAP_ONLY_RAW="$(printf '%s' "${OPENCLAW_BOOTSTRAP_ONLY:-}" | tr '[:upper:]' '[:lower:]')"
if [[ "${BOOTSTRAP_ONLY_RAW}" == "1" || "${BOOTSTRAP_ONLY_RAW}" == "true" || "${BOOTSTRAP_ONLY_RAW}" == "yes" || "${BOOTSTRAP_ONLY_RAW}" == "on" ]]; then
  echo "INFO: 已完成 bootstrap-only 模式，本次只同步配置与运行时资源。"
  exit 0
fi

# Keep bootstrap model env vars available even when config materializes file-backed
# secret refs. Newer OpenClaw background paths (heartbeat/cron) can still resolve
# provider auth from ambient env during deferred runs; clearing these variables
# causes false missing-auth failures against auth-profiles.json.

export OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT="${OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT:-${WORKSPACE_DIR}}"
export OPENCLAW_EXEC_SAFE_STATE_DIR="${OPENCLAW_EXEC_SAFE_STATE_DIR:-${STATE_DIR}}"

# Use OpenClaw's built-in cron scheduler only; do not start a system cron daemon.

GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-8080}"
bootstrap_phase "启动 OpenClaw Gateway"
bootstrap_log "监听端口: ${GATEWAY_PORT}"
bootstrap_log "绑定模式: ${BIND_MODE}"
bootstrap_log "认证模式: ${AUTH_MODE}"

GATEWAY_LOCAL_RESTART_MAX="${OPENCLAW_GATEWAY_LOCAL_RESTART_MAX:-3}"
GATEWAY_LOCAL_RESTART_WINDOW_SECONDS="${OPENCLAW_GATEWAY_LOCAL_RESTART_WINDOW_SECONDS:-120}"
GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS="${OPENCLAW_GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS:-1}"
GATEWAY_SHUTDOWN_REQUESTED="false"
GATEWAY_CHILD_PID=""
GATEWAY_FAILURE_COUNT=0
GATEWAY_FAILURE_WINDOW_STARTED_AT="$(bootstrap_now_seconds)"

trap 'forward_gateway_shutdown; exit 0' TERM INT

while true; do
  GATEWAY_EXIT_CODE=0
  start_gateway_process || GATEWAY_EXIT_CODE=$?

  if [[ "${GATEWAY_SHUTDOWN_REQUESTED}" == "true" ]]; then
    exit "${GATEWAY_EXIT_CODE}"
  fi

  if [[ "${GATEWAY_EXIT_CODE}" -eq 0 ]]; then
    bootstrap_log "gateway 正常退出，准备在容器内原地拉起。"
    sleep "${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS}"
    continue
  fi

  GATEWAY_NOW="$(bootstrap_now_seconds)"
  if (( GATEWAY_NOW - GATEWAY_FAILURE_WINDOW_STARTED_AT > GATEWAY_LOCAL_RESTART_WINDOW_SECONDS )); then
    GATEWAY_FAILURE_WINDOW_STARTED_AT="${GATEWAY_NOW}"
    GATEWAY_FAILURE_COUNT=0
  fi

  GATEWAY_FAILURE_COUNT=$((GATEWAY_FAILURE_COUNT + 1))
  bootstrap_log "gateway 异常退出 code=${GATEWAY_EXIT_CODE}，本地重启次数 ${GATEWAY_FAILURE_COUNT}/${GATEWAY_LOCAL_RESTART_MAX}。"

  if (( GATEWAY_FAILURE_COUNT > GATEWAY_LOCAL_RESTART_MAX )); then
    bootstrap_log "gateway 本地重启预算已耗尽，退出容器交由平台接管。"
    exit "${GATEWAY_EXIT_CODE}"
  fi

  sleep "${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS}"
done
