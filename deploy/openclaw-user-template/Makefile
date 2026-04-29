# OpenClaw 用户镜像模板 Makefile
# 用法示例：
#   make build IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-ns/openclaw-user-custom TAG=demo
#   make run IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-ns/openclaw-user-custom TAG=demo
#   make push IMAGE=hub-vpc-cn-beijing-6.kce.ksyun.com/your-ns/openclaw-user-custom TAG=demo
#
# 注意：Makefile 会通过 --build-arg OPENCLAW_BASE_IMAGE 覆盖 Dockerfile 里的 ARG 默认值。
# 如果你改了 Dockerfile 里的基础镜像，也要同步改这里，或者构建时显式传 OPENCLAW_BASE_IMAGE=...

# 基础镜像。默认固定 digest，避免用户构建时拿到漂移版本。
OPENCLAW_BASE_IMAGE ?= ghcr.io/openclaw/openclaw:2026.4.26@sha256:04e27383656941e59fba80a5a9c28b709f240ea980bd2cb375e4a7786d5a7a20

# 目标镜像地址和 tag。用户通常只需要覆盖 IMAGE/TAG。
IMAGE ?= hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/openclaw-user-custom
TAG ?= demo

# 默认按平台线上架构构建 linux/amd64。Apple Silicon 本地构建会看到平台 warning，属于预期。
PLATFORM ?= linux/amd64

# 本地运行参数。平台部署不使用这些变量。
HOST_PORT ?= 8080
CONTAINER_PORT ?= 8080
AUTH_MODE ?= none
GATEWAY_TOKEN ?= gateway-token-demo
STATE_VOLUME ?= openclaw-user-template-state
ENV_FILE ?=
RUN_ARGS ?=

ENV_FILE_ARG := $(if $(ENV_FILE),--env-file $(ENV_FILE),)
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
	@echo "  make build      构建并加载本地镜像"
	@echo "  make run        本地运行，默认 AUTH_MODE=none"
	@echo "  make run-token  本地运行 token 模式，可传 GATEWAY_TOKEN=..."
	@echo "  make run-debug  本地运行并挂载 Docker volume，便于检查 /home/node/.openclaw"
	@echo "  make push       构建并推送镜像"
	@echo "常用变量：IMAGE TAG OPENCLAW_BASE_IMAGE PLATFORM HOST_PORT AUTH_MODE ENV_FILE RUN_ARGS"
