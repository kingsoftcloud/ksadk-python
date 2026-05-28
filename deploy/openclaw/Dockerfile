## syntax=docker/dockerfile:1.7
# https://github.com/openclaw/openclaw/pkgs/container/openclaw
ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw:2026.5.22-slim@sha256:d35b8b681c223a85027502c7a82999aa772d6a09e1b28903951cac7fc27efed5
FROM ${OPENCLAW_BASE_IMAGE}

# ── 可通过 --build-arg 覆盖的参数 ──────────────────────────────
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG APT_MIRROR=http://mirrors.aliyun.com/debian
ARG APT_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
ARG TZ=Asia/Shanghai
ARG OPENCLAW_INSTALL_WEIXIN_PLUGIN=true
ARG OPENCLAW_WEIXIN_PLUGIN_SPEC=@tencent-weixin/openclaw-weixin
ARG OPENCLAW_WEIXIN_PLUGIN_ID=openclaw-weixin
ARG OPENCLAW_INSTALL_LARK_PLUGIN=true
ARG OPENCLAW_LARK_PLUGIN_SPEC=@larksuite/openclaw-lark
ARG OPENCLAW_LARK_PLUGIN_ID=openclaw-lark
ARG OPENCLAW_INSTALL_MEM0_PLUGIN=true
ARG OPENCLAW_MEM0_PLUGIN_ID=openclaw-mem0
ARG OPENCLAW_MEM0_PLUGIN_URL=https://memory-engine.ks3-cn-beijing.ksyuncs.com/ksc-openclaw-mem0-1.0.11.tgz
ARG OPENCLAW_MEM0_PLUGIN_SHA256=
ARG OPENCLAW_INSTALL_WPS_XIEZUO_PLUGIN=true
ARG OPENCLAW_WPS_XIEZUO_PLUGIN_ID=wps-xiezuo
ARG OPENCLAW_WPS_XIEZUO_PLUGIN_SPEC=@wps365/openclaw-wpsxiezuo
ARG OPENCLAW_INSTALL_DIAGNOSTICS_OTEL_PLUGIN=true
ARG OPENCLAW_DIAGNOSTICS_OTEL_PLUGIN_SPEC=@openclaw/diagnostics-otel
ARG OPENCLAW_DIAGNOSTICS_OTEL_PLUGIN_ID=diagnostics-otel
ARG OPENCLAW_PRESET_PLUGINS_ALLOWLIST=
ARG OPENCLAW_INSTALL_AGENT_BROWSER=true
ARG OPENCLAW_AGENT_BROWSER_SPEC=agent-browser
ARG KDOCS_SKILL_REPO=https://github.com/kdocs-app/kdocs-skill.git

USER root
ENV TZ=${TZ}
ENV OPENCLAW_PRESET_PLUGINS_ALLOWLIST=${OPENCLAW_PRESET_PLUGINS_ALLOWLIST}

