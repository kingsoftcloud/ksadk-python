#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/home/node}"
export HERMES_STATE_DIR="${HERMES_STATE_DIR:-${HOME}/.hermes}"
export HERMES_HOME="${HERMES_HOME:-${HERMES_STATE_DIR}}"
export HERMES_WORKDIR="${HERMES_WORKDIR:-${HERMES_HOME}/workspace}"
export HERMES_RUN_DIR="${HERMES_RUN_DIR:-${HERMES_HOME}/run}"
export HERMES_SESSION_DIR="${HERMES_SESSION_DIR:-${HERMES_HOME}/sessions}"
export HERMES_HOSTED_RUNTIME="${HERMES_HOSTED_RUNTIME:-1}"
export KSADK_WORKSPACE_ROOT="${KSADK_WORKSPACE_ROOT:-${HERMES_WORKDIR}}"
export KSADK_WORKSPACE_FILES_ENABLED="${KSADK_WORKSPACE_FILES_ENABLED:-1}"
export MCPORTER_HOME="${MCPORTER_HOME:-${HERMES_HOME}/mcporter}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-${HERMES_HOME}/xdg/config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${HERMES_HOME}/xdg/cache}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-${HERMES_HOME}/xdg/state}"
export AGENT_BROWSER_HOME="${AGENT_BROWSER_HOME:-/usr/local/lib/node_modules/agent-browser}"
export AGENT_BROWSER_STATE_DIR="${AGENT_BROWSER_STATE_DIR:-${HERMES_HOME}/browser}"
export AGENT_BROWSER_RUN_DIR="${AGENT_BROWSER_RUN_DIR:-${AGENT_BROWSER_STATE_DIR}/run}"
export AGENT_BROWSER_SESSION_DIR="${AGENT_BROWSER_SESSION_DIR:-${AGENT_BROWSER_STATE_DIR}/sessions}"
export AGENT_BROWSER_SOCKET_DIR="${AGENT_BROWSER_SOCKET_DIR:-${AGENT_BROWSER_RUN_DIR}}"
export AGENT_BROWSER_ARTIFACTS_DIR="${AGENT_BROWSER_ARTIFACTS_DIR:-${AGENT_BROWSER_STATE_DIR}/artifacts}"
export AGENT_BROWSER_LOG_DIR="${AGENT_BROWSER_LOG_DIR:-${AGENT_BROWSER_STATE_DIR}/logs}"
export PORT="${PORT:-8080}"
export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
export HERMES_DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
export HERMES_DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-}"
export HERMES_DASHBOARD_READY_TIMEOUT="${HERMES_DASHBOARD_READY_TIMEOUT:-120}"
export HERMES_MODEL_PROVIDER="${HERMES_MODEL_PROVIDER:-custom}"
export HERMES_CONTEXT_LENGTH="${HERMES_CONTEXT_LENGTH:-${OPENAI_CONTEXT_LENGTH:-${MODEL_CONTEXT_LENGTH:-}}}"
export HERMES_COMPRESSION_PROVIDER="${HERMES_COMPRESSION_PROVIDER:-${HERMES_MODEL_PROVIDER}}"
export HERMES_COMPRESSION_MODEL="${HERMES_COMPRESSION_MODEL:-${OPENAI_MODEL_NAME:-}}"
export HERMES_COMPRESSION_BASE_URL="${HERMES_COMPRESSION_BASE_URL:-${OPENAI_BASE_URL:-}}"
export HERMES_COMPRESSION_CONTEXT_LENGTH="${HERMES_COMPRESSION_CONTEXT_LENGTH:-${HERMES_CONTEXT_LENGTH}}"
export HERMES_COMPRESSION_TIMEOUT="${HERMES_COMPRESSION_TIMEOUT:-120}"
export HERMES_TITLE_GENERATION_PROVIDER="${HERMES_TITLE_GENERATION_PROVIDER:-${HERMES_MODEL_PROVIDER}}"
export HERMES_TITLE_GENERATION_MODEL="${HERMES_TITLE_GENERATION_MODEL:-${OPENAI_MODEL_NAME:-}}"
export HERMES_TITLE_GENERATION_BASE_URL="${HERMES_TITLE_GENERATION_BASE_URL:-${OPENAI_BASE_URL:-}}"
export HERMES_TITLE_GENERATION_TIMEOUT="${HERMES_TITLE_GENERATION_TIMEOUT:-30}"
export HERMES_FALLBACK_PROVIDER="${HERMES_FALLBACK_PROVIDER:-custom}"
export HERMES_FALLBACK_MODEL="${HERMES_FALLBACK_MODEL:-${OPENAI_FALLBACK_MODEL_NAME:-}}"
export HERMES_FALLBACK_BASE_URL="${HERMES_FALLBACK_BASE_URL:-${OPENAI_BASE_URL:-}}"
export HERMES_LANGFUSE_PUBLIC_KEY="${HERMES_LANGFUSE_PUBLIC_KEY:-${LANGFUSE_PUBLIC_KEY:-}}"
export HERMES_LANGFUSE_SECRET_KEY="${HERMES_LANGFUSE_SECRET_KEY:-${LANGFUSE_SECRET_KEY:-}}"
export HERMES_LANGFUSE_BASE_URL="${HERMES_LANGFUSE_BASE_URL:-${LANGFUSE_BASE_URL:-${LANGFUSE_HOST:-}}}"
export HERMES_LANGFUSE_ENV="${HERMES_LANGFUSE_ENV:-${LANGFUSE_ENV:-}}"
export HERMES_LANGFUSE_RELEASE="${HERMES_LANGFUSE_RELEASE:-${LANGFUSE_RELEASE:-}}"
export HERMES_LANGFUSE_AUTO_ENABLE="${HERMES_LANGFUSE_AUTO_ENABLE:-true}"
export HERMES_WPSXIEZUO_AUTO_ENABLE="${HERMES_WPSXIEZUO_AUTO_ENABLE:-true}"
export HERMES_ALLOW_LAZY_INSTALLS="${HERMES_ALLOW_LAZY_INSTALLS:-false}"
export HERMES_TUI_PREWARM="${HERMES_TUI_PREWARM:-true}"
export HERMES_TUI_PREWARM_TIMEOUT="${HERMES_TUI_PREWARM_TIMEOUT:-75}"
export TIRITH_ENABLED="${TIRITH_ENABLED:-false}"
export AGENT_BROWSER_EXECUTABLE_PATH="${AGENT_BROWSER_EXECUTABLE_PATH:-/usr/bin/chromium}"
export KDOCS_OPEN_BROWSER="${KDOCS_OPEN_BROWSER:-0}"
export HERMES_UI_LOCALE="${HERMES_UI_LOCALE:-zh}"
if [[ -z "${TERM:-}" || "${TERM}" == "dumb" ]]; then
  export TERM="xterm-256color"
