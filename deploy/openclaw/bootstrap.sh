#!/usr/bin/env bash
set -euo pipefail

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
WORKSPACE_TEMPLATE_DIR="${OPENCLAW_WORKSPACE_TEMPLATE_DIR:-/opt/openclaw/workspace-template}"
RUNTIME_DIST_DIR="${OPENCLAW_DIST_DIR:-/app/dist}"
BOOTSTRAP_CACHE_DIR="${OPENCLAW_BOOTSTRAP_CACHE_DIR:-${STATE_DIR}/.bootstrap-cache}"
PRESET_SKILLS_CONTENT_SIG_FILE="${OPENCLAW_PRESET_SKILLS_CONTENT_SIG_FILE:-${PRESET_SKILLS_DIR}/.content.sig}"
DIST_PATCH_MARKER_VERSION="${OPENCLAW_DIST_PATCH_MARKER_VERSION:-2026.3.19.2}"
DIST_PATCH_MARKER="${OPENCLAW_DIST_PATCH_MARKER:-${RUNTIME_DIST_DIR}/.agentengine-dist-patched-${DIST_PATCH_MARKER_VERSION}}"

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

configure_browser_executable() {
  if [[ -n "${OPENCLAW_BROWSER_EXECUTABLE_PATH:-}" && -x "${OPENCLAW_BROWSER_EXECUTABLE_PATH}" ]]; then
    return 0
  fi

  if [[ -n "${OPENCLAW_BROWSER_EXECUTABLE:-}" && -x "${OPENCLAW_BROWSER_EXECUTABLE}" ]]; then
    export OPENCLAW_BROWSER_EXECUTABLE_PATH="${OPENCLAW_BROWSER_EXECUTABLE}"
    return 0
  fi

  if [[ -x "${BROWSER_EXECUTABLE_DEFAULT}" ]]; then
    export OPENCLAW_BROWSER_EXECUTABLE_PATH="${BROWSER_EXECUTABLE_DEFAULT}"
    return 0
  fi

  return 1
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

  allowlist_raw="find-skills,multi-search-engine,kdocs"
  # self-improving-agent：仅严格模式
  is_truthy "${OPENCLAW_EXEC_STRICT_MODE:-false}" && allowlist_raw="${allowlist_raw},self-improving-agent"
  # tuanziguardianclaw：仅严格模式保留，宽松模式默认不内置
  is_truthy "${OPENCLAW_EXEC_STRICT_MODE:-false}" && allowlist_raw="${allowlist_raw},tuanziguardianclaw"
  printf '%s\n' "${allowlist_raw}"
}

