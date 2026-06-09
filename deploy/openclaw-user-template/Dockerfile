## syntax=docker/dockerfile:1.7

# -----------------------------------------------------------------------------
# OpenClaw 用户自定义镜像模板（轻量 bootstrap 版）
# -----------------------------------------------------------------------------
# 设计目标：
# 1. 使用用户模板专用轻量 bootstrap，不复刻平台版复杂启动链路。
# 2. 兼容我们的运行时约束：只持久化 /home/node/.openclaw，默认网关端口 8080。
# 3. 用户创建实例时额外传入的自定义环境变量，容器内可以直接读取。
# 4. 默认仍以 trusted-proxy 为主，但显式允许 token 模式。
# 5. 预置中国大陆常用镜像源，方便用户后续在容器内按需安装依赖。
#
# 重要说明：
# - custom/config/openclaw.json 首次初始化时支持 ${VAR} 占位符渲染。
# - 自定义环境变量不会自动映射进任意 openclaw.json 字段。
# - 如果你的插件 / skill / 业务代码要使用 env，请在代码里直接读取 process.env。
# - 如果你希望做复杂的平台增强配置，请使用平台版 OpenClaw runtime 镜像。
# -----------------------------------------------------------------------------

ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw:2026.6.1-slim@sha256:a83ee8716ab191534952299fe989374d75593aa9c7632c4e756e9d64b0ce8061
FROM ${OPENCLAW_BASE_IMAGE}

USER root

# -----------------------------------------------------------------------------
# 一、写入镜像内的“模板基线”
# -----------------------------------------------------------------------------
# 因为运行时真正会持久化的只有 /home/node/.openclaw，所以不能把默认内容只放在
# 持久化目录里；否则卷挂载后会被覆盖。这里把用户内容先放到只读模板区，再在
# 启动时按“缺什么补什么”的方式初始化到 state dir。
COPY custom/extensions /opt/openclaw-template/extensions
COPY custom/skills /opt/openclaw-template/skills
COPY custom/config/openclaw.json /opt/openclaw-template/config/openclaw.json
COPY --chmod=755 openclaw-user-bootstrap.sh /usr/local/bin/openclaw-user-bootstrap.sh

RUN set -eux; \
  mkdir -p /opt/openclaw-template/extensions /opt/openclaw-template/skills /opt/openclaw-template/config /home/node/.openclaw; \
  chown -R node:node /opt/openclaw-template /home/node/.openclaw /usr/local/bin/openclaw-user-bootstrap.sh

# -----------------------------------------------------------------------------
# 二、默认运行时环境
# -----------------------------------------------------------------------------
# 这里显式兼容 8080 和 trusted-proxy，同时预置中国大陆常用源。
# 其中也包含 ClawHub 中国镜像源，方便用户后续直接使用 clawhub CLI 或基于它的能力市场安装链路。
# 用户创建实例时新增的自定义 env 不会被覆盖，仍然会出现在容器环境里。
ENV HOME=/home/node \
  OPENCLAW_STATE_DIR=/home/node/.openclaw \
  OPENCLAW_GATEWAY_BIND=lan \
  OPENCLAW_GATEWAY_AUTH_MODE=trusted-proxy \
  OPENCLAW_TRUSTED_PROXY_USER_HEADER=x-forwarded-user \
  OPENCLAW_INTERNAL_TRUSTED_PROXY_USER=openclaw-backend \
  OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER=x-forwarded-user \
  OPENCLAW_TRUSTED_PROXIES=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,35.0.0.0/8 \
  OPENCLAW_GATEWAY_PORT=8080 \
  PORT=8080 \
  CLAWHUB_SITE=https://cn.clawhub-mirror.com \
  CLAWHUB_REGISTRY=https://cn.clawhub-mirror.com \
  NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
  npm_config_registry=https://registry.npmmirror.com \
  YARN_NPM_REGISTRY_SERVER=https://registry.npmmirror.com \
  PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  PIP_TRUSTED_HOST=mirrors.aliyun.com \
  UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright \
  PUPPETEER_DOWNLOAD_BASE_URL=https://npmmirror.com/mirrors/chromium-browser-snapshots \
  PATH=/home/node/.local/bin:${PATH}

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=5 \
  CMD node -e "const net=require('net'); const port=Number(process.env.OPENCLAW_GATEWAY_PORT||process.env.PORT||8080); const socket=net.connect({host:'127.0.0.1', port},()=>{socket.end(); process.exit(0);}); socket.on('error',()=>process.exit(1)); setTimeout(()=>process.exit(1), 3000);"

USER node

# -----------------------------------------------------------------------------
# 三、轻量启动逻辑
# -----------------------------------------------------------------------------
# 这里只做五件事：
# 1. 首次启动时，把镜像内模板内容补到 /home/node/.openclaw
# 2. 仅首次渲染 openclaw.json 中的 ${VAR} 占位符
# 3. 按需合并 OPENCLAW_CONFIG_PATCH_JSON
# 4. 校验鉴权模式只能是 trusted-proxy|token|none，并在 token 模式下写入 shared secret
# 5. 直接启动 OpenClaw Gateway
ENTRYPOINT []
CMD ["/usr/local/bin/openclaw-user-bootstrap.sh"]
