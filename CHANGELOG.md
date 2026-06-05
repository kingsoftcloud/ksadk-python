# 更新日志

本文件记录 **Kingsoft AgentEngine SDK (ksadk)** 的重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)，
版本遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## [0.6.2] - 2026-06-04

### 亮点

- **Skill Runtime 重构**：补齐 Skill Space 远端发现、按需下载、`sha256` 校验、安全解压、instruction-first 加载和 workflow 型隔离执行链路，支持 `local_process` 与 E2B backend。
- **内置 Toolset 渐进式披露**：新增 `get_agentengine_tools(include=[...])` / `describe_agentengine_tools(include=[...])` 的 profile 与工具名选择能力，推荐示例默认使用 `focused + agentengine_tool_dispatcher`，避免每轮上下文暴露所有低频或高风险工具。
- **Tool Gateway 与人工确认语义**：新增统一 Tool Gateway，Workspace 写入/删除、Skill Runtime 执行、sandbox 命令/代码执行等中高风险工具可在 strict 模式返回 `approval_required`，便于 Hosted/local UI 接入人工确认。
- **Workspace 与 Sandbox 内置工具增强**：新增 Workspace 精确片段编辑、轻量 lint、sandbox direct `run_command` / `run_code`，并统一限制在 AgentEngine workspace 或 isolated sandbox backend 边界内。
- **OTel-first 可观测配置**：`setup_tracing()` 优先识别标准 `OTEL_EXPORTER_OTLP_*` HTTP traces 环境变量，业务代码可以只写 OpenTelemetry spans、events 和 attributes，再由后端路由到 Langfuse 或其他 OTLP Collector。

### 变更

