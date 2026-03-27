## syntax=docker/dockerfile:1.7
ARG OPENCLAW_BASE_IMAGE=alpine/openclaw:2026.3.24
FROM ${OPENCLAW_BASE_IMAGE}

# ── 可通过 --build-arg 覆盖的参数 ──────────────────────────────
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG TZ=Asia/Shanghai
ARG OPENCLAW_INSTALL_WEIXIN_PLUGIN=true
ARG OPENCLAW_WEIXIN_PLUGIN_SPEC=@tencent-weixin/openclaw-weixin
ARG OPENCLAW_WEIXIN_PLUGIN_ID=openclaw-weixin
ARG OPENCLAW_INSTALL_LARK_PLUGIN=true
ARG OPENCLAW_LARK_PLUGIN_SPEC=@larksuite/openclaw-lark
ARG OPENCLAW_LARK_PLUGIN_ID=openclaw-lark
ARG OPENCLAW_INSTALL_AGENTSPACE_PLUGIN=true
ARG OPENCLAW_AGENTSPACE_PLUGIN_ID=agentspace
ARG OPENCLAW_INSTALL_AGENT_BROWSER=true
ARG OPENCLAW_AGENT_BROWSER_SPEC=agent-browser
ARG OPENCLAW_INSTALL_SKILLHUB=true
ARG OPENCLAW_SKILLHUB_KIT_URL=https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/latest.tar.gz
ARG OPENCLAW_SKILLHUB_KIT_SHA256=79a3b78a47f76c14a2411639b7205bf0ed539879f8ffff67f6202b61ec3ca004

