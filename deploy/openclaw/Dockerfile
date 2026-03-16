ARG OPENCLAW_BASE_IMAGE=alpine/openclaw:latest
FROM ${OPENCLAW_BASE_IMAGE}

# ── 可通过 --build-arg 覆盖的参数 ──────────────────────────────
ARG AGENT_REACH_ZIP_URL=https://github.com/Panniantong/agent-reach/archive/main.zip
ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG TZ=Asia/Shanghai

USER root
ENV TZ=${TZ}

# ── 单层 RUN: 换源 → 系统包 → 时区 → pip → npm → 监控脚本 → 清理 ──
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
  apk add --no-cache tzdata chromium curl github-cli; \
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
  tzdata chromium ca-certificates curl gh; \
  rm -rf /var/lib/apt/lists/*; \
  fi; \
  # ====== 2. 时区设为北京 ====== \
  cp /usr/share/zoneinfo/${TZ} /etc/localtime; \
  echo "${TZ}" > /etc/timezone; \
  # ====== 3. pip (若未预装) + cryptography ====== \
  if ! command -v pip3 >/dev/null 2>&1; then \
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py; \
  python3 /tmp/get-pip.py --break-system-packages \
  -i "${PYPI_INDEX_URL}"; \
  rm -f /tmp/get-pip.py; \
  fi; \
  python3 -m pip install --no-cache-dir --break-system-packages \
  --index-url "${PYPI_INDEX_URL}" \
  --trusted-host "$(echo ${PYPI_INDEX_URL} | awk -F/ '{print $3}')" \
  "${AGENT_REACH_ZIP_URL}" \
  "browser-cookie3>=0.19" \
  "cryptography>=42.0.0" \
  "yt-dlp"; \
  python3 -m pip cache purge || true; \
  # ====== 4. npm 换淘宝镜像 ====== \
  if command -v npm >/dev/null 2>&1; then \
  npm config set registry "${NPM_REGISTRY}"; \
  npm install -g mcporter xreach-cli undici; \
  npm cache clean --force || true; \
  fi; \
  # ====== 5. 清理缓存，最小化镜像体积 ====== \
  rm -rf /tmp/* /var/tmp/* /root/.cache /root/.npm 2>/dev/null || true

COPY --chmod=755 bootstrap.sh /usr/local/bin/bootstrap.sh
COPY --chmod=755 safe-bin/openclaw-safe-exec /opt/openclaw/safe-bin/openclaw-safe-exec
COPY workspace-template /opt/openclaw/workspace-template
COPY preset-skills /opt/openclaw/preset-skills

RUN set -eux; \
  mkdir -p /opt/openclaw/safe-bin; \
  for cmd in pwd ls whoami id uname date ps df du stat find cat head tail wc git mcporter sh-safe bash-safe web-safe; do \
    cp /opt/openclaw/safe-bin/openclaw-safe-exec "/opt/openclaw/safe-bin/${cmd}"; \
    chmod 755 "/opt/openclaw/safe-bin/${cmd}"; \
  done

ENV HOME=/home/node \
    OPENCLAW_STATE_DIR=/home/node/.openclaw
USER node

ENTRYPOINT ["/usr/local/bin/bootstrap.sh"]
