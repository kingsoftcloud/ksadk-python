#!/bin/sh
set -eu

log() {
  printf '[openclaw-user-bootstrap] %s\n' "$*"
}

is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

STATE_DIR="${OPENCLAW_STATE_DIR:-/home/node/.openclaw}"
TEMPLATE_DIR="${OPENCLAW_TEMPLATE_DIR:-/opt/openclaw-template}"
CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${STATE_DIR}/openclaw.json}"
CONFIG_TEMPLATE="${TEMPLATE_DIR}/config/openclaw.json"

mkdir -p "${STATE_DIR}" "${STATE_DIR}/extensions" "${STATE_DIR}/skills"
chmod 700 "${STATE_DIR}" 2>/dev/null || true
if [ -f "${STATE_DIR}/secrets.json" ]; then
  chmod 600 "${STATE_DIR}/secrets.json" 2>/dev/null || true
fi

render_template_config() {
  src="$1"
  dst="$2"
  strict="${OPENCLAW_TEMPLATE_ENV_STRICT:-1}"
  node -e '
const fs = require("fs");
const [src, dst, strictRaw] = process.argv.slice(1);
const strict = !["0", "false", "no", "off"].includes(String(strictRaw || "1").trim().toLowerCase());
const input = fs.readFileSync(src, "utf8");
const missing = [];
const escapeJsonStringContent = (value) => JSON.stringify(String(value ?? "")).slice(1, -1);
const rendered = input.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (match, name) => {
  if (Object.prototype.hasOwnProperty.call(process.env, name)) {
    return escapeJsonStringContent(process.env[name]);
  }
  missing.push(name);
  return match;
});
if (strict && missing.length > 0) {
  const unique = [...new Set(missing)];
  console.error(`[openclaw-user-bootstrap] unresolved template variables: ${unique.join(", ")}`);
  process.exit(1);
}
try {
  JSON.parse(rendered);
} catch (error) {
  console.error(`[openclaw-user-bootstrap] rendered openclaw.json is not valid JSON: ${error.message}`);
  process.exit(1);
}
fs.writeFileSync(dst, rendered.endsWith("\n") ? rendered : `${rendered}\n`);
' "${src}" "${dst}" "${strict}"
}

