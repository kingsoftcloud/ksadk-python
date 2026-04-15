# KSADK 技术文档

本文档描述 `ksadk-python` 当前实现形态，口径固定为“最新代码对应的当前设计”，不承担跨仓路线图职责。

当前实现基于 `ksadk-python` 最新代码状态整理，覆盖到 2026-04-14 前的近期变更，包括 hosted-first UI metadata、Hermes 原生远程 TUI、quick access refresh、KS3 upload fallback 和依赖缓存复用优化。

## 1. 产品定位与边界

`ksadk-python` 是 KSADK 的数据面仓库，负责：

- 本地运行与 runner 编排
- framework detection 与统一入口分发
- session / transcript / conversation runtime
- MCP runtime 与 toolset bind
- A2A `serve/card`
- sandbox / approval / tool safety
- 面向开发者的 CLI / SDK 接口
- 本地 Web UI 与云端 Dashboard / framework-native UI 的运行时消费侧适配

`ksadk-python` 不负责：

- registry server
- artifact / runtime 生命周期治理
- gateway discovery 入口
- hosted control plane
- 统一版本治理后台
- SkillHub / Tool Registry / A2A Registry 这类控制面系统

这些能力的 canonical 归属仍在 `agentengine-server`。跨仓架构主线请看：

- repo: `agentengine-server`
- path: `docs/ksadk-platform-architecture-draft.md`
- path: `docs/ksadk-next-2-weeks.md`
- path: `docs/unified-agent-ui-v1-technical-design.md`

仓库内边界约束与优先级说明见 [AGENTS.md](../AGENTS.md)。

## 2. 整体架构

### 2.1 本地开发态

本地开发态以项目目录为中心：

- `FrameworkDetector` 识别 ADK、LangGraph、LangChain、DeepAgents、OpenClaw、Hermes 项目模板
- `UnifiedRunner` 根据检测结果分发到具体 runner
- `agentengine run` 负责本地交互式运行
- `agentengine web` 启动统一本地 Web UI
- 本地 UI 会话默认落在项目目录下的 `.agentengine/ui`

`agentengine web` 当前统一走 ksadk 内建 Web UI，而不是再按框架分叉到旧的框架专属本地 Web UI。命令入口见 [ksadk/cli/cmd_web.py](../ksadk/cli/cmd_web.py)。

Hermes 在本地项目态属于 container-first 模板：`init -f hermes` 会生成参考 runtime 资产，但实际交互主线是云端 `agentengine hermes ...` 生命周期命令与远程 TUI，而不是本地 runner。

### 2.2 托管调用态

云端托管态下，`ksadk-python` 主要承担“运行与消费”职责，而不是“创建与治理”职责：

- `build/deploy/launch` 负责把本地项目打包成 `Code` 或 `Container` 制品，并调用控制面 API
- `agentengine hermes ...` 负责共享 Hermes runtime 镜像的生命周期管理、原生终端 attach 与受限子命令透传
- 远端 Agent / MCP / OpenClaw / Hermes 的创建、更新、删除和 Dashboard 链接生成由 `AgentEngine Server` 承担
- `ksadk-python` 消费这些控制面能力，并把本地项目状态持久化到 `.agentengine.state`

因此，仓库里的部署逻辑更接近“面向开发者的制品构建与控制面客户端”，而不是独立的托管平台。

### 2.3 本地 UI 与云端 UI 的关系

当前 UI 分为两层：

- 本地：`agentengine web` 启动本地统一 Invoke UI
- 云端：`agentengine dashboard open` 打开 hosted Dashboard / WebUI

二者的关系不是“两套完全不同的产品”，而是同一类对话与控制语义在本地和 hosted 场景下的不同宿主。当前跨仓 canonical 方向是 hosted/local 共享 transcript 与 control 语义；`ksadk-python` 负责本地实现与 hosted runtime 侧消费，server 负责 hosted bootstrap、session façade 与分享链接。

## 3. 核心子系统

### 3.1 Framework Detection 与 Runner 分发

入口文件：

- [ksadk/runners/unified_runner.py](../ksadk/runners/unified_runner.py)
- `ksadk/detection/*`

当前支持框架：

- ADK
- LangGraph
- LangChain
- DeepAgents
- OpenClaw
- Hermes

`UnifiedRunner.create(...)` 会根据检测结果分发到具体 runner。DeepAgents 当前单独有 `DeepAgentsRunner`，但实现上复用 LangGraph 路径，因此框架扩展保持了“检测层 + runner 分发层 + CLI 展示层”一致接入。

Hermes 是一个特例：它被识别为正式 framework，并参与项目模板、状态识别、Dashboard/Open 路径和 `invoke` transport 决策，但不复用本地 runner。它的交互主线是：

- `agentengine invoke <hermes-agent>` -> 原生远程 TUI websocket
- `agentengine hermes open <hermes-agent> --chat` -> hosted chat 页面
- `agentengine hermes exec <agent> -- <readonly-subcommand>` -> 受限运维透传