fi
export PYTHONPATH="/app/runtime${PYTHONPATH:+:${PYTHONPATH}}"
export PATH="/usr/local/bin:${HOME}/.local/bin:${PATH}"

MAIN_PID="$$"
GATEWAY_PID_FILE="${HERMES_RUN_DIR}/gateway.pid"
export HERMES_GATEWAY_PID_FILE="${GATEWAY_PID_FILE}"
GATEWAY_LOCAL_RESTART_MAX="${GATEWAY_LOCAL_RESTART_MAX:-5}"
GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS="${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS:-2}"
HERMES_GATEWAY_SHUTDOWN_REQUESTED=0

entrypoint_log() {
  printf '[hermes-entrypoint] %s\n' "$*" >&2
}

normalize_bool() {
  local raw="${1:-}"
  case "${raw,,}" in
    1|true|yes|on)
      printf 'true\n'
      ;;
    *)
      printf 'false\n'
      ;;
  esac
}

resolve_api_server_enabled() {
  local requested="${API_SERVER_ENABLED:-}"
  local has_key="false"
  if [[ -n "${API_SERVER_KEY:-}" ]]; then
    has_key="true"
  fi

  if [[ -z "${requested}" ]]; then
    if [[ "${has_key}" == "true" ]]; then
      printf 'true\n'
    else
      printf 'false\n'
    fi
    return 0
  fi

  if [[ "$(normalize_bool "${requested}")" != "true" ]]; then
    printf 'false\n'
    return 0
  fi

  if [[ "${has_key}" != "true" ]]; then
    entrypoint_log "API server requested but API_SERVER_KEY missing; disabling api_server platform"
    printf 'false\n'
    return 0
  fi

  printf 'true\n'
}

