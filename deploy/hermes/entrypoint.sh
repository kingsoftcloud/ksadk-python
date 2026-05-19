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
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export HERMES_MODEL_PROVIDER="${HERMES_MODEL_PROVIDER:-custom}"
export HERMES_CONTEXT_LENGTH="${HERMES_CONTEXT_LENGTH:-${OPENAI_CONTEXT_LENGTH:-${MODEL_CONTEXT_LENGTH:-}}}"
export HERMES_COMPRESSION_PROVIDER="${HERMES_COMPRESSION_PROVIDER:-${HERMES_MODEL_PROVIDER}}"
export HERMES_COMPRESSION_MODEL="${HERMES_COMPRESSION_MODEL:-${OPENAI_MODEL_NAME:-}}"
export HERMES_COMPRESSION_BASE_URL="${HERMES_COMPRESSION_BASE_URL:-${OPENAI_BASE_URL:-}}"
export HERMES_COMPRESSION_CONTEXT_LENGTH="${HERMES_COMPRESSION_CONTEXT_LENGTH:-${HERMES_CONTEXT_LENGTH}}"
export HERMES_COMPRESSION_TIMEOUT="${HERMES_COMPRESSION_TIMEOUT:-120}"
export HERMES_FALLBACK_PROVIDER="${HERMES_FALLBACK_PROVIDER:-custom}"
export HERMES_FALLBACK_MODEL="${HERMES_FALLBACK_MODEL:-${OPENAI_FALLBACK_MODEL_NAME:-}}"
export HERMES_FALLBACK_BASE_URL="${HERMES_FALLBACK_BASE_URL:-${OPENAI_BASE_URL:-}}"
export HERMES_LANGFUSE_PUBLIC_KEY="${HERMES_LANGFUSE_PUBLIC_KEY:-${LANGFUSE_PUBLIC_KEY:-}}"
export HERMES_LANGFUSE_SECRET_KEY="${HERMES_LANGFUSE_SECRET_KEY:-${LANGFUSE_SECRET_KEY:-}}"
export HERMES_LANGFUSE_BASE_URL="${HERMES_LANGFUSE_BASE_URL:-${LANGFUSE_BASE_URL:-${LANGFUSE_HOST:-}}}"
export HERMES_LANGFUSE_ENV="${HERMES_LANGFUSE_ENV:-${LANGFUSE_ENV:-}}"
export HERMES_LANGFUSE_RELEASE="${HERMES_LANGFUSE_RELEASE:-${LANGFUSE_RELEASE:-}}"
export HERMES_LANGFUSE_AUTO_ENABLE="${HERMES_LANGFUSE_AUTO_ENABLE:-true}"
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

cat > "${HERMES_HOME}/config.yaml" <<EOF
model:
  provider: "${HERMES_MODEL_PROVIDER}"
  default: "${OPENAI_MODEL_NAME:-}"
  base_url: "${OPENAI_BASE_URL:-}"
EOF

if [[ -n "${HERMES_CONTEXT_LENGTH}" ]]; then
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
  context_length: ${HERMES_CONTEXT_LENGTH}
EOF
fi

if [[ -n "${HERMES_COMPRESSION_MODEL}" ]]; then
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
auxiliary:
  compression:
    provider: "${HERMES_COMPRESSION_PROVIDER}"
    model: "${HERMES_COMPRESSION_MODEL}"
    base_url: "${HERMES_COMPRESSION_BASE_URL}"
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

if [[ -n "${HERMES_FALLBACK_MODEL}" && -n "${HERMES_FALLBACK_PROVIDER}" ]]; then
  cat >> "${HERMES_HOME}/config.yaml" <<EOF
fallback_model:
  provider: "${HERMES_FALLBACK_PROVIDER}"
  model: "${HERMES_FALLBACK_MODEL}"
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

prewarm_hermes_tui

cleanup() {
  HERMES_GATEWAY_SHUTDOWN_REQUESTED=1
  forward_gateway_shutdown
  kill "${HERMES_GATEWAY_SUPERVISOR_PID}" "${HERMES_DASHBOARD_PID}" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

exec uvicorn --app-dir /app runtime.app:app --host 0.0.0.0 --port "${PORT}"