### 3.2 Conversation Runtime 与 Session Continuity

主入口：

- [ksadk/conversations/runtime.py](../ksadk/conversations/runtime.py)

当前 conversation runtime 负责：

- 规范化用户输入、附件和 session 上下文
- 统一构造 runner request payload
- 管理 session title / summary / metadata 更新
- 对非 ADK runner 注入平台级 ambient context

近期的 session continuity 收口包括：

- 本地/托管路径都以 session 为真相源，而不是依赖客户端回传整段历史
- 会话标题与 summary 在 assistant turn 后统一回填
- 本地 UI 目录与项目根目录绑定，默认提供跨重启续聊能力

### 3.3 KB / LTM Ambient Context 与 ADK Native Path

当前 KB/LTM 有两条路径：

1. ADK native path
2. 非 ADK 平台 ambient path

对于 ADK：

- 在 runner 初始化时自动注入 `search_knowledge_base`
- 自动注入 `load_memory`
- 自动注入 `save_memory`
- 相关逻辑在 [ksadk/runners/adk_runner.py](../ksadk/runners/adk_runner.py)

对于 LangChain / LangGraph / DeepAgents：

- 由 conversation runtime 在调用前决定是否构建 `kb_context` / `memory_context`
- 默认策略是 `on_demand`
- 仅非 ADK runner 走平台 ambient path

关键行为：

- KB ambient 通过问题意图和主题词判断是否加载
- LTM ambient 会避开明显的短期上下文问题，只在“回忆历史/偏好/个人信息”类问题上触发
- `KSADK_KB_AMBIENT_POLICY` / `KSADK_LTM_AMBIENT_POLICY` 支持 `on_demand` 与 `always`

这使得当前实现保持了：

- ADK 尽量复用原生工具语义
- 其他框架无需改 agent 代码也能获得平台级上下文能力

### 3.4 MCP Runtime 与 Toolset Bind

主入口：

- [ksadk/mcp_runtime/__init__.py](../ksadk/mcp_runtime/__init__.py)
- [ksadk/runners/adk_runner.py](../ksadk/runners/adk_runner.py)

当前 MCP runtime 设计特点：

- 通过 `KSADK_MCP_SERVERS` 环境变量声明 MCP server 列表
- 每个 MCP server 需要是合法的 `/mcp` HTTP(S) endpoint
- 运行时会构造成 `McpToolset`
- 通过去重 key 防止重复注入同一 toolset

这套设计的定位是“运行时绑定远端 MCP 工具”，不是 MCP registry。也就是说：

- `ksadk-python` 负责消费 endpoint 并把它变成 runner 可用的工具集
- 资源注册、治理和统一发现仍然是控制面职责

### 3.5 Sandbox / Approval / Tool Safety

ADK runner 默认会尝试注入：

- 本地 sandbox 工具
- 远端 MCP toolsets

其中 sandbox 默认采用 `SandboxToolset(LocalCodeSandbox())`，可通过环境变量关闭。approval / tool safety 当前已有基线，但更完整的 hosted/local 一致 run control 仍在跨仓 canonical 方向中推进，仓库内保留的专项说明见 [Runner_Approval_Architecture.md](./Runner_Approval_Architecture.md)。

### 3.6 Web / Dashboard / Unified UI

本地 Web UI：

- 入口：`agentengine web`
- 实现：统一走 ksadk 内建 Web UI
- ADK 项目默认切到持久化 STM，保证跨重启 session continuity

云端 Dashboard：

- 入口：`agentengine dashboard open`
- 默认通过 `CreateDashboardAccessLink` 生成短链接
- 支持 `--share`、`--no-open`、`--direct`
- 支持从 `.agentengine.state` 自动解析 agent/openclaw 引用
- Hermes `agentengine hermes open` 默认打开管理 UI `/`，`--chat` 打开统一 hosted chat `/chat`

当前 UI 相关实现体现出两点：

- `ksadk-python` 已经承担本地 UI 与 hosted UI runtime 消费侧的收口
- hosted bootstrap / session façade / capability 协商依赖 `agentengine-server` 提供

对 Hermes 而言，云端 UI/终端面被拆成三类 contract：

- `/`：Hermes 管理 UI
- `/chat`：统一 hosted chat
- `/_ksadk/terminal/ws`：原生远程 TUI 与受限 `hermes exec`

### 3.7 Build / Deploy / Launch 与 Artifact 路径

CLI 主线包括：

- `build`
- `deploy`
- `launch`
- `hermes deploy/status/open/exec/delete`

当前实现支持：

- `Code` 制品
- `Container` 制品
- `serverless` / `kcf` / `kce` 目标
- `dry-run`
- 机器可读 JSON 输出

Hermes 生命周期则走另一条主线：

