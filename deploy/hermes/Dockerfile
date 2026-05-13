ARG HERMES_NODE_BASE_IMAGE=node:20-bookworm-slim
ARG HERMES_PYTHON_BASE_IMAGE=python:3.12-bookworm
ARG APT_MIRROR=https://mirrors.aliyun.com/debian

FROM ${HERMES_NODE_BASE_IMAGE} AS node_with_git

ARG APT_MIRROR=https://mirrors.aliyun.com/debian

RUN if [ -n "${APT_MIRROR}" ]; then \
      # Node base may not have trusted CAs yet; keep default deb.debian.org
      # here and only relax signature checks for cross-arch build stability.
      true; \
    fi \
    && mkdir -p /tmp/apt-cache \
    && printf 'Dir::Cache::archives "/tmp/apt-cache";\n' > /etc/apt/apt.conf.d/99cache-dir \
    && apt-get -o Dir::Cache::archives=/tmp/apt-cache \
      -o Acquire::AllowInsecureRepositories=true \
      -o Acquire::AllowDowngradeToInsecureRepositories=true \
      update \
    && DEBIAN_FRONTEND=noninteractive apt-get \
      -o Dir::Cache::archives=/tmp/apt-cache \
      -o APT::Get::AllowUnauthenticated=true \
      install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* /tmp/apt-cache /etc/apt/apt.conf.d/99cache-dir

FROM node_with_git AS hermes_src

ARG HERMES_AGENT_REF=v2026.5.7

WORKDIR /src

RUN git clone --depth 1 --branch "${HERMES_AGENT_REF}" https://github.com/NousResearch/hermes-agent.git .

FROM hermes_src AS hermes_web

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /src/web

RUN npm config set registry "${NPM_REGISTRY}" \
    && if [ -f package-lock.json ]; then npm ci; else npm install; fi \
    && npm run build

FROM node_with_git AS kdocs_skill

ARG KDOCS_SKILL_REPO=https://github.com/kdocs-app/kdocs-skill.git

RUN git clone --depth 1 "${KDOCS_SKILL_REPO}" /tmp/kdocs-skill \
    && mkdir -p /app/skills \
    && cp -R /tmp/kdocs-skill /app/skills/kdocs \
    && printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -euo pipefail' \
      'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' \
      'exec bash "${SCRIPT_DIR}/scripts/setup.sh" "$@"' \
      > /app/skills/kdocs/setup.sh \
    && chmod 755 /app/skills/kdocs/setup.sh

FROM ${HERMES_NODE_BASE_IMAGE} AS agent_browser

ARG NPM_REGISTRY=https://registry.npmmirror.com

RUN npm config set registry "${NPM_REGISTRY}" \
    && npm install -g --no-audit --no-fund agent-browser@0.13.0 mcporter

FROM ${HERMES_PYTHON_BASE_IMAGE}

ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PYPI_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG HERMES_AGENT_REF=v2026.5.7
ARG APT_MIRROR=https://mirrors.aliyun.com/debian

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=8 \
    PIP_PROGRESS_BAR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/node \
    HERMES_HOME=/home/node/.hermes \
    HERMES_STATE_DIR=/home/node/.hermes \
    PORT=8080 \
    API_SERVER_HOST=127.0.0.1 \
    API_SERVER_PORT=8642 \
    HERMES_DASHBOARD_HOST=127.0.0.1 \
    HERMES_DASHBOARD_PORT=9119 \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    AGENT_BROWSER_HOME=/usr/local/lib/node_modules/agent-browser \
    KDOCS_OPEN_BROWSER=0 \
    PYTHONPATH=/opt

WORKDIR /app

RUN if [ -n "${APT_MIRROR}" ]; then \
      sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|http://deb.debian.org/debian-security|${APT_MIRROR}-security|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && mkdir -p /tmp/apt-cache \
    && printf 'Dir::Cache::archives "/tmp/apt-cache";\n' > /etc/apt/apt.conf.d/99cache-dir \
    && apt-get -o Acquire::AllowInsecureRepositories=true \
      -o Dir::Cache::archives=/tmp/apt-cache \
      -o Acquire::AllowDowngradeToInsecureRepositories=true \
      update \
    && DEBIAN_FRONTEND=noninteractive apt-get \
      -o Dir::Cache::archives=/tmp/apt-cache \
      -o APT::Get::AllowUnauthenticated=true \
      install -y --no-install-recommends bash curl git tini chromium ripgrep \
    && if ! getent group node >/dev/null; then groupadd --gid 1000 node || groupadd node; fi \
    && if ! id -u node >/dev/null 2>&1; then useradd --uid 1000 --gid node --create-home --home-dir /home/node --shell /bin/bash node || useradd --gid node --create-home --home-dir /home/node --shell /bin/bash node; fi \
    && rm -rf /var/lib/apt/lists/* /tmp/apt-cache /etc/apt/apt.conf.d/99cache-dir

COPY --from=agent_browser /usr/local/bin/node /usr/local/bin/node
COPY --from=agent_browser /usr/local/bin/agent-browser /usr/local/bin/agent-browser
COPY --from=agent_browser /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN mkdir -p /usr/local/lib/python3.12/site-packages/node_modules \
    && ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -sf ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && ln -sf ../lib/node_modules/mcporter/dist/cli.js /usr/local/bin/mcporter \
    && ln -s /usr/local/lib/node_modules/agent-browser /usr/local/lib/python3.12/site-packages/node_modules/agent-browser \
    && mkdir -p /home/node/.hermes \
    && chown -R node:node /home/node /app

COPY --from=hermes_src /src /tmp/hermes-src
COPY ksadk_runtime_common /opt/ksadk_runtime_common

RUN pip install --no-cache-dir \
    "fastapi>=0.100" \
    "uvicorn[standard]>=0.23" \
    "httpx>=0.24" \
    "pyyaml>=6.0" \
    "python-dotenv>=1.0" \
    "aiohttp>=3.13.3,<4" \
    "websockets>=15.0.1,<16" \
    "cryptography>=46.0.5,<47" \
    "/tmp/hermes-src[web,feishu,pty,cron,mcp,cli]" \
    -i "${PYPI_INDEX_URL}" \
    --extra-index-url "${PYPI_EXTRA_INDEX_URL}" \
    && rm -rf /tmp/hermes-src

COPY --from=hermes_web /src/hermes_cli/web_dist /usr/local/lib/python3.12/site-packages/hermes_cli/web_dist

COPY deploy/hermes/runtime ./runtime
COPY deploy/hermes/skills/agent-browser-clawdbot ./skills/agent-browser-clawdbot
COPY deploy/hermes/skills/multi-search-engine ./skills/multi-search-engine
COPY --from=kdocs_skill /app/skills/kdocs ./skills/kdocs
COPY deploy/hermes/entrypoint.sh ./entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && chown -R node:node /app /home/node

EXPOSE 8080

USER node

ENTRYPOINT ["tini", "--"]
CMD ["/app/entrypoint.sh"]
