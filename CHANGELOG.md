# 更新日志

本文件记录 **Kingsoft AgentEngine SDK (ksadk)** 的重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)，
版本遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## [0.5.5] - 2026-05-13

### 亮点

- **`init --from-agent` 兼容性增强**：对已有 Agent 项目做更严格的入口校验，不再盲信失效的 `agentengine.yaml` / `langgraph.json`，降低从 LangGraph、DeepAgents、ADK 等现有项目迁移到 AgentEngine 的手工改造成本。
- **DeepAgents 服务型项目自动适配**：支持 FastAPI / lifespan 中异步初始化 DeepAgents graph 的项目，自动生成 `agentengine_adapter.py` 暴露 `root_agent`，避免用户必须改业务代码或手写适配层。
- **本地调试命令更贴近用户环境**：`agentengine web`、`agentengine run`、`agentengine a2a serve` 会优先进入项目 `.venv` 执行，减少“依赖已装但 CLI 解释器看不到”的问题。
- **默认运行时刷新**：Hermes 默认 base 镜像和上游 ref 更新到 `v2026.5.7`，OpenClaw 默认 base 镜像更新到 `2026.5.7`。

### 变更

- `--from-agent` 会校验入口文件是否存在、入口变量是否为模块顶层可导出对象；函数体内的局部变量不再被误判为可导入 agent。
- `--from-agent` 支持 `src/` layout 项目，生成和加载时自动补齐项目 `src` 导入路径，不要求用户手动设置 `PYTHONPATH`。
- `--from-agent` 支持读取 `langgraph.json` 的 graph target，并在目标变量不可静态验证时自动降级到目录扫描和适配器生成。
- DeepAgents service-style 检测覆盖 `init_agent_resources()`、`create_deep_agent(...)`、`FastAPI(lifespan=...)` 和 `DeepAgentRunnable` 等常见组合；生成的 adapter 会把 AgentEngine 输入映射为服务项目常见的 `message/thread_id` 结构，并归一化输出。
- `agentengine model` / `agentengine config model` 写入当前项目 `.env`，避免误更新父目录或用户主目录下的环境文件。
- code mode 构建会合并并补齐本地运行所需依赖，降低导入已有项目后缺少运行时依赖的概率。
- OpenClaw 微信渠道连接在 web login RPC 不可用时可回退到远端 OpenClaw CLI 登录流程。
- OpenClaw / Hermes 终端 exec 参数校验收口，拒绝空参数、shell 元字符和危险 launcher，减少远端终端命令注入风险。
- OpenClaw runtime bootstrap 与 Hermes runtime 模板补齐若干本地运行和 secretRef 场景下的默认配置。
- 本地构建模板、CLI fallback 默认值和测试断言同步到 Hermes `2026.5.7` / OpenClaw `2026.5.7`，与平台侧默认配置保持一致。

### 修复

- 修复 DeepAgents 项目中 `graph = create_deep_agent(...)` 位于 `init_agent_resources()` 函数体内时，被 `--from-agent` 误识别为模块顶层 `graph` 入口，导致生成的 `agentengine.yaml` 指向不存在变量的问题。
- 修复 DeepAgents / LangGraph `src/` layout 项目在本地 loader、code-mode 入口和生成项目中导入路径不一致的问题。
- 修复服务型 DeepAgents 项目导入阶段过早加载 Daytona、Postgres、MCP 等业务外部依赖，导致 runner 未真正执行前就失败的问题。
- 修复项目 `.venv/bin/python` 为符号链接时，本地调试命令可能误判已经处于项目虚拟环境、从而跳过 re-exec 的问题。
- 修复 `agentengine model` 兼容入口可能把模型配置写到非当前项目 `.env` 的问题。
- 修复 OpenClaw runtime proxy 仅允许 TUI 模式导致远端 CLI fallback 无法复用安全终端通道的问题。

## [0.5.4] - 2026-05-05

### 亮点

- **云上 K8s 多副本会话可恢复**：新增可插拔 session backend 与 PGSQL 共享后端，支持同一 agent 的多个 pod 读取同一份平台 session 列表、turn 事件和轻量状态，用于 Hosted UI 回显与 runtime transcript replay。
- **Hosted UI 原生运行时体验升级**：Hosted UI 支持 capability-driven 原生运行时入口，OpenClaw / Hermes 可通过统一能力声明展示管理入口和安全终端入口。
- **OpenClaw / Hermes 默认运行时刷新**：OpenClaw 默认运行时升级到 `2026.5.4`，Hermes 运行时模板同步 `2026.4.30` 默认镜像与上游 ref，并补齐终端会话控制面能力。

