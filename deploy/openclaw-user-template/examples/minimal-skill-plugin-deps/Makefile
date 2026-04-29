# OpenClaw 自定义 plugin + skill 示例 Makefile
# 这个示例会在构建阶段安装 custom/extensions/*/package.json 里的 npm 依赖。

# 基础镜像。需要和 Dockerfile 的 ARG 保持一致，或构建时显式传入。
OPENCLAW_BASE_IMAGE ?= ghcr.io/openclaw/openclaw:2026.4.26@sha256:04e27383656941e59fba80a5a9c28b709f240ea980bd2cb375e4a7786d5a7a20

# npm 依赖安装源。国内环境默认使用 npmmirror。
OPENCLAW_NPM_REGISTRY ?= https://registry.npmmirror.com

# 目标镜像地址和 tag。
IMAGE ?= hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/openclaw-user-custom-example
TAG ?= demo

# 默认按平台线上架构构建 linux/amd64。
PLATFORM ?= linux/amd64

# 本地运行参数。
HOST_PORT ?= 8080
CONTAINER_PORT ?= 8080
AUTH_MODE ?= none
GATEWAY_TOKEN ?= gateway-token-demo
STATE_VOLUME ?= openclaw-plugin-example-state
ENV_FILE ?=
RUN_ARGS ?=

ENV_FILE_ARG := $(if $(ENV_FILE),--env-file $(ENV_FILE),)
COMMON_RUN_ARGS := --rm -it -p $(HOST_PORT):$(CONTAINER_PORT) $(ENV_FILE_ARG) $(RUN_ARGS)

.PHONY: help build run run-none run-token run-debug push print-image

build:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		--build-arg OPENCLAW_NPM_REGISTRY=$(OPENCLAW_NPM_REGISTRY) \
		-t $(IMAGE):$(TAG) \
		--load \
		.

run:
	docker run $(COMMON_RUN_ARGS) \
		-e OPENCLAW_GATEWAY_AUTH_MODE=$(AUTH_MODE) \
		$(IMAGE):$(TAG)

run-none:
	$(MAKE) run AUTH_MODE=none

run-token:
	docker run $(COMMON_RUN_ARGS) \
		-e OPENCLAW_GATEWAY_AUTH_MODE=token \
		-e OPENCLAW_GATEWAY_TOKEN=$(GATEWAY_TOKEN) \
		$(IMAGE):$(TAG)

run-debug:
	docker volume create $(STATE_VOLUME)
	docker run $(COMMON_RUN_ARGS) \
		--name openclaw-debug \
		-v $(STATE_VOLUME):/home/node/.openclaw \
		-e OPENCLAW_GATEWAY_AUTH_MODE=$(AUTH_MODE) \
		-e APP_MODE=debug \
		$(IMAGE):$(TAG)

push:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		--build-arg OPENCLAW_NPM_REGISTRY=$(OPENCLAW_NPM_REGISTRY) \
		-t $(IMAGE):$(TAG) \
		--push \
		.

print-image:
	@echo "$(IMAGE):$(TAG)"

help:
	@echo "常用命令："
	@echo "  make build      构建示例镜像并安装 plugin npm 依赖"
	@echo "  make run        本地运行，默认 AUTH_MODE=none"
	@echo "  make run-token  本地运行 token 模式，可传 GATEWAY_TOKEN=..."
	@echo "  make run-debug  本地运行并挂载 Docker volume，同时注入 APP_MODE=debug"
	@echo "  make push       构建并推送镜像"
	@echo "常用变量：IMAGE TAG OPENCLAW_BASE_IMAGE OPENCLAW_NPM_REGISTRY PLATFORM HOST_PORT ENV_FILE RUN_ARGS"
