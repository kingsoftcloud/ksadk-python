# KsADK 公开分支与发布流程

本文档定义内部 `master` 与 GitHub 公开 `main` 的长期维护方式。它是发布和公开同步的执行依据。

## 当前模型

`master` 是内部源主干，GitHub `main` 是公开发布主干。两者不应该长期维护两套功能代码或两套发布门禁。公开版本由内部 `master` 的已审核状态通过 clean export 生成，导出脚本只移除不适合公开的材料。

公开 `main` 应包含：

- 公开 SDK 源码：`ksadk/`、`ksadk_runtime_common/`，包括 `ksadk/studio/react-ui` 的源码与固定依赖锁。
- `ksadk/server/static` 和 `ksadk/studio/static` 由构建流程生成，只进入 wheel/sdist，不提交 Git，也不从内部工作树复制到公开候选。
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
5. GitHub Release 与 PyPI 包必须能追溯到同一个已审核 GitHub `main` 提交；Pages 文档独立部署。
6. `.pypirc`、私有 registry 凭证、kubeconfig、真实 API key、临时 token 不得进入仓库。

## 0.8.7 候选对齐与保留规则

0.8.7 已经由 PR #116 审核并合入公开 `main`。发布负责人已授权从 `main` 发布主包和兼容发行包；正式发布仍须通过完整预检、来源与审批门禁及 PyPI environment 审批。Web 继续使用已发布的 0.3.18，本次不创建新的 npm 版本。
内部 `master` 持续保留；对齐代码在派生分支完成，通过审核后再决定主干合入。

当公开 `main` 包含内部尚未吸收的修复时，先逐项审计并在内部对齐分支补回有效修复。
同一功能以内外两边的实际代码和回归结果判断；保留内部更新的协议、鉴权及安全校验，
同时保留公开的安装、取消、UI 和发布门禁修复，不按提交日期整文件覆盖。

候选复制只新增或更新经过公开审计的内容，不自动删除公开 `main` 已有文件。
公开历史保留的拆分 helper 可作为兼容路径继续存在，运行时主入口采用对齐后的实现。
同步前后比较全部 SDK 源码与资源、公开路径清单以及 diff；任何缺失或没有审计依据的
覆盖都阻止候选交付。分支、现有工作目录和原工作区的未提交内容不在清理范围内。

`docs/maintainer-approval-record.md` 必须记录当前版本；候选阶段各正式 sign-off 保持
Pending，不能复制上一版本的 Approved 行。此状态应使 `make public-publish-gate` 拒绝发布，
但不代替或跳过源码、构建、安装及完整预检。

## PostgreSQL 兼容检查

公开候选的数据库 CI 矩阵覆盖 PostgreSQL 9.6.21、10、11、12、13、14、15、16。
每个版本都连接独立的真实数据库，执行 session/history、Kernel/interaction、checkpoint、
OAuth 恢复、A2A outbox/进程恢复、Memory 和 Teams 检查；执行授权另有独立报告。
两份 JUnit 报告均须非空且零跳过，测试失败或意外跳过都会阻断检查。

Memory 的 PostgreSQL 后端保留既有表、旧列与历史记录，只新增 `record_json` 列承载
当前记录协议；跨连接版本更新使用事务锁与 revision 校验，不以静默覆盖代替 CAS。
测试包括旧表迁移、scope/tenant/workspace 隔离、TTL、软硬删除与并发更新。

配套服务的跨仓检查可通过 `A2A_TEST_SERVER_REPO`、`A2A_TEST_GATEWAY_REPO` 指定已固定
提交的本地 checkout；OAuth 协议镜像检查同样接受 `A2A_TEST_SERVER_REPO`。
本地控制面/网关测试使用隔离的身份和模型 fixture，不等同于真实云端环境验收。

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

## 准备内部 master

在 `agentengine/ksadk-python` 内部主干完成代码、文档、版本和审批记录：

```bash
git checkout master
git status --short --branch
uv run pytest <相关测试>
git diff --check
```

如果本次需要绑定新的 UI 版本，确认 `KSADK_WEB_VERSION` 默认值、README、docs-site、approval record 都引用同一个 npm 版本。

Phase 2 发布还必须在最终内部 `master` 提交上重新生成本地制品证据，并在 npm 包发布、Hosted UI 以 digest 部署、发布候选环境浏览器矩阵完成后运行：

```bash
make release-candidate-gate \
  RELEASE_FINAL_COMMIT=<final-internal-master-commit> \
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
candidate_parent="$(mktemp -d /tmp/ksadk-python-candidate.XXXXXX)"
python scripts/prepare_ksadk_python_export.py \
  --output-dir "$candidate_parent/export" \
  --summary
python3 scripts/open_source_audit.py \
  --target public-repo \
  --root "$candidate_parent/export"
```

同步到长期 public worktree：

