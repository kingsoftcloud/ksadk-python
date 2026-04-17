# AgentEngine Makefile
# 用于构建 Web UI 和管理项目

.PHONY: help install build-webui sync-static clean clean-dist dev test publish publish-test openclaw-refresh-agentspace-assets openclaw-build openclaw-push openclaw-size hermes-build hermes-push hermes-size

# 默认目标
help:
	@echo ""
	@echo "  \033[1;36m金山云 AgentEngine\033[0m 开发工具"
	@echo ""
	@echo "  \033[1;32m开发命令:\033[0m"
	@echo "    make install        安装所有依赖 (Python + Node.js)"
	@echo "    make dev            启动开发服务器 (前后端)"
	@echo "    make test           运行测试"
	@echo ""
	@echo "  \033[1;32mWeb UI 构建:\033[0m"
	@echo "    make build-webui    构建 Web UI (Angular)"
	@echo "    make sync-static    同步构建产物到 ksadk/server/static"
	@echo "    make webui          构建 + 同步 (一键完成)"
	@echo ""
	@echo "  \033[1;32m版本管理:\033[0m"
	@echo "    make version         显示当前版本"
	@echo "    make set-version V=x.x.x  设置版本号"
	@echo "    make bump-patch      递增 patch 版本 (0.1.0 -> 0.1.1)"
	@echo "    make bump-minor      递增 minor 版本 (0.1.0 -> 0.2.0)"
	@echo "    make bump-major      递增 major 版本 (0.1.0 -> 1.0.0)"
	@echo ""
	@echo "  \033[1;32m发布:\033[0m"
	@echo "    make build           构建 Python 包"
	@echo "    make release V=x.x.x 指定版本构建"
	@echo "    make publish         发布到 PyPI"
	@echo ""
	@echo "  \033[1;32m离线打包:\033[0m"
	@echo "    make offline-current     当前平台离线包"
	@echo "    make offline-linux       Linux x86_64 离线包"
	@echo "    make offline-macos-intel macOS Intel 离线包"
	@echo "    make offline-macos-arm   macOS Apple Silicon 离线包"
	@echo "    make offline-windows     Windows x64 离线包"
	@echo "    make offline-all         打包所有平台"
	@echo ""
	@echo "  \033[1;32mOpenClaw 镜像:\033[0m"
	@echo "    make openclaw-build         构建 OpenClaw 镜像 (默认国内源)"
	@echo "    make openclaw-push          构建 + 推送到 KCR (默认 :latest)"
	@echo "    make openclaw-refresh-agentspace-assets  刷新 Agentspace 最新插件/技能并写 lock"
	@echo "    make openclaw-push OPENCLAW_TAG=v2026.3.13-guardian1"
	@echo "    make openclaw-build OPENCLAW_PYPI_INDEX_URL=https://pypi.org/simple  # 海外源"
	@echo "    make openclaw-size          查看镜像大小"
	@echo ""
	@echo "  \033[1;32mHermes 镜像:\033[0m"
	@echo "    make hermes-build           构建 Hermes runtime 镜像"
	@echo "    make hermes-push            构建 + 推送 Hermes runtime 镜像"
	@echo "    make hermes-size            查看 Hermes 镜像大小"
	@echo "    make hermes-build HERMES_TAG=2026.4.16"
	@echo "    make hermes-build HERMES_AGENT_REF=v2026.4.16  # 切换 Hermes 上游 release"
	@echo ""
	@echo "  \033[1;32m清理:\033[0m"
	@echo "    make clean          清理构建产物"
	@echo ""

# ============================================================
# 依赖安装
# ============================================================

install: install-python install-webui

install-python:
	@echo "📦 安装 Python 依赖..."
	pip install -e ".[dev]"

install-webui:
	@echo "📦 安装 Web UI 依赖..."
	cd webui && npm install

# ============================================================
# Web UI 构建
# ============================================================

# Web UI 输出目录 (支持软链接，webui 目录不存在时跳过)
WEBUI_DIR := $(shell cd webui 2>/dev/null && pwd || echo "")
WEBUI_DIST = $(WEBUI_DIR)/dist/agent_framework_web/browser
STATIC_DIR = ksadk/server/static

build-webui:
	@echo "🔨 构建 Web UI..."
	cd webui && npm run build
	@echo "✅ Web UI 构建完成: $(WEBUI_DIST)"

