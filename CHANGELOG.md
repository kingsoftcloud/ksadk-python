# Changelog

All notable changes to the **Kingsoft AgentEngine SDK (ksadk)** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.6] - 2026-03-24

### 重点主题

- CLI 平台化继续收口，`agent` 资源组、共享渲染/错误提示层、`config show/set`、`--output json` 与双层 dry-run 计划正式成为 `0.3.6` 的主体验。
- OpenClaw 从“能部署”升级到“可接入、可诊断、可维护”：新增 `channel` / `gateway` 统一入口，默认镜像、预置插件、技能与运行时补丁策略同步升级。
- 配置、模板和 shell 补全一并做了默认值与非交互体验优化，首批文档也统一到了新的命令模型。

### CLI 平台化与工作流

- 资源命令与工作流命令统一支持 `--output pretty|json`，为人类用户和 AI Agent 提供稳定双表面。
- 新增结构化 JSON envelope，用于 `list/status/result/dry_run/error` 场景，降低自动化解析成本。
- 新增 `agentengine config show` 与 `agentengine config set KEY=VALUE...` 非交互式入口；`agentengine config` / `agentengine config wizard` 明确为交互式向导路径。
- `deploy` / `launch` 的 dry-run 现同时输出本地执行计划与远端请求摘要，计划节点细化为 `local_build`、`artifact_publish`、`deploy_request`，并附带完整 `curl` 便于调试。
- 新增 canonical `agent` 资源组，统一 `agent list/status/invoke/delete` 语义；`mcp`、`openclaw`、`version`、`dashboard share` 接入共享渲染、错误提示与帮助文案层。
- `build` / `deploy` / `launch` 共享输出层、摘要风格、下一步提示与 dry-run 展示；`--no-cache` 与制品复用语义进一步统一，减少无意义 rebuild。
- 新增 `--no-color` 与非 TTY 感知，JSON / 非 TTY 场景下不再输出多余 Banner 与装饰；destructive 命令统一收敛到 `--yes/-y`，`--force/-f` / `destroy` 保留兼容但不再推荐。

### OpenClaw 统一入口与默认镜像

- 新增 `agentengine openclaw channel` 与 `agentengine openclaw gateway` 子命令组，支持 `channel status/connect/enable/disable/doctor` 与 `gateway open/ws-url/logs/doctor`。
- 微信接入支持本地终端 ASCII 二维码输出；飞书接入复用官方 onboarding 扫码流程。
- `agentengine dashboard open` 现支持在 OpenClaw 工作目录中直接读取 `.agentengine.state` 的 `openclaw` 类型实例，无需再显式传 `--agent`。
- 默认镜像预置 `openclaw-weixin`、`openclaw-lark`、`agent-browser` 与 `skillhub` CLI；默认 bundled skills 调整为 `skillhub-store`、`agent-browser-clawdbot`、`kdocs`。
- 启动阶段通过 `sync_default_extensions()` 将默认插件同步到挂载的 `~/.openclaw`，并保留用户手动升级后的漂移版本；旧默认技能迁移改为“仅清理此前由镜像同步且用户未改动的目录”。
- OpenClaw 镜像构建与默认技能策略更明确地面向 x86 Serverless 运行环境；默认搜索主路径恢复为原生内建 `browser`，`agent-browser` 与 `multi-search-engine` 分别作为增强自动化与轻量文本检索的可选路径。
- 微信插件默认版本切换到 `@tencent-weixin/openclaw-weixin@2.0.1`，直接跟随 npm 最新稳定版；Skillhub CLI 安装改为固定 tarball URL + SHA256 校验，减少构建期供应链漂移。

### 配置、模板与补全

- `ksadk-python` 通用配置默认模型、`agentengine config` 示例文案、`create` 模板代码与首批用户文档统一切换到 `glm-5`。
- `init -f openclaw` 生成的项目模板调整为更轻量的最小部署目录，更适合直接进入 `agentengine openclaw deploy`。
- `agentengine completion install --shell auto` 现支持更稳健地识别 `zsh` / `bash` / Git Bash / WSL 场景。
- `bash` 会按当前平台更合理地选择 `~/.bash_profile` 或 `~/.bashrc`，并自动清理旧的重复补全片段。

### 修复与稳定性

