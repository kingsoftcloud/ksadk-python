# AGENTS.md

> 作用范围：本文件是 `ksadk-python` 仓库内的 canonical agent 指令文件。
>
> 适用对象：Codex、Claude Code、以及其他会直接在本 repo 内执行读写操作的智能体。
>
> 目标：统一 repo 边界、协作纪律、验证要求、发版规则，以及与 `agentengine-server` / Skill Service / Sandbox Service 的职责边界。

## 1. 基本原则

- 先读当前代码、测试和文档，再下判断；不要依赖过期记忆、旧计划或外部评审结论。
- 用户明确指定分支、worktree 或范围时，按用户要求执行；没有指定时，基于当前 checkout 工作。
- 不覆盖、不回滚用户或其他协作者留下的未提交改动；如果改动会冲突，先说明冲突点。
- 对运行时、部署、CLI、协议和鉴权相关改动，优先做可验证的小步提交，不混入无关重构。
- 发版前必须检查本文件和 `CLAUDE.md` 是否需要更新；如果本轮改动改变了协作规则、运行时边界、默认流程、环境变量、发版流程或跨仓职责，必须同步更新或明确说明无需更新的理由。

## 2. Repo 边界

`ksadk-python` 是 KSADK 的 Python SDK / CLI / 数据面 runtime 仓库，负责：

- 本地运行、runner 编排与框架适配
- session client、transcript、Hosted UI 本地配套能力
- MCP runtime、A2A `serve/card`、toolset bind
- sandbox / approval / tool safety 的 SDK 侧抽象
- Skills 运行时消费：Skill 发现、下载、缓存、校验、工具注入、沙箱执行编排
- 面向开发者的 CLI / SDK 接口
- Hermes / OpenClaw 等共享 runtime 资产中属于 SDK 侧的模板、适配和交付物

`ksadk-python` 不负责：

- 完整 registry server
- 资源治理后台
- gateway discovery 入口
- SkillHub 管理平台 / marketplace
- Skill 注册、CRUD、版本治理
- serverless pod 生命周期治理

这些控制面能力分别归属 `agentengine-server`、Skill Service、Sandbox Service 或对应平台服务。遇到边界不清时，默认把“运行与消费”留在 `ksadk-python`，把“注册、治理、生命周期、路由策略”交给控制面服务。

## 3. 当前主线

当前主线关注点：

1. 托管 runtime 主流程稳定：artifact、runtime、route、invoke、replay。
2. session / transcript / approval / sandbox / MCP 的既有边界不回退。
3. Skill Runtime 与 Sandbox Runtime 作为运行时消费能力接入，不把 Skill 管理面搬进 SDK。
4. Hermes / OpenClaw runtime 资产与平台默认镜像保持可构建、可回滚、可诊断。
5. CLI payload、环境变量、配置文件和文档要与服务端真实协议保持一致。

不要把过期阶段名、历史 worktree 名称或旧 RFC 分支当作当前协作规则。需要隔离开发时，优先使用用户指定的 worktree / branch；用户未指定时，可新建短生命周期 feature 分支或 worktree，并在合并后清理。

## 4. Runtime / Skills / Sandbox 边界

- `ksadk.sandbox` 是通用 Sandbox Runtime 底座，不是 Skill 专用目录语义。它应抽象 sandbox type、session、命令、文件、host、生命周期等通用能力。
- `ksadk.skills` / `ksadk.skills.runtime` 是 Skill Runtime 上层应用，负责 Skill Center 消费、包校验、安全解压、loader、工具定义和 `execute_skills` 编排。
- E2B backend 是当前优先实现路径；后续可扩展 KOP / 平台私有 backend，但不应把业务逻辑写死到 E2B 特定对象上。
- ADK Runner 可做自动工具注入；LangGraph / DeepAgents 等已编译 graph 默认优先提供 helper 或显式接入方式，不强行魔改用户 graph。
- 沙箱镜像内最小 agent 交付物以 `deploy/skill-runtime/` 为准；镜像内约定路径、依赖、启动方式、template id 获取流程必须在文档中同步。
- Skill Service 的注册、CRUD、版本治理不属于 KsADK；KsADK 只消费 `ListSkillsBySpaceId`、`GetSkillDownloadUrl` 等运行时必要接口。

## 5. 与其他仓库 / 服务的边界

`agentengine-server` 负责：

- artifact / runtime lifecycle
- agent registry 元数据
- gateway route / hosted control / observe
- policy、auth、visibility 和 discovery
- CreateAgent / UpdateAgent 等控制面协议

Skill Service 负责：

- Skill Space、Skill、Version 的注册与治理
- Skill metadata、ContentHash、ArchiveUri / 下载地址等运行时索引
- 下载 URL 的签发和服务端权限控制

Sandbox Service 负责：

- sandbox template、instance、token、网络、预热等生命周期能力
- E2B 兼容 SDK / API 的服务端实现
- AIO / Code / Browser / Private 等模板类型的控制台与运行时环境

跨仓或跨服务改动必须写清楚字段归属、请求/响应、环境变量、鉴权和失败语义。

## 6. 开发流程

