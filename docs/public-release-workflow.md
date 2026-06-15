# KsADK 公开分支与发布流程

本文档定义内部 `master` 与 GitHub 公开 `main` 的长期维护方式。它是发布和公开同步的执行依据。

## 分支职责

| 分支 / 工作区 | 职责 | 允许内容 | 禁止内容 |
| --- | --- | --- | --- |
| `master` | 内部开发主干 | 内部文档、内部部署适配、ezone 流程、平台联调材料 | 真实凭证、真实 kubeconfig、未脱敏 token |
| `main` | GitHub 公开主干 | 可公开源码、公开 README、公开 docs、公开 CI、公开 release 资产 | 内部运行手册、私有部署资产、内部绝对路径、未脱敏示例 |
| `release/public-x.y.z` | 公开候选分支 | 从 `master` 挑选的公开变更、脱敏修正、发布文档 | 未审核的大范围内部改动 |
| `.worktrees/public-main` | 长期公开同步工作树 | 公开候选、公开 `main` 检查、发布前验证 | 日常内部开发 |

公开 `main` 工作树可以长期保留。它的定位是发布工作区，不是第二条开发主线。

## 基本规则

1. 不直接 `merge master -> main`。
2. 公开变更从 `master` 通过 cherry-pick、patch 或 clean export 进入 `release/public-x.y.z`。
3. 公开候选先推内部 ezone 审核，再推 GitHub `main`。
4. 公开 release tag、GitHub Release 资产、PyPI、别名包、Pages 必须基于 GitHub `main` 上已审核同步后的公开提交。
5. 发布动作必须有用户明确批准。
6. `.pypirc` 不得放在仓库根目录。PyPI 凭证只允许来自 `~/.pypirc`、环境变量或 CI Secret。
7. 每次公开 GitHub Release 对应的 `main` 提交都必须打 tag 留痕。

## 推荐目录

```text
ksadk-python/
  master 工作区
  .worktrees/
    public-main/              # 长期保留的公开 main / 公开候选工作树
```

创建公开工作树：

```bash
git fetch github main
git worktree add .worktrees/public-main github/main
```

如果本地没有 `github` remote，先添加：

```bash
git remote add github git@github.com:kingsoftcloud/ksadk-python.git
git fetch github
```

## 日常开发

日常开发只在内部 `master` 或内部 feature 分支进行。

```bash
git checkout master
git pull --ff-only origin master
```

完成变更后先跑相关验证：

```bash
uv run pytest <相关测试>
git diff --check
```

涉及部署、鉴权、环境变量、发布路径、CLI payload 的改动，必须补对应测试和文档。

## 公开同步

从内部主干准备公开候选：

```bash
git checkout master
git pull --ff-only origin master
make public-sync-check

cd .worktrees/public-main
git fetch github main
git checkout -B release/public-0.6.2 github/main
```

同步变更时优先选择最小公开补丁：

```bash
git cherry-pick <commit>
```

如果内部 commit 包含不适合公开的文件，改用 patch 或 clean export，只带公开安全内容。

## 发布前门禁

公开候选分支上必须运行：

```bash
make public-preflight
```

该目标至少覆盖：

- 工作区与分支策略检查。
- `.pypirc`、kubeconfig、私钥、token pattern 等安全围栏。
- 公开路径 denylist 检查。
- `uv run pytest`。
- `mkdocs build --strict`。
- `uv build`。
- `twine check dist/*`。

失败即停止发布。不要用“只改了文档”跳过门禁；可以在最终说明里明确某项因环境缺失无法运行，但不能把未验证状态说成已通过。

## 内部 ezone 审核

公开候选通过本地门禁后，先推内部 ezone 审核分支：

```bash
git push origin release/public-0.6.2
```

审核材料至少包含：

- diff 摘要。
- `make public-preflight` 输出摘要。
- 是否涉及版本号、PyPI、GitHub Release、Pages。
- 是否同步别名包 `agentengine-sdk-python`。
- 不能运行的 E2E 及原因。

内部审核通过前，不得推 GitHub `main`、创建 GitHub Release、上传 PyPI 或更新 Pages。

## GitHub 同步

审核通过后：

```bash
git push github release/public-0.6.2:main
```

推送后检查 GitHub Actions、Pages 和仓库状态：

```bash
make public-publish-check
```

如果 `scripts/check_publication_state.py` 存在，优先使用脚本输出；否则执行 Makefile 内置基础 HTTP 检查。

## 公开 release tag

公开候选分支只用于门禁和内部审核。内部审核通过并推送到 GitHub `main` 后，先确认本地 `main` 与 `github/main` 指向同一个已审核公开提交，再创建 tag：

```bash
git fetch github main
git checkout main
git pull --ff-only github main
make public-release-tag V=0.6.2
```

默认 tag 名是 `v0.6.2`。如果需要单独的公开留痕 tag，可以覆盖：

```bash
make public-release-tag V=0.6.2 PUBLIC_RELEASE_TAG=public-release-v0.6.2
```

确认 tag 指向 GitHub `main` 上的公开提交后，再推送 tag：

```bash
git push github v0.6.2
```

GitHub Release 资产、PyPI 版本、别名包版本和文档站应能通过该 tag 追溯到同一个 GitHub `main` 公开提交。不要从内部 `master` 或未同步的 `release/public-x.y.z` 候选分支直接创建公开 release 资产。

## PyPI 与 GitHub Release

发布前确认：

```bash
git status --short --branch
make public-preflight
uv run python -m twine check dist/*
```

正式 PyPI 发布默认走 `.github/workflows/publish-pypi.yml`：

- GitHub Release `published` 或手动 `workflow_dispatch` 触发。
- workflow 先运行 `make sync-ksadk-web-static`，默认从 `@kingsoftcloud/ksadk-web@latest` 同步 `dist-ksadk` 到 `ksadk/server/static`。
- workflow 再运行 `make public-preflight`，最后通过 PyPI Trusted Publishing/OIDC 上传。
- 正常路径不配置 `PYPI_API_TOKEN`；如需应急本地发布，必须先明确记录原因并使用 Makefile 的 `make publish` / `make publish-test`。

发布 `ksadk` 后检查：

```bash
python - <<'PY'
import json, urllib.request
for name in ["ksadk", "agentengine-sdk-python"]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=20) as r:
        data = json.load(r)
    print(name, data["info"]["version"], data["info"].get("project_urls"))
PY
```

如果 `ksadk` 版本变更，必须判断别名包 `agentengine-sdk-python` 是否同步发布。不同步必须写明原因。

## 客户问题修复策略

| 类型 | 修复位置 | 公开同步 |
| --- | --- | --- |
| SDK 通用 bug | 先修 `master` | cherry-pick 到公开候选，发 patch |
| 内部账号 / 内网兼容，但不泄露敏感细节 | 先修 `master` | 可公开，文档只写必要 endpoint 和错误处理 |
| 内部平台专用 runbook | 只留 `master` | 不进 `main` |
| 凭证、kubeconfig、私有 registry 细节 | 不进代码仓 | 通过 Secret / 本地配置管理 |

## 长期维护建议

- `master` 保持高频开发。
- `main` 保持低频、可发布、可审计。
- 每个公开版本用一个 `release/public-x.y.z` 候选分支。
- `.worktrees/public-main` 长期保留，但定期 `git fetch` 和清理已合并候选分支。
- 每次发布后记录 GitHub commit、tag、PyPI version、Pages URL 和别名包状态。