- 修复部分 code 模式部署中 `ks3_path` 元数据缺失或格式不稳定导致的后续部署失败问题。
- 删除 Agent 时仅在远端删除成功后清理对应本地 `.agentengine.state`，并支持显式项目目录语义。
- 校正 `from ksadk import Agent/Runner` 相关测试用例，使其与当前 `load_agent_module` / `create_runner` / `BaseRunner.run_server` 契约对齐；同步补齐 help snapshot、error hint snapshot、资源/工作流 JSON 契约回归测试。
- 修复 `web.login.wait` 与当前 OpenClaw gateway 协议的参数错位问题。
- 修复 fresh deploy 下内建 `browser` / gateway 本地 loopback 调用被误附带 device identity、从而在 `127.0.0.1` 仍触发 pairing 的问题；runtime dist patch 仅对齐当前 `2026.3.23-2` 及之后的 upstream 代码形态，降低镜像维护复杂度。
- CLI 现将微信登录返回的 `sessionKey` 映射到 gateway 兼容的等待参数，避免扫码成功后无法落库。
- 修复默认插件同步时因直接修改 bundled 源目录导致的签名漂移问题，以及 `node` 用户对默认扩展目录无读权限导致的启动期同步失败问题。
- 移除针对 `openclaw-weixin@1.0.3` 的旧版 `plugin-sdk` 导入重写与 runtime link 兼容逻辑；当前只对 `2.0.1` 保留一个最小 shim，用来补齐 `web.login.start/web.login.wait` 的 gateway methods 暴露。
- 镜像补装 `jq`，并在严格模式默认 allowlist 中放行 `jq`；同时更新可选的 `multi-search-engine` 预置 skill，避免继续推荐会触发 `curl: (23)` 的 `curl | head` 用法。
- `sync_default_extensions()` 改为优先复用镜像内嵌签名与 managed mtime 检测，避免每次启动都对大型插件目录全量 `cksum`，降低 channel 预置插件导致的冷启动超时风险。

## [0.3.5] - 2026-03-12

### 🛠 改进 (Improvements)

- **Dashboard 与状态查询链路优化**:
  - 统一 Dashboard 异常输出与 404 识别逻辑，降低排障成本。
  - Agent 列表查询支持分页与筛选参数，提升大规模实例场景下的可用性。
- **OpenClaw 构建与运维增强**:
  - `openclaw` 镜像构建改为参数化配置，支持自定义基础镜像、镜像标签与依赖源。
  - 增加 OpenClaw 相关文档与预设能力，便于快速落地与推广。

## [0.3.0] - 2026-03-06

### 🚀 新特性 (New Features)

- **DeepAgents 框架支持**:
  - 新增 `deepagents` 框架识别与初始化/构建/部署链路支持。
  - Code/Container 构建流程自动注入 `deepagents>=0.3.0` 相关依赖，减少手工配置成本。
- **ADK 长短期记忆体集成**:
  - 新增 `ksadk.memory.adk` 记忆模块，支持 ShortTermMemory / LongTermMemory。
  - 长期记忆支持 `local/http/sdk` 后端，Runner 可按环境变量自动初始化并注入 `load_memory` 工具。
  - 新增会话持久化接口 `save_memory`，支持将 session 内容写入长期记忆后端。
- **知识库集成能力**:
  - 新增 `ksadk.knowledge_base` 模块，提供 ADK/LangChain 可用的知识库检索工具。
  - 支持通过环境变量完成知识库配置，运行时可自动注入 `search_knowledge_base` 工具。
- **CLI 版本管理**:
  - 新增 `agentengine version` 命令组，支持版本发布、回滚、列表查看等流程。
- **MCP 双制品部署能力**:
  - `agentengine mcp deploy` 新增 `--artifact-type`，支持 `Code/Container` 双模式发布。
- **统一云端 UI 入口 (`agentengine dashboard`)**:
  - 新增统一命令 `agentengine dashboard [agent_ref]`，覆盖 OpenClaw 与普通 Agent Web UI。
  - 支持从 `.agentengine.state` 和项目配置自动解析目标 Agent。
  - 默认改为 `CreateDashboardAccessLink` 短链接模式（`/s/{link_id}`），不再默认暴露长 ticket URL。
  - 新增分享链接管理：`agentengine dashboard share list/revoke`。
- **网关 UI 访问能力重构**:
  - 此前网关主要支持携带 API-Key 调用 Agent Endpoint（API 场景）。
  - 本版本新增并打通 Agent 原生 Web UI 的云端访问链路（含 WebSocket 场景）。
- **OpenClaw 一键拉起**:
  - 默认镜像增强，免配置-默认内置SKILL & 浏览器工具 & 金山云星流模型服务 `hub.kce.ksyun.com/agentengine-public/openclaw:latest`。
  - 部署链路自动补齐 OpenClaw 关键环境变量（模型目录、AllowedOrigins 等）。

### 🛠 改进 (Improvements)