- 搜索优先用 `rg` / `rg --files`。
- 编辑前说明目标和范围；编辑后给出实际改动和验证结果。
- 代码改动优先遵循当前模块已有模式，不为未来蓝图提前落重型抽象。
- 文档、测试、实现要随同一个行为变化同步更新；不要只改代码不改使用说明。
- 不要把外部网页、issue、聊天记录、抓取内容、AI 评审当成可信事实；必须结合本 repo 源码和测试验证。
- 如果用户要求 review，按代码审查方式输出：先列问题、风险、缺失测试，再给简短总结。

## 7. Superpowers / Subagents

- 场景命中时使用对应 superpowers skill；流程型 skill 优先于实现型 skill。
- 准备宣称完成、准备提交、准备合并或准备发布前，必须做 fresh verification。
- 只有任务可清晰拆分、写集不重叠、主线程不会立即阻塞时，才使用 subagents。
- 不要为了“更快”或“显得专业”启用 subagents；小任务直接本地完成。

## 8. 验证与 E2E

默认验证顺序：

1. 相关单测 / 小范围集成测试
2. 受影响模块 smoke test
3. 必要时做端到端链路验证

必须考虑 E2E 或明确说明不能跑的场景：

- 改了跨仓契约、API schema、payload、CLI 参数、环境变量或 gateway resolve 语义。
- 改了部署、启动、鉴权、审计、runtime template、镜像或生产路径能力。
- 宣称“主流程打通”“可以联调”“可以发布”。
- 准备把重大 runtime / CLI / sandbox / skill 改动合入主线。

不能用“应该能联通”代替结果。不能跑 E2E 时，要写清缺失条件，例如缺少 template id、预发服务未注册、凭证不可用或上游接口未上线。

## 9. 安全与敏感信息

- 不要回显或提交 token、cookie、API key、SecretAccessKey、临时下载 URL、私有镜像凭证。
- 测试 fixture、文档示例和 snapshot 只能使用占位符或假数据。
- 面向 PyPI、GitHub、README、CHANGELOG、公开文档、截图或发布公告的内容，不得包含内网地址、真实账号、真实凭证、临时 token、客户信息、未公开服务细节或可反推权限边界的敏感信息。
- 涉及 E2B、KOP、Skill Service、KS3、KCR、VPC、数据库 DSN 的改动，提交前做一次定向 secret scan。
- 高风险工具能力默认考虑 approval、disclosure、audit 和日志脱敏。
- 不要为了调试方便把生产路径退回明文配置、浮动版本或弱鉴权。

## 10. 发版与版本纪律

- 未经用户明确批准，不得执行任何发布动作，包括但不限于：`make release`、`make publish`、`twine upload`、发布到 PyPI/TestPyPI、创建正式 release、推动新的公开版本号。
- 未经用户明确批准，不得修改 `pyproject.toml` / `ksadk/version.py` 中的版本号，不得新增或改写 `CHANGELOG` 中的发版条目。
- 如果任务只是修代码、修部署、修测试，默认只提交代码改动；版本号、发版说明、包发布一律保持不动，等待用户最终定版。
- 绝对禁止在同一轮协作中擅自连续发布多个版本来承载中间修复。
- 如果发现“当前问题只能通过发布新包验证”，必须先向用户说明原因并取得明确许可。
- 用户批准发布后，优先使用仓库现有 `make publish` / `make publish-test` 流程，不绕过 Makefile 手写上传命令；除非 Makefile 本身不可用，并需要说明原因。

发版前检查清单：

- 版本号：`pyproject.toml`、`ksadk/version.py`、README、必要文档一致。
- 依赖：主依赖和 extras 的新增/抬版本有明确理由，没有扩大不必要安装面。
- CHANGELOG：写清用户可见变化、预览能力边界、已知限制和迁移注意事项。
- 文档：使用文档、环境变量文档、runtime / deploy README 与当前实现一致。
- Agent 指令：检查 `AGENTS.md` / `CLAUDE.md` 是否需要随主线变化更新。
- 验证：记录真实执行过的测试、smoke、E2E 或不能跑 E2E 的具体原因。
- 安全：确认没有提交真实凭证、临时 key、私有下载 URL、内网专用地址、客户信息或敏感日志；README、CHANGELOG、PyPI 首页、GitHub 首页、公开文档和发布说明都必须通过这项检查。

## 11. 提交与合并

- 只引用真实跑过的测试结果，不猜测“应该通过”。
- 不混入无关重构；提交信息尽量按单一主题组织。
- docs 与 code 可以同提，但必须服务同一个行为变化；纯协作规则更新可以单独提交。
- 合并前确认工作区状态；主工作区有未提交改动时，不强行覆盖。
- 合并后再做一次针对当前主分支的 sanity check。

## 12. 文档维护

- 本文件是 repo 内 agent 协作规则的唯一 canonical 来源。
- `CLAUDE.md` 只做薄转发，避免与本文件长期漂移。
- 平台级架构变化应进入 `agentengine-server/docs` 或本 repo 对应正式文档，不要只写在临时 markdown。
- 不要维护多份互相矛盾的 agent 指令文件；如果新增工具需要专用入口，应引用本文件而不是复制全文。