# 说明：
# - 在 Apple Silicon 上构建 linux/amd64 会走 QEMU，首轮必然偏慢。
# - 这里优先按“缓存命中边界”组织步骤，而不是盲目压到最少层数。
# - npm / pip 下载缓存交给 BuildKit cache mount 管理，不写进最终镜像。
RUN --mount=type=cache,target=/root/.npm \
  set -eux; \
  # ====== 1. 系统包管理器换国内源 + 安装依赖 ====== \
  if command -v apk >/dev/null 2>&1; then \
  ALPINE_VER="$(cat /etc/alpine-release 2>/dev/null | cut -d. -f1,2)"; \
  if [ -n "${ALPINE_VER}" ]; then \
  printf '%s\n' \
  "https://mirrors.aliyun.com/alpine/v${ALPINE_VER}/main" \
  "https://mirrors.aliyun.com/alpine/v${ALPINE_VER}/community" \
  > /etc/apk/repositories; \
  fi; \
  apk update; \
  apk add --no-cache tzdata chromium curl jq python3 py3-pip ca-certificates tini; \
  elif command -v apt-get >/dev/null 2>&1; then \
  # Debian bookworm+: 使用 DEB822 格式 (.sources) \
  if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
  sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g; s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
  elif [ -f /etc/apt/sources.list ]; then \
  sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g; s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g; s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" /etc/apt/sources.list; \
  fi; \
  # 跨架构 QEMU 构建时 GPG 校验可能失败，临时放宽 \
  apt-get -o Acquire::AllowInsecureRepositories=true \
  -o Acquire::AllowDowngradeToInsecureRepositories=true \
  -o Acquire::Retries=5 \
  -o Acquire::http::Timeout=30 \
  -o Acquire::https::Timeout=30 \
  update; \
  DEBIAN_FRONTEND=noninteractive apt-get \
  -o APT::Get::AllowUnauthenticated=true \
  -o Acquire::Retries=5 \
  -o Acquire::http::Timeout=30 \
  -o Acquire::https::Timeout=30 \
  install -y --no-install-recommends \
  tzdata chromium ca-certificates curl jq python3 python3-pip tini; \
  rm -rf /var/lib/apt/lists/*; \
  fi; \
  if command -v tini >/dev/null 2>&1; then \
  ln -sf "$(command -v tini)" /usr/local/bin/tini; \
  fi; \
  # ====== 2. 时区设为北京 ====== \
  cp /usr/share/zoneinfo/${TZ} /etc/localtime; \
  echo "${TZ}" > /etc/timezone; \
  # ====== 3. npm 换淘宝镜像 ====== \
  if command -v npm >/dev/null 2>&1; then \
  npm config set registry "${NPM_REGISTRY}"; \
  npm install -g --no-audit --no-fund mcporter clawhub@latest; \
  fi; \
  # ====== 4. 清理缓存，最小化镜像体积 ====== \
  rm -rf /tmp/* /var/tmp/* /root/.cache 2>/dev/null || true

COPY ksadk_runtime_common /opt/ksadk_runtime_common
COPY deploy/openclaw/workspace_files_app.py /opt/openclaw/workspace_files_app.py
COPY deploy/openclaw/openclaw_runtime_proxy_app.py /opt/openclaw/openclaw_runtime_proxy_app.py
COPY --chmod=755 deploy/openclaw/safe-bin/openclaw-safe-exec /opt/openclaw/safe-bin/openclaw-safe-exec
COPY deploy/openclaw/workspace-template /opt/openclaw/workspace-template
RUN set -eux; \
  mkdir -p /opt/openclaw/safe-bin; \
  for cmd in pwd ls whoami id uname date ps df du stat find cat head tail wc git mcporter sh-safe bash-safe web-safe; do \
    cp /opt/openclaw/safe-bin/openclaw-safe-exec "/opt/openclaw/safe-bin/${cmd}"; \
    chmod 755 "/opt/openclaw/safe-bin/${cmd}"; \
  done

