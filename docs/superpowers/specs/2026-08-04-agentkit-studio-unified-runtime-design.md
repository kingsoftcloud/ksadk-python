# AgentKit Studio 与统一 Web RuntimeAdapter 收敛设计

> 日期：2026-08-04
> 状态：已确认，进入实施规划
> 代码库：`ksadk-python`
> 开发分支：`agentkit-studio-codex-ld1`
> 内部 eZone：`ssh://ezone.ksyun.com:23/ezone/bigdata-platform/ksadk-python.git`

## 1. 背景

AgentKit Studio 已经能够在本地创建 Codex Agent、构建 ManagedRuntime Bundle、运行会话并展示 OTEL Trace，但当前实现仍存在以下结构性问题：

1. Codex 同时存在 `CodexRuntime`、`CodexRunner`、`StudioCodexRuntimeAdapter` 和 `CodexStudioRuntime`，形成 `RuntimeAdapter → Runner → RuntimeAdapter` 的重复包装。
2. Studio 又维护 `StudioRuntimeAdapterRegistry` 和 `StudioRuntimeOrchestrator`，与核心 `RuntimeRegistry`、`RuntimeAdapter` 重复。
3. `ksadk web`、Studio、Responses、AG-UI、A2A、Harness 对 Runner 和 RuntimeAdapter 的依赖方向不一致。
4. Studio 虽能管理多个 Agent，但会话、构建、部署和 Trace 没有共享同一个全局 Agent/目标上下文。
5. 创建 Agent 仍以 Codex 专用快速表单为主，不能自然扩展到 ADK、LangGraph、导入、项目识别和多轮对话构建。
6. Tool 已有内置目录，但兼容性和权限信息尚未成为完整产品能力；Skill 仅能导入或读取 canonical 目录，缺少候选发现、风险校验和确认流程。

本设计在保留“YAML 即 Agent”、标准 RuntimeEvent、OpenAI Responses 和 OTLP 合同的前提下，统一运行架构和 Studio 产品模型。

## 2. 目标

交付一个可以发布、可以在应用内浏览器完整演示的本地 AgentKit Studio/统一 Web 版本：

- Codex、ADK、LangGraph 都能真实创建、检测依赖、构建、流式对话、取消、恢复会话并产生 OTEL Trace。
- Web、Studio、Responses、AG-UI、A2A、Harness 只依赖 `RuntimeAdapter`，不直接依赖 Runner。
- Codex 只保留公开的 `CodexRuntimeAdapter`，删除 Codex 和 Studio 的重复执行层。
- 多 Agent、运行目标、创建方式、Tool、Skill、Session、Build 和 Trace 形成一致闭环。
- 为后续真实金山云凭证和云 Agent 接入保留目标模型，但本地版本绝不返回 Mock 云成功状态。

### 2.1 执行 Goal

> 在 `ksadk-python` 的 `agentkit-studio-codex-ld1` 工作树交付可发布、可在应用内浏览器完整演示的 AgentKit Studio 本地版本：以 `RuntimeAdapter`、`RuntimeRegistry` 和 `RuntimeExecutor` 作为唯一上层运行合同，真实打通 Codex、ADK、LangGraph 的创建、依赖检测、构建、标准 Responses 流式对话、取消、刷新恢复、Session 和 OTLP Trace；完成多 Agent/目标全局切换、四种创建入口、模型绑定与会话级切换、Tool 管理、Skill 自动发现/校验/确认导入、Raw OTLP 复制，并删除 Studio/Runner 重复执行层。所有架构、协议、Runtime、前端和浏览器验收门禁通过后方可完成；真实金山云部署不在本 Goal 写集内，但必须保留诚实的未连接云目标，禁止 Mock 成功。

该 Goal 是结果目标，不以“完成若干模块”或“代码已提交”作为完成依据。

### 2.2 用户最终看到的完整效果

