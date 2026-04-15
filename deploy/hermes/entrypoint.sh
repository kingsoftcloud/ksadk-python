#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/home/agent}"
export PORT="${PORT:-8080}"
export API_SERVER_HOST="${API_SERVER_HOST:-127.0.0.1}"
export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
export HERMES_DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
export HERMES_DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
export HERMES_MODEL_PROVIDER="${HERMES_MODEL_PROVIDER:-custom}"
export HERMES_CONTEXT_LENGTH="${HERMES_CONTEXT_LENGTH:-${OPENAI_CONTEXT_LENGTH:-${MODEL_CONTEXT_LENGTH:-}}}"
export HERMES_FALLBACK_PROVIDER="${HERMES_FALLBACK_PROVIDER:-custom}"
export HERMES_FALLBACK_MODEL="${HERMES_FALLBACK_MODEL:-${OPENAI_FALLBACK_MODEL_NAME:-}}"
export HERMES_FALLBACK_BASE_URL="${HERMES_FALLBACK_BASE_URL:-${OPENAI_BASE_URL:-}}"
export AGENT_BROWSER_EXECUTABLE_PATH="${AGENT_BROWSER_EXECUTABLE_PATH:-/usr/bin/chromium}"

if [[ -z "${HERMES_CONTEXT_LENGTH}" ]]; then
  case "${OPENAI_MODEL_NAME,,}" in
    *glm-5.1*)
      HERMES_CONTEXT_LENGTH="200000"
      HERMES_FALLBACK_MODEL="${HERMES_FALLBACK_MODEL:-kimi-k2.5}"
      ;;
  esac
fi

mkdir -p "${HOME}/.hermes" "${HOME}/.hermes/skills"

cat > "${HOME}/.hermes/.env" <<EOF
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
AGENT_BROWSER_EXECUTABLE_PATH=${AGENT_BROWSER_EXECUTABLE_PATH}
EOF

for bundled_skill in /app/skills/*; do
  [[ -d "${bundled_skill}" ]] || continue
  skill_name="$(basename "${bundled_skill}")"
  rm -rf "${HOME}/.hermes/skills/${skill_name}"
  cp -R "${bundled_skill}" "${HOME}/.hermes/skills/${skill_name}"
done

cat > "${HOME}/.hermes/config.yaml" <<EOF
model:
  provider: "${HERMES_MODEL_PROVIDER}"
  default: "${OPENAI_MODEL_NAME:-}"
  base_url: "${OPENAI_BASE_URL:-}"
EOF

if [[ -n "${HERMES_CONTEXT_LENGTH}" ]]; then
  cat >> "${HOME}/.hermes/config.yaml" <<EOF
  context_length: ${HERMES_CONTEXT_LENGTH}
EOF
fi

if [[ -n "${HERMES_FALLBACK_MODEL}" && -n "${HERMES_FALLBACK_PROVIDER}" ]]; then
  cat >> "${HOME}/.hermes/config.yaml" <<EOF
fallback_model:
  provider: "${HERMES_FALLBACK_PROVIDER}"
  model: "${HERMES_FALLBACK_MODEL}"
EOF
  if [[ -n "${HERMES_FALLBACK_BASE_URL}" ]]; then
    cat >> "${HOME}/.hermes/config.yaml" <<EOF
  base_url: "${HERMES_FALLBACK_BASE_URL}"
EOF
  fi
fi

cat >> "${HOME}/.hermes/config.yaml" <<EOF
api_server:
  enabled: true
  host: "${API_SERVER_HOST}"
  port: ${API_SERVER_PORT}
EOF

hermes gateway run --replace &
HERMES_API_PID=$!

hermes dashboard --host "${HERMES_DASHBOARD_HOST}" --port "${HERMES_DASHBOARD_PORT}" --no-open &
HERMES_DASHBOARD_PID=$!

cleanup() {
  kill "${HERMES_API_PID}" "${HERMES_DASHBOARD_PID}" 2>/dev/null || true
}
trap cleanup EXIT

exec uvicorn runtime.app:app --host 0.0.0.0 --port "${PORT}"
