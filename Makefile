# AgentEngine Makefile
# 用于同步 KsADK Web static 和管理项目

.PHONY: help install clean clean-cache clean-dist clean-static clean-offline dev test publish publish-test public-status public-init-worktree public-worktree-status public-sync-check public-secret-audit public-audit open-source-audit-dist public-docs-build public-test public-sync-ksadk-web-static public-build-check public-preflight public-publish-check public-release-tag public-review openclaw-build openclaw-push openclaw-size hermes-build hermes-push hermes-size docs-check-wiki docs-prepare-source docs-docker-build docs-docker-push docs-helm-lint docs-helm-template docs-deploy docs-deploy-all docs-status docs-logs sync-ksadk-web-static sync-hosted-ui build-frontend build-webui sync-static webui build-wheel build-all clean-frontend

# 默认目标
help:
	@echo ""
	@echo "  \033[1;36m金山云 AgentEngine\033[0m 开发工具"
	@echo ""
	@echo "  \033[1;32m开发命令:\033[0m"
	@echo "    make install        安装 Python 依赖"
	@echo "    make dev            启动本地后端和已打包 Web UI"
	@echo "    make test           运行测试"
	@echo ""
	@echo "  \033[1;32mWeb UI 构建:\033[0m"
	@echo "    make sync-ksadk-web-static KSADK_WEB_VERSION=latest"
	@echo "                         从 @kingsoftcloud/ksadk-web npm 包同步 static"
	@echo "    make build-frontend 同步 ksadk-web static"
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
	@echo "  \033[1;32m公开发布门禁:\033[0m"
	@echo "    make public-status        查看公开发布相关状态"
	@echo "    make public-init-worktree 初始化/校验 .worktrees/public-main"
	@echo "    make public-preflight     GitHub/PyPI/Release 前必须通过的本地门禁"
	@echo "    make public-release-tag V=x.y.z  创建公开 release 留痕 tag"
	@echo "    make public-review        公开候选审核入口"
	@echo "    make public-publish-check 发布状态核对"
	@echo ""
	@echo "  \033[1;32m离线打包:\033[0m"
	@echo "    make offline-current     当前平台离线包"
	@echo "    make offline-linux       Linux x86_64 离线包"
	@echo "    make offline-macos-intel macOS Intel 离线包"
	@echo "    make offline-macos-arm   macOS Apple Silicon 离线包"
	@echo "    make offline-windows     Windows x64 离线包"
	@echo "    make offline-all         打包所有平台"
	@echo ""
	@echo "  \033[1;32mAgentEngine 镜像:\033[0m"
	@echo "    Hermes / OpenClaw / Skill Runtime 镜像已迁移到内部 agentengine-images 仓库"
	@echo "    可设置 AGENTENGINE_IMAGES_DIR=../agentengine-images 后继续使用兼容入口"
	@echo ""
	@echo "  \033[1;32mzread 文档站:\033[0m"
	@echo "    make docs-deploy-all   构建原生 zread 文档镜像 + 推送 + 部署到预发"
	@echo "    make docs-status       查看预发文档站状态"
	@echo "    make docs-deploy-all ENV=online DOCS_VERSION=x  # 部署线上"
	@echo ""
	@echo "  \033[1;32m清理:\033[0m"
	@echo "    make clean          清理构建产物和本地测试缓存"
	@echo "    make clean-cache    仅清理 Python/测试/类型检查缓存"
	@echo "    make clean-dist     仅清理 Python 发布构建产物"
	@echo ""

# ============================================================
# 依赖安装
# ============================================================

install: install-python

install-python:
	@echo "📦 安装 Python 依赖..."
	pip install -e ".[dev]"

# ============================================================
# Web UI static 同步
# ============================================================

STATIC_DIR = ksadk/server/static

build-webui sync-static webui: sync-ksadk-web-static
	@echo "Deprecated target: Web UI is sourced from $(KSADK_WEB_PACKAGE), not local source."

