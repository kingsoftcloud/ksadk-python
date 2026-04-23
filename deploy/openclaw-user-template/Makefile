OPENCLAW_BASE_IMAGE ?= ghcr.io/openclaw/openclaw:2026.4.21@sha256:70e0ab07deb72f4b3ee7bb701c5437fdc27b85d6705cc67f104aa8042ba52e00
IMAGE ?= hub-vpc-cn-beijing-6.kce.ksyun.com/agentengine-public/openclaw-user-custom
TAG ?= demo
PLATFORM ?= linux/amd64

.PHONY: build run push

build:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		-t $(IMAGE):$(TAG) \
		--load \
		.

run:
	docker run --rm -it -p 8080:8080 $(IMAGE):$(TAG)

push:
	docker buildx build \
		--platform $(PLATFORM) \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		-t $(IMAGE):$(TAG) \
		--push \
		.
