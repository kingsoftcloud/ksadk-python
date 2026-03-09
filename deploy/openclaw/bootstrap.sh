#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${OPENCLAW_STATE_DIR:-/root/.openclaw}"
CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${STATE_DIR}/openclaw.json}"
BOOTSTRAP_MARKER="${STATE_DIR}/.bootstrapped"
PUBLIC_PORT="${OPENCLAW_PUBLIC_PORT:-19089}"
BIND_MODE="${OPENCLAW_GATEWAY_BIND:-lan}"
AUTH_MODE="trusted-proxy"

mkdir -p "${STATE_DIR}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo '{}' > "${CONFIG_PATH}"
fi

# Always reconcile runtime config from environment variables.
# NOTE:
# - We intentionally apply env -> config on every startup so deployment-time
#   env updates (e.g. OPENCLAW_ALLOWED_ORIGINS / OPENCLAW_DISABLE_DEVICE_AUTH)
#   can take effect for existing instances.
# - BOOTSTRAP_MARKER is kept only for one-time preset-skill copy below.
export STATE_DIR CONFIG_PATH PUBLIC_PORT BIND_MODE AUTH_MODE
node <<'NODE'
const fs = require('fs');

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

let cfg = {};
try {
  cfg = JSON.parse(fs.readFileSync(configPath, 'utf8'));
} catch {
  cfg = {};
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

const providerId = (process.env.OPENCLAW_MODEL_PROVIDER_ID || '').trim();
const providerBaseUrl = (process.env.OPENCLAW_MODEL_BASE_URL || '').trim();
const providerApiKey = (process.env.OPENCLAW_MODEL_API_KEY || '').trim();
const providerApi = (process.env.OPENCLAW_MODEL_API || 'openai-completions').trim();
if (providerId && providerBaseUrl && providerApiKey) {
  cfg.models.providers[providerId] = cfg.models.providers[providerId] || {};
  cfg.models.providers[providerId].baseUrl = providerBaseUrl;
  cfg.models.providers[providerId].apiKey = providerApiKey;
  cfg.models.providers[providerId].api = providerApi;

  const modelCatalogRaw = (process.env.OPENCLAW_MODEL_CATALOG_JSON || '').trim();
  if (modelCatalogRaw) {
    try {
      cfg.models.providers[providerId].models = JSON.parse(modelCatalogRaw);
    } catch {
      // Keep existing models if invalid JSON is supplied.
    }
  }
}

const primaryModel = (process.env.OPENCLAW_DEFAULT_MODEL || '').trim();
const explicitProviderConfigured = !!(providerId && providerBaseUrl && providerApiKey);
const primaryModelQualified = primaryModel.includes('/');
if (primaryModel && (explicitProviderConfigured || primaryModelQualified)) {
  cfg.agents = cfg.agents || {};
  cfg.agents.defaults = cfg.agents.defaults || {};
  cfg.agents.defaults.model = cfg.agents.defaults.model || {};
  cfg.agents.defaults.model.primary = primaryModel;
  cfg.agents.defaults.models = cfg.agents.defaults.models || {};
  cfg.agents.defaults.models[primaryModel] = cfg.agents.defaults.models[primaryModel] || {};
}

fs.writeFileSync(configPath, JSON.stringify(cfg, null, 2));
NODE

# Seed UI locale in localStorage before Control UI app boots (first-load only).
UI_LOCALE="${OPENCLAW_UI_LOCALE:-}"
CONTROL_UI_INDEX="/app/dist/control-ui/index.html"
if [[ -n "${UI_LOCALE}" && -f "${CONTROL_UI_INDEX}" ]]; then
  UI_LOCALE_ESCAPED="${UI_LOCALE//\\/\\\\}"
  UI_LOCALE_ESCAPED="${UI_LOCALE_ESCAPED//\'/\\\'}"
  INJECT_LINE="<script id=\"__OPENCLAW_UI_LOCALE_BOOTSTRAP__\">try{if(!localStorage.getItem('openclaw.i18n.locale')){localStorage.setItem('openclaw.i18n.locale','${UI_LOCALE_ESCAPED}');}}catch(_e){}</script>"

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

# One-time bootstrap side-effects for filesystem assets.
if [[ ! -f "${BOOTSTRAP_MARKER}" ]]; then
  if [[ -d /opt/openclaw/preset-skills ]]; then
    mkdir -p "${STATE_DIR}/skills"
    cp -R /opt/openclaw/preset-skills/. "${STATE_DIR}/skills/"
  fi

  touch "${BOOTSTRAP_MARKER}"
fi

# Start cron daemon if available (for schedule tasks used by some skills/tools).
if command -v cron >/dev/null 2>&1; then
  if ! ps -ef | grep -q '[c]ron'; then
    if ! cron >/tmp/openclaw-cron.log 2>&1; then
      echo "WARN: failed to start cron; see /tmp/openclaw-cron.log" >&2
    fi
  fi
fi

# Launch gateway first, then warm up browser control so browser tools work OOTB.
GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
declare -a GATEWAY_CMD=(
  node
  openclaw.mjs
  gateway
  run
  --allow-unconfigured
  --bind "${BIND_MODE}"
  --port "${GATEWAY_PORT}"
  --auth "${AUTH_MODE}"
)

ensure_browser_ready() {
  # trusted-proxy 下 browser 子命令在容器内通常无法带上代理身份头，跳过预热避免误告警。
  return 0
}

"${GATEWAY_CMD[@]}" &
GATEWAY_PID=$!
BROWSER_WATCHDOG_PID=""

cleanup() {
  if [[ -n "${BROWSER_WATCHDOG_PID}" ]] && kill -0 "${BROWSER_WATCHDOG_PID}" >/dev/null 2>&1; then
    kill "${BROWSER_WATCHDOG_PID}" >/dev/null 2>&1 || true
    wait "${BROWSER_WATCHDOG_PID}" >/dev/null 2>&1 || true
  fi
  if kill -0 "${GATEWAY_PID}" >/dev/null 2>&1; then
    kill "${GATEWAY_PID}" >/dev/null 2>&1 || true
    wait "${GATEWAY_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup SIGTERM SIGINT

for _ in $(seq 1 60); do
  if ! kill -0 "${GATEWAY_PID}" >/dev/null 2>&1; then
    wait "${GATEWAY_PID}"
    exit $?
  fi
  if curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if kill -0 "${GATEWAY_PID}" >/dev/null 2>&1; then
  ensure_browser_ready || true
  BROWSER_WATCH_INTERVAL="${OPENCLAW_BROWSER_WATCH_INTERVAL:-45}"
  if ! [[ "${BROWSER_WATCH_INTERVAL}" =~ ^[0-9]+$ ]] || [[ "${BROWSER_WATCH_INTERVAL}" -lt 5 ]]; then
    BROWSER_WATCH_INTERVAL=45
  fi
  (
    while kill -0 "${GATEWAY_PID}" >/dev/null 2>&1; do
      sleep "${BROWSER_WATCH_INTERVAL}"
      if ! kill -0 "${GATEWAY_PID}" >/dev/null 2>&1; then
        exit 0
      fi
      ensure_browser_ready || true
    done
  ) &
  BROWSER_WATCHDOG_PID=$!
fi

wait "${GATEWAY_PID}"
