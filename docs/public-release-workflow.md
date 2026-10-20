# KsADK 公开分支与发布流程

本文档定义镜像 `master` 与 GitHub `main` 的长期维护方式。它是发布和公开同步的执行依据。

## 当前模型

`master` 与 GitHub `main` 保持同一提交和代码树，二者都只能包含可公开源码、安全测试与发布门禁。GitHub `main` 是公开审核入口，镜像主干在审核后同步到同一提交。

共享主干包含 SDK、运行时、Studio 源码、公开契约、文档站、可重复构建输入和必要回归测试。生成的 UI 静态资源只进入 wheel/sdist；内部设计、部署清单、个人环境、凭证与原始验证日志保留在共享主干之外。

已有内部历史不得直接合入共享主干。先生成 clean export，保留有效修复，检查当前文件及可达历史，再经过相同的公开测试与 review。

## 硬性规则

1. 不把包含内部材料的分支或历史直接合入共享主干；同步使用经审核的同一安全提交。
2. 公开同步必须走 clean export candidate 或等价的公开候选分支。
3. 公开候选必须先通过 `make public-preflight`。
4. npm、PyPI、GitHub Pages 都必须由可信 GitHub workflow 发布；不使用本地 `npm publish`、本地 `twine upload` 或手工上传 Pages。
5. GitHub Release、PyPI 包、Pages 文档必须能追溯到同一个已审核 GitHub `main` 提交。
6. `.pypirc`、私有 registry 凭证、kubeconfig、真实 API key、临时 token 不得进入仓库。

## 0.8.4 CI 与本地门禁分工

PR 的 Release Check 负责源码清洁检查、前端构建、可追溯的 wheel/sdist、全新环境安装和制品内容审计。它使用 `--skip-tests` 避免在组合门禁里重复执行实时运行时 E2E；报告中的 source E2E 保持 `not_run`，不得把该报告当作完整发布证据。

原因：#67 的组合门禁在兼容测试全部通过后出现长时间无输出，本地相同代码的完整门禁和 Python 3.11 对照均通过，尚未确定远端卡住的具体调用。独立 CI 的全量测试、原生运行时、浏览器页面及真实插件任务保留；本地 `make public-preflight` 仍必须通过。手动运行 Release Check 仍执行完整组合门禁，并启用详细测试输出及 90 秒线程栈诊断，整体任务限时 25 分钟。定位并修复卡点后再恢复 PR 中的组合 source E2E，不以修改断言或虚报通过代替验证。

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

## 准备共享主干

在 `agentengine/ksadk-python` 内部主干完成代码、文档、版本和审批记录：

```bash
git checkout -b codex/public-candidate github/main
git status --short --branch
uv run pytest <相关测试>
git diff --check
```

如果本次需要绑定新的 UI 版本，确认 `KSADK_WEB_VERSION` 默认值、README、docs-site、approval record 都引用同一个 npm 版本。

Phase 2 发布还必须在最终共享主干 提交上重新生成本地制品证据，并在 npm 包发布、Hosted UI 以 digest 部署、发布候选环境浏览器矩阵完成后运行：

```bash
make release-candidate-gate \
  RELEASE_FINAL_COMMIT=<final-reviewed-source-commit> \
  RELEASE_LOCAL_EVIDENCE=<local-phase2-evidence.json> \
  RELEASE_WEB_REGISTRY_EVIDENCE=<npm-registry-evidence.json> \
  RELEASE_DEPLOYMENT_EVIDENCE=<hosted-ui-deployment-evidence.json> \
  RELEASE_PREPROD_EVIDENCE=<release-environment-browser-e2e.json>
```

该门禁要求 npm integrity、Hosted UI 镜像 digest、Helm revision、wheel/sdist digest 和最终源码提交互相一致；Studio 新建 Agent 与历史 0.8.2 Agent 都必须通过 Studio/Hosted UI 两个入口的多轮流式验证。测试新建的 Agent 必须删除，历史 Agent 必须保留。缺少 registry 正式版本、使用可变镜像 tag、单轮响应或仅本地 mock 时，报告不会变绿。

