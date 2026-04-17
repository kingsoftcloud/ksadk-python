OPENCLAW_BASE_IMAGE ?= ghcr.io/openclaw/openclaw:2026.4.15@sha256:0e6bebecf4623216420851f5edd133a748335f45c3508b635f7c5c4bfbc6da7d
OPENCLAW_NPM_REGISTRY ?= https://registry.npmmirror.com
IMAGE ?= hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/openclaw-user-custom-example
TAG ?= demo
PLATFORM ?= linux/amd64

.PHONY: build run push

build:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		--build-arg OPENCLAW_NPM_REGISTRY=$(OPENCLAW_NPM_REGISTRY) \
		-t $(IMAGE):$(TAG) \
		--load \
		.

run:
	docker run --rm -it -p 8080:8080 $(IMAGE):$(TAG)

push:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		--build-arg OPENCLAW_NPM_REGISTRY=$(OPENCLAW_NPM_REGISTRY) \
		-t $(IMAGE):$(TAG) \
		--push \
		.