# ============================================================
# 开发服务器
# ============================================================

dev:
	@echo "🚀 启动开发服务器..."
	@echo "   后端: http://localhost:8000"
	@echo ""
	@echo "使用 Ctrl+C 停止服务"
	python -m ksadk.cli web .

dev-backend:
	@echo "🔧 启动后端开发服务器..."
	python -m ksadk.cli web .

# ============================================================
# 测试
# ============================================================

test:
	@echo "🧪 运行 Python 测试..."
	pytest tests/ -v

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

build: check-build-deps sync-ksadk-web-static
	@echo "📦 构建 Python 包 v$(VERSION)..."
	python -m build
	@# 删除 tar.gz 和临时目录，只保留 whl
	@rm -f dist/*.tar.gz
	@rm -rf build/ *.egg-info/
	@echo "✅ 构建完成: dist/"
	@ls -la dist/

# 仅构建 Python 包（跳过 npm 同步，使用现有静态文件）
build-only: check-build-deps
	@echo "📦 构建 Python 包 v$(VERSION)（使用现有静态文件）..."
	@if [ ! -f "ksadk/server/static/index.html" ]; then \
		echo "❌ 错误: ksadk/server/static/ 目录为空，请先运行 make sync-ksadk-web-static"; \
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

# 发布配置文件只允许使用用户目录凭证。仓库根目录不得存放 .pypirc。
PYPIRC := $(HOME)/.pypirc
DIST_DIR := dist

clean-dist:
	@echo "🧹 清理 dist/build 临时产物..."
	@rm -rf $(DIST_DIR)/* build/ *.egg-info/

publish: clean-dist build
	@echo "🚀 发布 v$(VERSION) 到 PyPI..."
	@if [ -f ".pypirc" ]; then \
		echo "❌ 错误: 仓库根目录存在 .pypirc，拒绝发布"; \
		echo "   请删除仓库内 .pypirc，只使用 $(PYPIRC) 或 CI Secret。"; \
		exit 1; \
	fi
	@if [ ! -f "$(PYPIRC)" ]; then \
		echo "❌ 错误: 找不到 $(PYPIRC)"; \
		echo "   PyPI 凭证只能放在用户目录或 CI Secret，不能放进仓库。"; \
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

publish-test: clean-dist build
	@echo "🧪 发布 v$(VERSION) 到 TestPyPI..."
	@if [ -f ".pypirc" ]; then \
		echo "❌ 错误: 仓库根目录存在 .pypirc，拒绝发布"; \
		echo "   请删除仓库内 .pypirc，只使用 $(PYPIRC) 或 CI Secret。"; \
		exit 1; \
	fi
	@if [ ! -f "$(PYPIRC)" ]; then \
		echo "❌ 错误: 找不到 $(PYPIRC)"; \
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
# 公开发布门禁
# ============================================================

PUBLIC_WORKTREE ?= .worktrees/public-main
PUBLIC_BRANCH ?= main
PUBLIC_REPO ?= https://github.com/kingsoftcloud/ksadk-python
PUBLIC_DOCS_URL ?= https://kingsoftcloud.github.io/ksadk-python/
PUBLIC_PYPI_PROJECT ?= ksadk
PUBLIC_ALIAS_PYPI_PROJECT ?= agentengine-sdk-python
PUBLIC_RELEASE_TAG ?= v$(V)

public-status:
	@echo "==> internal worktree"
	@git status --short --branch
	@echo ""
	@echo "==> remotes"
	@git remote -v
	@echo ""
	@echo "==> configured public targets"
	@echo "PUBLIC_WORKTREE=$(PUBLIC_WORKTREE)"
	@echo "PUBLIC_BRANCH=$(PUBLIC_BRANCH)"
	@echo "PUBLIC_REPO=$(PUBLIC_REPO)"
	@echo "PUBLIC_DOCS_URL=$(PUBLIC_DOCS_URL)"
	@echo "PUBLIC_PYPI_PROJECT=$(PUBLIC_PYPI_PROJECT)"
	@echo "PUBLIC_ALIAS_PYPI_PROJECT=$(PUBLIC_ALIAS_PYPI_PROJECT)"
	@echo ""
	@echo "==> worktrees"
	@git worktree list

public-init-worktree:
	@if ! git remote get-url github >/dev/null 2>&1; then \
		echo "==> adding github remote: $(PUBLIC_REPO)"; \
		git remote add github $(PUBLIC_REPO); \
	fi
	@git fetch github $(PUBLIC_BRANCH)
	@if [ -d "$(PUBLIC_WORKTREE)" ]; then \
		echo "==> public worktree exists: $(PUBLIC_WORKTREE)"; \
		git -C "$(PUBLIC_WORKTREE)" status --short --branch; \
	else \
		echo "==> creating public worktree: $(PUBLIC_WORKTREE)"; \
		if git show-ref --verify --quiet "refs/heads/$(PUBLIC_BRANCH)"; then \
			git worktree add "$(PUBLIC_WORKTREE)" "$(PUBLIC_BRANCH)"; \
		else \
			git worktree add -b "$(PUBLIC_BRANCH)" "$(PUBLIC_WORKTREE)" github/$(PUBLIC_BRANCH); \
		fi; \
	fi

public-worktree-status:
	@if [ ! -d "$(PUBLIC_WORKTREE)/.git" ] && [ ! -f "$(PUBLIC_WORKTREE)/.git" ]; then \
		echo "❌ 公开工作树不存在: $(PUBLIC_WORKTREE)"; \
		echo "   建议创建: git worktree add $(PUBLIC_WORKTREE) $(PUBLIC_BRANCH)"; \
		exit 1; \
	fi
	@git -C "$(PUBLIC_WORKTREE)" status --short --branch

public-sync-check:
	@echo "==> public sync policy"
	@branch=$$(git branch --show-current); \
	if [ "$$branch" != "master" ]; then \
		echo "❌ 当前分支不是 master: $$branch"; \
		echo "   公开候选应从内部 master 的已审核变更生成。"; \
		exit 1; \
	fi
	@if [ -f ".pypirc" ]; then \
		echo "❌ 仓库根目录存在 .pypirc，必须删除后再进入公开流程"; \
		exit 1; \
	fi
	@echo "✅ sync policy passed"

public-secret-audit:
	@echo "==> secret and sensitive-file audit"
	@if git ls-files | grep -E '(^|/)(\.pypirc|kubeconfig|.*\.kubeconfig|id_rsa|id_ed25519)$$'; then \
		echo "❌ 发现禁止跟踪的敏感文件"; \
		exit 1; \
	fi
	@uv run --extra dev python scripts/open_source_audit.py --target public-repo
	@echo "✅ secret audit passed"

public-audit: public-secret-audit
	@echo "==> public source audit"
	@blocked=$$(git ls-files | grep -E '^(\.pypirc$$|\.zread/(wiki|site)/)' || true); \
	if [ -n "$$blocked" ]; then \
		echo "❌ blocked tracked paths:"; \
		echo "$$blocked"; \
		exit 1; \
	fi
	@echo "✅ public path audit passed"

open-source-audit-dist:
	@echo "==> audit release artifacts"
	@uv run --extra dev python scripts/audit_release_artifacts.py dist

public-docs-build:
	@echo "==> docs build"
	@if [ -f "mkdocs.yml" ]; then \
		uv run mkdocs build --strict; \
	else \
		echo "⚠️  mkdocs.yml 不存在，跳过 docs build"; \
	fi

public-test:
	@echo "==> test"
	@uv sync --extra all
	@uv run --extra dev pytest \
		tests/test_open_source_audit.py \
		tests/test_runtime_common_packaging.py \
		tests/test_public_release_positioning.py \
		tests/test_tracing_setup_otlp.py \
		tests/test_check_publication_state.py \
		tests/test_check_approval_record.py \
		tests/test_markdown_repair.py \
		tests/test_conversation_runtime.py \
		tests/test_server_session_app.py \
		-q

PUBLIC_KSADK_WEB_VERSION ?= 0.2.11

public-sync-ksadk-web-static:
	@$(MAKE) sync-ksadk-web-static KSADK_WEB_VERSION="$(PUBLIC_KSADK_WEB_VERSION)"

public-build-check: clean-dist public-sync-ksadk-web-static
	@echo "==> build and twine check"
	@uv build
	@uv run --extra dev python scripts/audit_release_artifacts.py dist
	@uv run pytest tests/test_runtime_common_packaging.py -q
	@uv run --extra dev python -m twine check dist/*

public-preflight: public-audit public-build-check public-test public-docs-build
	@echo "✅ public preflight passed"

public-publish-check:
	@echo "==> publication state check"
	@if [ -f "scripts/check_publication_state.py" ]; then \
		uv run python scripts/check_publication_state.py --phase pre-publish; \
	else \
		echo "⚠️  scripts/check_publication_state.py 不存在，执行基础 HTTP 检查"; \
		python3 -c 'import json, urllib.request; targets={"repo":"$(PUBLIC_REPO)","docs":"$(PUBLIC_DOCS_URL)","pypi":"https://pypi.org/pypi/$(PUBLIC_PYPI_PROJECT)/json","alias_pypi":"https://pypi.org/pypi/$(PUBLIC_ALIAS_PYPI_PROJECT)/json"}; [print((lambda resp, name: f"{name}: HTTP {resp.status}" + (f"\n  version={json.load(resp)[\"info\"].get(\"version\")}" if name.endswith("pypi") else ""))(urllib.request.urlopen(url, timeout=20), name)) for name, url in targets.items()]'; \
	fi

public-release-tag:
ifndef V
	$(error ❌ 请指定版本号，例如: make public-release-tag V=0.6.2)
endif
	@echo "==> creating public release tag: $(PUBLIC_RELEASE_TAG)"
	@if git rev-parse "$(PUBLIC_RELEASE_TAG)" >/dev/null 2>&1; then \
		echo "❌ tag already exists: $(PUBLIC_RELEASE_TAG)"; \
		exit 1; \
	fi
	@git tag -a "$(PUBLIC_RELEASE_TAG)" -m "Public release $(PUBLIC_RELEASE_TAG)"
	@echo "✅ tag created: $(PUBLIC_RELEASE_TAG)"
	@echo "   push after approval: git push github $(PUBLIC_RELEASE_TAG)"

public-review: public-status public-preflight
	@echo "✅ public review gate passed"

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
# AgentEngine 镜像构建兼容入口
# ============================================================

AGENTENGINE_IMAGES_DIR ?= ../agentengine-images

openclaw-build openclaw-push openclaw-size hermes-build hermes-push hermes-size:
	@if [ ! -d "$(AGENTENGINE_IMAGES_DIR)" ]; then \
		echo "❌ AgentEngine 镜像资产已迁移到内部仓库 agentengine-images。"; \
		echo "   请先克隆仓库，或设置 AGENTENGINE_IMAGES_DIR=/path/to/agentengine-images"; \
		exit 1; \
	fi
	@$(MAKE) -C "$(AGENTENGINE_IMAGES_DIR)" $@


# ============================================================
# zread 文档站发布
# ============================================================
#
# 依赖本地 .zread/wiki/current 指向的完整 wiki 版本。发布镜像会运行
# zread browse 原生 UI，保留 zread 样式、前端交互和 Mermaid 渲染。
#

DOCS_PROJECT_NAME ?= ksadk-docs
DOCS_DOCKER_REGISTRY ?= hub.kce.ksyun.com
DOCS_DOCKER_NAMESPACE ?= bigdata-ai
DOCS_WIKI_VERSION ?= $(shell test -f .zread/wiki/current && sed 's|^versions/||' .zread/wiki/current || echo missing-wiki)
DOCS_VERSION ?= zread-$(DOCS_WIKI_VERSION)
ENV ?= pre
DOCS_FORCE_UPDATE ?= 0
DOCS_FORCE_UPDATE_NONCE ?= $(shell date '+%Y%m%d%H%M%S')

ifeq ($(ENV),online)
	DOCS_KUBECONFIG_PATH := $(HOME)/.kube/agentengine-online
	DOCS_VALUES_FILE := deploy/helm/ksadk-docs/values-online.yaml
else
	DOCS_KUBECONFIG_PATH := $(HOME)/.kube/agentengine-pre
	DOCS_VALUES_FILE := deploy/helm/ksadk-docs/values-pre.yaml
endif

DOCS_IMAGE := $(DOCS_DOCKER_REGISTRY)/$(DOCS_DOCKER_NAMESPACE)/$(DOCS_PROJECT_NAME):$(DOCS_VERSION)
DOCS_NAMESPACE ?= agentengine
DOCS_HELM_RELEASE ?= ksadk-docs
DOCS_HELM_CHART := deploy/helm/ksadk-docs
DOCS_HELM_TIMEOUT ?= 600s
DOCS_BASE_PATH ?= /ksadk-docs
DOCS_BASE_IMAGE ?= hub.kce.ksyun.com/bigdata-ai/agentengine-server-base:v0.4.1
DOCS_ZREAD_VERSION ?= 0.2.12
DOCS_ZREAD_SHA256 ?= faf5ef7f2f8edc24d41b84fd838322882846f4bab10f1a9210de29cba2a53a10
DOCS_HELM_SET_FLAGS := --set image.tag=$(DOCS_VERSION) --set docs.basePath=$(DOCS_BASE_PATH)

ifeq ($(DOCS_FORCE_UPDATE),1)
	DOCS_HELM_SET_FLAGS += --set-string podAnnotations.force-redeploy=$(DOCS_FORCE_UPDATE_NONCE)
endif

docs-check-wiki:
	@if [ ! -f ".zread/wiki/current" ]; then \
		echo "❌ 缺少 .zread/wiki/current，请先运行 zread generate -y --stdio"; \
		exit 1; \
	fi
	@if [ ! -f ".zread/wiki/versions/$(DOCS_WIKI_VERSION)/wiki.json" ]; then \
		echo "❌ 缺少 .zread/wiki/versions/$(DOCS_WIKI_VERSION)/wiki.json"; \
		exit 1; \
	fi
	@python3 -c 'import json; from pathlib import Path; version = Path(".zread/wiki/current").read_text().strip().removeprefix("versions/"); root = Path(".zread/wiki/versions", version); wiki = json.loads((root / "wiki.json").read_text()); pages = wiki.get("pages") or []; assert pages, "wiki.json 中没有页面，拒绝发布"; missing = [p.get("file") for p in pages if not (root / p.get("file", "")).exists()]; print(f"✅ zread wiki: {version}, pages={len(pages)}, missing={len(missing)}"); [print(f"❌ 缺失页面文件: {name}") for name in missing]; raise SystemExit(1 if missing else 0)'
	@if [ -f ".zread/wiki/drafts/wiki.json" ]; then \
		echo "⚠️  检测到 .zread/wiki/drafts/wiki.json，本次仍发布 current 完整版本: $(DOCS_WIKI_VERSION)"; \
	fi

docs-prepare-source: docs-check-wiki
	@python3 scripts/prepare_zread_source_snapshot.py

docs-docker-build: docs-check-wiki docs-prepare-source
	@echo "🐳 构建 KsADK 原生 zread 文档镜像: $(DOCS_IMAGE)"
	@DOCKER_BUILDKIT=1 docker build --pull=false --platform linux/amd64 \
		-f Dockerfile.docs \
		--build-arg DOCS_BASE_IMAGE=$(DOCS_BASE_IMAGE) \
		--build-arg ZREAD_VERSION=$(DOCS_ZREAD_VERSION) \
		--build-arg ZREAD_SHA256=$(DOCS_ZREAD_SHA256) \
		-t $(DOCS_IMAGE) \
		.

docs-docker-push: docs-docker-build
	@echo "📤 推送 KsADK 文档镜像: $(DOCS_IMAGE)"
	@docker push $(DOCS_IMAGE)

docs-helm-lint:
	@echo "==> helm lint $(DOCS_HELM_CHART)"
	@helm lint $(DOCS_HELM_CHART)

docs-helm-template:
	@echo "==> helm template $(DOCS_HELM_RELEASE) ($(ENV))"
	@helm template $(DOCS_HELM_RELEASE) $(DOCS_HELM_CHART) \
		--namespace $(DOCS_NAMESPACE) \
		--values $(DOCS_VALUES_FILE) \
		$(DOCS_HELM_SET_FLAGS)

docs-deploy: docs-helm-lint
	@echo "==> helm upgrade --install $(DOCS_HELM_RELEASE) ($(ENV))"
	@echo "    namespace=$(DOCS_NAMESPACE) image=$(DOCS_IMAGE) timeout=$(DOCS_HELM_TIMEOUT) force_update=$(DOCS_FORCE_UPDATE)"
	@set -e; \
	if helm upgrade --install $(DOCS_HELM_RELEASE) $(DOCS_HELM_CHART) \
		--kubeconfig $(DOCS_KUBECONFIG_PATH) \
		--namespace $(DOCS_NAMESPACE) \
		--create-namespace \
		--values $(DOCS_VALUES_FILE) \
		$(DOCS_HELM_SET_FLAGS) \
		--wait \
		--timeout $(DOCS_HELM_TIMEOUT); then \
		echo "==> deployment ready"; \
		echo "==> url: http://$$(helm get values $(DOCS_HELM_RELEASE) --kubeconfig $(DOCS_KUBECONFIG_PATH) -n $(DOCS_NAMESPACE) -a -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["ingress"]["host"])')$(DOCS_BASE_PATH)/"; \
	else \
		status=$$?; \
		echo "==> deployment failed, collecting diagnostics..."; \
		kubectl --kubeconfig $(DOCS_KUBECONFIG_PATH) get deploy,pods,svc,ingress -n $(DOCS_NAMESPACE) -l app.kubernetes.io/name=$(DOCS_PROJECT_NAME) -o wide || true; \
		latest_pod=$$(kubectl --kubeconfig $(DOCS_KUBECONFIG_PATH) get pods -n $(DOCS_NAMESPACE) -l app.kubernetes.io/name=$(DOCS_PROJECT_NAME) --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -n 1 | cut -d/ -f2); \
		if [ -n "$$latest_pod" ]; then \
			echo "==> latest pod: $$latest_pod"; \
			kubectl --kubeconfig $(DOCS_KUBECONFIG_PATH) describe pod -n $(DOCS_NAMESPACE) "$$latest_pod" | sed -n '/Events:/,$$p' || true; \
		fi; \
		exit $$status; \
	fi

docs-deploy-all: docs-docker-push docs-deploy

docs-status:
	@kubectl --kubeconfig $(DOCS_KUBECONFIG_PATH) get pods,svc,ingress -n $(DOCS_NAMESPACE) -l app.kubernetes.io/name=$(DOCS_PROJECT_NAME)

docs-logs:
	@kubectl --kubeconfig $(DOCS_KUBECONFIG_PATH) logs -f -n $(DOCS_NAMESPACE) deployment/$(DOCS_HELM_RELEASE)



# ============================================================
# KsADK Web static 同步
# ============================================================

STATIC_DIR := ksadk/server/static
KSADK_WEB_VERSION ?= latest
KSADK_WEB_PACKAGE ?= @kingsoftcloud/ksadk-web
KSADK_WEB_TARBALL_NAME := kingsoftcloud-ksadk-web-$(patsubst v%,%,$(KSADK_WEB_VERSION)).tgz
KSADK_WEB_RELEASE_URL ?=
KSADK_WEB_CACHE_DIR ?= .cache/ksadk-web

sync-ksadk-web-static:
	@echo "Sync KsADK Web static assets from $(KSADK_WEB_PACKAGE)@$(KSADK_WEB_VERSION)"
	@rm -rf "$(KSADK_WEB_CACHE_DIR)/package"
	@mkdir -p "$(KSADK_WEB_CACHE_DIR)" "$(STATIC_DIR)"
	@if [ -n "$(KSADK_WEB_RELEASE_URL)" ]; then \
		echo "Using explicit KSADK_WEB_RELEASE_URL=$(KSADK_WEB_RELEASE_URL)"; \
		curl -fL --retry 3 --retry-delay 2 --retry-all-errors "$(KSADK_WEB_RELEASE_URL)" -o "$(KSADK_WEB_CACHE_DIR)/$(KSADK_WEB_TARBALL_NAME)"; \
		echo "$(KSADK_WEB_TARBALL_NAME)" > "$(KSADK_WEB_CACHE_DIR)/.tarball-name"; \
	else \
		npm pack "$(KSADK_WEB_PACKAGE)@$(patsubst v%,%,$(KSADK_WEB_VERSION))" --pack-destination "$(KSADK_WEB_CACHE_DIR)" > "$(KSADK_WEB_CACHE_DIR)/.tarball-name"; \
	fi
	tar -xzf "$(KSADK_WEB_CACHE_DIR)/$$(cat "$(KSADK_WEB_CACHE_DIR)/.tarball-name")" -C "$(KSADK_WEB_CACHE_DIR)"
	@test -d "$(KSADK_WEB_CACHE_DIR)/package/dist-ksadk" || (echo "ERROR: dist-ksadk missing in $$(cat "$(KSADK_WEB_CACHE_DIR)/.tarball-name")" && exit 1)
	@rm -rf "$(STATIC_DIR)"
	@mkdir -p "$(STATIC_DIR)"
	cp -R "$(KSADK_WEB_CACHE_DIR)/package/dist-ksadk/." "$(STATIC_DIR)/"
	@echo "Synced KsADK Web $(KSADK_WEB_VERSION) static assets into $(STATIC_DIR)"

sync-hosted-ui: sync-ksadk-web-static
	@echo "sync-hosted-ui is deprecated; static assets now come from $(KSADK_WEB_PACKAGE)."

build-frontend: sync-ksadk-web-static
	@echo "Frontend static assets synced from $(KSADK_WEB_VERSION)"

build-wheel: build-frontend
	uv build

build-all: build-wheel
	@echo "Build complete. Wheel is in dist/"

clean-frontend:
	rm -rf $(STATIC_DIR)

# ============================================================
# 清理
# ============================================================

clean:
	@echo "🧹 清理构建产物和本地缓存..."
	rm -rf dist/ build/ *.egg-info/ .eggs/
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage coverage.xml htmlcov/ .tox/ .nox/
	rm -rf $(OFFLINE_DIR)/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ 清理完成"

clean-cache:
	@echo "🧹 清理本地测试/解释器缓存..."
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage coverage.xml htmlcov/ .tox/ .nox/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ 缓存清理完成"

clean-static:
	@echo "🧹 清理静态文件..."
	rm -rf $(STATIC_DIR)/*
	@echo "✅ 清理完成"

clean-offline:
	@echo "🧹 清理离线包..."
	rm -rf $(OFFLINE_DIR)/
	@echo "✅ 清理完成"