normalize_hermes_ui_locale() {
  local raw="${1:-}"
  local normalized="${raw%%.*}"
  normalized="${normalized//_/-}"
  normalized="${normalized,,}"
  case "${normalized}" in
    ""|c|c-utf-8|posix)
      printf 'zh\n'
      ;;
    en*)
      printf 'en\n'
      ;;
    zh*)
      printf 'zh\n'
      ;;
    *)
      printf 'zh\n'
      ;;
  esac
}
export HERMES_UI_LOCALE="$(normalize_hermes_ui_locale "${HERMES_UI_LOCALE}")"
export API_SERVER_ENABLED="$(resolve_api_server_enabled)"

if [[ -z "${HERMES_CONTEXT_LENGTH}" ]]; then
  case "${OPENAI_MODEL_NAME,,}" in
    *glm-5.1*)
      HERMES_CONTEXT_LENGTH="200000"
      HERMES_COMPRESSION_CONTEXT_LENGTH="${HERMES_COMPRESSION_CONTEXT_LENGTH:-${HERMES_CONTEXT_LENGTH}}"
      HERMES_FALLBACK_MODEL="${HERMES_FALLBACK_MODEL:-kimi-k2.6}"
      ;;
  esac
fi
export HERMES_COMPRESSION_CONTEXT_LENGTH="${HERMES_COMPRESSION_CONTEXT_LENGTH:-${HERMES_CONTEXT_LENGTH}}"

mkdir -p "${HOME}"
mkdir -p "${HERMES_HOME}" "${HERMES_HOME}/skills" "${HERMES_RUN_DIR}" "${HERMES_SESSION_DIR}"
mkdir -p "${HERMES_WORKDIR}"
mkdir -p "${MCPORTER_HOME}" "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" "${XDG_STATE_HOME}"
mkdir -p "${AGENT_BROWSER_STATE_DIR}" "${AGENT_BROWSER_RUN_DIR}" "${AGENT_BROWSER_SESSION_DIR}"
mkdir -p "${AGENT_BROWSER_ARTIFACTS_DIR}" "${AGENT_BROWSER_LOG_DIR}"
find "${AGENT_BROWSER_RUN_DIR}" -mindepth 1 -maxdepth 1 -type s -delete 2>/dev/null || true
cd "${HERMES_WORKDIR}"

cat > "${HERMES_HOME}/.env" <<EOF
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-}
OPENAI_MODEL_NAME=${OPENAI_MODEL_NAME:-}
API_SERVER_ENABLED=${API_SERVER_ENABLED}
API_SERVER_KEY=${API_SERVER_KEY:-}
API_SERVER_HOST=${API_SERVER_HOST}
API_SERVER_PORT=${API_SERVER_PORT}
TAVILY_API_KEY=${TAVILY_API_KEY:-}
FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY:-}
EXA_API_KEY=${EXA_API_KEY:-}
PARALLEL_API_KEY=${PARALLEL_API_KEY:-}
BROWSERBASE_API_KEY=${BROWSERBASE_API_KEY:-}
BROWSER_USE_API_KEY=${BROWSER_USE_API_KEY:-}
CAMOFOX_URL=${CAMOFOX_URL:-}
LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY:-}
LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY:-}
LANGFUSE_BASE_URL=${LANGFUSE_BASE_URL:-}
LANGFUSE_HOST=${LANGFUSE_HOST:-}
LANGFUSE_ENV=${LANGFUSE_ENV:-}
LANGFUSE_RELEASE=${LANGFUSE_RELEASE:-}
HERMES_LANGFUSE_PUBLIC_KEY=${HERMES_LANGFUSE_PUBLIC_KEY}
HERMES_LANGFUSE_SECRET_KEY=${HERMES_LANGFUSE_SECRET_KEY}
HERMES_LANGFUSE_BASE_URL=${HERMES_LANGFUSE_BASE_URL}
HERMES_LANGFUSE_ENV=${HERMES_LANGFUSE_ENV}
HERMES_LANGFUSE_RELEASE=${HERMES_LANGFUSE_RELEASE}
HERMES_LANGFUSE_SAMPLE_RATE=${HERMES_LANGFUSE_SAMPLE_RATE:-}
HERMES_LANGFUSE_MAX_CHARS=${HERMES_LANGFUSE_MAX_CHARS:-}
HERMES_LANGFUSE_DEBUG=${HERMES_LANGFUSE_DEBUG:-}
WPSXIEZUO_APP_ID=${WPSXIEZUO_APP_ID:-}
WPSXIEZUO_APP_KEY=${WPSXIEZUO_APP_KEY:-}
WPSXIEZUO_API_BASE=${WPSXIEZUO_API_BASE:-}
WPSXIEZUO_WS_ENDPOINT=${WPSXIEZUO_WS_ENDPOINT:-}
WPSXIEZUO_GROUP_AT_ONLY=${WPSXIEZUO_GROUP_AT_ONLY:-}
WPSXIEZUO_ALLOWED_USERS=${WPSXIEZUO_ALLOWED_USERS:-}
WPSXIEZUO_ALLOW_ALL_USERS=${WPSXIEZUO_ALLOW_ALL_USERS:-}
WPSXIEZUO_HOME_CHANNEL=${WPSXIEZUO_HOME_CHANNEL:-}
TIRITH_ENABLED=${TIRITH_ENABLED}
AGENT_BROWSER_EXECUTABLE_PATH=${AGENT_BROWSER_EXECUTABLE_PATH}
AGENT_BROWSER_HOME=${AGENT_BROWSER_HOME}
AGENT_BROWSER_STATE_DIR=${AGENT_BROWSER_STATE_DIR}
AGENT_BROWSER_SOCKET_DIR=${AGENT_BROWSER_SOCKET_DIR}
AGENT_BROWSER_SESSION_DIR=${AGENT_BROWSER_SESSION_DIR}
AGENT_BROWSER_ARTIFACTS_DIR=${AGENT_BROWSER_ARTIFACTS_DIR}
AGENT_BROWSER_LOG_DIR=${AGENT_BROWSER_LOG_DIR}
KDOCS_OPEN_BROWSER=${KDOCS_OPEN_BROWSER}
EOF