| 场景 | 必须达到的效果 |
|---|---|
| 启动 | `ksadk studio` 只监听 loopback，打开统一的 `AgentKit Studio`；不显示 `Codex Local`，不同时维护另一套 Studio Web |
| 创建 | 创建中心提供快速创建、对话构建、导入、项目识别四种真实入口；最终都生成可查看、可编辑、可校验、可构建的 YAML Revision |
| Runtime | Codex、ADK、LangGraph 显示真实依赖、版本和能力；缺依赖时给出安装/修复命令，不伪造可用状态 |
| Agent 管理 | 可以创建多个 Agent，真实列出、切换、编辑、删除；Agent ID 由系统生成稳定主键，用户只编辑名称/slug，避免未来云端冲突 |
| 全局上下文 | 顶栏统一切换 Workspace、Agent、Execution Target；会话、构建、部署、资源和 Trace 都跟随当前上下文，云未连接时明确显示未连接 |
| 构建 | 对 YAML、源码、依赖、Tool 和 Skill 做真实校验，产出可追溯 Bundle；Codex/ADK/LangGraph 使用各自 Codec/Builder，不抹平专属字段 |
| 模型 | Agent 可绑定多个模型，默认模型写入 Revision；会话可在允许集合内动态切换，服务端拒绝未绑定模型；模型能力来自真实探测而非前端常量 |
| 对话 | 请求与流式输出兼容 OpenAI Responses；输入框动态高度；长 Markdown、代码和 Tool 事件在消息区内滚动；切换会话或刷新不会误取消运行 |
| 会话 | Session 真实创建、切换、恢复和删除；删除范围有确认提示；活跃 Run 使用 handle attach 恢复，不能恢复时显示真实原因 |
| Tool/Skill | 展示 ksadk 内置 Tool、工作区 Tool 和 MCP Tool；Skill 可自动发现候选、检查风险、确认导入、绑定并进入构建产物 |
| Trace | Trace Explorer 使用 OTLP 数据模型，详情不跳转会话页；展示 Span 树、Attributes、Events、Resource、真实 Token/耗时和可复制 Raw OTLP |
| 协议 | Studio、统一 Web、Responses、AG-UI、A2A、Harness 都从同一 RuntimeEvent 流投影，不各自实现 Runtime 或事件转换 |

### 2.3 前端源码与发布物边界

- 可维护源码放在 `ksadk/studio/web/src/`，按 `app-shell`、`context`、`agents`、`authoring`、`builds`、`chat`、`resources`、`traces`、`api` 和 `store` 拆分，禁止继续扩张单一超大 `app.js`。
- 复用 `ksadk-web` 时采用构建期 npm 依赖和稳定导出，不复制其内部源码、不在浏览器运行时访问在线 npm。
- 构建后的静态文件写入 `ksadk/studio/static/` 并随 `ksadk` wheel 发布；安装 PyPI 包后无需 Node/npm 也能打开 Studio。
- `ksadk/server/static/` 不再作为第二套手工维护的产品前端；兼容入口必须指向同一构建产物，迁移完成后删除重复资源。
- 生产 Python 源码单文件超过 700 行触发 CI 告警，要求在后续扩张前说明深模块边界或拆分理由；新文件或非遗留文件超过 1000 行 CI 直接失败。仓库中已有的超限文件采用“当前行数只减不增”的显式基线，后续逐步拆分；测试 Fixture 与前端编译产物不计入该门禁。

## 3. 非目标

本 Goal 不实现以下内容：

- 金山云 Ding 订单、KOP 注册、Artifact Admission 和真实云部署。
- Skill Center 远程市场的完整管理面。
- 将所有第三方框架 SDK 改写为同一种内部对象模型。
- 在未安装依赖或未配置凭证时伪造运行成功。

真实云部署由后续独立 Goal 接续；本设计只冻结其所需的 `ExecutionTarget` 扩展点和未连接状态。

## 4. 核心原则

