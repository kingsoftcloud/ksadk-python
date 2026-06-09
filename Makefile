# AgentEngine Makefile
# 用于构建 Web UI 和管理项目

.PHONY: help install build-webui sync-static webui clean clean-cache clean-dist clean-static clean-offline dev dev-webui dev-backend test test-webui publish publish-test open-source-audit open-source-audit-public-repo open-source-audit-dist open-source-audit-ksadk-python-export open-source-audit-ksadk-web open-source-smoke-install open-source-smoke-ksadk-web open-source-review open-source-review-bundle open-source-review-bundle-verify open-source-approval-check open-source-publication-plan open-source-publication-state public-status public-sync-check public-secret-audit public-audit public-test public-build-check public-preflight public-publish-check public-release-tag public-review public-docs-build public-docs-serve public-docs-audit

# 默认目标
help:
	@echo ""
	@echo "  \033[1;36m金山云 AgentEngine\033[0m 开发工具"
	@echo ""
	@echo "  \033[1;32m开发命令:\033[0m"
	@echo "    make install        安装 Python 开发依赖"
	@echo "    make dev            启动本地 SDK Web 服务"
	@echo "    make test           运行测试"
	@echo ""
	@echo "  \033[1;32mWeb UI 静态产物:\033[0m"
	@echo "    make build-webui    校验 ksadk/server/static 已存在"
	@echo "    make sync-static    校验 ksadk/server/static 已存在"
	@echo "    make webui          校验已打包的 Web UI 静态产物"
	@echo "                         可编辑 Web UI 源码位于 https://github.com/kingsoftcloud/ksadk-web"
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
	@echo "    make open-source-audit  运行开源公开产物审计"
		@echo "    make open-source-audit-dist  审计 dist/ 中的 sdist/wheel 文件清单"
		@echo "    make open-source-audit-ksadk-python-export  生成并审计 ksadk-python 清洁导出候选仓"
		@echo "    make open-source-audit-ksadk-web  生成并审计 KSADK Web 候选仓"
		@echo "    make open-source-smoke-install  在干净 venv 安装 wheel 并检查 CLI"
		@echo "    make open-source-smoke-ksadk-web  在独立候选仓中测试并构建 KSADK Web"
	@echo "    make open-source-review  运行开源候选本地审核验证"
	@echo "    make open-source-review-bundle  生成本地维护者审核包"
	@echo "    make open-source-approval-check  校验公开发布审批记录是否完整"
	@echo "    make open-source-publication-plan  生成审批后的 GitHub 导入命令计划"
	@echo "    make open-source-publication-state  只读检查 GitHub/Pages/PyPI 外部发布状态"
	@echo "    make open-source-review-bundle-verify  校验本地开源审核包完整性"
	@echo ""
	@echo "  \033[1;32m公开发布门禁:\033[0m"
	@echo "    make public-status        查看公开发布相关状态"
	@echo "    make public-preflight     GitHub/PyPI/Release 前必须通过的本地门禁"
	@echo "    make public-release-tag V=x.y.z  在 GitHub main 对齐后创建公开 release 留痕 tag"
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
	@echo "  \033[1;32m公开文档站:\033[0m"
	@echo "    make public-docs-build  构建 GitHub Pages 候选文档站"
	@echo "    make public-docs-audit  审计 GitHub Pages 候选文件清单"
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

install-webui:
	@echo "ℹ️  Web UI 源码位于独立仓库: https://github.com/kingsoftcloud/ksadk-web"
	@echo "   ksadk-python 公开仓只包含已打包静态产物。"

# ============================================================
# Web UI 构建
# ============================================================

STATIC_DIR = ksadk/server/static
OPEN_SOURCE_SMOKE_VENV ?= /tmp/ksadk-open-source-smoke
OPEN_SOURCE_SMOKE_WHEEL ?= dist/ksadk-$(VERSION)-py3-none-any.whl