sync_preset_skills() {
  local src_dir="${PRESET_SKILLS_DIR}"
  local dst_dir="${STATE_DIR}/skills"
  local sig_file="${BOOTSTRAP_CACHE_DIR}/preset-skills.sig"
  local content_sig_file="${PRESET_SKILLS_CONTENT_SIG_FILE}"
  local item
  local skill_name
  local allowlist_raw
  local signature=""
  local content_signature=""
  local previous_signature=""
  allowlist_raw="$(resolve_preset_skills_allowlist)"
  local existing

  [[ -d "${src_dir}" ]] || return 0

  mkdir -p "${BOOTSTRAP_CACHE_DIR}" "${dst_dir}"
  if [[ -f "${content_sig_file}" ]]; then
    content_signature="$(cat "${content_sig_file}" 2>/dev/null || true)"
  fi
  if [[ -z "${content_signature}" ]]; then
    content_signature="$(
      find "${src_dir}" -type f ! -name '.content.sig' | LC_ALL=C sort | while IFS= read -r file_path; do
        cksum "${file_path}"
      done | cksum | awk '{print $1 ":" $2}'
    )"
  fi
  signature="$(
    {
      printf '%s\n' "${allowlist_raw}"
      printf '%s\n' "${content_signature}"
    } | cksum | awk '{print $1 ":" $2}'
  )"
  if [[ -f "${sig_file}" ]]; then
    previous_signature="$(cat "${sig_file}" 2>/dev/null || true)"
  fi
  if [[ -n "${signature}" && "${signature}" == "${previous_signature}" ]]; then
    return 0
  fi

  for existing in "${dst_dir}"/*; do
    [[ -d "${existing}" ]] || continue
    skill_name="$(basename "${existing}")"
    case ",${allowlist_raw}," in
      *,"${skill_name}",*)
        ;;
      *)
        rm -rf "${existing}"
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
    rm -rf "${dst_dir}/${skill_name}"
    cp -R "${item}" "${dst_dir}/${skill_name}"
  done

  if [[ -n "${signature}" ]]; then
    printf '%s\n' "${signature}" > "${sig_file}"
  fi
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
const replacements = [
  {
    label: 'control-ui websocket reconnect gap handling',
    marker: 'this.ws.addEventListener(`open`,()=>{this.lastSeq=null,this.queueConnect()})',
    needle: 'this.ws.addEventListener(`open`,()=>this.queueConnect())',
    replacement: 'this.ws.addEventListener(`open`,()=>{this.lastSeq=null,this.queueConnect()})',
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
    label: 'gateway backend self-pairing trusted-proxy bypass',
    marker: 'const usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";',
    needle: `function shouldSkipBackendSelfPairing(params) {
	if (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;
	const usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";
	return params.isLocalClient && !params.hasBrowserOriginHeader && params.sharedAuthOk && usesSharedSecretAuth;
}`,
    replacement: `function shouldSkipBackendSelfPairing(params) {
	if (!(params.connectParams.client.id === GATEWAY_CLIENT_IDS.GATEWAY_CLIENT && params.connectParams.client.mode === GATEWAY_CLIENT_MODES.BACKEND)) return false;
	const usesSharedSecretAuth = params.authMethod === "token" || params.authMethod === "password";
	const usesLoopbackTrustedProxyAuth = params.authMethod === "trusted-proxy";
	return params.isLocalClient && !params.hasBrowserOriginHeader && (usesLoopbackTrustedProxyAuth || params.sharedAuthOk && usesSharedSecretAuth);
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
	if (!(params.token || params.password)) return true;
	try {
		const parsed = new URL(params.url);
		return ![
			"127.0.0.1",
			"::1",
			"localhost"
		].includes(parsed.hostname);
	} catch {
		return true;
	}
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
for (const filePath of jsFiles) {
  let source = fs.readFileSync(filePath, 'utf8');
  let changed = false;
  for (const patch of replacements) {
    if (source.includes(patch.marker) || !source.includes(patch.needle)) continue;
    source = source.replaceAll(patch.needle, patch.replacement);
    patchedLabels.add(patch.label);
    changed = true;
  }
  if (!changed) continue;
  fs.writeFileSync(filePath, source);
}

if (patchedLabels.size > 0) {
  console.error(`[bootstrap] patched ${[...patchedLabels].join(', ')}`);
}

if (markerFile) {
  fs.writeFileSync(markerFile, `version=${path.basename(markerFile)}\n`, 'utf8');
}
NODE
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
}

build_exec_default_allowlist() {
  local -a wrapped_bins=(pwd ls whoami id uname date ps df du stat find cat head tail wc git mcporter sh-safe bash-safe web-safe)
  local -a direct_bins=(curl openclaw)
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
      openclaw)
        resolved="$(resolve_allowlisted_command_path "${bin}" /usr/local/bin/openclaw /usr/bin/openclaw /bin/openclaw || true)"
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
  patch_gateway_client_loopback_trusted_proxy_identity
  echo "INFO: dist-patch-only mode enabled; runtime assets reconciled."
  exit 0
fi

BOOTSTRAP_STARTED_AT="$(bootstrap_now_seconds)"

mkdir -p "${STATE_DIR}"

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
cfg.browser.noSandbox = parseBool(process.env.OPENCLAW_BROWSER_NO_SANDBOX, true);
cfg.browser.headless = parseBool(process.env.OPENCLAW_BROWSER_HEADLESS, true);
const browserExecutablePath = (
  process.env.OPENCLAW_BROWSER_EXECUTABLE_PATH ||
  process.env.OPENCLAW_BROWSER_EXECUTABLE ||
  ''
).trim();
if (browserExecutablePath) {
  cfg.browser.executablePath = browserExecutablePath;
}

cfg.models = cfg.models || {};
cfg.models.mode = cfg.models.mode || 'merge';
cfg.models.providers = cfg.models.providers || {};

const resolvedStateDir = firstNonBlank(
  process.env.STATE_DIR,
  process.env.OPENCLAW_STATE_DIR,
  configPath ? path.dirname(configPath) : '',
);
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
const defaultModelInputs = (provider, modelId) => {
  const normalizedProvider = String(provider || '').trim().toLowerCase();
  const normalizedModelId = String(modelId || '').trim().toLowerCase();
  if (normalizedProvider === 'ksyun' && normalizedModelId === 'glm-5') {
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
          id: 'glm-5',
          name: 'glm-5',
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
const catalogPrimaryModel = selectableModels
  .map((item) => normalizeModelRef(providerId, String(item?.id || item?.name || '').trim()))
  .find(Boolean);
const primaryModel = (
  normalizeModelRef(providerId, preferredDefaultModel) ||
  catalogPrimaryModel ||
  'ksyun/glm-5'
).trim();
const primaryModelQualified = primaryModel.includes('/');
if (primaryModel && (explicitProviderConfigured || primaryModelQualified)) {
  cfg.agents.defaults.model = cfg.agents.defaults.model || {};
  cfg.agents.defaults.model.primary = primaryModel;
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
  patch_gateway_client_loopback_trusted_proxy_identity &
  sync_preset_skills &
  sync_workspace_security_templates &
  sync_runtime_env_file &
  # 等待所有后台任务完成
  wait
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
  echo "INFO: bootstrap-only mode enabled; config reconciled."
  exit 0
fi

MODEL_API_KEY_SECRET_SOURCE_RAW="$(printf '%s' "${OPENCLAW_MODEL_API_KEY_SECRET_SOURCE:-file}" | tr '[:upper:]' '[:lower:]')"
if [[ "${MODEL_API_KEY_SECRET_SOURCE_RAW}" != "env" ]]; then
  unset OPENCLAW_MODEL_API_KEY OPENAI_API_KEY LLM_API_KEY MODEL_API_KEY
fi

export OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT="${OPENCLAW_EXEC_SAFE_WORKSPACE_ROOT:-${WORKSPACE_DIR}}"
export OPENCLAW_EXEC_SAFE_STATE_DIR="${OPENCLAW_EXEC_SAFE_STATE_DIR:-${STATE_DIR}}"

# Use OpenClaw's built-in cron scheduler only; do not start a system cron daemon.

GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-8080}"
exec node openclaw.mjs gateway run --allow-unconfigured --bind "${BIND_MODE}" --port "${GATEWAY_PORT}" --auth "${AUTH_MODE}"