RUN --mount=type=cache,target=/root/.npm \
  set -eux; \
  mkdir -p /opt/openclaw/default-extensions; \
  preset_plugin_allowed() { \
    plugin_id="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"; \
    shift || true; \
    allowlist="$(printf ',%s,' "${OPENCLAW_PRESET_PLUGINS_ALLOWLIST:-}" | tr '[:upper:]' '[:lower:]' | sed 's/[;[:space:]]\\+/,/g')"; \
    if [ -z "${OPENCLAW_PRESET_PLUGINS_ALLOWLIST:-}" ]; then \
      return 0; \
    fi; \
    case "${allowlist}" in *,"${plugin_id}",*) return 0 ;; esac; \
    for alias in "$@"; do \
      alias="$(printf '%s' "${alias:-}" | tr '[:upper:]' '[:lower:]')"; \
      [ -n "${alias}" ] || continue; \
      case "${allowlist}" in *,"${alias}",*) return 0 ;; esac; \
    done; \
    return 1; \
  }; \
  unpack_plugin_archive() { \
    archive_path="$1"; \
    plugin_id="$2"; \
    work_root="$3"; \
    src_dir="$4"; \
    extract_root="${work_root}/extract"; \
    mkdir -p "$(dirname "${src_dir}")"; \
    rm -rf "${extract_root}"; \
    mkdir -p "${extract_root}"; \
    tar -xzf "${archive_path}" -C "${extract_root}"; \
    if [ -d "${extract_root}/package" ]; then \
      mv "${extract_root}/package" "${src_dir}"; \
    else \
      archive_root="$(find "${extract_root}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"; \
      test -n "${archive_root}"; \
      mv "${archive_root}" "${src_dir}"; \
    fi; \
    if [ -f "${src_dir}/package.json" ]; then \
      (cd "${src_dir}" && node -e 'const fs=require("fs"); const p="package.json"; const pkg=JSON.parse(fs.readFileSync(p,"utf8")); const deps=pkg.devDependencies || {}; let changed=false; for (const [name, spec] of Object.entries(deps)) { if (typeof spec === "string" && spec.startsWith("workspace:")) { delete deps[name]; changed=true; } } if (Object.keys(deps).length) pkg.devDependencies=deps; else delete pkg.devDependencies; if (changed) fs.writeFileSync(p, JSON.stringify(pkg, null, 2) + "\n");'); \
      (cd "${src_dir}" && npm install --omit=dev --no-audit --no-fund --registry "${NPM_REGISTRY}"); \
      if [ -f /app/package.json ] && [ ! -e "${src_dir}/node_modules/openclaw" ]; then \
        mkdir -p "${src_dir}/node_modules"; \
        ln -s /app "${src_dir}/node_modules/openclaw"; \
      fi; \
    fi; \
    HOME="${work_root}/home" OPENCLAW_STATE_DIR="${work_root}/state" openclaw plugins inspect "${plugin_id}" >/dev/null; \
  }; \
  install_default_plugin() { \
    plugin_spec="$1"; \
    plugin_id="$2"; \
    work_root="$(mktemp -d)"; \
    pack_dir="${work_root}/packs"; \
    src_dir="${work_root}/state/extensions/${plugin_id}"; \
    dst_dir="/opt/openclaw/default-extensions/${plugin_id}"; \
    mkdir -p "${work_root}/home" "${work_root}/state" "${pack_dir}" "/opt/openclaw/default-extensions"; \
    plugin_archive="$(cd "${pack_dir}" && npm pack --silent "${plugin_spec}")"; \
    plugin_archive="${pack_dir}/${plugin_archive}"; \
    test -f "${plugin_archive}"; \
    unpack_plugin_archive "${plugin_archive}" "${plugin_id}" "${work_root}" "${src_dir}"; \
    test -d "${src_dir}"; \
    rm -rf "${dst_dir}"; \
    cp -R "${src_dir}" "${dst_dir}"; \
    rm -rf "${work_root}"; \
  }; \
  install_default_plugin_archive() { \
    archive_url="$1"; \
    archive_sha256="$2"; \
    plugin_id="$3"; \
    work_root="$(mktemp -d)"; \
    src_dir="${work_root}/state/extensions/${plugin_id}"; \
    dst_dir="/opt/openclaw/default-extensions/${plugin_id}"; \
    archive_path="${work_root}/${plugin_id}.tgz"; \
    mkdir -p "${work_root}/home" "${work_root}/state" "/opt/openclaw/default-extensions"; \
    curl -fsSL "${archive_url}" -o "${archive_path}"; \
    if [ -n "${archive_sha256}" ]; then \
      echo "${archive_sha256}  ${archive_path}" | sha256sum -c -; \
    fi; \
    unpack_plugin_archive "${archive_path}" "${plugin_id}" "${work_root}" "${src_dir}"; \
    test -d "${src_dir}"; \
    rm -rf "${dst_dir}"; \
    cp -R "${src_dir}" "${dst_dir}"; \
    rm -rf "${work_root}"; \
  }; \
  install_default_plugin_archive_file() { \
    archive_path="$1"; \
    plugin_id="$2"; \
    work_root="$(mktemp -d)"; \
    src_dir="${work_root}/state/extensions/${plugin_id}"; \
    dst_dir="/opt/openclaw/default-extensions/${plugin_id}"; \
    mkdir -p "${work_root}/home" "${work_root}/state" "/opt/openclaw/default-extensions"; \
    test -f "${archive_path}"; \
    # Keep this install path compatible with upstream OpenClaw 2026.3.28:
    # that CLI accepts `openclaw plugins install <archive>` but does not
    # support `--force`, which newer releases added later.
    #
    # We intentionally omit `--force` here because `work_root` is always a
    # brand-new temporary state directory, so there is no pre-existing plugin
    # to overwrite during image build.
    #
    # Safe removal rule for this compatibility note:
    # remove or revisit it only after we drop 2026.3.28-based images, or if
    # this helper stops installing into a fresh temp state dir and genuinely
    # needs overwrite semantics again.
    HOME="${work_root}/home" OPENCLAW_STATE_DIR="${work_root}/state" openclaw plugins install "${archive_path}"; \
    HOME="${work_root}/home" OPENCLAW_STATE_DIR="${work_root}/state" openclaw plugins inspect "${plugin_id}" >/dev/null; \
    test -d "${src_dir}"; \
    rm -rf "${dst_dir}"; \
    cp -R "${src_dir}" "${dst_dir}"; \
    rm -rf "${work_root}"; \
  }; \
  if [ "${OPENCLAW_INSTALL_WEIXIN_PLUGIN}" = "true" ] && preset_plugin_allowed "${OPENCLAW_WEIXIN_PLUGIN_ID}" "${OPENCLAW_WEIXIN_PLUGIN_SPEC}" weixin; then \
    install_default_plugin "${OPENCLAW_WEIXIN_PLUGIN_SPEC}" "${OPENCLAW_WEIXIN_PLUGIN_ID}"; \
  fi; \
  if [ "${OPENCLAW_INSTALL_LARK_PLUGIN}" = "true" ] && preset_plugin_allowed "${OPENCLAW_LARK_PLUGIN_ID}" "${OPENCLAW_LARK_PLUGIN_SPEC}" feishu lark; then \
    install_default_plugin "${OPENCLAW_LARK_PLUGIN_SPEC}" "${OPENCLAW_LARK_PLUGIN_ID}"; \
  fi; \
  if [ "${OPENCLAW_INSTALL_MEM0_PLUGIN}" = "true" ] && preset_plugin_allowed "${OPENCLAW_MEM0_PLUGIN_ID}" mem0; then \
    install_default_plugin_archive "${OPENCLAW_MEM0_PLUGIN_URL}" "${OPENCLAW_MEM0_PLUGIN_SHA256}" "${OPENCLAW_MEM0_PLUGIN_ID}"; \
  fi; \
  if [ "${OPENCLAW_INSTALL_WPS_XIEZUO_PLUGIN}" = "true" ] && preset_plugin_allowed "${OPENCLAW_WPS_XIEZUO_PLUGIN_ID}" wps wps-xiezuo; then \
    install_default_plugin "${OPENCLAW_WPS_XIEZUO_PLUGIN_SPEC}" "${OPENCLAW_WPS_XIEZUO_PLUGIN_ID}"; \
  fi; \
  if [ "${OPENCLAW_INSTALL_DIAGNOSTICS_OTEL_PLUGIN}" = "true" ] && preset_plugin_allowed "${OPENCLAW_DIAGNOSTICS_OTEL_PLUGIN_ID}" diagnostics otel diagnostics-otel; then \
    install_default_plugin "${OPENCLAW_DIAGNOSTICS_OTEL_PLUGIN_SPEC}" "${OPENCLAW_DIAGNOSTICS_OTEL_PLUGIN_ID}"; \
  fi; \
  chown -R node:node /opt/openclaw/default-extensions; \
  chmod -R a+rX /opt/openclaw/default-extensions