copy_children_once() {
  src_dir="$1"
  dst_dir="$2"
  [ -d "${src_dir}" ] || return 0
  for src in "${src_dir}"/*; do
    [ -e "${src}" ] || continue
    name="$(basename "${src}")"
    dst="${dst_dir}/${name}"
    if [ -e "${dst}" ]; then
      log "skip existing ${dst}"
      continue
    fi
    cp -R "${src}" "${dst}"
    log "materialized ${dst}"
  done
}

if [ ! -f "${CONFIG_PATH}" ] && [ -f "${CONFIG_TEMPLATE}" ]; then
  render_template_config "${CONFIG_TEMPLATE}" "${CONFIG_PATH}"
  chmod 600 "${CONFIG_PATH}" 2>/dev/null || true
  log "materialized ${CONFIG_PATH}"
else
  log "skip existing ${CONFIG_PATH}"
fi

copy_children_once "${TEMPLATE_DIR}/extensions" "${STATE_DIR}/extensions"
copy_children_once "${TEMPLATE_DIR}/skills" "${STATE_DIR}/skills"

if [ -n "${OPENCLAW_CONFIG_PATCH_JSON:-}" ]; then
  node -e '
const fs = require("fs");
const [configPath, patchRaw] = process.argv.slice(1);
const isPlainObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const clone = (value) => Array.isArray(value)
  ? value.map(clone)
  : isPlainObject(value)
    ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, clone(child)]))
    : value;
const merge = (base, overlay) => {
  const result = isPlainObject(base) ? clone(base) : {};
  for (const [key, value] of Object.entries(overlay || {})) {
    result[key] = isPlainObject(value) ? merge(result[key], value) : clone(value);
  }
  return result;
};
const normalizePatch = (value) => {
  const patch = isPlainObject(value) ? clone(value) : {};
  const diagnostics = patch.diagnostics;
  if (isPlainObject(diagnostics) && Object.prototype.hasOwnProperty.call(diagnostics, "captureContent")) {
    diagnostics.otel = isPlainObject(diagnostics.otel) ? diagnostics.otel : {};
    if (!Object.prototype.hasOwnProperty.call(diagnostics.otel, "captureContent")) {
      diagnostics.otel.captureContent = diagnostics.captureContent;
    }
    delete diagnostics.captureContent;
  }
  return patch;
};
let cfg = {};
try {
  cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));
} catch {
  cfg = {};
}
let patch;
try {
  patch = JSON.parse(patchRaw);
} catch (error) {
  console.error(`[openclaw-user-bootstrap] OPENCLAW_CONFIG_PATCH_JSON is not valid JSON: ${error.message}`);
  process.exit(1);
}
if (!isPlainObject(patch)) {
  console.error("[openclaw-user-bootstrap] OPENCLAW_CONFIG_PATCH_JSON must be a JSON object");
  process.exit(1);
}
fs.writeFileSync(configPath, `${JSON.stringify(merge(normalizePatch(cfg), normalizePatch(patch)), null, 2)}\n`);
' "${CONFIG_PATH}" "${OPENCLAW_CONFIG_PATCH_JSON}"
  log "applied OPENCLAW_CONFIG_PATCH_JSON"
fi

auth_mode="$(printf '%s' "${OPENCLAW_GATEWAY_AUTH_MODE:-trusted-proxy}" | tr '[:upper:]' '[:lower:]')"
case "${auth_mode}" in
  trusted-proxy|token|none) ;;
  *) echo >&2 "OPENCLAW_GATEWAY_AUTH_MODE only supports trusted-proxy|token|none"; exit 1 ;;
esac

gateway_token="${OPENCLAW_GATEWAY_TOKEN:-${OPENCLAW_GATEWAY_PASSWORD:-}}"
if [ "${auth_mode}" = "token" ] && [ -z "${gateway_token}" ]; then
  echo >&2 "OPENCLAW_GATEWAY_AUTH_MODE=token requires OPENCLAW_GATEWAY_TOKEN or OPENCLAW_GATEWAY_PASSWORD"
  exit 1
fi

trusted_proxy_user_header="${OPENCLAW_TRUSTED_PROXY_USER_HEADER:-${OPENCLAW_GATEWAY_TRUSTED_PROXY_USER_HEADER:-x-forwarded-user}}"
trusted_proxies="${OPENCLAW_TRUSTED_PROXIES:-127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,35.0.0.0/8}"
allowed_origins="${OPENCLAW_ALLOWED_ORIGINS:-}"

node -e '
const fs = require("fs");
const [configPath, authMode, gatewayToken, userHeader, trustedProxiesRaw, allowedOriginsRaw] = process.argv.slice(1);
const parseStringList = (raw) => {
  const text = String(raw || "").trim();
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed.map((item) => String(item).trim()).filter(Boolean);
    }
  } catch {
    // Fallback to separated text.
  }
  return text.split(/[,\s;]+/).map((item) => item.trim()).filter(Boolean);
};
const unique = (items) => [...new Set(items.map((item) => String(item).trim()).filter(Boolean))];
const agentengineOrigins = () => {
  const runtimeId = String(process.env.AGENT_RUNTIME_ID || "").trim();
  if (!runtimeId) return [];
  const origins = [];
  for (const domain of ["agent-pre.kspmas.ksyun.com", "agent.kspmas.ksyun.com"]) {
    origins.push(`http://${runtimeId}.${domain}`);
    origins.push(`https://${runtimeId}.${domain}`);
  }
  return origins;
};
const normalizeAllowedOrigins = (raw) => {
  const origins = parseStringList(raw);
  if (origins.length === 0) return [];
  const expanded = [];
  for (const origin of origins) {
    if (origin === "*") {
      expanded.push(...agentengineOrigins());
    } else {
      expanded.push(origin);
    }
  }
  return unique(expanded.length > 0 ? expanded : origins);
};
let cfg = {};
try {
  cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));
} catch {
  cfg = {};
}
cfg.gateway = cfg.gateway || {};
cfg.gateway.auth = cfg.gateway.auth || {};
cfg.gateway.auth.mode = authMode;
if (authMode === "token") {
  cfg.gateway.auth.password = gatewayToken;
} else {
  delete cfg.gateway.auth.token;
  delete cfg.gateway.auth.password;
}
cfg.gateway.auth.trustedProxy = cfg.gateway.auth.trustedProxy || {};
cfg.gateway.auth.trustedProxy.userHeader = String(userHeader || "x-forwarded-user").trim().toLowerCase();
const trustedProxies = parseStringList(trustedProxiesRaw);
if (trustedProxies.length > 0) {
  cfg.gateway.trustedProxies = trustedProxies;
}
const allowedOrigins = normalizeAllowedOrigins(allowedOriginsRaw);
if (allowedOrigins.length > 0) {
  cfg.gateway.controlUi = cfg.gateway.controlUi || {};
  cfg.gateway.controlUi.allowedOrigins = allowedOrigins;
}
fs.writeFileSync(configPath, `${JSON.stringify(cfg, null, 2)}\n`);
' "${CONFIG_PATH}" "${auth_mode}" "${gateway_token}" "${trusted_proxy_user_header}" "${trusted_proxies}" "${allowed_origins}"
log "gateway auth reconciled: ${auth_mode}"

if is_truthy "${OPENCLAW_BOOTSTRAP_PRINT_CONFIG:-}"; then
  node -e '
const fs = require("fs");
const [configPath] = process.argv.slice(1);
const redact = (value, key = "") => {
  if (/password|token|secret|apikey|api_key/i.test(key)) return "***REDACTED***";
  if (Array.isArray(value)) return value.map((item) => redact(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([childKey, child]) => [childKey, redact(child, childKey)]));
  }
  return value;
};
try {
  const cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));
  console.log(JSON.stringify(redact(cfg), null, 2));
} catch (error) {
  console.error(`[openclaw-user-bootstrap] failed to print config: ${error.message}`);
  process.exit(1);
}
' "${CONFIG_PATH}"
fi

if is_truthy "${OPENCLAW_BOOTSTRAP_ONLY:-}"; then
  log "bootstrap-only completed"
  exit 0
fi

bind_mode="${OPENCLAW_GATEWAY_BIND:-lan}"
if [ "${auth_mode}" = "none" ] && [ "${bind_mode}" = "lan" ]; then
  bind_mode="loopback"
fi

exec node openclaw.mjs gateway run \
  --allow-unconfigured \
  --bind "${bind_mode}" \
  --port "${OPENCLAW_GATEWAY_PORT:-${PORT:-8080}}" \
  --auth "${auth_mode}"
