## syntax=docker/dockerfile:1.7

# -----------------------------------------------------------------------------
# OpenClaw 用户自定义镜像模板示例
# 主题：skill + plugin + 自定义依赖（极简版）
# -----------------------------------------------------------------------------
# 这份示例在主模板基础上只多做一件事：
# - 对 custom/extensions 下带 package.json 的插件目录执行 npm install
#
# 适用场景：
# - 你要内置一个最小 OpenClaw tool plugin
# - 这个 plugin 依赖少量 npm 包
# - 你还希望同时带一个配套 skill，直接调用该 plugin 提供的 tool
# -----------------------------------------------------------------------------

ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw:2026.4.15@sha256:0e6bebecf4623216420851f5edd133a748335f45c3508b635f7c5c4bfbc6da7d
ARG OPENCLAW_NPM_REGISTRY=https://registry.npmmirror.com

FROM ${OPENCLAW_BASE_IMAGE}

ARG OPENCLAW_NPM_REGISTRY

USER root

# -----------------------------------------------------------------------------
# 一、写入镜像内模板内容
# -----------------------------------------------------------------------------
COPY custom/extensions /opt/openclaw-template/extensions
COPY custom/skills /opt/openclaw-template/skills
COPY custom/config/openclaw.json /opt/openclaw-template/config/openclaw.json

# -----------------------------------------------------------------------------
# 二、安装用户 plugin 的 npm 依赖
# -----------------------------------------------------------------------------
# 这里只扫描 custom/extensions 下自带 package.json 的目录。
# 如果用户没有自定义依赖，这一段也不会报错。
RUN set -eux; \
  find /opt/openclaw-template/extensions -mindepth 1 -maxdepth 1 -type d | while read -r dir; do \
    if [ -f "$dir/package.json" ]; then \
      cd "$dir"; \
      npm install --omit=dev --no-audit --no-fund --registry="${OPENCLAW_NPM_REGISTRY}"; \
    fi; \
  done; \
  mkdir -p /opt/openclaw-template/extensions /opt/openclaw-template/skills /opt/openclaw-template/config /home/node/.openclaw; \
  chown -R node:node /opt/openclaw-template /home/node/.openclaw

# -----------------------------------------------------------------------------
# 三、默认运行时环境
# -----------------------------------------------------------------------------
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
# 四、最小启动逻辑
# -----------------------------------------------------------------------------
CMD ["sh","-lc","set -eu; STATE_DIR=\"${OPENCLAW_STATE_DIR:-/home/node/.openclaw}\"; TEMPLATE_DIR=\"/opt/openclaw-template\"; mkdir -p \"$STATE_DIR\" \"$STATE_DIR/extensions\" \"$STATE_DIR/skills\"; chmod 700 \"$STATE_DIR\" 2>/dev/null || true; if [ -f \"$STATE_DIR/secrets.json\" ]; then chmod 600 \"$STATE_DIR/secrets.json\" 2>/dev/null || true; fi; if [ ! -f \"$STATE_DIR/openclaw.json\" ] && [ -f \"$TEMPLATE_DIR/config/openclaw.json\" ]; then cp \"$TEMPLATE_DIR/config/openclaw.json\" \"$STATE_DIR/openclaw.json\"; fi; if [ -d \"$TEMPLATE_DIR/extensions\" ]; then for src in \"$TEMPLATE_DIR\"/extensions/*; do [ -e \"$src\" ] || continue; name=\"$(basename \"$src\")\"; dst=\"$STATE_DIR/extensions/$name\"; [ -e \"$dst\" ] || cp -R \"$src\" \"$dst\"; done; fi; if [ -d \"$TEMPLATE_DIR/skills\" ]; then for src in \"$TEMPLATE_DIR\"/skills/*; do [ -e \"$src\" ] || continue; name=\"$(basename \"$src\")\"; dst=\"$STATE_DIR/skills/$name\"; [ -e \"$dst\" ] || cp -R \"$src\" \"$dst\"; done; fi; auth_mode=\"$(printf '%s' \"${OPENCLAW_GATEWAY_AUTH_MODE:-trusted-proxy}\" | tr '[:upper:]' '[:lower:]')\"; case \"$auth_mode\" in trusted-proxy|none) ;; *) echo >&2 'OPENCLAW_GATEWAY_AUTH_MODE 仅支持 trusted-proxy|none；token 模式会导致网关无法通过平台接入'; exit 1 ;; esac; bind_mode=\"${OPENCLAW_GATEWAY_BIND:-lan}\"; if [ \"$auth_mode\" = \"none\" ] && [ \"$bind_mode\" = \"lan\" ]; then bind_mode=\"loopback\"; fi; exec node openclaw.mjs gateway run --allow-unconfigured --bind \"$bind_mode\" --port \"${OPENCLAW_GATEWAY_PORT:-${PORT:-8080}}\" --auth \"$auth_mode\""]