1. `RuntimeAdapter` 是唯一上层执行合同。
2. `RuntimeRegistry` 是唯一 Runtime 注册表。
3. Runner 只是部分框架内部的 SDK 兼容层，不是 Web/API 合同。
4. RuntimeEvent 是唯一实时事件合同，Responses、AG-UI、A2A 和 OTLP 都从它投影。
5. YAML 是 Agent 的可审计配置源；内部归一化不抹平 Runtime 专属字段。
6. 所有创建方式汇聚到同一种 Agent Draft/Revision 流程。
7. 自动发现只产生候选，不静默安装、不静默绑定。
8. UI 不展示假按钮、假状态、猜测的 Token 或猜测的 Runtime 能力。

## 5. 目标架构

```mermaid
flowchart TB
    subgraph Surface["协议与产品入口"]
        Web["统一 Web"]
        Studio["AgentKit Studio"]
        Responses["OpenAI Responses"]
        AGUI["AG-UI"]
        A2A["A2A"]
        Harness["Harness / Container"]
    end

    Surface --> Executor["RuntimeExecutor"]
    Executor --> Registry["RuntimeRegistry"]

    Registry --> Codex["CodexRuntimeAdapter"]
    Registry --> ADK["ADKRuntimeAdapter"]
    Registry --> LangGraph["LangGraphRuntimeAdapter"]
    Registry --> Legacy["RunnerRuntimeAdapter"]

    Codex --> CodexClient["Codex Client / 星流 Proxy"]
    ADK --> ADKRunner["ADKRunner"]
    LangGraph --> LangGraphRunner["LangGraphRunner"]
    Legacy --> OtherRunner["LangChain / DeepAgents / Custom Runner"]

    Executor --> Events["RuntimeEvent"]
    Events --> Session["Session / Run Store"]
    Events --> Otlp["OTLP Trace"]
    Events --> Projection["Responses / AG-UI / A2A Projection"]
```

### 5.1 `RuntimeExecutor`

通用执行编排从 `ksadk/studio` 移入核心 `ksadk/runtime`，命名为 `RuntimeExecutor`。它只处理：

- 用 `RuntimeRegistry` 创建 Adapter。
- 生成并传递 `StartRequest`。
- 驱动 `start / stream / cancel / resume / checkpoint / close`。
- 将 RuntimeEvent 发送给注入的 Observer/Sink。
- 保证终态和资源释放。

它不读取 Studio Build Repository，不解析 Codex Manifest，不保存 Studio UI 状态。Studio、Server 和协议层分别注入自己的事件存储与投影器。

### 5.2 `RuntimeRegistry`

核心 `RuntimeRegistry` 同时支持 Adapter 类型和 Adapter Factory，替代 Studio 私有注册表。Runtime Factory 接收核心 `RuntimeLaunchContext`，返回 `RuntimeAdapter`：

```python
class RuntimeLaunchContext:
    runtime_type: str
    project_dir: Path
    detection: DetectionResult | None
    config: dict[str, Any]
    services: RuntimeServices

RuntimeAdapterFactory = Callable[[RuntimeLaunchContext], RuntimeAdapter]
```

Registry 不感知 Agent Build、Studio Draft 或 HTTP Request。

### 5.3 Codex

`CodexRuntime` 重命名为 `CodexRuntimeAdapter`，作为 Codex 唯一公开执行类。它直接拥有 Codex Client、thread、turn、cancel、resume、checkpoint 和 RuntimeEvent 翻译。

删除：

- `CodexRunner`
- `CodexStudioRuntime`
- `StudioCodexRuntimeAdapter`
- Codex 专用 Runner chunk 往返转换

Codex Runner 中仍有价值的逻辑迁移如下：

