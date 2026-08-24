# KsADK 公开分支与发布流程

本文档定义内部 `master` 与 GitHub 公开 `main` 的长期维护方式。它是发布和公开同步的执行依据。

## 当前模型

`master` 是内部源主干，GitHub `main` 是公开发布主干。两者不应该长期维护两套功能代码或两套发布门禁。公开版本由内部 `master` 的已审核状态通过 clean export 生成，导出脚本只移除不适合公开的材料。

公开 `main` 应包含：

- 公开 SDK 源码：`ksadk/`、`ksadk_runtime_common/`。
- 公开构建与发布门禁：`Makefile`、`scripts/open_source_audit.py`、`scripts/check_*`、`.github/workflows/*`。
- 公开文档站：`docs-site/`。
- 公开 README、CHANGELOG、LICENSE、CONTRIBUTING、AGENTS、CLAUDE。
- 公开发布所需的最小测试集。

内部 `master` 可以额外包含：

- 内部 docs、archive、runbook、设计草稿和预览材料。
- 内部 agent skills / operator playbooks。
- 内部验证脚本、E2E、长任务和平台集成测试。
- 内部部署资产、临时缓存、zread/site/build 产物等。

因此不能简单理解为“只差 docs 和 skills 两个目录”。准确说法是：**公开导出的源码和发布门禁必须与 `master` 的公开子集一致；非公开材料由导出脚本排除**。

## 硬性规则

1. 不直接 `merge master -> main`，也不把内部 `master` 直接 push 到 GitHub。
2. 公开同步必须走 clean export candidate 或等价的公开候选分支。
3. 公开候选必须先通过 `make public-preflight`。
4. npm、PyPI、GitHub Pages 都必须由可信 GitHub workflow 发布；不使用本地 `npm publish`、本地 `twine upload` 或手工上传 Pages。
5. GitHub Release、PyPI 包、Pages 文档必须能追溯到同一个已审核 GitHub `main` 提交。
6. `.pypirc`、私有 registry 凭证、kubeconfig、真实 API key、临时 token 不得进入仓库。

## 准备 ksadk-web

`ksadk-web` 是共享 UI 源头。需要新 UI 时，先在 `agentengine/ksadk-web` 发 npm 版本，再让 `ksadk-python` 和 `agentengine-hosted-ui` 消费 registry 里的固定版本。

本地只做验证：

```bash
cd agentengine/ksadk-web
npm test
node --test tests/*.test.mjs
npm run build:all
npm pack --dry-run --access public
```

正式 npm 发布只走 GitHub workflow：

- 推送 `ksadk-web` 代码到 GitHub `main`。
- 创建 GitHub Release 或手动触发 `publish-npm.yml`。
- workflow 使用 `npm publish --provenance` 发布。
- 发布后用 `npm view @kingsoftcloud/ksadk-web@<version>` 确认 registry 可见。

## 准备内部 master

在 `agentengine/ksadk-python` 内部主干完成代码、文档、版本和审批记录：

```bash
git checkout master
git status --short --branch
uv run pytest <相关测试>
git diff --check
```

如果本次需要绑定新的 UI 版本，确认 `KSADK_WEB_VERSION` 默认值、README、docs-site、approval record 都引用同一个 npm 版本。

RuntimeEvent schema v2 发布的额外约束：当 Python 发布把运行事件主路径切到 canonical `schema_version=2`（能力描述 `RuntimeEventVersions=[1,2]`、`RuntimeEventDefault=2`、`RuntimeEventV1ProjectionModes=["snapshot_only","identity_replace"]`、`RuntimeEventV1ProjectionDefault="snapshot_only"`）时，配套的 `ksadk-web`、Studio react-ui 与 `agentengine-hosted-ui` 必须是与本次发布一致的 identity-aware 版本，才能按 run/scope/item/part identity 正确归并流式与回放输出。候选报告必须记录 Python 与三个 UI 仓库各自的 commit 和包版本，作为同一发布单元评审。

更新审批记录：

```bash
uv run python scripts/check_approval_record.py \
  --expected-current-commit <reviewed-internal-master-commit>
uv run pytest tests/test_check_approval_record.py tests/test_public_release_positioning.py -q
```

审批记录必须写清：

- reviewed internal `ksadk-python` commit。
- `ksadk-web` npm version 和 source commit。
- 已执行的 public preflight / docs build / package audit 证据。
- Maintainer、Security reviewer、Release owner sign-off。

## 生成公开候选

公开候选从内部 master clean export 生成：