### 变更

- 新增 `KSADK_SESSION_BACKEND` 统一选择 session backend，内置 `memory`、`local/sqlite`、`postgres`；保留 `AGENTENGINE_SESSION_BACKEND`、`KSADK_STM_BACKEND` 作为兼容别名。
- 新增 `KSADK_SESSION_DSN` 作为 PGSQL session backend 主连接串配置，兼容 `KSADK_STM_URL`、`KSADK_STM_DB_URL`；`postgres` 后端缺少 DSN 时会快速失败，不静默降级到本地存储。
- 新增 `PostgresSessionService`，保存平台 session index、turn-level events、轻量 state 与 continuity metadata；表结构包含 `tenant_id`、`workspace_id`、`agent_id`、`user_id`、`session_id` 隔离维度。
- `GetAgentUiBootstrap` 新增 `SessionBackend` 诊断信息，标记 backend 类型、是否 shared、是否 production safe 与 continuity 默认等级；诊断结果不向前端暴露 DSN。
- 修正 `local` 语义：`local` / `sqlite` 均表示本地 SQLite，`memory` 才表示纯内存；K8s 多副本生产场景推荐使用 `postgres`。
- code mode 运行时依赖补齐 `asyncpg>=0.30.0,<1.0.0`，确保部署包启用 PGSQL session backend 时具备数据库驱动。
- Hosted UI 前端协议保持兼容，`CreateSession`、`ListSessions`、`GetSession`、`ListSessionEvents`、`DeleteSession`、`RunAgent` 继续走现有 action，只切换底层 session service。
- LangGraph runner 继续把 `session_id` 映射到 `configurable.thread_id`，P0 只提供平台 transcript replay；完整 runtime checkpoint continuity 仍由业务 agent 配置共享 checkpointer / STM。
- OpenClaw Hosted UI 改为基于 runtime capabilities 选择原生 launcher / chat 入口，新增终端 session 列表、创建、附着和关闭能力。
- OpenClaw runtime proxy 与 bootstrap 同步 `2026.5.4` 默认配置，补齐终端 websocket、proxy auth、gateway token 与 password 处理。
- Hermes runtime app / Dockerfile / 测试同步当前默认镜像，新增 `/_ksadk/terminal/sessions` 控制面，支持远端终端会话的创建、复用和关闭。
- Web UI session 列表、run state 与 terminal session 工具函数补齐单测，覆盖会话恢复、终端状态和 responses 流式状态回收。

### 修复

- 修复 OpenClaw Responses API 与 remote runner 事件互操作问题，改善 responses 流式事件、终端状态事件和 session reload 的兼容性。
- 修复 OpenClaw 原生终端在 gateway token、state token、password 场景下的认证透传问题。
- 修复 Hosted UI 在 session 切换、运行中状态恢复、终端完成 / 失败事件回收时的状态残留问题。
- 修复多模态能力解析对模型目录 capability 的识别边界，避免把不支持的模型误判为可原生处理图片输入。

## [0.5.3] - 2026-04-28

### 亮点

- **Web UI 工作区文件管理重构**：右侧文件区改为可调整宽度、可全屏的工作区面板，上传入口和路径展示收敛为更轻量的布局，并保持打开文件区时左侧对话区可继续正常使用。
- **工作区文件预览能力增强**：支持在 Web UI 内预览文本、Markdown、代码、CSV/TSV、图片与 PDF 文件，便于直接查看上传文件或大模型生成的文件产物。
- **hosted UI 同步链路可移植**：`agentengine-server` 可从完整 `ksadk-python` 源码构建并同步最新 hosted UI；本地缺少 ksadk 源码时会尝试从 ezone 拉取，避免硬编码个人路径。

### 变更