build-webui:
	@echo "ℹ️  ksadk-python 不包含可编辑 Web UI 源码。"
	@echo "   请在 https://github.com/kingsoftcloud/ksadk-web 构建，并在 release 时同步静态产物到 $(STATIC_DIR)。"
	@if [ ! -f "$(STATIC_DIR)/index.html" ]; then \
		echo "❌ 错误: $(STATIC_DIR)/index.html 不存在"; \
		exit 1; \
	fi
	@echo "✅ 已找到 Web UI 静态产物: $(STATIC_DIR)"

sync-static:
	@$(MAKE) build-webui

webui: build-webui sync-static
	@echo "✅ Web UI 静态产物检查完成。"

# ============================================================
# 开发服务器
# ============================================================

dev:
	@echo "🚀 启动本地 SDK Web 服务..."
	@echo "   URL: http://localhost:8000"
	@echo ""
	@echo "使用 Ctrl+C 停止服务"
	python -m ksadk.cli web .

dev-webui:
	@echo "ℹ️  Web UI 开发服务器请在独立仓库运行:"
	@echo "   git clone https://github.com/kingsoftcloud/ksadk-web"
	@echo "   cd ksadk-web && npm ci && npm run dev"

dev-backend:
	@echo "🔧 启动后端开发服务器..."
	python -m ksadk.cli web .

# ============================================================
# 测试
# ============================================================

test:
	@echo "🧪 运行 Python 测试..."
	pytest tests/ -v

open-source-audit: open-source-audit-public-repo

open-source-audit-public-repo:
	@echo "🔎 生成并审计 ksadk-python 清洁导出候选仓..."
	@rm -rf /tmp/ksadk-python-export-candidate
	@python3 scripts/prepare_ksadk_python_export.py --output-dir /tmp/ksadk-python-export-candidate --json > /tmp/ksadk-python-export-candidate.json
	@python3 scripts/open_source_audit.py --target public-repo --root /tmp/ksadk-python-export-candidate

open-source-audit-dist:
	@echo "🔎 审计 sdist/wheel 发布产物..."
	@python3 scripts/audit_release_artifacts.py dist

open-source-audit-ksadk-python-export:
	@echo "🔎 生成并审计 ksadk-python 清洁导出候选仓..."
	@rm -rf /tmp/ksadk-python-export-candidate
	@python3 scripts/prepare_ksadk_python_export.py --output-dir /tmp/ksadk-python-export-candidate --json > /tmp/ksadk-python-export-candidate.json
	@python3 scripts/open_source_audit.py --target public-repo --root /tmp/ksadk-python-export-candidate

open-source-audit-ksadk-web:
	@echo "🔎 生成并审计 ksadk-web 候选仓..."
	@rm -rf /tmp/ksadk-web-export-candidate
	@python3 scripts/prepare_ksadk_web_export.py --output-dir /tmp/ksadk-web-export-candidate --json > /tmp/ksadk-web-export-candidate.json
	@python3 scripts/open_source_audit.py --target ksadk-web-candidate --root /tmp/ksadk-web-export-candidate

open-source-smoke-install:
	@echo "🧪 在干净 venv 中安装 wheel 并检查 CLI..."
	@if [ ! -f "$(OPEN_SOURCE_SMOKE_WHEEL)" ]; then \
		echo "❌ 找不到 $(OPEN_SOURCE_SMOKE_WHEEL)，请先运行 uv build"; \
		exit 1; \
	fi
	@rm -rf "$(OPEN_SOURCE_SMOKE_VENV)"
	@python3 -m venv "$(OPEN_SOURCE_SMOKE_VENV)"
	@"$(OPEN_SOURCE_SMOKE_VENV)/bin/python" -m pip install --upgrade pip >/tmp/ksadk-open-source-smoke-pip.log
	@"$(OPEN_SOURCE_SMOKE_VENV)/bin/python" -m pip install "$(OPEN_SOURCE_SMOKE_WHEEL)" >/tmp/ksadk-open-source-smoke-install.log
	@"$(OPEN_SOURCE_SMOKE_VENV)/bin/agentengine" --help >/tmp/ksadk-open-source-smoke-agentengine-help.txt
	@"$(OPEN_SOURCE_SMOKE_VENV)/bin/agentengine" web --help >/tmp/ksadk-open-source-smoke-agentengine-web-help.txt
	@echo "✅ wheel 安装 smoke 通过：agentengine --help 与 agentengine web --help 均可用"