| 逻辑 | 新归属 |
|---|---|
| 历史消息预处理 | `prepare_runtime_start` |
| model、prompt、cwd | `StartRequest` 和 Codex Factory |
| 星流 Responses/Chat 转换 | Codex Client/Proxy |
| Proxy 观测 | 直接生成 RuntimeEvent |
| Web SSE | 通用 RuntimeEvent Projection |
| cancel/resume/thread | `CodexRuntimeAdapter` |

### 5.4 ADK 与 LangGraph

ADK 和 LangGraph 继续复用其框架 Runner，但上层只能看到对应 RuntimeAdapter：

- `ADKRuntimeAdapter → ADKRunner → ADK SDK`
- `LangGraphRuntimeAdapter → LangGraphRunner → LangGraph SDK`

ADK 保留 forward-only resume 语义；LangGraph 保留 checkpoint/time-travel/fork 语义，不能为了统一界面虚报相同能力。

### 5.5 BaseRunner

`BaseRunner` 保留为内部框架 SDK 兼容层，但必须瘦身。

保留：

- 加载原生 Agent/Graph。
- 产生框架原生流。
- 释放框架资源。
- 声明原生 cancel/checkpoint 能力。
- 自定义 Runner 扩展点。

移除：

- `run_server()`。
- HTTP、Web、Responses、AG-UI 和 A2A 逻辑。
- Session/Trace 持久化。
- 进程级模型环境变量修改。
- 可由 RuntimeAdapter 收敛完成的 `invoke()`。

CI 增加架构测试，禁止 `ksadk/server`、`ksadk/studio`、`ksadk/agui`、`ksadk/a2a` 直接导入 `ksadk.runners`。

## 6. Agent 定义与 Revision

### 6.1 逻辑模型

四种创建方式都生成统一的逻辑 Draft：

```text
AgentDraft
├── Metadata
├── RuntimeRef
├── Instructions
├── ModelBindings
├── CapabilityBindings
├── SourceProvenance
├── ValidationReport
├── SourceYaml
└── Revision
```

### 6.2 YAML 序列化

YAML 仍是 Agent 的配置源。不同 Runtime 通过各自 Definition Codec 解析和生成 YAML：

- Codex 保留 `framework: codex`、`artifact_type: ManagedRuntime` 和 Runtime Catalog 引用。
- ADK/LangGraph 保留入口模块、变量名、依赖与框架配置。
- 导入时保留无法识别但合法的扩展字段。
- Studio 展示归一化投影，但构建读取已校验的原始 Revision。

### 6.3 Runtime 迁移

- 首次成功 Build 前允许修改 Runtime。
- 已有 Build 后更换 Runtime 必须进入迁移流程。
- 迁移产生新 Revision，不覆盖旧 Build、Session、Run 和 Trace。
- 迁移先执行字段映射和兼容性诊断，再由用户确认。

## 7. 创建 Agent 中心

创建入口不是固定表单，而是四种真实流程。

### 7.1 快速创建

用户选择 Runtime、模型、提示词和能力；依赖探测通过后生成 YAML Draft。Runtime 不可用时展示版本和安装命令，不允许继续构建。

### 7.2 对话构建

真实模型通过多轮对话收集职责、边界、Runtime、模型和能力需求。模型只生成结构化 Draft Patch：

1. Patch 经过 Schema 和策略 Validator。
2. UI 展示字段变化和 YAML diff。
3. 用户确认后才写入 Draft。
4. 模型不得直接写工作区文件。

### 7.3 导入 Agent

支持 `agentengine.yaml`、Agent ZIP 和可识别 Bundle。处理过程分为 inspect 和 commit：

- Inspect 只读解析格式、Runtime、能力、文件清单、摘要和风险。
- Commit 在用户确认后写入 canonical 工作区。
- Agent ID 冲突时要求选择覆盖 Revision、另存为或取消。

### 7.4 识别现有项目

复用 `FrameworkDetector`，扫描入口、依赖、YAML、Tool 和 Skill。结果包含证据和置信度；冲突时不能静默选择 Runtime。

## 8. Runtime Catalog

