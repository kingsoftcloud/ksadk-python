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

## 审计重点

- sdist/wheel 不包含 `ksadk/server/web-ui`。
- sdist/wheel 不包含 `.zread/`、`.pypirc`、内部部署文件。
- PyPI metadata 指向 GitHub 和 GitHub Pages。
