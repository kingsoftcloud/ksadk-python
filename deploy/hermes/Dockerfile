FROM node:20-bookworm-slim AS hermes_web

ARG HERMES_AGENT_REF=v2026.4.13
ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${HERMES_AGENT_REF}" https://github.com/NousResearch/hermes-agent.git .

WORKDIR /src/web

RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi \
    && npm run build

FROM node:20-bookworm-slim AS agent_browser

ARG NPM_REGISTRY=https://registry.npmmirror.com

RUN npm config set registry "${NPM_REGISTRY}" \
    && npm install -g agent-browser@0.13.0

FROM python:3.12-bookworm

ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG HERMES_AGENT_REF=v2026.4.13
ARG APT_MIRROR=https://mirrors.aliyun.com/debian

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    API_SERVER_HOST=127.0.0.1 \
    API_SERVER_PORT=8642 \
    HERMES_DASHBOARD_HOST=127.0.0.1 \
    HERMES_DASHBOARD_PORT=9119 \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /app

RUN if [ -n "${APT_MIRROR}" ]; then \
      sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|http://deb.debian.org/debian-security|${APT_MIRROR}-security|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash curl git tini chromium ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=agent_browser /usr/local/bin/node /usr/local/bin/node
COPY --from=agent_browser /usr/local/bin/agent-browser /usr/local/bin/agent-browser
COPY --from=agent_browser /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN mkdir -p /usr/local/lib/python3.12/site-packages/node_modules \
    && ln -s /usr/local/lib/node_modules/agent-browser /usr/local/lib/python3.12/site-packages/node_modules/agent-browser

RUN pip install --no-cache-dir \
    "fastapi>=0.100" \
    "uvicorn[standard]>=0.23" \
    "httpx>=0.24" \
    "pyyaml>=6.0" \
    "python-dotenv>=1.0" \
    "hermes-agent[web,messaging,feishu] @ git+https://github.com/NousResearch/hermes-agent.git@${HERMES_AGENT_REF}" \
    -i "${PYPI_INDEX_URL}"

COPY --from=hermes_web /src/hermes_cli/web_dist /usr/local/lib/python3.12/site-packages/hermes_cli/web_dist

COPY runtime ./runtime
COPY skills ./skills
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["tini", "--"]
CMD ["/app/entrypoint.sh"]