Studio 提供统一 Runtime Catalog，至少返回：

- `runtimeType`
- 显示名称与版本
- 安装状态和版本兼容性
- 安装/修复命令
- 支持的 Session、Cancel、Resume、Checkpoint、Tool、Skill、MCP 能力
- 本地依赖来源

Codex、ADK、LangGraph 卡片只展示真实探测结果。能力矩阵由 Adapter/Factory 声明，不由前端硬编码。

## 9. 全局工作上下文

全局顶栏固定为：

```text
[Workspace ▼] [Agent ▼] [Local / 金山云 ▼]
```

- 删除产品标识旁的 `Codex Local`。
- Runtime 作为当前 Agent 的只读徽标展示。
- 会话、构建、部署、资源绑定和 Trace 默认跟随当前 Agent。
- Agent 列表允许设为当前 Agent。
- URL 保存 `agentId`、`sessionId` 和 `target`；不保存凭证和 Secret。
- Agent 切换后清理不属于新 Agent 的 Session/Build 临时选择。
- Trace 默认筛选当前 Agent，可显式选择“全部 Agent”。

`ExecutionTarget` 支持：

```text
local
cloud:<account>/<region>/<agent-id>
```

未绑定云凭证时只显示诚实的“未连接”和绑定入口；云 API 不返回 Mock Agent、Mock Build 或 Mock Trace。

## 10. Tool 管理

Tool Catalog 合并以下来源：

- ksadk 内置 Tool。
- 工作区自定义 Tool。
- MCP Probe 发现的 Tool。

每个 Tool 展示：

- 来源和版本。
- Input/Output Schema。
- 读写/外部副作用。
- 权限和审批策略。
- 支持的 Runtime。
- 健康与解析状态。

绑定时做 Runtime 兼容性校验；Build 锁定版本和摘要；Runtime 调用前再次检查绑定和权限。

## 11. Skill 自动发现

### 11.1 发现范围

默认扫描：

- `capabilities/skills/`
- 当前工作区内常见 Skill 目录。
- 用户显式添加的扫描目录。

默认排除 `.git`、虚拟环境、`node_modules`、构建产物和工作区外路径；不自动递归扫描用户主目录。

### 11.2 流程

```text
扫描候选
→ 解析 SKILL.md/frontmatter
→ 校验文件、路径、依赖和风险
→ 展示候选及差异
→ 用户确认导入
→ 复制到 capabilities/skills/<slug>
→ 计算 SHA-256
→ 加入 Catalog
→ 用户绑定 Agent
```

发现不等于安装，安装不等于绑定。

### 11.3 校验

- `SKILL.md` 必须唯一且 frontmatter 闭合。
- 必须包含 name 和 description。
- 拒绝路径穿越、外部软链接、设备文件、超限文件和压缩炸弹。
- 识别脚本、执行命令、网络需求和 Runtime 兼容性。
- 名称或版本冲突必须展示差异并由用户选择。

## 12. 统一 Web、Session 与 Responses

以下入口全部接收已经创建的 RuntimeAdapter：

- `ksadk web`
- `ksadk run --server`
- `/v1/responses`
- AG-UI
- A2A
- Studio 会话
- Harness 和容器启动入口

### 12.1 Responses

- 非流式和 SSE 流式都从 RuntimeEvent 投影。
- 模型选择必须属于 Agent Build 的绑定模型。
- Local Studio 使用标准 Responses 请求体；Studio Session/CSRF 仅保护控制面写接口，不泄露到部署后的 Runtime 调用示例。

### 12.2 Session

- Session 真实创建、列出、切换和删除。
- 刷新页面不取消 Runtime Run。
- 活跃 Run 通过 handle/attach 能力恢复；不能恢复时明确标注中断原因，不伪装成用户取消。
- Session 删除同时清理其本地 Run/Trace，并通过确认弹窗说明不可恢复范围。

## 13. OTEL Trace Explorer

