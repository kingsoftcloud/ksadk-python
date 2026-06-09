## syntax=docker/dockerfile:1.7

# -----------------------------------------------------------------------------
# OpenClaw 用户自定义镜像模板示例
# 主题：直接复用基础镜像内置的飞书 plugin + skills
# -----------------------------------------------------------------------------
# 适用场景：
# 1. 用户只是想基于我们已经打好的公共 OpenClaw 镜像，再封一层自己的分发镜像。
# 2. 不想自己维护 custom/extensions、custom/skills 或独立 bootstrap。
# 3. 目标只是直接复用基础镜像里已经内置的飞书 channel plugin 与随包 skills。
#
# 关键点：
# - 基础镜像里已经带 openclaw-lark（Feishu/Lark channel plugin）。
# - 相关飞书 skills 也已经跟着 plugin 一起打进镜像。
# - 这里不覆写基础镜像的启动入口，继续走官方 bootstrap.sh 启动链路。
# - 只额外补平台常用默认值，方便直接运行和继续分发。
# -----------------------------------------------------------------------------

ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw:2026.6.1-slim@sha256:a83ee8716ab191534952299fe989374d75593aa9c7632c4e756e9d64b0ce8061
FROM ${OPENCLAW_BASE_IMAGE}

ENV OPENCLAW_GATEWAY_PORT=8080 \
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
    PUPPETEER_DOWNLOAD_BASE_URL=https://npmmirror.com/mirrors/chromium-browser-snapshots
