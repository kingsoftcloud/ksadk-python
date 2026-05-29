# AGENTS.md

> `ksadk-python` 的 canonical agent 指令。适用于 Codex、Claude Code 和其他会在本 repo 内执行读写操作的智能体。

## 1. 核心纪律

- 先读当前代码、测试和文档，再下判断；不要依赖过期计划、旧记忆或外部评审结论。
- 用户指定分支、worktree 或范围时，按用户要求执行；未指定时，基于当前 checkout 工作。
- 不覆盖、不回滚用户或其他协作者留下的未提交改动；遇到冲突先说明。
- 改运行时、部署、CLI、协议、鉴权或发版路径时，小步修改、真实验证，不混入无关重构。
- 搜索优先用 `rg` / `rg --files`。

## 2. Repo 边界

`ksadk-python` 负责 Python SDK / CLI / 数据面 runtime：

- 本地运行、runner 编排、框架适配、session / transcript、本地 Hosted UI 配套能力。
- MCP runtime、A2A `serve/card`、toolset bind、sandbox / approval / tool safety 的 SDK 侧抽象。
- Skill Runtime 运行时消费：Skill 发现、下载、缓存、校验、工具注入、沙箱执行编排。
- Hermes / OpenClaw 等共享 runtime 资产中属于 SDK 侧的模板、适配和交付物。

`ksadk-python` 不负责完整 registry server、资源治理后台、gateway discovery、SkillHub marketplace、Skill 注册/CRUD/版本治理、serverless pod 生命周期。这些分别归属 `agentengine-server`、Skill Service、Sandbox Service 或平台控制面。

## 3. 当前主线

- 托管 runtime 主流程稳定：artifact、runtime、route、invoke、replay。
- session / transcript / approval / sandbox / MCP 既有边界不回退。
- Skill Runtime 与 Sandbox Runtime 只做运行时消费，不把 Skill 管理面搬进 SDK。
- Hermes / OpenClaw runtime 资产与平台默认镜像保持可构建、可回滚、可诊断。
- CLI payload、环境变量、配置文件和文档要与服务端真实协议一致。

不要把过期阶段名、历史 worktree 名称或旧 RFC 分支当作当前规则。需要隔离开发时，优先使用用户指定的 worktree / branch；未指定时可新建短生命周期 feature 分支或 worktree，并在合并后清理。

## 4. Runtime / Skills / Sandbox

- `ksadk.sandbox` 是通用 Sandbox Runtime 底座，不是 Skill 专用目录语义。
- `ksadk.skills` / `ksadk.skills.runtime` 是 Skill Runtime 上层应用，负责 Skill Center 消费、包校验、安全解压、loader、工具定义和 `execute_skills` 编排。
- E2B backend 是当前优先实现路径；后续可扩展 KOP / 平台私有 backend，但业务逻辑不要写死到 E2B 特定对象。
- ADK Runner 可做自动工具注入；LangGraph / DeepAgents 等已编译 graph 默认提供 helper 或显式接入，不强行魔改用户 graph。
- 沙箱镜像内最小 agent 交付物以 `deploy/skill-runtime/` 为准。
- Skill Service 管注册、CRUD、版本治理；KsADK 只消费运行时必要接口，例如 `ListSkillsBySpaceId`、`GetSkillDownloadUrl`。

## 5. 跨仓边界

- `agentengine-server`：artifact / runtime lifecycle、agent registry、gateway route、hosted control、observe、policy、auth、visibility、CreateAgent / UpdateAgent。
- Skill Service：Skill Space / Skill / Version 治理，metadata、ContentHash、ArchiveUri、下载 URL 和服务端权限。
- Sandbox Service：sandbox template、instance、token、网络、预热、E2B 兼容 SDK / API、AIO / Code / Browser / Private 模板。
- 跨仓或跨服务改动必须写清字段归属、请求/响应、环境变量、鉴权和失败语义。

## 6. Superpowers / Subagents

- 场景命中时使用对应 superpowers skill；流程型 skill 优先于实现型 skill。
- 准备宣称完成、提交、合并或发布前，必须 fresh verification。
- 只有任务可清晰拆分、写集不重叠、主线程不会立即阻塞时，才使用 subagents。
- 不要为了“更快”启用 subagents；小任务直接本地完成。

## 7. 验证与 E2E

默认顺序：相关单测 / 小范围集成测试 -> 受影响模块 smoke test -> 必要时端到端链路验证。
- 跑 e2e / web / run / build 验证时，优先使用项目自己的 `uv` 虚拟环境或 `uv run`，不要拿全局 `pip` 环境当通过标准；最近很多依赖缺口只会在干净的 uv 环境里暴露出来。
- 看到 `pip install -U ksadk[all]` 后才出现运行时缺包，不要默认是用户业务漏装，先核对默认依赖、extras、lock 和 `uv run` 下的实际导入结果。