- RuntimeEvent 统一投影为 OTLP。
- Trace/Span ID、parentSpanId、时间戳、Status、Resource 和 Scope 符合 OTLP。
- Token 与耗时只展示 Runtime/Provider 上报值；缺失时显示“未上报”。
- Trace 详情保留概览、Attributes、Events、Resource 和 Raw OTLP。
- Raw OTLP 使用固定视口独立滚动，进入 Raw 时自动展开详情。
- Raw OTLP 增加复制按钮，复制当前 Trace 的完整标准 JSON，并提供成功/失败反馈。

## 14. UI 信息架构

- 保留现有 AgentKit Studio 高密度浅色工作台视觉体系。
- 产品标识只显示 `AgentKit Studio`，不显示 `Codex Local`。
- 创建 Agent 按钮进入创建中心，不直接打开 Codex 专用表单。
- Runtime 不可用、云未连接、模型缺少凭证等状态必须可见且可解释。
- 未接通的能力不显示可成功执行的主要按钮。
- 长消息、Markdown、代码块、Tool 事件和 Trace JSON 都必须在各自容器内正常滚动。

## 15. 错误处理

统一错误至少包含：

- `RUNTIME_NOT_INSTALLED`
- `RUNTIME_VERSION_INCOMPATIBLE`
- `RUNTIME_NOT_SUPPORTED`
- `AGENT_RUNTIME_MISMATCH`
- `BUILD_STALE`
- `MODEL_NOT_BOUND`
- `TOOL_RUNTIME_INCOMPATIBLE`
- `SKILL_DISCOVERY_INVALID`
- `SKILL_IMPORT_CONFLICT`
- `RUN_ATTACH_UNAVAILABLE`
- `CLOUD_TARGET_NOT_CONNECTED`

错误响应包含稳定 code、用户可读 message、field、details 和可执行 hint。前端不得把服务端错误改写成笼统“运行失败”。

## 16. 安全边界

- Secret 不写入 YAML、URL、Bundle、日志或 Trace。
- 对话构建模型不能直接写文件，只能提交可校验 Patch。
- 导入和 Skill 发现先 inspect，用户确认后 commit。
- Runtime 不执行未绑定或不兼容的 Tool/Skill。
- Local Studio 继续只监听 loopback。
- 云目标未连接时 fail closed。

## 17. 实施波次与依赖

### Wave 0：合同与架构门禁

- 冻结 RuntimeAdapter/RuntimeEvent 行为测试。
- 增加禁止上层依赖 Runner 的架构测试。
- 增加待删除符号零引用测试。

### Wave 1：执行内核

- `CodexRuntime` 重命名为 `CodexRuntimeAdapter` 并直连核心执行链。
- 核心 RuntimeRegistry Factory。
- 核心 RuntimeExecutor。
- 统一 Web/Server 接收 RuntimeAdapter。
- BaseRunner 瘦身。

Codex、ADK/LangGraph、Web Composition 可在合同冻结后并行。

### Wave 2：Authoring 与 Catalog

- Runtime Catalog 和依赖检测。
- 统一 Agent Draft/Revision/Codec。
- Tool 兼容矩阵。
- Skill Discovery/Inspect/Import。
- 全局上下文前端。

### Wave 3：创建与运行闭环

- 快速创建。
- 对话构建。
- 导入。
- 项目识别。
- Build、Session、Responses、取消和刷新恢复。

### Wave 4：可观测与验收

- OTLP 一致性。
- Raw OTLP 复制。
- 应用内浏览器全场景回归。
- 删除旧代码、文档和兼容提示。

### 17.1 模块化工作包、依赖和工作量

工作量用于表示复杂度，不是工期承诺：`S` 为局部修改，`M` 为单模块完整闭环，`L` 为跨层闭环，`XL` 为多个跨层闭环。