open-source-smoke-ksadk-web:
	@echo "🧪 在独立候选仓中测试并构建 KSADK Web..."
	@rm -rf /tmp/ksadk-web-export-candidate
	@python3 scripts/prepare_ksadk_web_export.py --output-dir /tmp/ksadk-web-export-candidate --json > /tmp/ksadk-web-export-candidate.json
	@cd /tmp/ksadk-web-export-candidate && npm ci
	@cd /tmp/ksadk-web-export-candidate && npm test
	@cd /tmp/ksadk-web-export-candidate && npm run build:ksadk
	@cd /tmp/ksadk-web-export-candidate && npm run build:hosted
	@cd /tmp/ksadk-web-export-candidate && npm audit --audit-level=moderate
	@echo "✅ KSADK Web 独立候选仓测试、双构建与 npm audit 通过"

open-source-review: public-preflight open-source-audit-ksadk-python-export open-source-audit-ksadk-web public-docs-audit open-source-audit-dist open-source-smoke-install
	@echo "✅ 开源候选本地审核验证完成"

open-source-review-bundle:
	@echo "📦 生成本地开源审核包..."
	@python3 scripts/prepare_open_source_review_bundle.py
	@$(MAKE) open-source-review-bundle-verify

open-source-review-bundle-verify:
	@echo "🔎 校验本地开源审核包..."
	@python3 scripts/verify_open_source_review_bundle.py

open-source-approval-check:
	@echo "🔐 校验公开发布审批记录..."
	@python3 scripts/check_approval_record.py

open-source-publication-plan:
	@echo "🧭 生成审批后的 GitHub 导入命令计划..."
	@python3 scripts/plan_github_publication.py --output /tmp/ksadk-open-source-review-bundle/github-publication-plan.md

open-source-publication-state:
	@echo "🔎 只读检查公开发布外部状态..."
	@python3 scripts/check_publication_state.py --phase placeholder

public-docs-build:
	@echo "📚 构建 GitHub Pages 候选文档站..."
	@uv run --extra dev python -m mkdocs build --strict

public-docs-serve:
	@echo "🌐 启动公开文档站预览..."
	@uv run --extra dev python -m mkdocs serve

public-docs-audit: public-docs-build
	@echo "🔎 审计 GitHub Pages 候选文档站..."
	@find site -type f | sed 's|^site/||' | python3 scripts/open_source_audit.py --target github-pages --file-list -

PUBLIC_BRANCH ?= main
PUBLIC_REPO ?= https://github.com/kingsoftcloud/ksadk-python
PUBLIC_DOCS_URL ?= https://kingsoftcloud.github.io/ksadk-python/
PUBLIC_PYPI_PROJECT ?= ksadk
PUBLIC_ALIAS_PYPI_PROJECT ?= agentengine-sdk-python
PUBLIC_RELEASE_TAG ?= v$(V)
PUBLIC_PUBLISH_PHASE ?= pre-publish

public-status:
	@echo "==> public candidate"
	@git status --short --branch
	@echo ""
	@echo "==> remotes"
	@git remote -v
	@echo ""
	@echo "==> configured public targets"
	@echo "PUBLIC_BRANCH=$(PUBLIC_BRANCH)"
	@echo "PUBLIC_REPO=$(PUBLIC_REPO)"
	@echo "PUBLIC_DOCS_URL=$(PUBLIC_DOCS_URL)"
	@echo "PUBLIC_PYPI_PROJECT=$(PUBLIC_PYPI_PROJECT)"
	@echo "PUBLIC_ALIAS_PYPI_PROJECT=$(PUBLIC_ALIAS_PYPI_PROJECT)"