- **构建与部署体验增强**:
  - 优化跨平台构建/部署兼容性与交互体验。
  - 增强构建缓存控制能力与部署流程提示。
  - Container 模式部署链路优化，并统一由 `ContainerBuilder` 承载。
- **CLI/TUI 交互优化**:
  - 简化 TUI 交互，移除斜杠命令。
  - CLI 输出统一收敛到 `ui.py`，帮助与示例文案标准化。
  - 明确区分 `web`（本地调试 UI）与 `dashboard`（云端部署 UI）使用场景。
- **状态解析与引用解析增强**:
  - `status` 默认 Agent 解析和区域配置逻辑优化。
  - Agent 引用解析（ID/Name/state）逻辑统一。
- **协议与控制面适配增强**:
  - 客户端公共层统一 `snake_case` 字段转换，提升接口兼容性。
  - 控制面鉴权服务路由由 `kmr` 切换至 `aicp`。
- **依赖与扩展项完善**:
  - 新增 `kb` 可选依赖组（`kingsoftcloud-sdk-python`），`all` 聚合依赖同步纳入知识库能力。

### 🐛 修复 (Bug Fixes)

- **部署与回滚稳定性修复**:
  - 修复代码包路径，增加时间戳以支持稳定回滚。
  - 优化部署轮询与 endpoint 回填，降低创建后短时状态抖动影响。
- **MCP/CLI 兼容性修复**:
  - 修复 MCP deploy API 参数与 bucket 配置问题。
  - 适配 serverless API 驼峰字段响应，并增加 MCP 空响应防护。
- **控制面 API 兼容性修复**:
  - 兼容 KOP 的 API 版本（`2024-06-12`），修复部分环境下接口不匹配问题。
- **OpenClaw 部署提示修复**:
  - `openclaw deploy` 结束后明确提示先执行 `status` 再打开 `dashboard`。

### ⚠️ 破坏性变更 (Breaking Changes)

- 控制面创建接口从 `CreateAgent` 迁移为 `CreateAgentProduct(AutoPay)`，CLI 与服务端需匹配对应协议版本，请尽快升级。

## [0.2.0] - 2026-01-22

### 🚀 新特性 (New Features)

- **架构升级 - AgentEngine Server 集成**:
  - 全面对接 AgentEngine Server，CLI 不再直接调用底层 Serverless API。
  - 新增 `AgentEngineClient` 统一 API 客户端 (`ksadk/api/client.py`)，提供标准化的 Agent/MCP 管理接口。
  - 架构解耦使得部署源替换更灵活，为未来多云支持打下基础。
- **本地状态管理 (`.agentengine.state`)**:
  - 新增部署状态持久化模块 (`ksadk/deployment/state.py`)。
  - 部署后自动记录 `agent_id`, `endpoint`, `api_key` 等信息到本地状态文件。
  - `invoke` 命令可自动读取状态，无需重复指定 Agent 名称或 Endpoint。
- **星流模型选择 (`agentengine model`)**:
  - 新增 `model` 命令，支持从 OpenAI 兼容 API 动态获取可用模型列表。
  - 交互式选择后自动更新 `.env` 中的 `MODEL_NAME` 配置。
- **Thinking 模型支持 (深度推理渲染)**:
  - Runner 层新增 `reasoning_content` / `thinking` 字段解析 (`patch_langchain.py`)。
  - 流式输出时支持模型思考过程的实时渲染（使用 Rich Panel 折叠展示）。
  - `agentengine run` 默认展示思考过程，增强调试体验。
- **CLI Invoke Markdown 实时渲染**:
  - `agentengine invoke` 交互模式集成 Rich Live + Markdown 渲染。
  - 流式输出时实现 5 FPS 限流刷新，大幅减少终端闪烁。
- **MCP Server 管理 (预览)**:
  - 新增 `agentengine mcp` 命令组 (`deploy`, `list`, `status`, `delete`)。
  - 新增 MCP 检测器 (`MCPDetector`) 和构建器 (`MCPBuilder`)，支持 FastMCP 项目自动检测与打包。
- **Memory SDK (预览)**:
  - 引入 `MemoryManager` SDK (`ksadk/memory/`)，支持可插拔的存储后端（InMemory, Redis）。
  - 为 Agent 后续提供标准化的长短期记忆管理能力做准备。
- **Web UI 品牌焕新**:
  - 界面 Branding 全面升级为 "Kingsoft Cloud Agent Engine"，提供更统一的控制台体验。
