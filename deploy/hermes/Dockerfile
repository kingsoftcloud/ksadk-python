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

ARG HERMES_AGENT_REF=v2026.5.29.2

WORKDIR /src

RUN git clone --depth 1 --branch "${HERMES_AGENT_REF}" https://github.com/NousResearch/hermes-agent.git .

FROM hermes_src AS hermes_web

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /src

RUN npm config set registry "${NPM_REGISTRY}" \
    && if [ -f package-lock.json ]; then npm ci --workspace web; else npm install --workspace web; fi \
    && npm --workspace web run build

FROM hermes_src AS hermes_tui

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /src

RUN npm config set registry "${NPM_REGISTRY}" \
    && if [ -f package-lock.json ]; then npm ci --workspace ui-tui; else npm install --workspace ui-tui; fi \
    && npm --workspace ui-tui run build

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
      install -y --no-install-recommends bash curl git tini chromium ripgrep ffmpeg \
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
    "/tmp/hermes-src[web,feishu,pty,cron,mcp,cli]" \
    "langfuse>=3.9.0,<4" \
    "discord.py[voice]==2.7.1" \
    "brotlicffi==1.2.0.1" \
    "hermes-wpsxiezuo" \
    -i "${PYPI_INDEX_URL}" \
    --extra-index-url "${PYPI_EXTRA_INDEX_URL}" \
    && python -c 'from pathlib import Path; import json, shutil, site; exec("""roots = [Path(p) / \"plugins\" for p in site.getsitepackages()]\nroots.extend(Path(p) / \"plugins\" for p in site.getusersitepackages().splitlines())\nfor plugins_root in roots:\n    if not plugins_root.exists():\n        continue\n    for manifest_file in plugins_root.glob(\"**/dashboard/manifest.json\"):\n        dashboard_dir = manifest_file.parent\n        plugin_root = dashboard_dir.parent\n        try:\n            manifest = json.loads(manifest_file.read_text(encoding=\"utf-8\"))\n        except Exception:\n            continue\n        entry = str(manifest.get(\"entry\") or \"\").strip()\n        if not entry:\n            continue\n        entry_path = dashboard_dir / entry\n        if entry_path.is_file():\n            continue\n        shutil.rmtree(plugin_root)\n        print(f\"Removed incomplete bundled dashboard plugin: {plugin_root}\")""")' \
    && rm -rf /tmp/hermes-src

COPY --from=hermes_src /src/hermes_cli/dashboard_auth /usr/local/lib/python3.12/site-packages/hermes_cli/dashboard_auth
COPY --from=hermes_web /src/hermes_cli/web_dist /usr/local/lib/python3.12/site-packages/hermes_cli/web_dist
COPY --from=hermes_tui /src/ui-tui/dist/entry.js /usr/local/lib/python3.12/site-packages/hermes_cli/tui_dist/entry.js

RUN python -c 'from pathlib import Path; import hermes_cli, plugins, tools; from tools import lazy_deps; missing = lazy_deps.feature_missing("platform.discord"); missing and (_ for _ in ()).throw(SystemExit(f"Hermes dashboard dependency missing: {missing}")); hermes_cli_dir = Path(hermes_cli.__file__).resolve().parent; web_dist = hermes_cli_dir / "web_dist" / "index.html"; web_dist.is_file() or (_ for _ in ()).throw(SystemExit(f"Hermes web_dist missing: {web_dist}")); tui_dist = hermes_cli_dir / "tui_dist" / "entry.js"; tui_dist.is_file() or (_ for _ in ()).throw(SystemExit(f"Hermes tui_dist missing: {tui_dist}")); plugin_root = Path(plugins.__file__).resolve().parent; required = [plugin_root / "kanban/dashboard/dist/index.js", plugin_root / "hermes-achievements/dashboard/dist/index.js"]; missing_assets = [str(p) for p in required if not p.is_file()]; missing_assets and (_ for _ in ()).throw(SystemExit(f"Hermes dashboard plugin assets missing: {missing_assets}")); import hermes_cli.dashboard_auth.routes; import hermes_cli.main as m; argv, cwd = m._make_tui_argv(Path("/missing-ui-tui"), False); str(tui_dist) in argv or (_ for _ in ()).throw(SystemExit(f"Hermes bundled TUI not selected: argv={argv} cwd={cwd}")); import hermes_cli.web_server; print("Hermes dashboard smoke import passed")'

RUN python - <<'PY'
from pathlib import Path

path = Path("/usr/local/lib/python3.12/site-packages/hermes_cli/web_server.py")
text = path.read_text(encoding="utf-8")
old = '''    if gateway_ws_url := _build_gateway_ws_url():
        env["HERMES_TUI_GATEWAY_URL"] = gateway_ws_url
'''
new = '''    # AgentEngine runtime keeps the dashboard Chat tab enabled, but Hermes
    # v2026.6.5's in-process gateway attach path currently trips a React TUI
    # render loop in the embedded PTY. Let the TUI use its normal subprocess
    # gateway path until upstream fixes the attach transport.
    if os.environ.get("KSADK_HERMES_DASHBOARD_GATEWAY_ATTACH") == "1":
        if gateway_ws_url := _build_gateway_ws_url():
            env["HERMES_TUI_GATEWAY_URL"] = gateway_ws_url
'''
if old not in text:
    raise SystemExit("Hermes dashboard gateway attach patch target missing")
path.write_text(text.replace(old, new), encoding="utf-8")
PY

COPY deploy/hermes/runtime ./runtime
COPY deploy/hermes/skills/agent-browser-clawdbot ./skills/agent-browser-clawdbot
COPY deploy/hermes/skills/multi-search-engine ./skills/multi-search-engine
COPY --from=kdocs_skill /app/skills/kdocs ./skills/kdocs
COPY deploy/hermes/entrypoint.sh ./entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && chown -R node:node /app /home/node

EXPOSE 8080

USER node

ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
CMD []