- 新增 `ksadk.toolsets` 内置工具入口：`get_skill_tools()`、`get_workspace_tools()`、`get_platform_tools()`、`get_sandbox_tools()` 和聚合入口 `get_agentengine_tools()`。
- `get_agentengine_tools()` 无参保持全量工具兼容；新增 `include=["skill"|"workspace"|"platform"|"sandbox"]`、`include=["focused"]` / `include=["core"]`、以及 `include=["focused", "run_code"]` 这类按具体工具名扩展的选择方式。
- `focused/core` profile 默认只直接暴露 `list_skills`、`search_skills`、`load_skill`、`workspace_status`、`search_workspace_files`、`edit_workspace_file`、`lint_workspace_file`、`component_status`、`sandbox_status`。
- 新增 `agentengine_tool_dispatcher(action, tool_name=None, arguments=None, include=None)`，支持 `list` / `describe` / `call` KsADK 本地内置工具；dispatcher 不接远端 Tool Space 数据库，也不会递归调用自身。
- 新增 `describe_agentengine_tools()`，返回工具分组、描述、风险等级、审批需求、side effects、backend/boundary 等元信息，供 Agent Studio、demo、UI 或调试诊断展示。
- 新增 `list_skills`、`search_skills`、`load_skill`、`execute_skills`，支持按 Skill Space 查询、按 name/alias/tag/description/examples 匹配、下载并读取 `SKILL.md`，以及通过 Skill Runtime 执行 workflow。
- Skill Runtime 请求协议新增 `--request-file` JSON envelope，携带 `workflow_prompt` 和 `skill_names`；保留 `--prompt-file` 兼容，但二者不能同时使用。
- Runtime agent 改为按显式 `skill_names` 或 prompt 命中的技能元数据下载所需 Skill，不再默认拉取同一空间下全部 active Skill。
- 新增公共 Skill Space 追加机制：`KSADK_PUBLIC_SKILL_SPACE_IDS` 会追加在用户 `KSADK_SKILL_SPACE_IDS` / `SKILL_SPACE_ID` 之后，`KSADK_PUBLIC_SKILL_ALLOWLIST` 可限制公共/预置 Skill。
- Skill Service 地址解析支持 `KSADK_SKILL_SERVICE_URL`，也支持按 `KSADK_AICP_ENDPOINT_MODE`、`KSADK_SKILL_SERVICE_ENDPOINT`、`KSADK_SKILL_SERVICE_SCHEME` 自动选择内外网 AICP endpoint。
- 新增通用 sandbox 抽象与 E2B backend，优先读取 `KSADK_SANDBOX_TEMPLATE_ID`、`KSADK_SANDBOX_TIMEOUT`、`KSADK_SANDBOX_ALLOW_INTERNET_ACCESS`，兼容旧的 `KSADK_SKILL_RUNTIME_*` 变量。
- 新增 sandbox direct tools：`sandbox_status`、`run_command`、`run_code`；命令和代码只通过 configured isolated sandbox backend 执行，不退化为宿主机 shell。
- Workspace toolset 新增 `workspace_status`、`list_workspace_files`、`read_workspace_file`、`write_workspace_file`、`write_workspace_files`、`edit_workspace_file`、`lint_workspace_file`、`search_workspace_files`、`delete_workspace_file`。
- `edit_workspace_file` 支持 exact snippet replacement，并在未命中或匹配次数不符合预期时返回 `snippet_not_found` / `ambiguous_edit`；`lint_workspace_file` 支持 Python AST、JSON parse 和通用文本轻量检查。
- ADK Runner、LangGraph Runner 和 DeepAgents Runner 示例/测试接入 Skill Runtime 或 toolset 注入路径；LangGraph demo 默认改为 `focused + agentengine_tool_dispatcher` 绑定方式，并保留业务自定义 tool 与 graph node 示例。
- `component_status` 展示模型、知识库、长期记忆、Skill Space、Skill Runtime、sandbox 和 Workspace 绑定状态，帮助区分“已绑定”“可发现”“隔离执行已启用”等边界。
- 新增 `OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`、`OTEL_EXPORTER_OTLP_PROTOCOL`、`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`、`OTEL_EXPORTER_OTLP_HEADERS` 和 `OTEL_EXPORTER_OTLP_TRACES_HEADERS` 的自动 HTTP traces exporter 支持。
- 当只设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 时，KsADK 会派生 `/v1/traces` 作为 traces endpoint；显式 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 优先。
- HTTP headers 支持标准 OTLP 逗号分隔格式，并对 header value 做 URL decode，例如 `Authorization=Bearer%20token`。
- `OTEL_EXPORTER_OTLP_TRACES_*` 配置优先于通用 `OTEL_EXPORTER_OTLP_*` 配置；CLI / runtime template 在只配置 OTLP 环境变量时也会初始化 tracing。
- 环境变量 registry 补齐 AICP endpoint mode、Skill Service endpoint/scheme、Sandbox、Skill Runtime、Artifact 和 OTel 相关变量登记。

### 修复

- 修复 Skill Runtime 长 prompt 通过 shell quoting 传递时不稳定的问题，改为写入 `/tmp/ksadk-workflow-request.json` 后由 runtime agent 读取。
- 修复 E2B Skill Runtime 错误信息可能泄漏 `E2B_API_KEY`、Skill Service token 或 secret 的问题，异常回传会做敏感值 redaction。
- 修复 E2B session 未稳定清理的问题，workflow 执行结束或异常后都会尝试 kill sandbox。
- 修复 public Skill Space 与用户 Skill Space 混用时的去重和 allowlist 边界，避免重复下载或加载非预期公共 Skill。
- 修复 workspace 编辑能力只能整文件覆盖的问题，新增片段级替换和轻量 lint 以降低常见代码/文本改动风险。
- 修复高风险工具直接执行缺少统一审批 envelope 的问题，Tool Gateway strict 模式下会阻止执行并返回 `approval_required`。
- 修复内置工具全量绑定导致 LangGraph demo 上下文过大的问题，默认改为 focused 工具加 dispatcher 渐进式披露。

### 兼容性说明