sync-static:
	@echo "📋 同步静态文件到 $(STATIC_DIR)..."
	@if [ ! -d "$(WEBUI_DIST)" ]; then \
		echo "❌ 错误: $(WEBUI_DIST) 不存在，请先运行 make build-webui"; \
		exit 1; \
	fi
	@rm -rf $(STATIC_DIR)/*
	@cp -r $(WEBUI_DIST)/* $(STATIC_DIR)/
	@echo "✅ 同步完成！"
	@echo "📁 文件列表:"
	@ls -la $(STATIC_DIR)/

# 一键构建 + 同步
webui: build-webui sync-static
	@echo ""
	@echo "🎉 Web UI 构建并同步完成！"

# ============================================================
# 开发服务器
# ============================================================

dev:
	@echo "🚀 启动开发服务器..."
	@echo "   后端: http://localhost:8000"
	@echo "   前端: http://localhost:4200"
	@echo ""
	@echo "使用 Ctrl+C 停止服务"
	@# 并行启动前后端
	(cd webui && npm run serve) & \
	(python -m ksadk.cli web .) & \
	wait

dev-webui:
	@echo "🌐 启动 Web UI 开发服务器..."
	cd webui && npm run serve

dev-backend:
	@echo "🔧 启动后端开发服务器..."
	python -m ksadk.cli web .

# ============================================================
# 测试
# ============================================================

test:
	@echo "🧪 运行 Python 测试..."
	pytest tests/ -v

test-webui:
	@echo "🧪 运行 Web UI 测试..."
	cd webui && npm test

# ============================================================
# 构建和发布
# ============================================================

# 获取当前版本
VERSION := $(shell python -c "from ksadk.version import VERSION; print(VERSION)" 2>/dev/null || echo "0.0.0")

# 版本管理
version:
	@echo "📌 当前版本: $(VERSION)"

# 设置版本号: make set-version V=0.2.0
set-version:
ifndef V
	$(error ❌ 请指定版本号，例如: make set-version V=0.2.0)
endif
	@echo "📝 设置版本为: $(V)"
	@sed -i '' 's/VERSION = ".*"/VERSION = "$(V)"/' ksadk/version.py
	@sed -i '' 's/^version = ".*"/version = "$(V)"/' pyproject.toml
	@echo "✅ 版本已更新到 $(V)"
	@echo "   - ksadk/version.py"
	@echo "   - pyproject.toml"

# 版本号递增
bump-patch:
	@echo "📝 递增 patch 版本..."
	@python -c "\
import re; \
v = '$(VERSION)'.split('.'); \
v[2] = str(int(v[2]) + 1); \
new_v = '.'.join(v); \
print(f'$(VERSION) -> {new_v}'); \
open('ksadk/version.py', 'w').write(f'\"\"\"KsADK 版本信息\"\"\"\\n\\nVERSION = \"{new_v}\"\\n__version__ = VERSION\\n'); \
import subprocess; \
subprocess.run(['sed', '-i', '', f's/^version = \".*\"/version = \"{new_v}\"/', 'pyproject.toml'])"
	@echo "✅ 版本已更新"

bump-minor:
	@echo "📝 递增 minor 版本..."
	@python -c "\
import re; \
v = '$(VERSION)'.split('.'); \
v[1] = str(int(v[1]) + 1); \
v[2] = '0'; \
new_v = '.'.join(v); \
print(f'$(VERSION) -> {new_v}'); \
open('ksadk/version.py', 'w').write(f'\"\"\"KsADK 版本信息\"\"\"\\n\\nVERSION = \"{new_v}\"\\n__version__ = VERSION\\n'); \
import subprocess; \
subprocess.run(['sed', '-i', '', f's/^version = \".*\"/version = \"{new_v}\"/', 'pyproject.toml'])"
	@echo "✅ 版本已更新"

bump-major:
	@echo "📝 递增 major 版本..."
	@python -c "\
import re; \
v = '$(VERSION)'.split('.'); \
v[0] = str(int(v[0]) + 1); \
v[1] = '0'; \
v[2] = '0'; \
new_v = '.'.join(v); \
print(f'$(VERSION) -> {new_v}'); \
open('ksadk/version.py', 'w').write(f'\"\"\"KsADK 版本信息\"\"\"\\n\\nVERSION = \"{new_v}\"\\n__version__ = VERSION\\n'); \
import subprocess; \
subprocess.run(['sed', '-i', '', f's/^version = \".*\"/version = \"{new_v}\"/', 'pyproject.toml'])"
	@echo "✅ 版本已更新"

# 确保构建工具已安装
check-build-deps:
	@python -c "import build" 2>/dev/null || (echo "📦 安装构建依赖..." && pip install build twine)

build: check-build-deps webui
	@echo "📦 构建 Python 包 v$(VERSION)..."
	python -m build
	@# 删除 tar.gz 和临时目录，只保留 whl
	@rm -f dist/*.tar.gz
	@rm -rf build/ *.egg-info/
	@echo "✅ 构建完成: dist/"
	@ls -la dist/

# 仅构建 Python 包（跳过 webui，使用现有静态文件）
build-only: check-build-deps
	@echo "📦 构建 Python 包 v$(VERSION)（使用现有静态文件）..."
	@if [ ! -f "ksadk/server/static/index.html" ]; then \
		echo "❌ 错误: ksadk/server/static/ 目录为空，请先运行 make webui"; \
		exit 1; \
	fi
	python -m build
	@rm -f dist/*.tar.gz
	@rm -rf build/ *.egg-info/
	@echo "✅ 构建完成: dist/"
	@ls -la dist/

# 带版本号构建: make release V=0.2.0
release:
ifndef V
	$(error ❌ 请指定版本号，例如: make release V=0.2.0)
endif
	@$(MAKE) set-version V=$(V)
	@$(MAKE) build
	@echo "🎉 v$(V) 发布包已准备就绪"

# 发布配置文件 (优先使用项目本地的 .pypirc)
PYPIRC := $(shell [ -f .pypirc ] && echo ".pypirc" || echo "~/.pypirc")
DIST_DIR := dist

clean-dist:
	@echo "🧹 清理 dist/build 临时产物..."
	@rm -rf $(DIST_DIR)/* build/ *.egg-info/

publish: clean-dist build-only
	@echo "🚀 发布 v$(VERSION) 到 PyPI..."
	@if [ ! -f ".pypirc" ] && [ ! -f ~/.pypirc ]; then \
		echo "❌ 错误: 找不到 .pypirc 配置文件"; \
		echo "   请在项目根目录创建 .pypirc 文件:"; \
		echo "   [pypi]"; \
		echo "   username = __token__"; \
		echo "   password = pypi-你的token"; \
		exit 1; \
	fi
	@FILES=$$(ls $(DIST_DIR)/ksadk-$(VERSION)-*.whl 2>/dev/null || true); \
	if [ -z "$$FILES" ]; then \
		echo "❌ 错误: 未找到当前版本构建产物 (ksadk-$(VERSION)-*.whl)"; \
		echo "   当前 dist 目录内容:"; \
		ls -la $(DIST_DIR); \
		exit 1; \
	fi; \
	echo "📦 将上传文件:"; \
	echo "$$FILES"; \
	python -m twine upload --config-file $(PYPIRC) $$FILES

publish-test: clean-dist build-only
	@echo "🧪 发布 v$(VERSION) 到 TestPyPI..."
	@if [ ! -f ".pypirc" ] && [ ! -f ~/.pypirc ]; then \
		echo "❌ 错误: 找不到 .pypirc 配置文件"; \
		exit 1; \
	fi
	@FILES=$$(ls $(DIST_DIR)/ksadk-$(VERSION)-*.whl 2>/dev/null || true); \
	if [ -z "$$FILES" ]; then \
		echo "❌ 错误: 未找到当前版本构建产物 (ksadk-$(VERSION)-*.whl)"; \
		echo "   当前 dist 目录内容:"; \
		ls -la $(DIST_DIR); \
		exit 1; \
	fi; \
	echo "📦 将上传文件:"; \
	echo "$$FILES"; \
	python -m twine upload --config-file $(PYPIRC) --repository testpypi $$FILES

# ============================================================
# 离线打包 (多平台支持)
# ============================================================

# 离线包输出目录
OFFLINE_DIR = offline-packages
VERSION := $(shell python -c "from ksadk.version import VERSION; print(VERSION)")

# 平台参数
LINUX_PLATFORM = manylinux2014_x86_64
MACOS_INTEL_PLATFORM = macosx_10_9_x86_64
MACOS_ARM_PLATFORM = macosx_11_0_arm64
WINDOWS_PLATFORM = win_amd64

offline-all: offline-linux offline-macos-intel offline-macos-arm offline-windows
	@echo ""
	@echo "🎉 所有平台离线包已打包完成！"
	@echo "📁 输出目录: $(OFFLINE_DIR)/"
	@ls -la $(OFFLINE_DIR)/

offline-linux: build
	@echo "🐧 打包 Linux (x86_64) 离线包..."
	@mkdir -p $(OFFLINE_DIR)/linux-x86_64
	@cp dist/*.whl $(OFFLINE_DIR)/linux-x86_64/
	pip download -d $(OFFLINE_DIR)/linux-x86_64 \
		--platform $(LINUX_PLATFORM) \
		--python-version 310 \
		--only-binary=:all: \
		-r <(pip freeze --exclude-editable) 2>/dev/null || \
	pip download -d $(OFFLINE_DIR)/linux-x86_64 \
		--platform $(LINUX_PLATFORM) \
		--python-version 310 \
		dist/*.whl
	@echo "✅ Linux 离线包: $(OFFLINE_DIR)/linux-x86_64/"
	@cd $(OFFLINE_DIR) && tar -czf ksadk-$(VERSION)-linux-x86_64.tar.gz linux-x86_64/
	@echo "📦 压缩包: $(OFFLINE_DIR)/ksadk-$(VERSION)-linux-x86_64.tar.gz"

offline-macos-intel: build
	@echo "🍎 打包 macOS (Intel) 离线包..."
	@mkdir -p $(OFFLINE_DIR)/macos-intel
	@cp dist/*.whl $(OFFLINE_DIR)/macos-intel/
	pip download -d $(OFFLINE_DIR)/macos-intel \
		--platform $(MACOS_INTEL_PLATFORM) \
		--python-version 310 \
		--only-binary=:all: \
		dist/*.whl 2>/dev/null || true
	@# 对于纯 Python 包，也下载一份
	pip download -d $(OFFLINE_DIR)/macos-intel \
		--no-deps \
		dist/*.whl 2>/dev/null || true
	@echo "✅ macOS Intel 离线包: $(OFFLINE_DIR)/macos-intel/"
	@cd $(OFFLINE_DIR) && tar -czf ksadk-$(VERSION)-macos-intel.tar.gz macos-intel/
	@echo "📦 压缩包: $(OFFLINE_DIR)/ksadk-$(VERSION)-macos-intel.tar.gz"

offline-macos-arm: build
	@echo "🍎 打包 macOS (Apple Silicon) 离线包..."
	@mkdir -p $(OFFLINE_DIR)/macos-arm64
	@cp dist/*.whl $(OFFLINE_DIR)/macos-arm64/
	pip download -d $(OFFLINE_DIR)/macos-arm64 \
		--platform $(MACOS_ARM_PLATFORM) \
		--python-version 310 \
		--only-binary=:all: \
		dist/*.whl 2>/dev/null || true
	@echo "✅ macOS ARM64 离线包: $(OFFLINE_DIR)/macos-arm64/"
	@cd $(OFFLINE_DIR) && tar -czf ksadk-$(VERSION)-macos-arm64.tar.gz macos-arm64/
	@echo "📦 压缩包: $(OFFLINE_DIR)/ksadk-$(VERSION)-macos-arm64.tar.gz"

offline-windows: build
	@echo "🪟 打包 Windows (x64) 离线包..."
	@mkdir -p $(OFFLINE_DIR)/windows-x64
	@cp dist/*.whl $(OFFLINE_DIR)/windows-x64/
	pip download -d $(OFFLINE_DIR)/windows-x64 \
		--platform $(WINDOWS_PLATFORM) \
		--python-version 310 \
		--only-binary=:all: \
		dist/*.whl 2>/dev/null || true
	@echo "✅ Windows 离线包: $(OFFLINE_DIR)/windows-x64/"
	@cd $(OFFLINE_DIR) && tar -czf ksadk-$(VERSION)-windows-x64.tar.gz windows-x64/
	@echo "📦 压缩包: $(OFFLINE_DIR)/ksadk-$(VERSION)-windows-x64.tar.gz"

# 打包当前平台的完整离线包（包含所有依赖）
offline-current: build
	@echo "📦 打包当前平台离线包..."
	@mkdir -p $(OFFLINE_DIR)/current
	@cp dist/*.whl $(OFFLINE_DIR)/current/
	pip download -d $(OFFLINE_DIR)/current dist/*.whl
	@echo "✅ 当前平台离线包: $(OFFLINE_DIR)/current/"
	@echo ""
	@echo "💡 离线安装方法:"
	@echo "   pip install --no-index --find-links=$(OFFLINE_DIR)/current ksadk"

# ============================================================
# OpenClaw 镜像构建
# ============================================================
#
# 基于 pinned 官方 ghcr.io/openclaw/openclaw，叠加 chromium + 预装 skills
# 构建上下文: deploy/openclaw/ (Dockerfile + bootstrap.sh + preset-skills)
#
# 用法:
#   make openclaw-build    # 构建镜像
#   make openclaw-push     # 构建 + 推送到 KCR
#

# OpenClaw 配置
OPENCLAW_IMAGE := hub.kce.ksyun.com/agentengine-public/openclaw
OPENCLAW_VPC_REGISTRY ?= hub-vpc-cn-beijing-6.kce.ksyun.com
OPENCLAW_VPC_IMAGE ?= $(subst hub.kce.ksyun.com,$(OPENCLAW_VPC_REGISTRY),$(OPENCLAW_IMAGE))
OPENCLAW_TAG ?= latest
OPENCLAW_CONTEXT := deploy/openclaw
OPENCLAW_BASE_IMAGE ?= ghcr.io/openclaw/openclaw:2026.4.15@sha256:0e6bebecf4623216420851f5edd133a748335f45c3508b635f7c5c4bfbc6da7d
OPENCLAW_PYPI_INDEX_URL ?= https://mirrors.aliyun.com/pypi/simple
OPENCLAW_NPM_REGISTRY ?= https://registry.npmmirror.com

## 刷新 Agentspace 最新插件/技能资产并更新 lock manifest
openclaw-refresh-agentspace-assets:
	@echo "🔄 刷新 Agentspace 最新资产..."
	@python3 deploy/openclaw/scripts/refresh_agentspace_assets.py --repo-root .
	@echo "✅ Agentspace 资产已刷新"

## 构建 OpenClaw 镜像 (chromium + preset-skills)
openclaw-build: openclaw-refresh-agentspace-assets
	@echo "🐳 构建 OpenClaw 镜像..."
	@echo "============================================================"
	@echo "   基础镜像: $(OPENCLAW_BASE_IMAGE)"
	@echo "   目标镜像: $(OPENCLAW_IMAGE):$(OPENCLAW_TAG)"
	@echo "   内网地址: $(OPENCLAW_VPC_IMAGE):$(OPENCLAW_TAG)"
	@echo "   PyPI 源:  $(OPENCLAW_PYPI_INDEX_URL)"
	@echo "   NPM 源:   $(OPENCLAW_NPM_REGISTRY)"
	@echo "   构建上下文: $(OPENCLAW_CONTEXT)/"
	@echo "============================================================"
	@if [ ! -f "$(OPENCLAW_CONTEXT)/Dockerfile" ]; then \
		echo "❌ 错误: $(OPENCLAW_CONTEXT)/Dockerfile 不存在"; \
		exit 1; \
	fi
	@DOCKER_BUILDKIT=1 docker build --platform linux/amd64 \
		--build-arg OPENCLAW_BASE_IMAGE=$(OPENCLAW_BASE_IMAGE) \
		--build-arg PYPI_INDEX_URL=$(OPENCLAW_PYPI_INDEX_URL) \
		--build-arg NPM_REGISTRY=$(OPENCLAW_NPM_REGISTRY) \
		-t $(OPENCLAW_IMAGE):$(OPENCLAW_TAG) \
		-t $(OPENCLAW_VPC_IMAGE):$(OPENCLAW_TAG) \
		$(OPENCLAW_CONTEXT)
	@echo "✅ 构建完成: $(OPENCLAW_IMAGE):$(OPENCLAW_TAG)"
	@echo "🔗 对应内网地址: $(OPENCLAW_VPC_IMAGE):$(OPENCLAW_TAG)"

## 推送 OpenClaw 镜像到 KCR (构建 + 推送)
openclaw-push: openclaw-build
	@echo "📤 推送 OpenClaw 镜像: $(OPENCLAW_IMAGE):$(OPENCLAW_TAG)"
	@echo "🔗 对应内网地址: $(OPENCLAW_VPC_IMAGE):$(OPENCLAW_TAG)"
	@docker push $(OPENCLAW_IMAGE):$(OPENCLAW_TAG)
	@docker push $(OPENCLAW_VPC_IMAGE):$(OPENCLAW_TAG)
	@echo "✅ 推送完成"

## 查看 OpenClaw 镜像大小
openclaw-size:
	@docker images $(OPENCLAW_IMAGE):$(OPENCLAW_TAG) --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"


# ============================================================
# Hermes 镜像构建
# ============================================================
#
# 基于 deploy/hermes/ 里的 Dockerfile + wrapper runtime 构建 Hermes runtime
#
# 用法:
#   make hermes-build
#   make hermes-push HERMES_TAG=2026.4.16
#

HERMES_IMAGE := hub.kce.ksyun.com/agentengine-public/hermes-agent
HERMES_VPC_REGISTRY ?= hub-vpc-cn-beijing-6.kce.ksyun.com
HERMES_VPC_IMAGE ?= $(subst hub.kce.ksyun.com,$(HERMES_VPC_REGISTRY),$(HERMES_IMAGE))
HERMES_TAG ?= 2026.4.16
HERMES_CONTEXT := deploy/hermes
HERMES_PYPI_INDEX_URL ?= https://mirrors.aliyun.com/pypi/simple
HERMES_AGENT_REF ?= v2026.4.16
HERMES_APT_MIRROR ?= https://mirrors.aliyun.com/debian
HERMES_NPM_REGISTRY ?= https://registry.npmmirror.com

hermes-build:
	@echo "🐳 构建 Hermes runtime 镜像..."
	@echo "============================================================"
	@echo "   目标镜像: $(HERMES_IMAGE):$(HERMES_TAG)"
	@echo "   内网地址: $(HERMES_VPC_IMAGE):$(HERMES_TAG)"
	@echo "   Hermes Git ref: $(HERMES_AGENT_REF)"
	@echo "   PyPI 源:  $(HERMES_PYPI_INDEX_URL)"
	@echo "   APT 源:   $(HERMES_APT_MIRROR)"
	@echo "   NPM 源:   $(HERMES_NPM_REGISTRY)"
	@echo "   构建上下文: $(HERMES_CONTEXT)/"
	@echo "============================================================"
	@if [ ! -f "$(HERMES_CONTEXT)/Dockerfile" ]; then \
		echo "❌ 错误: $(HERMES_CONTEXT)/Dockerfile 不存在"; \
		exit 1; \
	fi
	@DOCKER_BUILDKIT=1 docker build --platform linux/amd64 \
		--build-arg PYPI_INDEX_URL=$(HERMES_PYPI_INDEX_URL) \
		--build-arg HERMES_AGENT_REF=$(HERMES_AGENT_REF) \
		--build-arg APT_MIRROR=$(HERMES_APT_MIRROR) \
		--build-arg NPM_REGISTRY=$(HERMES_NPM_REGISTRY) \
		-t $(HERMES_IMAGE):$(HERMES_TAG) \
		-t $(HERMES_VPC_IMAGE):$(HERMES_TAG) \
		$(HERMES_CONTEXT)
	@echo "✅ 构建完成: $(HERMES_IMAGE):$(HERMES_TAG)"
	@echo "🔗 对应内网地址: $(HERMES_VPC_IMAGE):$(HERMES_TAG)"

hermes-push: hermes-build
	@echo "📤 推送 Hermes 镜像: $(HERMES_IMAGE):$(HERMES_TAG)"
	@echo "🔗 对应内网地址: $(HERMES_VPC_IMAGE):$(HERMES_TAG)"
	@docker push $(HERMES_IMAGE):$(HERMES_TAG)
	@docker push $(HERMES_VPC_IMAGE):$(HERMES_TAG)
	@echo "✅ 推送完成"

hermes-size:
	@docker images $(HERMES_IMAGE):$(HERMES_TAG) --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"



# ============================================================
# 清理
# ============================================================

clean:
	@echo "🧹 清理构建产物..."
	rm -rf dist/ build/ *.egg-info/
	rm -rf webui/dist/
	rm -rf .pytest_cache/
	rm -rf $(OFFLINE_DIR)/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ 清理完成"

clean-static:
	@echo "🧹 清理静态文件..."
	rm -rf $(STATIC_DIR)/*
	@echo "✅ 清理完成"

clean-offline:
	@echo "🧹 清理离线包..."
	rm -rf $(OFFLINE_DIR)/
	@echo "✅ 清理完成"