COPY deploy/openclaw/preset-skills/agent-browser-clawdbot /opt/openclaw/preset-skills/agent-browser-clawdbot
COPY deploy/openclaw/preset-skills/clawhub-store /opt/openclaw/preset-skills/clawhub-store
COPY deploy/openclaw/preset-skills/multi-search-engine /opt/openclaw/preset-skills/multi-search-engine
COPY deploy/openclaw/preset-skills/self-improving-agent /opt/openclaw/preset-skills/self-improving-agent
COPY deploy/openclaw/preset-skills/tuanziguardianclaw /opt/openclaw/preset-skills/tuanziguardianclaw
COPY deploy/openclaw/preset-skills/wps365-skill /opt/openclaw/preset-skills/wps365-skill
COPY --chmod=755 deploy/openclaw/bootstrap.sh /usr/local/bin/bootstrap.sh

RUN set -eux; \
  rm -rf /tmp/kdocs-skill /opt/openclaw/preset-skills/kdocs; \
  git clone --depth 1 "${KDOCS_SKILL_REPO}" /tmp/kdocs-skill; \
  mkdir -p /opt/openclaw/preset-skills/kdocs/scripts; \
  cp -R /tmp/kdocs-skill/. /opt/openclaw/preset-skills/kdocs/; \
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' \
    'exec bash "${SCRIPT_DIR}/scripts/setup.sh" "$@"' \
    > /opt/openclaw/preset-skills/kdocs/setup.sh; \
  chmod 755 /opt/openclaw/preset-skills/kdocs/setup.sh; \
  cp /tmp/kdocs-skill/scripts/setup.sh /opt/openclaw/preset-skills/kdocs/scripts/setup.sh; \
  rm -rf /tmp/kdocs-skill