- `get_agentengine_tools()` 无参仍返回全量内置工具，避免破坏已有 LangGraph/LangChain/DeepAgents 项目；新示例推荐显式使用 `include=["focused", "agentengine_tool_dispatcher"]`。
- `execute_skills`、`run_command`、`run_code`、Workspace 写入/删除等能力仍可显式绑定或通过 dispatcher 调用；dispatcher 调用真实工具对象，不绕过 Tool Gateway 审批策略。
- 当前 dispatcher v1 只调度 KsADK 本地内置工具，不连接控制台 Tool Space、数据库动态工具绑定或远端 Tool Gateway 目录；这些属于后续控制面能力。
- Sandbox 新部署优先使用 `KSADK_SANDBOX_*` 通用变量；`KSADK_SKILL_RUNTIME_TEMPLATE_ID`、`KSADK_SKILL_RUNTIME_TIMEOUT`、`KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS` 继续作为兼容变量保留。
- Skill Runtime 默认 backend 仍为 disabled；未显式设置 `KSADK_SKILL_RUNTIME_BACKEND` 但存在 `KSADK_SANDBOX_TEMPLATE_ID` 时会自动走 E2B。
- 显式传入 `setup_tracing(enable_langfuse=True)` 仍可强制启用 Langfuse 兼容路径；`LANGFUSE_USE_CALLBACK=true` 仍用于 LangChain / LangGraph callback-only 模式，避免 callback 与 direct OTLP 双写。
- OTel attributes 中的 `score.*` 字段只是后端无关的推荐表达，不直接依赖 Langfuse SDK，也不承诺所有后端都会自动显示为 native score。

## [0.6.1] - 2026-05-28

### 变更

- 将首个公开候选版本调整为 `0.6.1`，用于 GitHub release、PyPI 包元数据和 GitHub Pages 文档站对齐。
- README 中的 Web UI 链接改为中性“Web UI repository / Web UI 仓库”表述，避免在项目首页使用“真源”作为对外标签。
- README 保留安装、文档、仓库和运行时说明；凭证、kubeconfig、私有 registry 等审计边界继续放在安全策略、贡献指南和发布检查清单中维护。
- 公开文档补充双语框架接入与工具/Skill Runtime 页面，增加检测到 runner、ADK 工具注入和 Skill Runtime backend 的 Mermaid 架构图。

## [0.6.0] - 2026-05-27

### 亮点

- **OpenAI 输入语义对齐**：`/v1/responses` 和 `/v1/chat/completions` 对外继续保持各自协议语义，内部 runner 统一消费 Responses-style canonical 输入，并保留 legacy 输入兼容。
- **多模态会话体验修复**：Hosted UI、本地 `agentengine web` 和 `RunAgent` 对图片/文件上传统一使用 Responses content，刷新后可以正确回显附件并继续多轮追问。
- **流式会话恢复**：本地 UI 运行改为后台 detached run，刷新页面或 SSE 断开后可按同一 session / invocation 重新订阅后续事件。
- **本地 Web UI 体验收敛**：默认 sqlite session、低干扰运行状态胶囊、Workspace 预览刷新保护和会话切换行为一起收敛到更稳定的本地调试体验。

### 变更

