OPENCLAW_BASE_IMAGE ?= ghcr.io/openclaw/openclaw:2026.4.11@sha256:436f2608cb503d80a7c5ffa7edb04d6f653e3122c231890547889ad5653ea069
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