public-sync-check:
	@echo "==> public candidate branch policy"
	@branch=$$(git branch --show-current); \
	case "$$branch" in \
		$(PUBLIC_BRANCH)|release/public-*|review/public-*|open-source/*) ;; \
		*) \
			echo "❌ 当前分支不是公开 main 或公开候选分支: $$branch"; \
			echo "   公开候选应在 release/public-x.y.z、open-source/* 或等价审核分支运行门禁。"; \
			exit 1; \
			;; \
	esac
	@if [ -f ".pypirc" ]; then \
		echo "❌ 仓库根目录存在 .pypirc，必须删除后再进入公开流程"; \
		exit 1; \
	fi
	@echo "✅ public branch policy passed"

public-secret-audit: public-sync-check
	@echo "==> secret and sensitive-file audit"
	@if git ls-files | grep -E '(^|/)(\.pypirc|kubeconfig|.*\.kubeconfig|id_rsa|id_ed25519)$$'; then \
		echo "❌ 发现禁止跟踪的敏感文件"; \
		exit 1; \
	fi
	@if rg -n --hidden -S --glob '!.git/**' --glob '!node_modules/**' --glob '!dist/**' --glob '!build/**' --glob '!*.egg-info/**' 'pypi-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|SecretAccessKey\s*[:=]\s*[^<\s]+' .; then \
		echo "❌ secret pattern audit failed"; \
		exit 1; \
	fi
	@echo "✅ secret audit passed"

public-audit: public-secret-audit
	@echo "==> public source audit"
	@blocked=$$(git ls-files | grep -E '^(\.pypirc|docs/internal/|\.zread/(wiki|site)/)' || true); \
	if [ -n "$$blocked" ]; then \
		echo "❌ blocked tracked paths:"; \
		echo "$$blocked"; \
		exit 1; \
	fi
	@$(MAKE) open-source-audit-public-repo
	@echo "✅ public source audit passed"

public-test:
	@echo "==> public tests"
	@uv run --extra dev pytest tests/test_open_source_audit.py tests/test_prepare_ksadk_python_export.py tests/test_prepare_ksadk_web_export.py tests/test_runtime_common_packaging.py tests/test_tracing_setup_otlp.py -q

public-build-check: clean-dist
	@echo "==> build and twine check"
	@uv build
	@uv run --extra dev python -m twine check dist/*

public-preflight: public-audit public-test public-docs-build public-build-check
	@git diff --check
	@$(MAKE) open-source-audit-dist
	@echo "✅ public preflight passed"

public-publish-check:
	@echo "==> publication state check"
	@if [ -f "scripts/check_publication_state.py" ]; then \
		uv run python scripts/check_publication_state.py --phase "$(PUBLIC_PUBLISH_PHASE)" --version "$(VERSION)"; \
	else \
		echo "⚠️  scripts/check_publication_state.py 不存在，执行基础 HTTP 检查"; \
		python3 -c 'import json, urllib.request; targets={"repo":"$(PUBLIC_REPO)","docs":"$(PUBLIC_DOCS_URL)","pypi":"https://pypi.org/pypi/$(PUBLIC_PYPI_PROJECT)/json","alias_pypi":"https://pypi.org/pypi/$(PUBLIC_ALIAS_PYPI_PROJECT)/json"}; [print("%s: HTTP %s%s" % (name, resp.status, ("\n  version=%s" % json.load(resp)["info"].get("version")) if name.endswith("pypi") else "")) for name, url in targets.items() for resp in [urllib.request.urlopen(url, timeout=20)]]'; \
	fi

public-release-tag:
ifndef V
	$(error ❌ 请指定版本号，例如: make public-release-tag V=$(VERSION))
endif
	@branch=$$(git branch --show-current); \
	if [ "$$branch" != "$(PUBLIC_BRANCH)" ]; then \
		echo "❌ public release tag 必须在公开 $(PUBLIC_BRANCH) 分支创建，当前分支是 $$branch"; \
		echo "   请先完成内部审核，并将已审核候选推送到 GitHub $(PUBLIC_BRANCH)。"; \
		exit 1; \
	fi
	@if ! git rev-parse --verify "github/$(PUBLIC_BRANCH)" >/dev/null 2>&1; then \
		echo "❌ 找不到 github/$(PUBLIC_BRANCH)，请先 git fetch github $(PUBLIC_BRANCH)"; \
		exit 1; \
	fi
	@if [ "$$(git rev-parse HEAD)" != "$$(git rev-parse github/$(PUBLIC_BRANCH))" ]; then \
		echo "❌ 当前 HEAD 未与 github/$(PUBLIC_BRANCH) 对齐，不能创建公开 release tag"; \
		exit 1; \
	fi
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

test-webui:
	@echo "🧪 运行 Web UI 测试..."
	cd $(WEBUI_DIR) && npm test

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

# 发布配置文件仅允许放在用户主目录，避免 PyPI/TestPyPI token 进入仓库。
PYPIRC := ~/.pypirc
DIST_DIR := dist

clean-dist:
	@echo "🧹 清理 dist/build 临时产物..."
	@rm -rf $(DIST_DIR)/* build/ *.egg-info/

publish: clean-dist build-only
	@echo "🚀 发布 v$(VERSION) 到 PyPI..."
	@if [ -f ".pypirc" ]; then \
		echo "❌ 错误: 项目根目录不允许存在 .pypirc，避免 PyPI token 进入仓库"; \
		echo "   请移到 ~/.pypirc 或使用环境变量 TWINE_USERNAME/TWINE_PASSWORD"; \
		exit 1; \
	fi
	@if [ ! -f ~/.pypirc ] && [ -z "$$TWINE_PASSWORD" ]; then \
		echo "❌ 错误: 找不到 ~/.pypirc，也未设置 TWINE_PASSWORD"; \
		echo "   请在用户主目录创建 ~/.pypirc 文件:"; \
		echo "   [pypi]"; \
		echo "   username = __token__"; \
		echo "   password = <pypi-api-token>"; \
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
	if [ -n "$$TWINE_PASSWORD" ]; then \
		python -m twine upload $$FILES; \
	else \
		python -m twine upload --config-file $(PYPIRC) $$FILES; \
	fi

publish-test: clean-dist build-only
	@echo "🧪 发布 v$(VERSION) 到 TestPyPI..."
	@if [ -f ".pypirc" ]; then \
		echo "❌ 错误: 项目根目录不允许存在 .pypirc，避免 TestPyPI token 进入仓库"; \
		echo "   请移到 ~/.pypirc 或使用环境变量 TWINE_USERNAME/TWINE_PASSWORD"; \
		exit 1; \
	fi
	@if [ ! -f ~/.pypirc ] && [ -z "$$TWINE_PASSWORD" ]; then \
		echo "❌ 错误: 找不到 ~/.pypirc，也未设置 TWINE_PASSWORD"; \
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
	if [ -n "$$TWINE_PASSWORD" ]; then \
		python -m twine upload --repository testpypi $$FILES; \
	else \
		python -m twine upload --config-file $(PYPIRC) --repository testpypi $$FILES; \
	fi

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
# 清理
# ============================================================

clean:
	@echo "🧹 清理构建产物和本地缓存..."
	rm -rf dist/ build/ *.egg-info/ .eggs/
	rm -rf $(WEBUI_DIST)
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