```bash
git fetch github main
git worktree add .worktrees/public-main github/main  # 首次需要
rsync -a --checksum --exclude .git \
  "$candidate_parent/export/" \
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

默认 Web 来源为 npm 已发布的 `@kingsoftcloud/ksadk-web@0.3.18`。版本、源码提交、npm integrity 和两个构建目录的摘要统一记录在 `ksadk/studio/react-ui/vendor/ksadk-web-source.json`；该目录内的 JSON 是来源清单，默认构建不再使用本地 tarball。Studio 通过 `npm ci` 安装同一正式版本；SDK 页面从经摘要校验的官方 npm 归档生成。下载缓存不能替代来源校验，校验失败不会回退到旧包。

默认发布包在冷缓存时需要访问 npm registry。受限网络环境可在联网机器提前下载并验证，
将项目 `.cache/ksadk-web/` 中对应 SHA256 的归档缓存带入构建机；该路径命中后仍执行摘要校验。
本地联调也可使用下述显式归档入口。离线 CI 还需预备自身的 npm/uv 依赖缓存，
Web 归档缓存不等于全部依赖已可离线安装；不需要把临时 tarball 重新提交到 vendor。

联合验证尚未发布的 Web 修复时，可显式传入本地 `npm pack` 生成的 tarball：

```bash
make public-preflight PUBLIC_PREFLIGHT_MODE=source-sync \
  KSADK_WEB_TARBALL=/tmp/ksadk-web-local.tgz
```

仅更新本地 `ksadk web` 页面时，执行 `make sync-ksadk-web-static KSADK_WEB_TARBALL=/tmp/ksadk-web-local.tgz` 即可；无需编译 Studio。
显式本地路径不与默认来源清单比对版本或摘要，也不要求 `SOURCE_CANDIDATE.json`，仍保留包名、完整构建目录和安全解包检查。
产物记录实际 tarball 和目录摘要，标记为本地来源；后续同步、验证及打包必须继续传入同一路径。
不传本地路径时，默认来源的版本、源码及摘要校验保持不变。

验证记录必须同时保留构建时的实际 Web 源码提交和 tarball SHA-256，区分 npm registry 的正式包与本地源码候选。包内来源声明不能代替实际构建记录。源码候选通过门禁不表示待审 Web 修复已经发布，也不授予发布权限。

完整公开预检会在临时目录安装官方 DSH 工具链，再启动浏览器测试所需的 Provider 注册。主机需要 Node、pnpm、Chromium 与工具链下载网络；测试使用独立 profile，不修改用户已有插件配置。响应式浏览器检查及 Codex 插件 turn/回滚检查均为阻断项，不能以跳过后的证据声明完整通过。

该模式允许保留当前已发布版本，仍拒绝降版；其余公开测试、文档、浏览器、构建和制品审计不变。默认 `release` 模式仍要求版本高于已发布版本。源码同步结果不是发布授权或发布证据。

该门禁至少覆盖：

- PyPI 版本未重复发布。
- secret 和公开路径 audit。
- 从 npm registry 同步 `@kingsoftcloud/ksadk-web` 静态资源。
- 公开测试集。
- `docs-site` Fumadocs 静态构建。
- wheel/sdist 构建。
- wheel 与 sdist 重建 wheel 的默认安装均验证 ADK 原生 SQLite 跨进程写入/读取，不依赖 `[all]` 或 A2A extra 补齐数据库依赖。
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
cd .worktrees/public-main
git pull --ff-only github main
make public-release-tag V=<version>
git push github v<version>
```

创建 GitHub Release 时使用该 tag。发布说明应引用：

- GitHub `main` commit。
- tag。
- `ksadk-web` npm version。
- `make public-preflight` 结果。
- PyPI workflow run；文档另记 Pages workflow run。

## PyPI 与 GitHub Pages

正式发布只走 `.github/workflows/publish-pypi.yml`：

- 触发条件：GitHub Release `published` 或手动 `workflow_dispatch`。
- 输入：`ksadk_web_version`、`approved_source_commit` 和 `publish_target`。
- 正常发版使用 `publish_target=full`：公开候选须先通过完整本地 `make public-preflight`；workflow 再运行 `make public-preflight-publish` 和审批门禁，在首次上传前完成主包、`agentengine-sdk-python` 别名包的构建审计，然后发布两个发行包；文档独立部署。
- 补发别名包使用 `publish_target=alias-only`：workflow 只跑公开审计、公开测试、别名包构建审计和 approval gate，只发布 `agentengine-sdk-python`，不重发 `ksadk`，也不部署 GitHub Pages。
- workflow 先同步 npm registry 中的 UI 静态资源。
- workflow 再按 `publish_target` 运行对应发布前检查。
- workflow 再运行 `make public-publish-gate`，校验 approval record。
- PyPI 上传使用 OIDC Trusted Publishing。
- GitHub Pages 独立使用 `.github/workflows/pages.yml`，保留原来的 `docs-site` 构建步骤、目录和线上地址，不调用包发布任务。
- 文档变更推送到 `main` 或已有部署权限的 `gh-pages` 分支时部署；候选文档通过专用 `gh-pages` 文档源分支更新，不修改 `main`，不改变现有 environment 规则。
- `gh-pages` 只放跟踪的 `docs-site/`、`pages.yml` 和许可证，不复制 SDK、包发布工作流或本地构建依赖。

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
8. workflow 发布 PyPI；需要更新文档时，单独运行 `pages.yml`。
9. 运行 post-publish publication check。

这不是“提交 PR 到 main 后手工打 tag 和 release 文件”就结束。PR 到 `main` 只是公开源码同步；真正的 npm、PyPI、Pages 发布必须由 GitHub workflow 完成并通过发布后核对。