```bash
cd agentengine/ksadk-python
rm -rf /tmp/ksadk-python-export-candidate-<version>
python scripts/prepare_ksadk_python_export.py \
  --output-dir /tmp/ksadk-python-export-candidate-<version> \
  --summary
python3 scripts/open_source_audit.py \
  --target public-repo \
  --root /tmp/ksadk-python-export-candidate-<version>
```

同步到长期 public worktree：

```bash
git fetch github main
git worktree add .worktrees/public-main github/main  # 首次需要
rsync -a --checksum --delete --exclude .git \
  /tmp/ksadk-python-export-candidate-<version>/ \
  .worktrees/public-main/
```

`.worktrees/public-main` 是公开候选工作区，不做日常内部开发。

## 公开候选门禁

在 public worktree 运行完整门禁：

```bash
cd .worktrees/public-main
make public-preflight
```

该门禁至少覆盖：

- PyPI 版本未重复发布。
- secret 和公开路径 audit。
- 从 npm registry 同步 `@kingsoftcloud/ksadk-web` 静态资源。
- 公开测试集。
- `docs-site` Fumadocs 静态构建。
- wheel/sdist 构建。
- `twine check dist/*`。
- wheel/sdist 文件列表 audit。

失败即停止，不创建 Release，不触发 PyPI，不部署 Pages。

## 同步 GitHub main

公开候选通过门禁后，通过 GitHub PR 或受保护 main 策略合入 GitHub `main`。推荐路径：

1. 在 `.worktrees/public-main` 提交候选。
2. 推送到 GitHub release candidate 分支。
3. 开 PR 到 GitHub `main`。
4. 等 CI / release-check / docs-site build 通过并完成 review。
5. 合并 PR，使 GitHub `main` 成为唯一公开发布源。

如果维护者明确选择 fast-forward 或直接更新 `main`，也必须满足同样门禁和 review 条件。不要从内部 `master` 创建公开 release 资产。

## Tag 与 GitHub Release

tag 必须指向 GitHub `main` 上已审核、已合入的公开提交：

```bash
git fetch github main
git checkout .worktrees/public-main
git pull --ff-only github main
make public-release-tag V=<version>
git push github v<version>
```

创建 GitHub Release 时使用该 tag。发布说明应引用：

- GitHub `main` commit。
- tag。
- `ksadk-web` npm version。
- `make public-preflight` 结果。
- PyPI/Pages workflow run。

## PyPI 与 GitHub Pages

正式发布只走 `.github/workflows/publish-pypi.yml`：

- 触发条件：GitHub Release `published` 或手动 `workflow_dispatch`。
- 输入：`ksadk_web_version`、`approved_source_commit` 和 `publish_target`。
- 正常发版使用 `publish_target=full`：workflow 会跑完整 `make public-preflight`，发布 `ksadk` 主包，构建并发布 `agentengine-sdk-python` 别名包，并部署 GitHub Pages。
- 补发别名包使用 `publish_target=alias-only`：workflow 只跑公开审计、公开测试、别名包构建审计和 approval gate，只发布 `agentengine-sdk-python`，不重发 `ksadk`，也不部署 GitHub Pages。
- workflow 先同步 npm registry 中的 UI 静态资源。
- workflow 再按 `publish_target` 运行对应发布前检查。
- workflow 再运行 `make public-publish-gate`，校验 approval record。
- PyPI 上传使用 OIDC Trusted Publishing。
- GitHub Pages 由同一个 workflow 构建 `docs-site` 并部署。

发布后核对：

```bash
python scripts/check_publication_state.py --phase post-publish --version <version>
npm view @kingsoftcloud/ksadk-web@<web-version> version
python - <<'PY'
import json, urllib.request
for name in ["ksadk", "agentengine-sdk-python"]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=20) as r:
        data = json.load(r)
    print(name, data["info"]["version"], data["info"].get("project_urls"))
PY
```

## 最短可执行清单

一次正常公开发布的最短路径是：

1. `ksadk-web` 合入 GitHub `main`，由 GitHub workflow 发布 npm。
2. 内部 `ksadk-python/master` 记录版本、文档、approval evidence。
3. 从内部 master 生成 clean export。
4. 在 public candidate 运行 `make public-preflight`。
5. public candidate 通过 GitHub PR 合入公开 `main`。
6. 在公开 `main` commit 上打 `v<version>` tag。
7. 创建 GitHub Release 或手动触发 `publish-pypi.yml`。
8. workflow 发布 PyPI 并部署 GitHub Pages。
9. 运行 post-publish publication check。

这不是“提交 PR 到 main 后手工打 tag 和 release 文件”就结束。PR 到 `main` 只是公开源码同步；真正的 npm、PyPI、Pages 发布必须由 GitHub workflow 完成并通过发布后核对。