必须考虑 E2E 或明确说明不能跑的场景：

- 改了跨仓契约、API schema、payload、CLI 参数、环境变量或 gateway resolve。
- 改了部署、启动、鉴权、审计、runtime template、镜像或生产路径。
- 宣称“主流程打通”“可以联调”“可以发布”。
- 合入重大 runtime / CLI / sandbox / skill 改动。

不能用“应该能联通”代替结果；不能跑 E2E 时写清缺失条件，例如缺少 template id、预发服务未注册、凭证不可用或上游接口未上线。

## 8. 安全与公开内容

- 不要回显或提交 token、cookie、API key、SecretAccessKey、临时下载 URL、私有镜像凭证。
- 测试 fixture、文档示例和 snapshot 只能使用占位符或假数据。
- README、CHANGELOG、PyPI / GitHub 首页、公开文档、截图和发布公告不得包含内网地址、真实账号、真实凭证、临时 token、客户信息、未公开服务细节或可反推权限边界的敏感信息。
- 涉及 E2B、KOP、Skill Service、KS3、KCR、VPC、数据库 DSN 的改动，提交前做定向 secret scan。
- 高风险工具能力默认考虑 approval、disclosure、audit 和日志脱敏。

## 9. 发版纪律

- 未经用户明确批准，不得执行发布动作，包括 `make release`、`make publish`、`twine upload`、PyPI/TestPyPI、正式 release 或公开版本号变更。
- 未经用户明确批准，不得改 `pyproject.toml` / `ksadk/version.py` 版本号，不得新增或改写 CHANGELOG 发版条目。
- 用户批准发布后，优先使用仓库现有 `make publish` / `make publish-test`，不绕过 Makefile 手写上传命令。
- 不得在同一轮协作中擅自连续发布多个版本承载中间修复。
- `master` 是内部开发主干；GitHub `main` 是公开主干。不得直接 `merge master -> main`，公开同步必须走 `release/public-x.y.z` 或等价候选分支。
- 公开候选必须先推内部 ezone 审核，再推 GitHub、发 GitHub Release、上传 PyPI 或发布 Pages。
- 公开发布前必须运行 `make public-preflight`。如果只做发布状态核对，运行 `make public-publish-check`。失败时不得发布。
- 每次公开 GitHub Release 对应的公开提交都必须打 tag 留痕，优先使用 `make public-release-tag V=x.y.z`。
- 公开分支长期工作树可以保留，但只能作为公开同步/发布工作区，不做日常内部开发。
- 不得把 `.pypirc`、私有 registry 凭证、kubeconfig、真实 API Key 或临时 token 放入仓库根目录；PyPI 凭证只允许来自 `~/.pypirc`、环境变量或 CI Secret。
- 完整公开同步流程见 `docs/public-release-workflow.md`；该文档优先于口头约定。

发布前必须检查：

- 工作区干净：没有未提交源码、文档、构建产物或未跟踪文件；如果必须保留，先说明。
- 版本一致：`pyproject.toml`、`ksadk/version.py`、README、CHANGELOG、必要文档一致。
- 别名包：检查 `agentengine-sdk-python` 是否需要同步版本、依赖和发布说明；如不同步，说明原因。
- 依赖合理：主依赖和 extras 的新增/抬版本有明确理由，没有扩大不必要安装面。
- PyPI 渲染：README 不使用 PyPI 不支持的相对文档链接或 Mermaid；构建后跑 `twine check`。
- 公开安全：README、CHANGELOG、PyPI/GitHub 首页、公开文档和发布说明不含真实凭证、内网专用地址、客户信息或敏感日志。
- 验证记录：列出真实执行过的测试、smoke、E2E，或不能跑 E2E 的具体原因。
- Agent 指令：检查 `AGENTS.md` / `CLAUDE.md` 是否需要随主线变化更新。

## 10. 提交与文档

- 只引用真实跑过的测试结果，不猜测“应该通过”。
- 提交信息按单一主题组织；docs 与 code 可以同提，但必须服务同一个行为变化。
- 合并前确认工作区状态；主工作区有未提交改动时不强行覆盖。
- 本文件是 repo 内 agent 协作规则唯一 canonical 来源；`CLAUDE.md` 只做薄转发，避免规则漂移。
- 平台级架构变化进入 `agentengine-server/docs` 或本 repo 正式文档，不要只写临时 markdown，文档标题和内容尽量使用中文。