- 默认共享镜像：`hub.kce.ksyun.com/agentengine-public/hermes-agent:v2026.4.15-ks10`
- 不要求用户本地 build/push
- deploy 时把模型 env 注入到共享 runtime
- 若配置的是 `kspmas.ksyun.com` 公网模型地址，deploy 会自动改写成 `kspmas-internal.sdns.ksyun.com` 供云端 Pod 使用
- runtime 同时聚合 `/`、`/v1/*`、`/_ksadk/terminal/ws` 与 `/health`

与“平台生命周期治理”不同，仓库内实现聚焦于：

- 检测项目
- 补齐运行所需依赖
- 生成并上传制品
- 调控制面 API 发起部署
- 把状态回填到本地 `.agentengine.state`

对 Hermes 来说，这里的“制品”不是用户本地 build 结果，而是平台共享 runtime 镜像引用和对应的环境变量 / UI metadata。

近期行为变化包括：

- 优化 KS3 上传 fallback
- 优化依赖缓存复用
- 部署后回查并持久化 quick access 信息
- Agent 创建后重试 quick access refresh

## 4. 关键调用链

### 4.1 `agentengine run`

调用链概览：

1. 检测项目框架
2. 创建对应 runner
3. 初始化模型与运行环境
4. 进入本地交互式会话
5. 由 conversation runtime 处理 session、上下文和 turn metadata

### 4.2 `agentengine web`

调用链概览：

1. 检测项目框架
2. 设置 `KSADK_PROJECT_DIR` 与 `AGENTENGINE_UI_DIR`
3. 对 ADK 默认启用持久化 STM
4. 创建统一 runner
5. 启动本地统一 Web UI

它的定位是“本地 Invoke UI”，不是 hosted Dashboard 的本地镜像。

### 4.3 `build / deploy / launch`

调用链概览：

1. 读取项目配置与环境
2. 识别框架与入口
3. 生成 `Code` 或 `Container` 制品
4. 上传制品 / 推送镜像
5. 调控制面 API 创建或更新远端资源
6. 回填 `.agentengine.state` 和 quick access 信息

### 4.4 `dashboard open`

调用链概览：

1. 解析 Agent 引用
2. 优先读取 `.agentengine.state`
3. 根据 UI 配置推导目标路径
4. 默认创建 Dashboard access link
5. 打开浏览器或以 JSON 方式输出 URL

### 4.5 KB / LTM 注入

调用链概览：

1. conversation runtime 根据 runner 类型决定是否使用 ambient path
2. 非 ADK 场景按 `on_demand` / `always` 策略构建 `kb_context` / `memory_context`
3. ADK 场景通过 runner 自动注入工具
4. 统一把 `platform_context`、`kb_context`、`memory_context` 放入 runner payload

## 5. 与 AgentEngine Server 的边界

当前边界可以简化为一句话：

`ksadk-python` 负责运行与消费，`agentengine-server` 负责注册与治理。

具体到当前实现：

`ksadk-python` 负责：

- runner consume
- local fallback
- session / conversation runtime
- runtime tool bind
- 本地 UI
- 面向开发者的构建、部署和资源访问 CLI

`agentengine-server` 负责：

- artifact / runtime lifecycle
- hosted bootstrap
- Dashboard access link
- registry 元数据
- hosted session façade
- 统一 resolve / auth / visibility / hosted control

因此文档约束是：

- 本文只写 `ksadk-python` 当前真实实现与依赖面
- 不重复 server 侧中长期架构草案
- 跨仓 canonical 方向直接引用 server 仓库文档，不在本仓再造一份总蓝图

## 6. 当前实现备注

截至 2026-04-06，近期值得记录的实现状态包括：

- 本地 `web` 已统一为 ksadk 内建 Web UI，ADK 项目默认启用持久化 STM
- Dashboard / hosted UI 路径继续向 hosted-first metadata 收口，并已有移动端聊天 UI 适配
- 部署链路会回查 quick access，并在首次创建后做重试刷新
- Code 构建链路继续优化 KS3 上传 fallback 与依赖缓存复用
- README 曾长期承载大量使用说明，已不再适合作为唯一用户入口，因此本次文档重构将使用说明和技术说明拆成两份主文档

## 7. 非目标与已知约束

当前阶段明确不做：

- 在 `ksadk-python` 内实现 registry server
- 在 SDK 侧重复实现 hosted control plane
- 把 SkillHub / Tool Registry / A2A Registry 提前落到数据面仓库
- 为未来蓝图一次性引入大量没有消费方的重抽象

当前实现已知约束：

- hosted/local 的 approval / stop / resume 一致 run control 仍依赖跨仓推进
- server 侧 bootstrap、HostedRuntime、capability 协商仍以 `agentengine-server` 提供的契约为准
- `ksadk-python` 中仍保留若干历史专题文档，主线信息请优先以本文件和 [ksadk_usage_guide.md](./ksadk_usage_guide.md) 为准