- Web UI 的工作区面板改为 workspace-relative 路径展示，移除冗余导航行，新增文件类型支持说明、紧凑文件列表、内容预览和 PDF iframe/blob 预览。
- `agentengine-server` Makefile 新增 `KSADK_SOURCE_DIR`、`KSADK_REPO_URL` 与 `sync-ksadk-source`，`sync-hosted-ui` 在源码缺失时自动尝试补齐 ksadk 源码。
- hosted UI 同步脚本在构建前自动执行 `npm ci` 或 `npm install`，降低新机器同步静态资源时的依赖缺失风险。
- code mode 打包随运行时同时携带 `ksadk_runtime_common` 源码，并补齐 `python-multipart` 依赖，覆盖上传文件处理链路。
- ADK runner 在模型不支持原生图片输入时跳过图片二进制直传，并追加明确的系统提示，避免把不支持的图片附件误传给模型。

### 修复

- 修复新版 OpenClaw gateway request stage 形态下 workspace files proxy patch 无法插入的问题，兼容 `2026.4.26` 与更早 `2026.3.28` 形态。
- 修复 hosted UI / 本地静态资源构建产物未同步到 `ksadk/server/static` 与 `agentengine-server/app/static/hosted-ui` 的发布一致性问题。

## [0.5.2] - 2026-04-27

### 亮点

- **工作区文件管理正式进入 CLI 主线**：新增 `agentengine files`，覆盖远端 workspace 的浏览、单文件上传/下载/删除、目录 `push/pull` 同步，并打通 `agentengine agent invoke --local-workspace` 的调用前同步能力。
- **Responses API 兼容显著增强**：`/v1/responses` 从薄兼容壳升级为正式 serializer，支持更接近 OpenAI 的 response object、SSE 生命周期、思考流、工具调用、工具结果和人工审核 / interrupt 渲染。
- **LangGraph custom-state 接入更稳**：`ksadk_prepare_state(payload, session_context)` 成为正式契约，`init --from-agent` 可为 custom-state / ambiguous LangGraph 项目自动生成 adapter 模板。
- **默认运行时和模型目录升级**：默认 OpenClaw 镜像升级到 `v2026.4.24`，承接上游 DeepSeek V4 Flash / V4 Pro bundled catalog、V4 Flash onboarding default、Google Meet participant plugin、realtime voice loop、浏览器自动化和模型目录启动优化；默认 Hermes / OpenClaw 托管配置同步升级到 `glm-5.1` + `kimi-k2.6` 组合。

### 变更

- `/v1/responses` 从薄兼容层升级为更完整的 Responses serializer，新增 `instructions`、`metadata` 请求字段，补齐 richer response object、官方风格 SSE 生命周期，以及 `output_text` / `session_id` 兼容扩展。
- Responses 流式输出新增 reasoning、function call、tool result 与人工审核 / interrupt 渲染，失败和中断场景分别使用 `response.failed`、`response.incomplete`。
- `agentengine invoke` / RunAgent 优先适配 responses 协议事件，同时保留旧事件名兼容，支持通过参数传入 model 和 session id。
- 新增 `agentengine files` workspace 文件管理子命令，支持 `list`、`upload`、`download`、`delete`、`push`、`pull`；输出包含逻辑路径、真实路径、文件大小、传输模式和 `created/overwritten/skipped` 同步结果，支持 `--output json`、路径逃逸保护、`100MB` 默认上传上限与 OpenClaw `action_proxy` / 常规 agent `runtime_direct` 双传输模式。
- `agentengine agent invoke --local-workspace` 支持在 Hermes 远端 native 模式启动前同步本地目录到远端 workspace，自动读取 `GetAgentUiBootstrap` 的 `WorkspaceFiles.MaxUploadBytes`，并支持 `--remote-workspace-path` 与同步进度展示。
- LangGraph / DeepAgents runner 正式化 `ksadk_prepare_state(payload, session_context)` custom-state 契约，hook 可收到完整 normalized payload 与固定 session context，返回非 `dict` 时 fail fast。
- `init --from-agent` 增加 LangGraph 状态形态静态诊断，对 custom-state / ambiguous 图自动生成 `agentengine_adapter.py` 模板并改写入口，对 messages-based 图保持原入口。
- 默认 OpenClaw / Hermes runtime 镜像更新到 `openclaw:2026.4.24` 与 `hermes-agent:2026.4.23`，同步刷新一键部署模板、Dockerfile、Makefile 和文档。
- 默认模型和模型目录进一步升级：Hermes 默认模型保持 `glm-5.1`，自动补齐 `HERMES_CONTEXT_LENGTH=200000` 与 `HERMES_FALLBACK_MODEL=kimi-k2.6`；OpenClaw bootstrap 的 KSYUN 默认目录从 `kimi-k2.5` 升级到 `kimi-k2.6`，并继续使用 `glm-5.1` primary、`kimi-k2.6` fallback / image model。
- 随 OpenClaw `v2026.4.24` 默认镜像，托管 OpenClaw 可使用上游新增的 DeepSeek V4 Flash / V4 Pro 模型目录；DeepSeek V4 Pro / Flash 上游模型卡标注支持 `1M` context，适合长上下文代码库分析和复杂 agentic 任务。
- 中文使用文档新增 `/v1/responses` 使用章节，补齐非流式、流式、思考、工具执行、人工审核和 ksadk 扩展字段说明。

