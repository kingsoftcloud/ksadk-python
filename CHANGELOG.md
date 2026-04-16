# 更新日志

本文件记录 **Kingsoft AgentEngine SDK (ksadk)** 的重要变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)，
版本遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## [0.5.0] - 2026-04-16

### 变更

- 新增 `agentengine hermes` 一等公民资源组，支持 `deploy`、`list`、`status`、`open`、`connect`、`exec`、`pairing`、`delete`，并让 `agentengine invoke <hermes-agent>` 默认进入 Hermes 原生远程 TUI。
- Hermes 以共享 runtime 镜像方式接入，不要求用户本地 `build/push`；新增 runtime 资产、公共镜像工作流，以及 `/`、`/chat`、`/v1/*`、`/_ksadk/terminal/ws` 的统一运行时 contract。
- 新增 OpenClaw 用户自定义镜像模板与示例，支持在平台运行时约束下自定义插件、skills 和默认配置。
- `agentengine openclaw deploy` 新增 `--env KEY=VALUE` 透传能力，允许业务自定义环境变量直接进入容器运行时。
- OpenClaw 部署新增 `OPENCLAW_CHANNEL_BOOTSTRAP_JSON`、Agentspace bootstrap 配置与 `OPENCLAW_BROWSER_SSRF_POLICY_JSON` 透传，便于渠道预配置和内网访问策略收口。
- 新增 `agentengine openclaw repair`，并支持 `agentengine openclaw gateway doctor --fix` 通过控制面直接触发 `doctor-fix` 修复动作。

### 修复

- 进一步完善 OpenClaw managed runtime 在当前 upstream bundle 下的 trusted-proxy loopback、backend self-pairing 与默认 browser 行为兼容性，降低诊断和修复成本。
- 改进 hosted Hermes 运行时默认行为，网关进程改为容器内托管与重启，减少对宿主机 daemon 能力的依赖。
- hosted Hermes 运行时默认补齐 `TERM=xterm-256color` 与统一状态目录布局，提升远端 setup / pairing 交互稳定性。

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