- `/v1/responses` 对外按 OpenAI Responses 语义接收 `input_text`、`input_image`、`input_file`，内部生成 `input_content`、`input_messages` 和 legacy `input_parts`。
- `/v1/chat/completions` 保持 Chat Completions 对外语义，内部转换到 runner canonical 输入；Chat 图片块 `text` / `image_url` 会转换为 `input_text` / `input_image`。
- 非官方 Chat 文件形态只作为兼容扩展处理，不在文档中伪装成 OpenAI Chat Completions 官方能力。
- `RunAgent` 普通运行支持 `ResponsesInput`，不再只在 resume / approval 场景消费 Responses-style input item。
- LangGraph runner 与 `RuntimeContext` 透传 `input_content`、`input_messages`、`input_parts`、`current_attachments`、`current_attachment_results`、`has_current_files`，并保持旧字段兼容。
- Conversation runtime 在 `KSADK_LTM_BACKEND=sdk` 且 `KSADK_LTM_NAMESPACE` 存在时默认开启 `KSADK_LTM_AUTO_SAVE`，每轮完成后 best-effort 写入 user/assistant 文本及 `agent_id/session_id/invocation_id/model/runner_type` metadata；图片和文件只写附件摘要。
- 本地 UI 新增 `SubscribeRunEvents` action，用于按 session / invocation 从持久化事件流中恢复订阅；该能力不改变 `/v1/responses` 和 `/v1/chat/completions` 的 OpenAI-compatible 对外协议。
- `agentengine web` 在用户未显式配置 STM 时，默认给 LangGraph、LangChain、DeepAgents、ADK 启用项目级 sqlite session，路径为 `.agentengine/ui/sessions.sqlite`。
- 本地 Web UI static 同步逻辑改为先清空 `ksadk/server/static` 再复制当前构建产物，避免旧 chunk 残留造成 UI 行为漂移。
- `ksadk/server/web-ui` 和 `ksadk/server/static` 同步自 `agentengine-hosted-ui`，并新增同步契约测试，明确 Hosted UI 是当前 UI 真源。
- OpenClaw 默认 runtime 更新到 `2026.5.22`，同步 CLI、Makefile、Dockerfile、用户自定义镜像模板、控制面 bootstrap 默认值和测试断言。
- 文档补充 OpenAI 双协议边界、runner canonical 输入字段、KsADK 扩展字段，以及如何判断当前轮是否包含文件。
- 新增 ksadk 开源准备计划文档，梳理未来拆出中性 UI core、Hosted UI 真源和本地 static 派生产物的演进方向。

### 修复

- 修复 `inlineData` 图片上传后 `current_attachments` 为空，且 `attachments` 也没有值的问题。
- 修复 Responses `input_image.image_url` data URL 未稳定进入 runner payload 和会话事件回放的问题。
- 修复 Responses `input_file.file_data` / `file_url` 在会话回放中无法还原为附件展示的问题。
- 修复 conversation runtime 落库时只保存 display 文本和附件提示，导致刷新后图片变成纯文本占位的问题。
- 修复 Hosted UI 回放事件时未识别 Responses `input_file.file_data` / `input_file.file_url` 的问题。
- 修复 server responses session mirror 在 `account_id` 为空或 PostgreSQL duplicate session 错误文本变化时可能失败，导致预发 Hosted UI 上传图片后报“连接断开或生成出错”的问题。
- 修复刷新正在流式输出的会话时，订阅增量事件被单独构建成多条空“思考过程”消息的问题；恢复路径现在先合并完整 session events，再重建消息列表。
- 修复会话列表重复项、活动 invocation 判定过早失效、运行中会话锁住其他 session 切换等 UI 状态问题。
- 修复 Workspace 文件列表自动刷新时把当前 Markdown/HTML/文本预览强制切回编辑态的问题。
- 修复本地 web Ctrl+C 退出时暴露 Python threading shutdown 栈的问题。
- 修复 lazy chunk hash 变化后旧页面请求历史 `CodeBlock-*.js` 404 时不能自动恢复的问题。
- 修复本地 static 构建产物残留导致 `ksadk/server/static` 与 `ksadk/server/web-ui/dist` 不一致的问题。

### 兼容性说明

- OpenAI 官方字段语义保持不混写：`/v1/responses` 与 `/v1/chat/completions` 对外仍分别遵循各自协议；`attachments`、`current_attachments`、`has_current_files` 等字段仍是 KsADK runner 扩展，不属于 OpenAI 官方字段。
- `input_parts`、legacy `Messages + inlineData/fileData`、历史附件 fallback 均继续保留，避免现有业务 runner 和老客户端直接断裂。
- 非图片文件在 Hosted UI 中优先走上传引用并生成 `input_file.file_url`；Responses API 仍支持 `input_file.file_data` 作为内联文件内容。
- `KSADK_LTM_AUTO_SAVE` 是 KsADK 平台增强能力，不改变 OpenAI Responses / Chat Completions 对外协议语义；自动保存失败只记录 warning，不影响主回复。