RuntimeEvent schema v2 发布的额外约束：当 Python 发布把运行事件主路径切到 canonical `schema_version=2`（能力描述 `RuntimeEventVersions=[1,2]`、`RuntimeEventDefault=2`、`RuntimeEventV1ProjectionModes=["snapshot_only","identity_replace"]`、`RuntimeEventV1ProjectionDefault="snapshot_only"`）时，配套的 `ksadk-web`、Studio react-ui 与 `agentengine-hosted-ui` 必须是与本次发布一致的 identity-aware 版本，才能按 run/scope/item/part identity 正确归并流式与回放输出。候选报告必须记录 Python 与三个 UI 仓库各自的 commit 和包版本，作为同一发布单元评审。

更新审批记录：

```bash
uv run python scripts/check_approval_record.py \
  --expected-current-commit <reviewed-source-commit>
uv run pytest tests/test_check_approval_record.py tests/test_public_release_positioning.py -q
```

审批记录必须写清：

- reviewed public `ksadk-python` commit。
- `ksadk-web` npm version 和 source commit。
- 已执行的 public preflight / docs build / package audit 证据。
- Maintainer、Security reviewer、Release owner sign-off。

## 生成公开候选

公开候选从共享主干 clean export 生成：

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

仅同步源码、不创建 tag / Release、不发布 PyPI 或 Pages 时，显式运行：

```bash
make public-preflight PUBLIC_PREFLIGHT_MODE=source-sync
```

联合验证尚未发布的 Web 修复时，应使用与 CI 固定源码提交一致、已构建并审计的 tarball：

```bash
make public-preflight PUBLIC_PREFLIGHT_MODE=source-sync \
  KSADK_WEB_VERSION=0.3.12 KSADK_WEB_TARBALL=/tmp/ksadk-web-reviewed.tgz
```

验证记录必须同时保留 Web 源码提交和 tarball SHA-256，区分 npm registry 的正式包与源码候选。源码候选通过门禁不表示待审 Web 修复已经发布，也不授予发布权限。

完整公开预检会在临时目录安装官方 DSH 工具链，再启动浏览器测试所需的 Provider 注册。主机需要 Node、pnpm、Chromium 与工具链下载网络；测试使用独立 profile，不修改用户已有插件配置。响应式浏览器检查及 Codex 插件 turn/回滚检查均为阻断项，不能以跳过后的证据声明完整通过。

该模式允许保留当前已发布版本，仍拒绝降版；其余公开测试、文档、浏览器、构建和制品审计不变。默认 `release` 模式仍要求版本高于已发布版本。源码同步结果不是发布授权或发布证据。

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

如果维护者明确选择 fast-forward 或直接更新 `main`，也必须满足同样门禁和 review 条件。仅从已审核的 GitHub `main` 提交创建公开 release 资产。

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
- 正常发版使用 `publish_target=full`：公开候选先通过本地完整 `make public-preflight`、PR review 和 CI；发布 workflow 再运行 `make public-preflight-publish`，复核版本、公开审计、公开测试及主包构建。主包与 `agentengine-sdk-python` 兼容发行包必须都完成构建、`twine check` 和制品审计后，才依次上传 PyPI；同一 workflow 随后部署 GitHub Pages。
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
3. 从共享主干 生成 clean export。
4. 在 public candidate 运行 `make public-preflight`。
5. public candidate 通过 GitHub PR 合入公开 `main`。
6. 在公开 `main` commit 上打 `v<version>` tag。
7. 创建 GitHub Release 或手动触发 `publish-pypi.yml`。
8. workflow 发布 PyPI 并部署 GitHub Pages。
9. 运行 post-publish publication check。

这不是“提交 PR 到 main 后手工打 tag 和 release 文件”就结束。PR 到 `main` 只是公开源码同步；真正的 npm、PyPI、Pages 发布必须由 GitHub workflow 完成并通过发布后核对。

## 镜像同步核验

审核完成后，将镜像主干同步到 GitHub `main` 的同一提交。同步前确认目标分支没有并发更新，涉及历史替换时使用绑定旧 SHA 的 lease，并保留本地恢复副本。分支保护需要维护者审批时保持保护，先提供候选和验证结果。

再次读取两端主干的 SHA，要求精确相等；仅祖先包含或代码树相等都不足以说明提交一致。当前树和候选可达历史都要通过敏感路径、凭证与私有部署信息检查。历史 tag、PR 缓存等其他引用的清理范围另行记录，不把主干检查扩大为平台已彻底删除旧对象。
