# 构建与打包

公开发布前要分别验证源码候选、文档、sdist、wheel 和干净环境安装。

## 常用命令

```bash
uv run --extra dev pytest
uv run --extra dev python -m mkdocs build --strict
uv build
uv run --extra dev python -m twine check dist/*
make open-source-review
```

## Web UI 产物

`ksadk-python` wheel 包含 `ksadk/server/static`，保证用户安装后无需 Node 也能打开本地 UI。
可编辑 UI 源码不进入 `ksadk-python`，只在 `ksadk-web` 仓库维护。

## 发布门禁

`make public-build-check` 在 `uv build` 与 `twine check dist/*` 之间执行 wheel 内容检查，确保发布产物干净可用。

```bash hl_lines="3"
make public-build-check
# 1) clean-dist 清空旧 dist/
# 2) sync-ksadk-web-static 拉取最新 UI 静态产物
# 3) uv build 构建 sdist/wheel
# 4) tests/test_runtime_common_packaging.py 校验 wheel 内容
# 5) twine check dist/*
```

该检查会断言:

- wheel 不含旧 `ksadk/server/web-ui/` 源码、构建产物与 `node_modules/`。
- wheel 不含历史构建残留(如上一次 `dist/` 残片、`.zread/`、`.pypirc`)。
- wheel 含同步后的 `ksadk/server/static/index.html` 及 `assets/` 入口,保证安装即可打开本地 UI。

!!! tip "本地门禁与正式发布一致"
    `make public-preflight` 在 `public-build-check` 之上追加 `public-audit`、`public-test`、`public-docs-build`,是推 GitHub/PyPI/Release 前必须通过的完整本地门禁。

## 审计重点

- sdist/wheel 不包含 `ksadk/server/web-ui`。
- sdist/wheel 不包含 `.zread/`、`.pypirc`、内部部署文件。
- PyPI metadata 指向 GitHub 和 GitHub Pages。
- 正式 PyPI 发布走 `.github/workflows/publish-pypi.yml`,由 GitHub Release `published` 事件或 `workflow_dispatch` 触发;workflow 先 `make sync-ksadk-web-static`(默认拉取 `@kingsoftcloud/ksadk-web@latest`,可通过 `ksadk_web_version` input 指定版本)再 `make public-preflight`,最后通过 OIDC Trusted Publishing 上传,不依赖长期 PyPI token。

!!! new "0.6.7 新增"
    Serverless 部署会在运行时 Pod 注入 UI 配置环境变量:`KSADK_UI_PROFILE`、`KSADK_UI_PATH`、`KSADK_UI_URL`、`KSADK_UI_BUNDLE_PATH`。Pod 内 `ksadk.server.app` 读取这些变量还原 UI 运行时配置,无需把本地 `.agentengine/` 状态打包进镜像。