## [0.5.9] - 2026-05-25

### 亮点

- **Responses API 官方会话语义对齐**：`/v1/responses` 支持 OpenAI Responses API 的 `conversation`、`safety_identifier`、`prompt_cache_key`、`user`、`store` 字段，并按官方语义把 `conversation` 映射为内部 session，把 `safety_identifier` 映射为最终用户标识。
- **Langfuse UserID / SessionID 修复**：直连 `/v1/responses` 时不再固定写入 `user`，Langfuse trace 会优先使用 `safety_identifier` 和 `conversation`，并兼容新旧 span attribute key。
- **运行时依赖与记忆后端兼容增强**：code mode 默认依赖升级到新版 `kingsoftcloud-sdk-python`，补齐 `ksadk_runtime_common` 记忆后端渲染和注册能力，改善 ADK memory / SDK LTM 在运行时镜像中的可用性。
- **文件上下文当前轮语义优化**：runner payload 增加 `has_current_files`、`current_attachments`、`current_attachment_results`，把“当前最新 user turn 是否带文件”和“历史有效附件 fallback”明确拆开，避免业务 agent 在追问轮次误判仍有新文件上传。
- **Skill Runtime 渐进式披露优化**：ADK 注入 Skill Runtime 时先只把远端技能 manifest 写入 agent instruction，引导模型按需调用 `execute_skills(..., skill_names=[...])`；runtime agent 再按显式 `skill_names` 或 prompt 命中的技能名下载并加载对应技能，避免每轮对话暴露完整技能包或拉取同一空间下全部 active 技能。

### 变更

- `/v1/responses` 在同时传入 `conversation` 与 legacy `session_id` 且不一致时返回 `400`，避免双会话语义冲突。
- `/v1/responses` 在同时传入 `conversation` 与 `previous_response_id` 时返回 `400`，与官方二选一语义保持一致。
- Runtime 接口文档补充 `/v1/responses` 推荐字段、legacy `session_id` 边界，以及 Hosted UI Action 与 OpenAI-compatible endpoint 的会话语义差异。
- Runner 输入、LangGraph state 和 `RuntimeContext` 透传当前轮附件字段：`has_current_files`、`current_attachments`、`current_attachment_results`；原 `attachments` / `attachment_results` 继续表示最近有效附件上下文，保持历史 fallback 兼容。
- OpenClaw bootstrap 支持通过 memory backend patch 禁用指定插件并清理插件 slot，避免 memory 配置切换后遗留旧插件配置。
- 新增 `KSADK_PUBLIC_SKILL_SPACE_IDS` 环境变量，公共 Skill Space 会追加在用户 `KSADK_SKILL_SPACE_IDS` / `SKILL_SPACE_ID` 之后，并保持去重顺序。
- `execute_skills` 新增可选 `skill_names` 参数，本地进程和 E2B Skill Runtime 后端会通过 `KSADK_SELECTED_SKILL_NAMES` 传给 runtime agent，实现远端技能按需加载。
- 开发测试依赖补充 `fastmcp`，`uv sync --extra all` 后可直接运行 MCP runtime e2e 测试。
- `agentengine openclaw deploy` 未显式传 `--name` 时优先复用本地 state 或 init 配置中的 OpenClaw 项目名，避免重复部署时生成新的随机名称。
- `agentengine agent list` 默认隐藏 OpenClaw / Hermes 专用框架实例，显式传入框架筛选时仍可查看。

### 修复