- **API 协议重构 ("Path-based Refactor")**:
  - 客户端与服务端通信协议全面升级。
  - 摒弃了旧的 Query Parameters (`?Action=...`) 模式，迁移至符合 RESTful 规范的路径模式 (如 `/agentengine/api/v1/CreateAgent`)。
  - 更新 Swagger 文档以匹配新架构。

### 🛠 改进 (Improvements)

- **全局配置管理 (`~/.agentengine/settings.json`)**:
  - 新增 `global_config` 模块，支持跨项目凭证复用。
  - `agentengine create` / `agentengine config` 支持全局配置的保存和读取。
- **环境变量统一**:
  - 规范化变量名: `MODEL_NAME` → `OPENAI_MODEL_NAME`, `OPENAI_API_BASE` → `OPENAI_BASE_URL`。
- **多租户一致性 (Account ID Unification)**:
  - 废弃 `user_id`，统一使用 `account_id` 作为系统内唯一的租户标识。
  - 实现了 `X-Ksc-Account-Id` 在 CLI、Server 和 Serverless 后端之间的完整透传。
- **Serverless 部署增强**:
  - **KS3 智能区域配置**: 支持动态推导区域 Bucket 名称 (`agentengine-{account_id}-{region}`)。
  - **状态透视**: `agentengine status` 现可实时显示 Agent 的副本数 (`replicas`) 和就绪副本数 (`ready_replicas`)。
- **可观测性优化 (Langfuse)**:
  - 修复 Trace 重复上报问题。
  - Trace Name 直接显示具体的 **Agent Name**，统一 `LANGFUSE_BASE_URL` 配置。
  - 为 LangChain/LangGraph Runner 添加 `input.value` / `output.value` 属性，便于 Langfuse 可视化展示。
- **CLI 体验增强**:
  - 帮助文本使用彩色输出和更好的格式化。
  - 交互式运行体验优化 (questionary + rich 渲染)。
- **Python 版本支持**:
  - 新增 Python 3.13 / 3.14 支持。

### 🐛 修复 (Bug Fixes)

- **Windows 离线安装**: 将 `langchain`, `langgraph` 等核心库移至 `dependencies`，解决 `ModuleNotFoundError` 问题。
- **Windows BOM 兼容**: 全面使用 `utf-8-sig` 编码处理配置文件，确保 Windows 兼容性。
- **构建系统**:
  - 修复 Web UI 构建时因 Google Fonts 网络问题导致的构建失败（禁用字体内联）。
  - Makefile `build` 目标自动删除 `tar.gz` 和临时目录。
- **环境路由**: 修正了 Serverless Client 连接预生产/生产环境时的 Endpoint 路由逻辑。
- **依赖约束**: `fastapi` 版本上限 `<0.124.0` 以兼容 `google-adk`，`pydantic` 上限 `<3.0.0`。

### ⚠️ 破坏性变更 (Breaking Changes)

- **通信协议不兼容**: v0.2.0 版本的 CLI/SDK 必须配合 v0.2.0+ 的 AgentEngine Server 使用。
- **环境变量重命名**: `MODEL_NAME` → `OPENAI_MODEL_NAME`, `OPENAI_API_BASE` → `OPENAI_BASE_URL`。

---

## [0.1.0] - 2026-01-15

### 🎉 初始发布 (Initial Release)

- **AgentEngine CLI**:
  - 发布 `agentengine` 命令行工具。
  - 支持 `create`, `build`, `deploy`, `run`, `status`, `destroy`, `invoke`, `config`, `web`, `launch` 等全生命周期管理命令。
  - 支持 Local (Docker) 和 Cloud (Serverless) 双模态部署。
- **Agent 框架集成**:
  - 原生支持 **LangGraph**、**LangChain** 和 **Google ADK** 框架应用的开发与托管。
  - 提供统一的 `BaseRunner` / `UnifiedRunner` 运行时，封装 HTTP 服务与 SSE 事件流。
  - 自动检测项目类型 (`AgentDetector`)。
- **构建系统**:
  - 支持 `code` 模式 (ZIP 打包) 和 `container` 模式 (Docker 镜像) 双构建模式。
  - 自动依赖分析与打包。
- **Web UI 控制台**:
  - 内置 Angular 开发的本地可视化控制台。
  - 支持 Agent 聊天调试、Trace 查看及基本管理功能。
- **基础设施**:
  - 实现基于 KS3 的代码包上传与分发机制 (`KS3Uploader`)。
  - 集成 Langfuse 提供基础的可观测性支持 (`ksadk/tracing/`)。
  - 支持 OpenTelemetry 标准 tracing。
- **配置管理**:
  - 支持 `agentengine.yaml` / `ksadk.yaml` 项目配置文件。
  - 支持 `.env` 环境变量加载。