### 修复

- 修复 LangGraph custom-state 图在传入 `{"input": ...}` 时只能走 messages-first 约定、无法显式映射业务 state 的问题。
- 修复 responses 流式中 interrupt 被误包装为 completed 的兼容风险，generic interrupt 继续通过 `response.ksadk.approval_request` 暴露。
- 修复 LangChain / ADK / session continuity 在 `instructions` 注入和会话历史转换中的若干兼容边界，避免指令污染用户 transcript。
- 修复 OpenClaw bootstrap 默认环境变量、secretRef、heartbeat、模型目录生成和 upstream `v2026.4.24` 配置兼容问题。
- 修复 hosted UI / 本地 Web UI 对新 responses 生命周期事件的增量渲染、工具调用和 approval 展示兼容性。

## [0.5.1] - 2026-04-17

### 变更

- 新增 `agentengine hermes` 一等公民资源组，支持 `deploy`、`list`、`status`、`open`、`connect`、`exec`、`pairing`、`delete`，并让 `agentengine invoke <hermes-agent>` 默认进入 Hermes 原生远程 TUI。
- Hermes 以共享 runtime 镜像方式接入，不要求用户本地 `build/push`；新增 runtime 资产、公共镜像工作流，以及 `/`、`/chat`、`/v1/*`、`/_ksadk/terminal/ws` 的统一运行时 contract。
- 新增 OpenClaw 用户自定义镜像模板与示例，支持在平台运行时约束下自定义插件、skills 和默认配置。
- `agentengine openclaw deploy` 新增 `--env KEY=VALUE` 透传能力，允许业务自定义环境变量直接进入容器运行时。
- OpenClaw 部署新增 `OPENCLAW_CHANNEL_BOOTSTRAP_JSON`、Agentspace bootstrap 配置与 `OPENCLAW_BROWSER_SSRF_POLICY_JSON` 透传，便于渠道预配置和内网访问策略收口。
- 新增 `agentengine openclaw repair`，并支持 `agentengine openclaw gateway doctor --fix` 通过控制面直接触发 `doctor-fix` 修复动作。
- code mode 构建新增 Linux Runtime 兼容性 / ABI 校验，关键原生扩展不兼容时会在打包阶段提前失败。
- 默认 Hermes 共享 runtime 镜像更新为 `hub.kce.ksyun.com/agentengine-public/hermes-agent:2026.4.23`，并把构建默认 `HERMES_AGENT_REF` 同步到上游 `v2026.4.23`。
- 默认 OpenClaw 基础镜像 pin 到官方 `ghcr.io/openclaw/openclaw:2026.4.24@sha256:7c4370ff8777555d4c9fe5ab821aaaad7c87188d389a6cf761270725d96ec3e9`，同步刷新自定义镜像模板和一键部署文档。

### 修复

- 进一步完善 OpenClaw managed runtime 在当前 upstream bundle 下的 trusted-proxy loopback、backend self-pairing 与默认 browser 行为兼容性，降低诊断和修复成本。
- 改进 hosted Hermes 运行时默认行为，网关进程改为容器内托管与重启，减少对宿主机 daemon 能力的依赖。
- hosted Hermes 运行时默认补齐 `TERM=xterm-256color` 与统一状态目录布局，提升远端 setup / pairing 交互稳定性。
- Hermes hosted 默认模型进一步收口：对 `glm-5.1` 在未显式配置时自动补齐 `context_length=200000`，并把 fallback model 默认设为 `kimi-k2.6`。
- OpenClaw heartbeat 默认改为 `every=30m`、`target=none`、`isolatedSession=true`，并继续保留 `lightContext=true`，避免心跳占用当前聊天窗口和会话历史。
- OpenClaw 默认模型目录和自动补齐的 primary model 项把 `maxTokens` 基线从 `8192` 提升到 `20000`。