for bundled_skill in /app/skills/*; do
  [[ -d "${bundled_skill}" ]] || continue
  skill_name="$(basename "${bundled_skill}")"
  rm -rf "${HERMES_HOME}/skills/${skill_name}"
  cp -R "${bundled_skill}" "${HERMES_HOME}/skills/${skill_name}"
done

python - <<'PY'
from __future__ import annotations

import os
import shutil
from pathlib import Path

try:
    import hermes_wpsxiezuo
except Exception as exc:  # pragma: no cover - image-time compatibility guard
    print(f"[hermes-entrypoint] WPSXiezuo plugin install failed: {exc}", flush=True)
else:
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    src = Path(hermes_wpsxiezuo.__file__).resolve().parent
    dst = hermes_home / "plugins" / "platforms" / "wpsxiezuo"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print("[hermes-entrypoint] WPSXiezuo plugin installed: platforms/wpsxiezuo", flush=True)
PY

cat > "${HERMES_HOME}/config.yaml" <<EOF
model:
  provider: "${HERMES_MODEL_PROVIDER}"
  default: "${OPENAI_MODEL_NAME:-}"
  base_url: "${OPENAI_BASE_URL:-}"
  api_key: "${OPENAI_API_KEY:-}"
EOF

if [[ -n "${HERMES_CONTEXT_LENGTH}" ]]; then
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
  context_length: ${HERMES_CONTEXT_LENGTH}
EOF
fi

cat >> "${HERMES_HOME}/config.yaml" <<EOF
display:
  interface: tui
EOF

if [[ -n "${HERMES_COMPRESSION_MODEL}" ]]; then
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
auxiliary:
  compression:
    provider: "${HERMES_COMPRESSION_PROVIDER}"
    model: "${HERMES_COMPRESSION_MODEL}"
    base_url: "${HERMES_COMPRESSION_BASE_URL}"
    api_key: "${OPENAI_API_KEY:-}"
EOF
  if [[ -n "${HERMES_COMPRESSION_CONTEXT_LENGTH}" ]]; then
    cat >> "${HERMES_HOME}/config.yaml" <<EOF
    context_length: ${HERMES_COMPRESSION_CONTEXT_LENGTH}
EOF
  fi
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
    timeout: ${HERMES_COMPRESSION_TIMEOUT}
EOF
fi

if [[ -n "${HERMES_TITLE_GENERATION_MODEL}" ]]; then
  if ! grep -q '^auxiliary:' "${HERMES_HOME}/config.yaml"; then
    cat >> "${HERMES_HOME}/config.yaml" <<EOF
auxiliary:
EOF
  fi
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
  title_generation:
    provider: "${HERMES_TITLE_GENERATION_PROVIDER}"
    model: "${HERMES_TITLE_GENERATION_MODEL}"
    base_url: "${HERMES_TITLE_GENERATION_BASE_URL}"
    api_key: "${OPENAI_API_KEY:-}"
    timeout: ${HERMES_TITLE_GENERATION_TIMEOUT}
EOF
fi

if [[ -n "${HERMES_FALLBACK_MODEL}" && -n "${HERMES_FALLBACK_PROVIDER}" ]]; then
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
fallback_model:
  provider: "${HERMES_FALLBACK_PROVIDER}"
  model: "${HERMES_FALLBACK_MODEL}"
  api_key: "${OPENAI_API_KEY:-}"
EOF
  if [[ -n "${HERMES_FALLBACK_BASE_URL}" ]]; then
    cat >> "${HERMES_HOME}/config.yaml" <<EOF
  base_url: "${HERMES_FALLBACK_BASE_URL}"
EOF
  fi
fi

cat >> "${HERMES_HOME}/config.yaml" <<EOF
api_server:
  enabled: ${API_SERVER_ENABLED}
  host: "${API_SERVER_HOST}"
  port: ${API_SERVER_PORT}
security:
  allow_lazy_installs: ${HERMES_ALLOW_LAZY_INSTALLS}
  tirith_enabled: ${TIRITH_ENABLED}
  tirith_path: "tirith"
  tirith_timeout: 5
  tirith_fail_open: true
EOF

case "${HERMES_LANGFUSE_AUTO_ENABLE,,}" in
  0|false|no|off)
    entrypoint_log "Langfuse auto-enable disabled"
    ;;
  *)
    if [[ -n "${HERMES_LANGFUSE_PUBLIC_KEY}" && -n "${HERMES_LANGFUSE_SECRET_KEY}" ]]; then
      cat >> "${HERMES_HOME}/config.yaml" <<EOF
plugins:
  enabled:
    - observability/langfuse
EOF
      python - <<'PY'
from __future__ import annotations

try:
    from hermes_cli.plugins_cmd import _get_enabled_set, _save_enabled_set
except Exception as exc:  # pragma: no cover - image-time compatibility guard
    print(f"[hermes-entrypoint] Langfuse plugin enable failed: {exc}", flush=True)
else:
    enabled = _get_enabled_set()
    if "observability/langfuse" not in enabled and "langfuse" not in enabled:
        enabled.add("observability/langfuse")
        _save_enabled_set(enabled)
    print("[hermes-entrypoint] Langfuse plugin enabled: observability/langfuse", flush=True)
PY
    else
      entrypoint_log "Langfuse credentials missing; tracing plugin not enabled"
    fi
    ;;
esac

case "${HERMES_WPSXIEZUO_AUTO_ENABLE,,}" in
  0|false|no|off)
    entrypoint_log "WPSXiezuo auto-enable disabled"
    ;;
  *)
    if [[ -n "${WPSXIEZUO_APP_ID:-}" && -n "${WPSXIEZUO_APP_KEY:-}" ]]; then
      python - <<'PY'
from __future__ import annotations

try:
    from hermes_cli.plugins_cmd import _get_enabled_set, _save_enabled_set
except Exception as exc:  # pragma: no cover - image-time compatibility guard
    print(f"[hermes-entrypoint] WPSXiezuo plugin enable failed: {exc}", flush=True)
else:
    enabled = _get_enabled_set()
    if "platforms/wpsxiezuo" not in enabled:
        enabled.add("platforms/wpsxiezuo")
        _save_enabled_set(enabled)
    print("[hermes-entrypoint] WPSXiezuo plugin enabled: platforms/wpsxiezuo", flush=True)
PY
    else
      entrypoint_log "WPSXiezuo credentials missing; platform plugin installed but not enabled"
    fi
    ;;
esac

prewarm_hermes_tui() {
  case "${HERMES_TUI_PREWARM,,}" in
    0|false|no|off)
      entrypoint_log "Hermes TUI prewarm disabled"
      return 0
      ;;
  esac

  (
    set +e
    entrypoint_log "Hermes TUI prewarm starting"
    hermes status >/dev/null 2>&1
    if command -v timeout >/dev/null 2>&1 && command -v script >/dev/null 2>&1; then
      timeout "${HERMES_TUI_PREWARM_TIMEOUT}" script -q -c "hermes chat" /dev/null >/dev/null 2>&1
      prewarm_code=$?
      if [[ "${prewarm_code}" -eq 0 || "${prewarm_code}" -eq 124 || "${prewarm_code}" -eq 130 || "${prewarm_code}" -eq 143 ]]; then
        entrypoint_log "Hermes TUI prewarm completed code=${prewarm_code}"
      else
        entrypoint_log "Hermes TUI prewarm exited code=${prewarm_code}"
      fi
    else
      entrypoint_log "Hermes TUI prewarm skipped; timeout/script unavailable"
    fi
  ) &
}

wait_for_dashboard_ready() {
  local status_url="http://${HERMES_DASHBOARD_HOST}:${HERMES_DASHBOARD_PORT}/api/status"
  local timeout_seconds="${HERMES_DASHBOARD_READY_TIMEOUT}"
  local waited=0

  entrypoint_log "Waiting for Hermes dashboard readiness: ${status_url}"
  while (( waited < timeout_seconds )); do
    if curl -fsS "${status_url}" >/dev/null 2>&1; then
      entrypoint_log "Hermes dashboard ready after ${waited}s"
      return 0
    fi
    if ! kill -0 "${HERMES_DASHBOARD_PID}" 2>/dev/null; then
      entrypoint_log "Hermes dashboard exited before readiness"
      return 1
    fi
    sleep 1
    waited="$((waited + 1))"
  done

  entrypoint_log "Hermes dashboard readiness timed out after ${timeout_seconds}s"
  return 1
}

start_gateway_process() {
  hermes gateway run --replace &
  HERMES_GATEWAY_PID=$!
  printf '%s\n' "${HERMES_GATEWAY_PID}" > "${GATEWAY_PID_FILE}"
  set +e
  wait "${HERMES_GATEWAY_PID}"
  local exit_code=$?
  set -e
  rm -f "${GATEWAY_PID_FILE}"
  HERMES_GATEWAY_PID=""
  return "${exit_code}"
}

forward_gateway_shutdown() {
  HERMES_GATEWAY_SHUTDOWN_REQUESTED=1
  if [[ -n "${HERMES_GATEWAY_PID:-}" ]]; then
    kill -TERM "${HERMES_GATEWAY_PID}" 2>/dev/null || true
    wait "${HERMES_GATEWAY_PID}" 2>/dev/null || true
    HERMES_GATEWAY_PID=""
  fi
  rm -f "${GATEWAY_PID_FILE}"
}

supervise_gateway() {
  local failure_count=0
  local gateway_exit_code=0
  while true; do
    gateway_exit_code=0
    start_gateway_process || gateway_exit_code=$?

    if [[ "${HERMES_GATEWAY_SHUTDOWN_REQUESTED}" == "1" ]]; then
      return 0
    fi

    if [[ "${gateway_exit_code}" -eq 0 || "${gateway_exit_code}" -eq 130 || "${gateway_exit_code}" -eq 143 ]]; then
      failure_count=0
      entrypoint_log "gateway exited code=${gateway_exit_code}; restarting under container supervision"
      sleep "${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS}"
      continue
    fi

    failure_count="$((failure_count + 1))"
    entrypoint_log "gateway exited code=${gateway_exit_code}; local restart ${failure_count}/${GATEWAY_LOCAL_RESTART_MAX}"
    if [[ "${failure_count}" -ge "${GATEWAY_LOCAL_RESTART_MAX}" ]]; then
      entrypoint_log "gateway restart budget exhausted; terminating main process so the platform can recreate the pod"
      kill -TERM "${MAIN_PID}" 2>/dev/null || true
      return "${gateway_exit_code}"
    fi

    sleep "${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS}"
  done
}

supervise_gateway &
HERMES_GATEWAY_SUPERVISOR_PID=$!

hermes dashboard --host "${HERMES_DASHBOARD_HOST}" --port "${HERMES_DASHBOARD_PORT}" --no-open &
HERMES_DASHBOARD_PID=$!

wait_for_dashboard_ready
prewarm_hermes_tui

cleanup() {
  HERMES_GATEWAY_SHUTDOWN_REQUESTED=1
  forward_gateway_shutdown
  kill "${HERMES_GATEWAY_SUPERVISOR_PID}" "${HERMES_DASHBOARD_PID}" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

exec uvicorn --app-dir /app runtime.app:app --host 0.0.0.0 --port "${PORT}"