USER root
ENV TZ=${TZ}

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
  apk add --no-cache tzdata chromium curl jq python3 py3-pip; \
  elif command -v apt-get >/dev/null 2>&1; then \
  # Debian bookworm+: 使用 DEB822 格式 (.sources) \
  if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
  sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
  elif [ -f /etc/apt/sources.list ]; then \
  sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
  fi; \
  # 跨架构 QEMU 构建时 GPG 校验可能失败，临时放宽 \
  apt-get -o Acquire::AllowInsecureRepositories=true \
  -o Acquire::AllowDowngradeToInsecureRepositories=true \
  update; \
  DEBIAN_FRONTEND=noninteractive apt-get \
  -o APT::Get::AllowUnauthenticated=true \
  install -y --no-install-recommends \
  tzdata chromium ca-certificates curl jq python3 python3-pip; \
  rm -rf /var/lib/apt/lists/*; \
  fi; \
  # ====== 2. 时区设为北京 ====== \
  cp /usr/share/zoneinfo/${TZ} /etc/localtime; \
  echo "${TZ}" > /etc/timezone; \
  # ====== 3. npm 换淘宝镜像 ====== \
  if command -v npm >/dev/null 2>&1; then \
  npm config set registry "${NPM_REGISTRY}"; \
  npm install -g --no-audit --no-fund mcporter; \
  fi; \
  # ====== 4. 清理缓存，最小化镜像体积 ====== \
  rm -rf /tmp/* /var/tmp/* /root/.cache 2>/dev/null || true

COPY --chmod=755 bootstrap.sh /usr/local/bin/bootstrap.sh
COPY --chmod=755 safe-bin/openclaw-safe-exec /opt/openclaw/safe-bin/openclaw-safe-exec
COPY workspace-template /opt/openclaw/workspace-template
RUN set -eux; \
  mkdir -p /opt/openclaw/safe-bin; \
  for cmd in pwd ls whoami id uname date ps df du stat find cat head tail wc git mcporter sh-safe bash-safe web-safe; do \
    cp /opt/openclaw/safe-bin/openclaw-safe-exec "/opt/openclaw/safe-bin/${cmd}"; \
    chmod 755 "/opt/openclaw/safe-bin/${cmd}"; \
  done

RUN --mount=type=cache,target=/root/.npm \
  set -eux; \
  mkdir -p /opt/openclaw/default-extensions; \
  unpack_plugin_archive() { \
    archive_path="$1"; \
    plugin_id="$2"; \
    work_root="$3"; \
    src_dir="$4"; \
    mkdir -p "$(dirname "${src_dir}")"; \
    tar -xzf "${archive_path}" -C "${work_root}"; \
    test -d "${work_root}/package"; \
    mv "${work_root}/package" "${src_dir}"; \
    if [ -f "${src_dir}/package.json" ]; then \
      (cd "${src_dir}" && npm install --omit=dev --no-audit --no-fund --registry "${NPM_REGISTRY}"); \
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
  if [ "${OPENCLAW_INSTALL_WEIXIN_PLUGIN}" = "true" ]; then \
    install_default_plugin "${OPENCLAW_WEIXIN_PLUGIN_SPEC}" "${OPENCLAW_WEIXIN_PLUGIN_ID}"; \
  fi; \
  if [ "${OPENCLAW_INSTALL_LARK_PLUGIN}" = "true" ]; then \
    install_default_plugin "${OPENCLAW_LARK_PLUGIN_SPEC}" "${OPENCLAW_LARK_PLUGIN_ID}"; \
  fi; \
  chown -R node:node /opt/openclaw/default-extensions; \
  chmod -R a+rX /opt/openclaw/default-extensions

COPY preset-skills /opt/openclaw/preset-skills

RUN --mount=type=cache,target=/root/.cache/pip \
  set -eux; \
  skill_requirements="/opt/openclaw/preset-skills/wps365-skill/requirements.txt"; \
  if [ -f "${skill_requirements}" ] && command -v python3 >/dev/null 2>&1; then \
    PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install -i "${PYPI_INDEX_URL}" -r "${skill_requirements}"; \
  fi; \
  find /opt/openclaw/preset-skills -type f ! -name '.content.sig' | LC_ALL=C sort | while IFS= read -r file_path; do \
    cksum "${file_path}"; \
  done | cksum | awk '{print $1 ":" $2}' > /opt/openclaw/preset-skills/.content.sig

COPY agentspace-assets.lock.json /opt/openclaw/agentspace-assets.lock.json

RUN --mount=type=cache,target=/root/.npm \
  set -eux; \
  mkdir -p /opt/openclaw/default-extensions; \
  unpack_plugin_archive() { \
    archive_path="$1"; \
    plugin_id="$2"; \
    work_root="$3"; \
    src_dir="$4"; \
    mkdir -p "$(dirname "${src_dir}")"; \
    tar -xzf "${archive_path}" -C "${work_root}"; \
    test -d "${work_root}/package"; \
    mv "${work_root}/package" "${src_dir}"; \
    if [ -f "${src_dir}/package.json" ]; then \
      (cd "${src_dir}" && npm install --omit=dev --no-audit --no-fund --registry "${NPM_REGISTRY}"); \
    fi; \
    HOME="${work_root}/home" OPENCLAW_STATE_DIR="${work_root}/state" openclaw plugins inspect "${plugin_id}" >/dev/null; \
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
  if [ "${OPENCLAW_INSTALL_AGENTSPACE_PLUGIN}" = "true" ]; then \
    agentspace_url="$(jq -r '.assets.agentspace_plugin.url // empty' /opt/openclaw/agentspace-assets.lock.json)"; \
    agentspace_sha256="$(jq -r '.assets.agentspace_plugin.sha256 // empty' /opt/openclaw/agentspace-assets.lock.json)"; \
    if [ -z "${agentspace_url}" ]; then \
      echo "ERROR: agentspace plugin url missing in /opt/openclaw/agentspace-assets.lock.json" >&2; \
      exit 1; \
    fi; \
    install_default_plugin_archive "${agentspace_url}" "${agentspace_sha256}" "${OPENCLAW_AGENTSPACE_PLUGIN_ID}"; \
  fi; \
  chown -R node:node /opt/openclaw/default-extensions; \
  chmod -R a+rX /opt/openclaw/default-extensions

RUN --mount=type=cache,target=/root/.npm \
  set -eux; \
  if [ "${OPENCLAW_INSTALL_AGENT_BROWSER}" = "true" ] && command -v npm >/dev/null 2>&1; then \
    npm install -g --no-audit --no-fund "${OPENCLAW_AGENT_BROWSER_SPEC}"; \
  fi; \
  mkdir -p /home/node; \
  if [ "${OPENCLAW_INSTALL_SKILLHUB}" = "true" ]; then \
    tmp_dir="$(mktemp -d)"; \
    curl -fsSL "${OPENCLAW_SKILLHUB_KIT_URL}" -o "${tmp_dir}/skillhub.tar.gz"; \
    echo "${OPENCLAW_SKILLHUB_KIT_SHA256}  ${tmp_dir}/skillhub.tar.gz" | sha256sum -c -; \
    tar -xzf "${tmp_dir}/skillhub.tar.gz" -C "${tmp_dir}"; \
    HOME=/home/node bash "${tmp_dir}/cli/install.sh" --cli-only; \
    rm -rf "${tmp_dir}"; \
    chown -R node:node /home/node/.skillhub /home/node/.local; \
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
    PATH=/home/node/.local/bin:${PATH}
USER node

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
  CMD node -e "const net=require('node:net'); const port=Number(process.env.OPENCLAW_PUBLIC_PORT||process.env.PORT||8080); const socket=net.connect({host:'127.0.0.1', port}, ()=>{socket.end(); process.exit(0);}); socket.on('error', ()=>process.exit(1)); socket.setTimeout(5000, ()=>{socket.destroy(); process.exit(1);});"

ENTRYPOINT ["/usr/local/bin/bootstrap.sh"]