## [0.4.0] - 2026-04-07

### 变更

- 文档体系重构为“使用指南 + 技术设计”双主文档，`README.md` 收口为轻量入口页。
- 增加 hosted-first UI metadata、移动端聊天 UI 和 quick access 刷新重试，收口本地与 hosted OpenClaw 体验。
- 统一附件处理链路，覆盖 hosted/local transcript、runner 输入、replay 和 Web UI 上传，并引入结构化 `attachment_results` 与 OCR fallback。
- 默认 OpenClaw 基础镜像切换到官方 `ghcr.io/openclaw/openclaw`，当前 `Dockerfile` 默认 pin 到 `2026.4.14`。
- 默认内置能力从 `skillhub` 切换为 `clawhub`，同时写入中国镜像源默认配置并更新 strict-mode allowlist。
- 优化 code mode 构建链路，包括 KS3 上传 fallback、依赖缓存复用和目标运行时优先安装 Linux wheel。

### 修复

- 修复 code mode 构建忽略显式 `PIP_INDEX_URL` 和 `UV_INDEX_URL` 的问题。
- 收敛非目标构建机上的源码编译 fallback，减少在 macOS 等环境下误触发本地源码安装。

## [0.3.6] - 2026-03-24

### 变更

- 新增 canonical `agent` 资源组，并统一资源命令与工作流命令的 `--output pretty|json` 输出结构。
- 新增 `agentengine config show` 与 `agentengine config set KEY=VALUE...`，补齐非交互式配置管理入口。
- 新增 `agentengine mcp build`，支持 MCP `Code` / `Container` 双制品构建。
- 新增 `agentengine openclaw channel` 与 `agentengine openclaw gateway` 命令组，补齐 OpenClaw 接入、排障和诊断入口。
- `agentengine dashboard open` 现可在 OpenClaw 工作目录中直接解析 `.agentengine.state`。
- 统一 build、deploy、launch 的输出层、dry-run 摘要、下一步提示和 `--no-cache` 语义。
- 优化非 TTY 与 JSON 场景输出，破坏性命令统一收口到 `--yes/-y`。
- 默认模型、示例和模板切换到 `glm-5.1`。
- 简化 `init -f openclaw` 模板，并改进 `zsh`、`bash`、Git Bash、WSL 下的补全安装体验。
- 更新默认 OpenClaw 镜像、预置插件、预置技能和搜索默认策略，适配当前 x86 Serverless 运行环境。
- MCP deploy / update 请求体改为更完整透传服务端嵌套 schema 以及显式 `Code` / `Container` 字段。

### 修复

- 修复部分 code mode 部署链路中 `ks3_path` 元数据不稳定的问题。
- 修复本地 `.agentengine.state` 过早清理的问题，现仅在远端删除成功后清理。
- 修复 `GetAgent` 的兼容回退判断，避免将正常 `404` 误判为协议兼容问题。
- 修复 `web.login.wait` 与当前 OpenClaw gateway 协议的参数映射问题。
- 修复 fresh deploy 后 builtin browser / gateway loopback 调用错误携带 device identity，导致误触发 pairing 的问题。
- 修复微信登录结果未正确映射 `sessionKey`，导致扫码成功后等待链路不兼容的问题。
- 修复默认插件同步过程中的 bundled 源目录漂移、目录权限、无效 checksum 和冷启动开销问题。
- 收敛 OpenClaw 兼容补丁面，移除 `openclaw-weixin@1.0.3` 的旧兼容逻辑，仅保留官方 `2.1.7+` 所需的最小 shim。
- 镜像补装 `jq` 并加入 strict-mode allowlist，同时更新推荐的可选 `multi-search-engine` 路径。

## [0.3.5] - 2026-03-12

### 变更

- 统一 Dashboard 异常输出与 `404` 处理逻辑，降低排障成本。
- Agent 列表查询新增分页与筛选参数，改善大规模实例场景下的可用性。
- OpenClaw 镜像构建改为参数化配置，支持自定义基础镜像、镜像标签和依赖源。
- 补充 OpenClaw 相关文档与预设能力，降低接入门槛。

## [0.3.0] - 2026-03-06