- 修复 `/v1/responses` 直连 runtime 时 Langfuse UserID 固定显示为 `user`，导致不同最终用户无法区分的问题。
- 修复 Langfuse exporter 只读取旧版 `langfuse.user_id` / `langfuse.session_id` key，导致新版 span attribute 下用户和会话信息丢失的问题。
- 修复 runtime memory backend 渲染缺少必要注册信息时，部署后可能出现 `ksadk_runtime_common` 记忆后端不可用的问题。
- 修复多轮对话中 `attachments` 使用历史 fallback 时，业务代码无法直接判断当前轮是否真正上传文件的问题。
- 修复本地 loopback MCP / FastMCP e2e 在开发机配置 `HTTP_PROXY` 时，请求被代理到本机代理端口而不是临时 MCP server 的问题；远程 MCP URL 仍保留系统代理行为。
- 修复 OpenClaw secretRef / memory backend 组合下旧插件配置不能被显式关闭的边界问题。
- 修复 OpenClaw bootstrap 测试从开发机环境继承 Langfuse / OTEL 变量后出现非确定性失败的问题。

## [0.5.8] - 2026-05-22

### 亮点

- **Hosted UI 架构重构**：重构 Web UI 的 API facade、RunEngine 状态机、SSE transport、stream protocol、capability plugin、hooks 与 Zustand stores，降低 `App.tsx` 复杂度，并为长对话、工具调用、工作区、Artifact 和原生 TUI 留出更清晰的扩展边界。
- **工作区与 HTML 预览体验升级**：工作区文件支持路径化访问、同级资源预览、Markdown / HTML / 图片 / PDF 等内容预览、Artifact 面板、zip 导出和更紧凑的文件操作布局；本地 `agentengine web` 与生产 hosted UI 的访问路径语义进一步对齐。
- **Hermes / OpenClaw 远程 TUI 稳定性增强**：托管 runtime 支持终端 WebSocket keepalive，Windows raw terminal mode 增加 ANSI OSC 过滤和 raw mode 保护，减少远程 TUI 空闲或长任务时断连、乱码和输入异常。
- **默认运行时刷新**：Hermes 默认 runtime 更新到 `2026.5.16-ksadk-v1`，OpenClaw 默认 runtime 更新到 `2026.5.20`，并同步 CLI、Makefile、Dockerfile、用户自定义镜像模板和测试断言。

### 变更

- Hosted UI 前端抽出 `src/core/api`、`src/core/run`、`src/core/stream`、`src/core/transport`、`src/core/capability` 等核心层，组件侧统一通过连接组件、hooks 和 stores 消费状态，不再把 action URL 与流式解析散落在页面组件内。
- MessageMarkdown 拆分出代码块、Mermaid、KaTeX 等 lazy chunk，新增前端契约测试、run engine 测试、SSE parser 测试、workspace 测试和 plugin registry 测试。
- Hosted UI 运行状态新增更细的 `creating-session`、`uploading-files`、`connecting`、`streaming`、`completing`、`recovering`、`error` 阶段，并支持服务端 ping / tick 更新连接活跃时间。
- `RunAgent` 增加停止接收与远端取消运行的边界区分；后端 runner 新增可选 `request_cancel()` contract，本地 server 增加取消运行 action 与 SSE heartbeat 注释帧。
- HTML / Artifact 预览改用更明确的 sandbox 与 CSP 策略；链接导航和 iframe 事件通过 `postMessage` 通道处理，避免父页面直接读取跨源 iframe DOM。
- 工作区文件服务新增预览类型识别、路径化文件读取和 sibling resource 解析，便于 agent 生成的 HTML 使用相对资源。
- Dashboard private 链接服务端默认有效期调整为 24 小时；CLI 在未显式传入 `--expires-seconds` 时交给服务端默认处理，以兼容尚未升级的控制面，OpenClaw / Hermes 默认仍进入 `/chat`。
- `agentengine create` 的快速开始提示按 Windows PowerShell / cmd.exe 与 POSIX shell 分别生成可复制命令，包含空格的项目目录会被正确 quoting。
- Docker daemon 未运行时，Windows 环境提示用户启动 Docker Desktop，不再输出 Linux `systemctl` 提示。
- Hermes 默认镜像更新为 `ghcr.io/kingsoftcloud/hermes-agent:2026.5.16-ksadk-v1`，上游 Hermes ref 保持 `v2026.5.16`。
- OpenClaw 默认镜像更新为 `ghcr.io/kingsoftcloud/openclaw:2026.5.20`，基础镜像 pin 到 `ghcr.io/openclaw/openclaw:2026.5.20-slim@sha256:db199be23add581ef18ca8c8a866af84db13586d5bfcd566c8ac73d8d106eebb`。
- `deploy/openclaw-user-template` 及示例模板默认基础镜像同步升级到 OpenClaw `2026.5.20-slim`。
- 文档补充 hosted UI 生产路由与独立部署说明；`ksadk-python` 继续保留本地 SDK UI 静态资源，生产 hosted UI 由独立服务发布。