| 工作包 | 主要工作 | 可独立验收的产物 | 依赖 | 工作量 |
|---|---|---|---|---|
| G0 合同与门禁 | 冻结 RuntimeAdapter 六动词、RuntimeEvent、Responses/OTLP 投影规则、删除符号和禁止导入规则 | 合同测试与架构测试先红后绿 | 无 | M |
| G1 Runtime 内核统一 | 完成 Factory Registry、RuntimeExecutor、Handle 所有权；Codex 统一为 `CodexRuntimeAdapter`；BaseRunner 退回框架内部 | 三 Runtime 合同测试；上层不再选择 Runner | G0 | XL |
| G2 Server 与协议入口 | Web、Run、Responses、AG-UI、A2A、Harness 全部注入 Executor/Adapter；统一 stream/cancel/resume/attach | 同一 Fake Adapter 可通过所有协议入口；标准 SSE Fixture 一致 | G1 | XL |
| G3 Agent Draft/Revision 与构建 | 四种创建方式汇聚 Draft；Runtime Codec、依赖探测、校验、Bundle 和供应链摘要 | Codex/ADK/LangGraph 各生成一个可重复构建的本地 Bundle | G0，可与 G1 后半段并行 | XL |
| G4 全局上下文与多 Agent | Workspace/Agent/Target 选择器；所有 Tab 查询显式带上下文；Agent 编辑、删除、迁移 | 至少三个 Agent 跨 Tab 切换无串数据 | G3，接口模型可与 G2 并行 | L |
| G5 模型、Tool 与 Skill | 模型真实探测、多绑定和会话切换；内置/自定义/MCP Tool Catalog；Skill 发现、inspect、commit、绑定 | 能绑定两个模型、一个 Tool、一个 Skill 并进入 Revision/Bundle | G3，Catalog 可独立并行 | XL |
| G6 会话与对话体验 | 标准 Responses 请求、增量事件、动态输入框、长内容滚动、Session CRUD、Run attach/cancel | 运行中刷新可恢复，显式取消才进入 CANCELLED，会话真删除 | G2 + G4；UI 可与 G5 并行 | XL |
| G7 OTLP Trace Explorer | RuntimeEvent→OTLP、持久化/查询、Span 树详情、真实 usage/duration、Raw OTLP 复制 | 本地三 Runtime Trace 均通过 OTLP Schema 与 UI 验收 | G2；采集可与 G4-G6 并行 | L |
| G8 前端工程化与去重 | 拆分 Studio Web 源码、复用 ksadk-web 稳定包、统一构建产物、删除旧静态界面和重复 Runtime | wheel 内只有一套可用 Studio 前端，静态资源可离线加载 | G2-G7 逐步收口 | L |
| G9 发布与浏览器验收 | 全量测试、lint、mypy/语法、wheel 构建安装、应用内浏览器逐项验证并留证 | 干净环境安装 wheel 后完整演示，验收矩阵全部 PASS | G1-G8 | L |

### 17.2 并行关系

```mermaid
flowchart LR
    G0["G0 合同与门禁"] --> G1["G1 Runtime 内核"]
    G0 --> G3["G3 Draft / 构建"]
    G1 --> G2["G2 Server / 协议"]
    G3 --> G4["G4 全局上下文"]
    G3 --> G5["G5 模型 / Tool / Skill"]
    G2 --> G6["G6 Session / Chat"]
    G4 --> G6
    G2 --> G7["G7 OTLP Trace"]
    G4 --> G8["G8 前端统一 / 去重"]
    G5 --> G8
    G6 --> G8
    G7 --> G8
    G8 --> G9["G9 发布 / 浏览器验收"]
```

- G0 冻结后，G1、G3 的非重叠部分可以并行。
- G2 完成 Executor 注入合同后，协议入口、G5 Catalog、G7 Trace 采集可以并行。
- G4 与 G6 必须共享同一个上下文键和 URL 合同，不能分别造前端全局状态。
- G8 不是最后一次“大重写”；各功能合并时就迁入统一前端模块，最终只做去重和发布收口。