RUN --mount=type=cache,target=/root/.cache/pip \
  set -eux; \
  PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install -i "${PYPI_INDEX_URL}" \
    "fastapi>=0.100.0,<0.124.0" \
    "pydantic>=2.0.0,<3.0.0" \
    "jsonschema>=4.0.0,<5.0.0" \
    "httpx>=0.24.0,<1.0.0" \
    "uvicorn>=0.23.0,<1.0.0" \
    "websockets>=11.0.0,<16.0.0" \
    "python-multipart>=0.0.9,<1.0.0"

RUN --mount=type=cache,target=/root/.cache/pip \
  set -eux; \
  skill_requirements="/opt/openclaw/preset-skills/wps365-skill/requirements.txt"; \
  if [ -f "${skill_requirements}" ] && command -v python3 >/dev/null 2>&1; then \
    PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install -i "${PYPI_INDEX_URL}" -r "${skill_requirements}"; \
  fi; \
  find /opt/openclaw/preset-skills -type f ! -name '.content.sig' | LC_ALL=C sort | while IFS= read -r file_path; do \
    cksum "${file_path}"; \
  done | cksum | awk '{print $1 ":" $2}' > /opt/openclaw/preset-skills/.content.sig

RUN --mount=type=cache,target=/root/.npm \
  set -eux; \
  if [ "${OPENCLAW_INSTALL_AGENT_BROWSER}" = "true" ] && command -v npm >/dev/null 2>&1; then \
    npm install -g --no-audit --no-fund "${OPENCLAW_AGENT_BROWSER_SPEC}"; \
  fi; \
  OPENCLAW_DIST_PATCH_ONLY=1 /usr/local/bin/bootstrap.sh

RUN node <<'NODE'
const fs = require('fs');
const path = require('path');

const root = '/opt/openclaw/default-extensions';
const entries = fs.existsSync(root) ? fs.readdirSync(root, { withFileTypes: true }) : [];

for (const entry of entries) {
  if (!entry.isDirectory()) continue;
  const dirPath = path.join(root, entry.name);
  const packageJsonPath = path.join(dirPath, 'package.json');
  let signature = `${entry.name}@unknown|bundle`;

  try {
    const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'));
    const packageName = typeof packageJson.name === 'string' && packageJson.name.trim()
      ? packageJson.name.trim()
      : entry.name;
    const packageVersion = typeof packageJson.version === 'string' && packageJson.version.trim()
      ? packageJson.version.trim()
      : 'unknown';
    const markers = ['bundle'];

    if (entry.name === 'openclaw-weixin') {
      const channelPath = path.join(dirPath, 'src', 'channel.ts');
      if (fs.existsSync(channelPath)) {
        const channelSource = fs.readFileSync(channelPath, 'utf8');
        if (channelSource.includes('gatewayMethods: ["web.login.start", "web.login.wait"]')) {
          markers.push('weixin-web-login-shim');
        }
      }
    }

    signature = `${packageName}@${packageVersion}|${markers.join('+')}`;
  } catch {}

  fs.writeFileSync(path.join(dirPath, '.content.sig'), `${signature}\n`, 'utf8');
}
NODE

ENV HOME=/home/node \
    OPENCLAW_STATE_DIR=/home/node/.openclaw \
    PYTHONPATH=/opt \
    PATH=/home/node/.local/bin:${PATH}
USER node

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
  CMD sh -lc 'curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT:-8080}/healthz" >/dev/null || exit 1'

ENTRYPOINT ["/usr/local/bin/tini","--","/usr/local/bin/bootstrap.sh"]