### 修复

- 修复 Hosted UI 刷新后运行状态残留、停止接收无反馈、SSE 空闲期间误判超时、Hermes / OpenClaw 长响应缺少中间状态反馈等问题。
- 修复工作区空目录删除、文件夹删除路径归一化、HTML 相对资源预览、新窗口打开 workspace 页面触发下载或访问错误的问题。
- 修复 HTML 预览安全策略中过度放开图片外链或过度收紧导致 agent 生成页面交互不可用的边界问题。
- 修复 CLI 彩色 help 行列对齐问题，避免客户端与终端 help 文本在中文宽度下错位。
- 修复 code mode 依赖安装前环境缺少 `pip` 时不能自动 bootstrap 的问题。
- 修复 Hermes 冷启动时 runtime 目录、安全默认值和 trace 依赖初始化不完整的问题。
- 修复 Windows TUI 复制时误用 OSC52、远程 raw terminal 收到 OSC 背景色序列后残留乱码的问题。

## [0.5.7] - 2026-05-19

### 亮点

- **OpenClaw 默认运行时升级**：默认 OpenClaw 镜像升级到 `ghcr.io/openclaw/openclaw:2026.5.18-slim@sha256:5ea30d02a706c49795ed0a3c1526dec51ed90107a6859e93bf27a663105d1c28`，并适配新版 gateway dist 补丁能力扫描。
- **Hermes 默认运行时升级**：默认 Hermes 上游 ref 与镜像版本升级到 `v2026.5.16` / `2026.5.16`，补齐 OTel / Langfuse 相关运行时依赖与环境变量透传。
- **CLI 入口体验统一**：Hermes / OpenClaw 的状态、打开页面、TUI 入口和浏览器打开行为更一致，减少本地状态文件类型差异带来的误判。

### 变更

- OpenClaw 默认镜像构建改用新版 slim digest；WPS 协作插件改为通过 npm 包 `@wps365/openclaw-wpsxiezuo` 安装，不再携带本地 `openclaw-wps-xiezuo-1.6.0.tgz`。
- OpenClaw 插件预置支持默认全量安装，也可继续通过 `OPENCLAW_PRESET_PLUGINS_ALLOWLIST` 收窄预置插件集合。
- OpenClaw bootstrap 新增 2026.5.18 gateway bundle 结构对应的 `backend-self-pairing` 与 `trusted-proxy-loopback` dist patch 变体。
- Hermes runtime 启动时会在检测到 Langfuse 地址与密钥后尝试开启 trace，未提供配置时保持无凭证启动。
- `agentengine status` 可识别 `.agentengine.state` 中的 OpenClaw 类型并走对应状态查询；Hermes TUI 入口前增加状态预热，降低首次进入远端 TUI 卡住的概率。
- Hermes / OpenClaw status 统一展示 Langfuse trace 地址；`agentengine dashboard open` 默认进入 `/chat`，管理 UI 仍可通过显式 `--path` 打开。
- `agentengine web` 默认自动打开浏览器，新增并保留 `--no-open` 用于只打印 URL。
- 构建链路抽出框架依赖窗口，统一 code builder、container builder 与 deploy manager 对 ADK / LangChain / LangGraph / DeepAgents 的默认依赖补齐逻辑。