## 18. 验收门禁

以下条件必须全部满足：

1. `CodexRunner`、`CodexStudioRuntime`、`StudioCodexRuntimeAdapter`、`StudioRuntimeAdapterRegistry` 零引用。
2. Codex 公开执行类只有 `CodexRuntimeAdapter`。
3. `server/`、`studio/`、`agui/`、`a2a/` 不直接导入 `ksadk.runners`。
4. Codex、ADK、LangGraph 分别通过创建、构建、流式对话、取消、刷新恢复和 Trace 验收。
5. 四种创建入口都产生同一种可构建 Draft，无纯 UI Mock。
6. 多 Agent 能真实切换、编辑和删除，所有 Tab 跟随全局上下文。
7. 内置 Tool 可见；自定义 Tool 可绑定和执行。
8. Skill 可发现、校验、确认导入、绑定并进入 Bundle。
9. Responses 非流式和 SSE 流式协议测试通过。
10. OTLP 结构测试通过，Raw OTLP 可以完整复制。
11. 相关全量测试、lint、类型/语法检查和 `git diff --check` 全绿。
12. 应用内浏览器完成并记录所有真实功能验收。

## 19. Goal 停止条件

### 19.1 允许标记完成的停止条件

只有第 18 节 12 项验收门禁全部满足，并同时具备以下证据，Goal 才允许标记 `complete`：

1. 架构证据：禁止上层导入 Runner 的测试通过，所有待删除类和重复静态入口零引用。
2. 自动化证据：目标测试、相关全量测试、Ruff、mypy/语法、前端构建、wheel 构建安装和 `git diff --check` 全绿。
3. 协议证据：三 Runtime 的非流式/流式 Responses、cancel、resume/attach、Session 和 OTLP Fixture 通过。
4. 产品证据：在应用内浏览器逐项执行第 2.2 节场景，记录实际输入、结果、Trace/Run ID 和截图；不能用 API 单测替代 UI 验收。
5. 发布证据：从新构建的 wheel 安装启动，而不是依赖源码目录或开发服务器残留资源。
6. 诚实性证据：断开模型凭证、缺 Runtime 依赖、未连接云目标时均明确失败，无 Mock 成功、猜测 Token 或假能力。

满足这些条件时停止的是本地演示 Goal；真实金山云部署仍由后续 Goal 承接。

### 19.2 运行中允许暂停并请求用户的条件

不得因为时间、token、工作量、测试失败、改动文件较多或第一次方案不可行而停止。

只有以下情况可以暂停并请求用户：

- 需要改变已确认的产品合同。
- 需要不可恢复的破坏性迁移。
- 需要新的外部账号、凭证或系统权限。
- 同一个外部依赖连续三个 Goal 回合阻塞，且已经穷尽安全替代路径。
- 现有公开兼容合同与本设计存在无法自动解决的冲突。

对于外部依赖阻塞，必须记录三轮使用了哪些检查和替代路径；未达到三轮同因阻塞不得标记 `blocked`。

### 19.3 产品内 Agent Run 的停止语义

- 用户点击取消时，按 `(runtimeType, invocationId, sessionId)` 精确取消当前 Handle，并持久化 Runtime 返回的真实终态。
- 页面刷新、路由切换、Agent 切换和浏览器 SSE 断开不得自动把运行标记为 `CANCELLED`；Server 必须继续持有运行并允许重新 attach。
- Runtime 已不可恢复时标记 `INTERRUPTED/ATTACH_UNAVAILABLE` 并说明原因，不能冒充用户取消。
- Session 删除若涉及活跃 Run，必须先明确提示并由用户确认是否取消；不得静默终止。
- token、运行时长和前端等待超时默认只用于展示/诊断，不构成自动停止条件；只有 Agent Revision 明确配置了预算策略时才由 Runtime 执行相应终止。