### 破坏性变更

- 控制面创建接口从 `CreateAgent` 迁移到 `CreateAgentProduct(AutoPay)`，CLI 与服务端需要同步升级。

### 变更

- 新增 DeepAgents 框架识别以及构建、部署支持。
- 新增 ADK 长短期记忆集成，并支持通过环境变量完成运行时注入。
- 新增 ADK 与 LangChain 可用的知识库工具链。
- 新增 `agentengine version` 版本管理命令组。
- `agentengine mcp deploy` 新增 `--artifact-type`，支持 `Code` / `Container` 双模式发布。
- 新增统一的 `agentengine dashboard` 与 dashboard share/revoke 流程，用于 hosted Web UI 访问。
- 打通 hosted 原生 Web UI 与 WebSocket 网关访问链路。
- 新增 OpenClaw 一键部署，自动补齐默认镜像与关键运行时环境变量。
- 优化跨平台构建部署兼容性、缓存控制和 container 构建编排。
- 简化 TUI 交互并统一 CLI 输出、帮助文案和示例。
- 统一 Agent 引用解析与默认状态解析逻辑。
- 公共客户端层统一 `snake_case` 字段转换，并将鉴权路由从 `kmr` 切换到 `aicp`。
- 新增 `kb` 可选依赖组，并将知识库能力合并进 `all` extras。

### 修复

- 修复代码包路径和部署轮询问题，提升回滚与 endpoint 回填稳定性。
- 修复 MCP deploy API 参数、bucket 配置、serverless 驼峰字段响应和空响应处理问题。
- 修复 KOP `2024-06-12` 版本兼容性问题。
- 补充 `openclaw deploy` 结束后的后续操作提示。

## [0.2.0] - 2026-01-22

### 破坏性变更

- `v0.2.0` 起 CLI/SDK 需要搭配 `AgentEngine Server v0.2.0+` 使用。
- 环境变量 `MODEL_NAME` 重命名为 `OPENAI_MODEL_NAME`，`OPENAI_API_BASE` 重命名为 `OPENAI_BASE_URL`。

### 变更

- 底层架构切换为 AgentEngine Server 承载，并引入统一的 `AgentEngineClient`。
- 新增本地部署状态文件 `.agentengine.state`。
- 新增 `agentengine model`，支持从 OpenAI 兼容接口交互式选择模型。
- runner 输出新增 thinking / reasoning 流式渲染支持。
- `agentengine invoke` 新增 Markdown 实时渲染。
- 新增预览版 MCP Server 管理命令与构建能力。
- 新增预览版 Memory SDK，支持可插拔存储后端。
- 本地 Web UI 品牌统一更新。
- 客户端与服务端 API 从 query-style action 迁移为 REST path 风格。
- 新增全局配置文件 `~/.agentengine/settings.json`。
- 统一核心环境变量命名，并统一租户标识为 `account_id`。
- 强化 serverless 部署诊断能力，包括 KS3 bucket 推导与副本状态展示。
- 优化 Langfuse 可观测性输出与 CLI 交互体验。
- 新增 Python `3.13` 与 `3.14` 支持。

### 修复

- 修复 Windows 离线安装时核心依赖缺失的问题。
- 修复 Windows BOM 文件兼容性，统一按 `utf-8-sig` 读取配置。
- 修复 Web UI 构建阶段 Google Fonts 资源导致的失败问题。
- 修复预发与生产 serverless 客户端的环境路由问题。
- 为 `fastapi` 与 `pydantic` 增加兼容性版本上限约束。

## [0.1.0] - 2026-01-15

### 变更

- 发布 `agentengine` CLI 初始版本。
- 提供 `create`、`build`、`deploy`、`run`、`status`、`destroy`、`invoke`、`config`、`web`、`launch` 等生命周期命令。
- 支持本地 Docker 与云端 Serverless 两种部署模式。
- 通过 `BaseRunner` 与 `UnifiedRunner` 原生支持 LangGraph、LangChain 和 Google ADK。
- 提供 `code` 与 `container` 两种构建模式，并支持自动依赖分析与打包。
- 提供本地 Web UI 用于 Agent 调试和管理。
- 集成 KS3 制品上传与分发、Langfuse 和 OpenTelemetry tracing。
- 支持 `agentengine.yaml` / `ksadk.yaml` 项目配置文件与 `.env` 加载。