### 修复

- 修复新版 OpenClaw 上游 bundle 结构变化导致必需 dist 补丁能力未命中、镜像构建失败的问题。
- 修复 OpenClaw 工作目录下 `agentengine status` 不能自动从 `.agentengine.state` 解析 OpenClaw Agent 的问题。
- 修复 dashboard / web / status 等 CLI 入口在 Hermes 与 OpenClaw 场景下默认页面、浏览器打开策略和下一步提示不一致的问题。

## [0.5.6] - 2026-05-18

### 亮点

- **Skill Runtime / Sandbox 集成预览**：新增面向 Skill Center 的运行时消费链路，支持从 Skill Space 发现技能、按需下载技能包、校验内容哈希并加载执行。
- **通用 Sandbox 底座预览**：新增 `ksadk.sandbox` 通用抽象和 E2B backend，Skill Runtime 通过通用 sandbox session 运行，后续可扩展 Code / Browser / Private 等模板类型。
- **CLI 支持 VPC 网络参数**：`agentengine deploy`、`agentengine launch`、`agentengine openclaw deploy` 在创建和更新 Agent 时可显式传入公网 / VPC 网络配置。

### 变更

- 新增 `ksadk[skills]` extra，首版包含 E2B backend 所需依赖；沙箱团队可基于该 extra 构建 Skill Runtime AIO 镜像。
- 新增 `ksadk.skills` 与 `ksadk.skills.runtime`，包含 Skill Service client、技能包缓存、安全解压、`sha256` 校验、技能 loader、`execute_skills` 工具和镜像内最小 agent 入口。
- 新增 `deploy/skill-runtime/` 镜像交付物，约定镜像内路径 `/home/ksadk/agent.py`，优先通过 E2B SDK 连接控制台创建的 AIO 模板。
- ADK Runner 支持 Skill Runtime 自动发现：本地模式注入技能工具，sandbox 模式只注入 `execute_skills`，并识别通用 `KSADK_SANDBOX_*` 与兼容 `KSADK_SKILL_RUNTIME_*` 环境变量。
- `agentengine deploy` 与 `agentengine launch` 新增 `--enable-public-access / --disable-public-access`、`--enable-vpc-access`、`--vpc-id`、`--subnet-id`、`--security-group-id`、`--availability-zone`。
- `agentengine openclaw deploy` 新增同一组 network 参数，创建和更新 OpenClaw Agent 时都会传入 `network` payload。
- 配置文件继续支持顶层 `network` 与 `deploy.network`，CLI 显式参数优先于配置文件。
- 开启 VPC 访问或传入任一 VPC ID 字段时，CLI 会校验 `VpcId`、`SubnetId`、`SecurityGroupId` 必须同时存在；`AvailabilityZone` 为可选字段。

### 说明

- 这是 Skill Runtime / Sandbox 的集成预览版。当前保证本地 runtime pod 内 Skill Center 消费链路、sandbox backend 基础逻辑、镜像交付物和 CLI network 参数可联调；沙箱团队基于 `0.5.6` 构建 AIO template 后继续做完整业务 E2E，后续修复进入 `0.5.7`。
- Skill Center `ContentHash` 校验保持 fail closed：服务端返回的 `sha256` 与实际 zip 不一致时，KsADK 会拒绝加载该技能包。
- E2B backend 仅使用 SDK 原生 `E2B_API_URL` / `E2B_API_KEY` 环境变量，不把凭证写入代码、文档示例、测试 fixture 或日志。

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
- 默认 Hermes 共享 runtime 镜像更新为 `ghcr.io/kingsoftcloud/hermes-agent:2026.4.23`，并把构建默认 `HERMES_AGENT_REF` 同步到上游 `v2026.4.23`。
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
