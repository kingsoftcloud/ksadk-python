# OpenClaw 内置飞书 plugin + skills 示例 Makefile
# 这个示例不覆写基础镜像启动入口，只在基础镜像上补默认运行环境。

# 基础镜像。这个示例依赖已内置 openclaw-lark 的 AgentEngine 公共镜像。
OPENCLAW_BASE_IMAGE ?= ghcr.io/openclaw/openclaw:2026.6.1-slim@sha256:a83ee8716ab191534952299fe989374d75593aa9c7632c4e756e9d64b0ce8061

# 目标镜像地址和 tag。
IMAGE ?= hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/openclaw-user-feishu-bundled
TAG ?= demo

# 默认按平台线上架构构建 linux/amd64。
PLATFORM ?= linux/amd64

# 本地运行参数。CHANNEL_BOOTSTRAP_JSON 可用于预配置飞书 channel。
HOST_PORT ?= 8080
CONTAINER_PORT ?= 8080
AUTH_MODE ?= none
GATEWAY_TOKEN ?= gateway-token-demo
CHANNEL_BOOTSTRAP_JSON ?=
STATE_VOLUME ?= openclaw-feishu-example-state
ENV_FILE ?=
RUN_ARGS ?=

ENV_FILE_ARG := $(if $(ENV_FILE),--env-file $(ENV_FILE),)
CHANNEL_ARG := $(if $(CHANNEL_BOOTSTRAP_JSON),-e OPENCLAW_CHANNEL_BOOTSTRAP_JSON='$(CHANNEL_BOOTSTRAP_JSON)',)
COMMON_RUN_ARGS := --rm -it -p $(HOST_PORT):$(CONTAINER_PORT) $(ENV_FILE_ARG) $(RUN_ARGS)

.PHONY: help build run run-none run-token run-debug push print-image

build:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		-t $(IMAGE):$(TAG) \
		--load \
		.

run:
	docker run $(COMMON_RUN_ARGS) \
		-e OPENCLAW_GATEWAY_AUTH_MODE=$(AUTH_MODE) \
		$(CHANNEL_ARG) \
		$(IMAGE):$(TAG)

run-none:
	$(MAKE) run AUTH_MODE=none

run-token:
	docker run $(COMMON_RUN_ARGS) \
		-e OPENCLAW_GATEWAY_AUTH_MODE=token \
		-e OPENCLAW_GATEWAY_TOKEN=$(GATEWAY_TOKEN) \
		$(CHANNEL_ARG) \
		$(IMAGE):$(TAG)

run-debug:
	docker volume create $(STATE_VOLUME)
	docker run $(COMMON_RUN_ARGS) \
		--name openclaw-debug \
		-v $(STATE_VOLUME):/home/node/.openclaw \
		-e OPENCLAW_GATEWAY_AUTH_MODE=$(AUTH_MODE) \
		$(CHANNEL_ARG) \
		$(IMAGE):$(TAG)

push:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		-t $(IMAGE):$(TAG) \
		--push \
		.

print-image:
	@echo "$(IMAGE):$(TAG)"

help:
	@echo "常用命令："
	@echo "  make build      构建飞书示例镜像"
	@echo "  make run        本地运行，默认 AUTH_MODE=none"
	@echo "  make run-token  本地运行 token 模式，可传 GATEWAY_TOKEN=..."
	@echo "  make run-debug  本地运行并挂载 Docker volume，便于检查 /home/node/.openclaw"
	@echo "  make push       构建并推送镜像"
	@echo "常用变量：IMAGE TAG OPENCLAW_BASE_IMAGE PLATFORM HOST_PORT CHANNEL_BOOTSTRAP_JSON ENV_FILE RUN_ARGS"
