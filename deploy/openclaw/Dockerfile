ARG OPENCLAW_BASE_IMAGE=alpine/openclaw:2026.3.23-2
FROM ${OPENCLAW_BASE_IMAGE}

# ── 可通过 --build-arg 覆盖的参数 ──────────────────────────────
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG TZ=Asia/Shanghai
ARG OPENCLAW_INSTALL_WEIXIN_PLUGIN=true
ARG OPENCLAW_WEIXIN_PLUGIN_SPEC=@tencent-weixin/openclaw-weixin@2.0.1
ARG OPENCLAW_WEIXIN_PLUGIN_ID=openclaw-weixin
ARG OPENCLAW_INSTALL_LARK_PLUGIN=true
ARG OPENCLAW_LARK_PLUGIN_SPEC=@larksuite/openclaw-lark
ARG OPENCLAW_LARK_PLUGIN_ID=openclaw-lark
ARG OPENCLAW_INSTALL_AGENT_BROWSER=true
ARG OPENCLAW_AGENT_BROWSER_SPEC=agent-browser
ARG OPENCLAW_INSTALL_SKILLHUB=true
ARG OPENCLAW_SKILLHUB_KIT_URL=https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/latest.tar.gz
ARG OPENCLAW_SKILLHUB_KIT_SHA256=79a3b78a47f76c14a2411639b7205bf0ed539879f8ffff67f6202b61ec3ca004

USER root
ENV TZ=${TZ}

# ── 单层 RUN: 换源 → 系统包 → 时区 → npm → 清理 ──
RUN set -eux; \
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
  apk add --no-cache tzdata chromium curl jq python3; \
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
  tzdata chromium ca-certificates curl jq python3; \
  rm -rf /var/lib/apt/lists/*; \
  fi; \
  # ====== 2. 时区设为北京 ====== \
  cp /usr/share/zoneinfo/${TZ} /etc/localtime; \
  echo "${TZ}" > /etc/timezone; \
  # ====== 3. npm 换淘宝镜像 ====== \
  if command -v npm >/dev/null 2>&1; then \
  npm config set registry "${NPM_REGISTRY}"; \
  npm install -g mcporter undici; \
  npm cache clean --force || true; \
  fi; \
  # ====== 4. 清理缓存，最小化镜像体积 ====== \
  rm -rf /tmp/* /var/tmp/* /root/.cache /root/.npm 2>/dev/null || true

COPY --chmod=755 bootstrap.sh /usr/local/bin/bootstrap.sh
COPY --chmod=755 safe-bin/openclaw-safe-exec /opt/openclaw/safe-bin/openclaw-safe-exec
COPY workspace-template /opt/openclaw/workspace-template
COPY preset-skills /opt/openclaw/preset-skills

RUN set -eux; \
  find /opt/openclaw/preset-skills -type f ! -name '.content.sig' | LC_ALL=C sort | while IFS= read -r file_path; do \
    cksum "${file_path}"; \
  done | cksum | awk '{print $1 ":" $2}' > /opt/openclaw/preset-skills/.content.sig

RUN set -eux; \
  mkdir -p /opt/openclaw/safe-bin; \
  for cmd in pwd ls whoami id uname date ps df du stat find cat head tail wc git mcporter sh-safe bash-safe web-safe; do \
    cp /opt/openclaw/safe-bin/openclaw-safe-exec "/opt/openclaw/safe-bin/${cmd}"; \
    chmod 755 "/opt/openclaw/safe-bin/${cmd}"; \
  done

RUN set -eux; \
  mkdir -p /opt/openclaw/default-extensions; \
  install_default_plugin() { \
    plugin_spec="$1"; \
    plugin_id="$2"; \
    work_root="$(mktemp -d)"; \
    home_dir="${work_root}/home"; \
    state_dir="${work_root}/state"; \
    pack_dir="${work_root}/packs"; \
    src_dir="${state_dir}/extensions/${plugin_id}"; \
    dst_dir="/opt/openclaw/default-extensions/${plugin_id}"; \
    mkdir -p "${home_dir}" "${state_dir}" "${pack_dir}" "/opt/openclaw/default-extensions"; \
    plugin_archive="$(cd "${pack_dir}" && npm pack --silent "${plugin_spec}")"; \
    plugin_archive="${pack_dir}/${plugin_archive}"; \
    test -f "${plugin_archive}"; \
    HOME="${home_dir}" OPENCLAW_STATE_DIR="${state_dir}" openclaw plugins install "${plugin_archive}"; \
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

RUN set -eux; \
  if [ "${OPENCLAW_INSTALL_AGENT_BROWSER}" = "true" ] && command -v npm >/dev/null 2>&1; then \
    npm install -g "${OPENCLAW_AGENT_BROWSER_SPEC}"; \
    npm cache clean --force || true; \
  fi

RUN set -eux; \
  mkdir -p /home/node; \
  if [ "${OPENCLAW_INSTALL_SKILLHUB}" = "true" ]; then \
    tmp_dir="$(mktemp -d)"; \
    curl -fsSL "${OPENCLAW_SKILLHUB_KIT_URL}" -o "${tmp_dir}/skillhub.tar.gz"; \
    echo "${OPENCLAW_SKILLHUB_KIT_SHA256}  ${tmp_dir}/skillhub.tar.gz" | sha256sum -c -; \
    tar -xzf "${tmp_dir}/skillhub.tar.gz" -C "${tmp_dir}"; \
    HOME=/home/node bash "${tmp_dir}/cli/install.sh" --cli-only; \
    rm -rf "${tmp_dir}"; \
    chown -R node:node /home/node/.skillhub /home/node/.local; \
  fi

RUN set -eux; \
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
