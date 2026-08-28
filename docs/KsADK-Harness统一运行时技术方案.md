# KsADK Harness 统一运行时技术方案

> 文档状态：设计基线（已评审修订）  
> 适用仓库：`ksadk-python`  
> 当前基线分支：`feat-studio-workbench`（基线测试：后端 `pytest tests/ -q` 全绿、React `npm test` + `run test:ui` 全绿）  
> 盘点日期：2026-08-25

## 1. 文档目的

本文定义 KsADK Harness 的目标、边界、现状、开源借鉴、核心协议、默认执行引擎、迁移路径和验收标准。

KsADK 当前已经具备 RuntimeAdapter、Conversation、Session、Memory、MCP、Skill、Sandbox、Studio 生命周期和多 Agent 样板等能力，但这些能力分散在多条执行路径中，尚未形成一个由 KsADK 统一掌控的默认 Harness。

本文要解决的核心问题不是“再写一个 Agent Loop”，而是：

1. 把已有能力收敛到同一条默认执行主线；
2. 明确 KsADK 与 LangGraph、Codex、ADK 等执行框架的所有权边界；
3. 让 Studio 创建、调试、构建、部署和正式调用使用同一套运行语义；
4. 形成可测试、可替换、可治理的企业 Agent Harness；
5. 保留已有 Runner 的兼容托管能力，但不要求普通用户选择 Runner。

---

## 2. 产品定位与核心决策

### 2.1 产品定位

KsADK Studio 是企业 Agent 的统一创建、能力接入、调试、评测、发布和运行治理平台。用户可以通过自然语言、模板或手动方式创建 Agent，平台负责把业务意图编译为可审计、可构建、可部署的 Agent Revision，并运行在 KsADK Managed Runtime 中。

### 2.2 默认运行模式

普通 Studio 用户只看到：

```text
KsADK Managed Runtime
```

其内部默认执行链路为：

```text
Studio
  -> AgentProject / AgentRevision
  -> HarnessCompiler
  -> HarnessSpec
  -> KsADK Harness
  -> ManagedLangGraphEngine
  -> Model / MCP / Skill / Memory / Sandbox
```

普通创建流程不展示 Codex、ADK、LangGraph 等 Runner 选择。

### 2.3 兼容运行模式

已有 Agent 项目通过“导入已有项目”进入兼容托管路径：

```text
RuntimeAdapter Contract
  |- KsADK Harness                默认、深度接管
  |- ADK RuntimeAdapter           兼容托管
  |- External LangGraph Adapter   兼容用户已有 Graph
  |- Codex RuntimeAdapter         Coding Agent / Computer-use Agent
  `- BYO Runtime                  外部或容器托管
```

兼容模式保留原框架的 Agent Loop 所有权，平台只承诺适配器真实支持的能力，不伪造一致性。

### 2.4 关键技术决策

1. **KsADK 自己定义 Harness 语义**：Prompt、Context、Memory、Tool、Approval、Artifact、Lifecycle 和 Event Contract 归 KsADK。
2. **LangGraph 作为默认执行内核**：复用状态图、Checkpoint、Interrupt、Resume、Streaming 和 Durable Execution，不把 LangGraph 类型暴露到平台协议。
3. **不从零重写图执行器**：KsADK 聚焦企业 Agent 语义和治理。
4. **不直接 Fork Codex 作为唯一默认 Harness**：Codex 保留为强力 Coding/Computer-use Engine，并借鉴其 Agent Loop、Tool、Sandbox 和 Conformance 设计。
5. **多 Agent 是 Execution Strategy**：不固化进业务模板，不要求所有 Agent 使用 Manager–Executor–Reviewer。
6. **Studio 调试和正式调用同源**：Draft 调试允许显式降级，Revision/Deployment 调用必须使用同一 Harness Compiler 和 Engine。

---

## 3. 当前实现盘点

### 3.1 当前主线已经收口为默认 Harness

```text
Studio / CLI / RuntimeAdapter
          |
      KsADK Harness
          |
  HarnessSpec + Context/Memory + MCP/Skill + Approval/Receipt
          |
  ManagedLangGraphEngine（默认）
          |
  RuntimeEvent / Checkpoint / Artifact / Lifecycle
```

业务用户默认使用 KsADK Harness，不选择 Runner。LangGraph 提供状态图、Checkpoint
和 Interrupt/Resume，但 Prompt、Context、Memory、能力披露、审批、生命周期与事件协议
均由 KsADK 定义。ADK、Codex 和外部 LangGraph 项目通过 RuntimeAdapter 兼容接入，
不等同于由平台完整接管其内部 Context 和 Agent Loop。

### 3.2 核心能力现状

| 能力 | 当前实现 | 主要缺口 |
|---|---|---|
| Agent Loop | 默认 ManagedLangGraphEngine 已接入 Reason/Tool/Approval/Resume | 多模型与真实远程 Sandbox 仍需扩大一致性验证 |
| Runtime Contract | HarnessSpec、RuntimeAdapter、Capability 声明 | ADK/Codex 深接管程度仍不同 |
| Session/恢复 | Checkpoint、跨进程 attach/resume、Transcript 投影 | 云端 Session 事实源属于控制面协同范围 |
| Context | Manifest、预算、WorkingContext Patch、压缩与关键事实重注入 | 更大规模长任务数据集仍需持续评测 |
| 长期 Memory | Scope 检索、纠错/遗忘/锁定、审计、真实模型评测 | 生产数据治理与规模化评测仍需平台服务配合 |
| MCP Runtime | L0-L3 渐进披露、健康/熔断、审批、幂等、结果外置 | 更多真实 MCP Transport 故障注入待覆盖 |
| Skill Runtime | L0-L3 渐进披露、三级解析、父子 Agent 透传 | Skill 管理面仍由 Skill Service 负责 |
| Sandbox | 异步 Backend 合同、能力声明、本地只读/进程后端、超时/取消/Artifact/清理 Conformance | E2B/平台私有后端仍需在真实模板和凭证环境跑远程矩阵 |
| Tool Approval | Interrupt/Resume、Receipt、动态风险判定 | 外部副作用仍取决于 Transport 幂等合同 |
| Observability | Run/Turn/Tool/Context/Memory/Usage/Artifact 统一事件 | Studio 的聚合展示仍可继续增强 |
| Lifecycle | DraftRuntime 即时调试与 Revision/Build/Deploy/Activate 正式链路隔离 | 云端准入、路由和回滚由控制面完成 |
| 多 Agent | 可选 Strategy、子 Agent 事件与能力继承 | 不固化进业务模板，更多策略后续按需增加 |
| Tool Reliability | Receipt、Transport 幂等、四级诚实交付语义 | 不承诺跨系统事务意义的 exactly-once |

### 3.3 当前边界

- 默认引擎是 ManagedLangGraphEngine，但平台合同不暴露 LangGraph 私有类型；
- 兼容多 Runner 表示统一接入、事件和能力声明，不表示平台能无条件接管第三方
  Runner 的 Prompt、Context、恢复与 Tool Loop；
- Skill Service 负责 Skill 注册、CRUD 和版本治理，SDK 只负责运行时消费；
- Agent Registry、Gateway Route、Hosted Runtime 和云端回滚属于控制面；
- Sandbox 以通用 Backend 抽象接入，当前优先 E2B，不把业务逻辑写死到单一后端；
- 对外部副作用只能按实际能力声明可靠性，不能用 Receipt 掩盖 Transport 不幂等。

### 3.4 可复用资产

建设 KsADK Harness 不应重复实现以下已有能力：

| 已有资产 | 应迁移到 Harness 的角色 |
|---|---|
| `RuntimeAdapter` | Harness 对外统一运行协议 |
| `RuntimeEvent` | Run/Turn/Tool/Usage/Artifact 事件基线 |
| Conversation preprocessing | Harness 输入标准化和上下文准备 |
| Session backends | Thread/Session 持久化 |
| Transcript events | 可恢复会话事实源 |
| Compaction pipeline | 默认 ContextEngine 的压缩能力 |
| LongTermMemoryService | Memory Store 与检索基础 |
| MCP Toolset | Tool discovery 与真实调用 |
| Skill Runtime | Skill 加载、校验和 Sandbox 执行 |
| Sandbox Runtime | 通用隔离执行后端 |
| Studio Revision/Build | HarnessSpec 编译与 Bundle 生命周期 |
| Orchestration contracts | 可选 Multi-agent Execution Strategy |

---

## 4. 开源项目借鉴原则

### 4.1 借鉴方式

对开源项目采用三种方式：

1. **直接依赖执行原语**：例如 LangGraph StateGraph、Checkpointer；
2. **转译公开设计思想**：例如 OpenClaw 压缩前整理、Letta Core Memory Block；
3. **保留独立兼容 Engine**：例如 Codex，不把 Coding 假设上移到通用 Harness。

任何源码复用必须完成 License、NOTICE、第三方依赖和安全评审。第三方 Prompt、逆向分析结果和未公开协议不能直接复制到 KsADK。

### 4.2 借鉴矩阵

| 项目 | 借鉴能力 | 在 KsADK 的落点 | 明确不采用 |
|---|---|---|---|
| OpenClaw | Stable Prompt/动态 Context 分离、Tool Pair 边界、压缩前 Memory Flush、保留最近尾部、overflow 后压缩重试（上限对齐 §8.4"紧急压缩只重试一次"，防循环） | `context/builder.py`、`context/compaction.py` | 不把本地 `MEMORY.md` 当企业云端事实源 |
| Hermes | Working Context、Session、Memory、Skill 分层；主动/紧急压缩；可替换 Context Engine | `context/engine.py`、Provider 接口 | 不采用固定 Token 常数，不做 Skill 自进化 |
| Letta/MemGPT | 有名称、描述、大小限制、写权限的 Core Memory Block | `memory/blocks.py` | 不允许所有 Block 无限常驻 Prompt |
| OpenHands | Skill Manifest、先描述后正文、按需资源路径 | `capabilities/disclosure.py` | 不引入其完整 Coding Agent 运行模型 |
| LangGraph | Thread Checkpoint、Store、Interrupt/Resume、Subgraph、Durable Execution | `engine/langgraph.py` | 不向 Studio 暴露 Graph 类型，不魔改用户已编译 Graph |
| Mem0 | Memory Extract/Dedup/Conflict/Add/Update/Delete | `memory/pipeline.py` | 首阶段不引入完整服务和图记忆 |
| Codex | Agent Loop、Tool Pair、Sandbox Policy、Rollout、事件、Cancel、长任务连续性、Conformance | Codex Engine + Harness 设计参考 | 不复制专有 Prompt，不把 Coding Workspace 当通用 Memory |
| Claude Code 公开分析 | Prompt Section、缓存边界、Working Notes、分层压缩、压缩后重注入 | Context Trace 与 Cache Diagnostics | 不依赖第三方逆向结果作为正式接口 |
| claw-code | Prompt Builder 输入约束、Tool Pair、cache-break 统计、Parity Test | `conformance/` | 不采用实验性 Completion Cache |

### 4.3 为什么默认使用 LangGraph，而不是把 LangGraph 当平台协议

LangGraph 适合提供：

- 状态图和条件路由；
- 节点级 Checkpoint；
- Interrupt/Resume；
- Pending Write；
- 子图和并行；
- Streaming；
- Durable Execution。

KsADK 必须自己定义：

- Stable Prompt 的组成；
- Working Context 的结构和预算；
- Memory 的事实、权限和生命周期；
- Skill 渐进披露；
- MCP 降级和企业 Gateway；
- Tool Approval；
- Revision/Build/Deploy/Activate；
- Tenant/User/Session 权限；
- RuntimeEvent 和 Artifact Provenance；
- Engine Conformance。

平台协议不得出现 `StateGraph`、`RunnableConfig`、`Command`、`ToolNode` 或 LangGraph Channel 等类型。

### 4.4 Codex 的定位

Codex 开源核心值得深入参考，也可以作为独立 Engine 继续演进，但不建议直接成为所有企业 Agent 的唯一默认 Harness，原因包括：

- 核心以 Rust 和 Coding Workspace 为中心，与 KsADK Python 双栈；
- Shell、Patch、Git、Repository Search 等 Coding 假设较强；
- 多模型 Tool Calling 行为仍需单独验证；
- 企业 Memory、组织权限、审批和发布生命周期仍需 KsADK 实现；
- 财务、客服等 Agent 的 Artifact 和 Approval 语义与 Coding Agent 不同。

推荐定位：

```text
KsADK Harness + ManagedLangGraphEngine   通用企业 Agent 默认引擎
CodexExecutionEngine                     Coding / Computer-use 强引擎
```

---

## 5. 目标总体架构

```text
Studio / CLI / OpenAI API / A2A
                |
        RuntimeAdapter Contract
                |
          KsADK Harness
                |
  +-------------+--------------+----------------+
  |             |              |                |
HarnessSpec  ContextEngine  CapabilityRuntime  PolicyRuntime
  |             |              |                |
  |         Session/Memory   MCP/Skill       Approval/Sandbox
  |             |              |                |
  +-------------+--------------+----------------+
                |
          ExecutionStrategy
          |       |        |
        Single  PlanExec  PlanExecReview
                |
          ExecutionEngine
                |
     ManagedLangGraphEngine (default)
                |
      Event / Trace / Usage / Artifact
```

### 5.1 分层职责

| 层 | 职责 | 不负责 |
|---|---|---|
| Harness Compiler | Revision 到 HarnessSpec、依赖固定、构建清单 | 运行 Tool |
| Harness Runtime | Run 生命周期、状态机、服务编排 | Studio 页面 |
| Context Engine | Prompt/Context 组装、预算、压缩、恢复 | 长期数据物理存储 |
| Memory Runtime | Memory Block、检索、写入策略、冲突 | 图节点调度 |
| Capability Runtime | MCP/Skill 发现、加载、调用、降级 | 发布审批 |
| Policy Runtime | Tool 风险、审批、网络、数据策略 | 模型推理 |
| Sandbox Runtime | 隔离执行、资源限制、Artifact | Memory 事实判断 |
| Execution Strategy | 单 Agent/多 Agent 业务无关策略 | LangGraph 存储协议 |
| Execution Engine | 图执行、Checkpoint、Interrupt/Resume | 平台产品语义 |
| Observability | Run/Turn/Tool/Context/Usage/Artifact 事件 | 修改执行结果 |

---

## 6. 核心领域模型

### 6.1 HarnessSpec

`HarnessSpec` 是 Revision 编译后的、与执行引擎无关的不可变运行规范。

```python
class HarnessSpec(BaseModel):
    schema_version: str
    harness_version: str
    agent_revision_ref: str

    model: ModelBinding
    prompt: PromptSpec
    context_policy: ContextPolicy
    memory_policy: MemoryPolicy
    capabilities: CapabilityBindings
    execution_strategy: ExecutionStrategySpec
    approval_policy: ApprovalPolicy
    sandbox_policy: SandboxPolicy
    observability_policy: ObservabilityPolicy
```

要求：

1. 所有资源引用必须固定版本；
2. 不包含明文 Secret；
3. 不包含 Runner 专有类型；
4. 支持内容 Hash；
5. 能够生成可审计 Build Manifest；
6. 同一 Spec 与 Harness 版本应具有可解释的行为基线。

### 6.2 HarnessState

```python
class HarnessState(BaseModel):
    tenant_id: str
    user_id: str
    agent_id: str
    session_id: str
    run_id: str
    turn_id: str

    messages: list[Message]
    working_context: WorkingContext
    memory_refs: list[MemoryRef]
    capability_snapshot: CapabilitySnapshot
    pending_tool_calls: list[ToolCall]
    pending_approval: ApprovalRequest | None
    artifacts: list[ArtifactRef]
    retry_state: RetryState
    status: RunStatus
```

State 中只保存恢复所需的最小结构化状态；大 Tool Result、大文件和 Artifact 使用外部引用，避免 Checkpoint 无限膨胀。

### 6.2.1 HarnessState 与引擎图 State 的关系（决策）

`HarnessState` **不是** LangGraph 图 State，二者关系固定如下：

- 引擎图 State（Channel）只保存最小路由信息：消息引用列表（不含正文）、待执行 ToolCall 索引、轮次计数、状态机枚举。单个节点序列化体积必须可预估；
- `HarnessState` 是 Checkpoint 之外的影子状态，由 Harness Runtime 持有并持久化到 Session/Transcript 存储；
- 恢复语义：进程重启后先恢复图 Checkpoint（路由位置），再由 Harness Runtime 从 Transcript 重建 `HarnessState` 的消息与上下文部分；两份状态以 `checkpoint_id` 关联，出现不一致时以 Transcript 为准并记录 `context.recovered` 事件。

理由：这是 §21"Checkpoint 过大"风险的根治方案，而不只是 TTL 和压缩。选择影子状态方案的代价（恢复要重放两份状态）通过"以 Transcript 为准"的一致性规则封顶。

### 6.2.2 Checkpointer 租户编码（决策）

LangGraph Checkpointer 的 `thread_id` 是扁平字符串，无租户语义。从第一天起统一编码：

```text
thread_id = tenant:{tenant_id}/user:{user_id}/agent:{agent_id}/session:{session_id}/run:{run_id}
```

编码规则由 `ksadk/harness/engine/thread_ids.py` 唯一实现，禁止各处手拼。Checkpointer Adapter 必须在写入侧强制校验编码合法性，读取侧按前缀过滤实现租户隔离。Approval 跨进程恢复（§11.2）依赖该编码，因此它是 Phase 1 而非 Phase 3 的交付物。

### 6.3 ExecutionEngine

```python
class ExecutionEngine(Protocol):
    async def compile(self, spec: HarnessSpec) -> CompiledHarness: ...
    async def start(self, request: StartRequest) -> RunHandle: ...
    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]: ...
    async def cancel(self, handle: RunHandle) -> CancelResult: ...
    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle: ...
    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor: ...
    async def close(self, handle: RunHandle) -> None: ...
```

一期实现 `ManagedLangGraphEngine`，接口用于隔离领域语义，不代表一期向用户提供多 Engine 选择。

### 6.3.1 两个 compile 的关系（澄清）

`ExecutionStrategy.compile(spec) -> ExecutionPlan` 与 `ExecutionEngine.compile(spec) -> CompiledHarness` 是两阶段编译，顺序固定：

```text
HarnessSpec
  -> ExecutionStrategy.compile   产出 ExecutionPlan：图拓扑（几个 reason 节点、
                                  怎么路由、是否含 plan/review 节点）——纯数据，
                                  引擎无关
  -> ExecutionEngine.compile     产出 CompiledHarness：把 ExecutionPlan 绑定到具体
                                  执行机制（Checkpointer、Streamer、节点实现）——
                                  可执行单元
```

Strategy 只描述"图长什么样"，Engine 负责"怎么执行这张图"。单 Agent 策略的 ExecutionPlan 是只有一个 reason 节点自环的平凡拓扑。

---

## 7. 默认 Agent Loop

### 7.1 默认图

```text
START
  -> load_revision
  -> prepare_context
  -> context_budget_check
       |- normal -----------------------------+
       |- proactive -> pre_compaction -> compact
       `- emergency -> emergency_compact ------+
                                                 |
                                              reason
                                                |
                  +-----------------------------+------------------+
                  |                                                |
               final                                           tool_calls
                  |                                                |
          persist_memory_candidates                         resolve_tools
                  |                                                |
          persist_turn / artifacts                          policy_check
                  |                                      /       |       \
                 END                                  allow  approval   deny
                                                        |       |        |
                                                  execute    INTERRUPT  tool_error
                                                        |       |
                                                     tool_result
                                                        |
                                                  update_context
                                                        |
                                                      reason
```

### 7.2 稳定性策略

模型调用必须区分：

- 可重试 Provider 错误；
- Rate Limit；
- Timeout；
- Context Overflow；
- Schema/Tool Call 错误；
- 不可重试鉴权和配置错误。

Tool 调用必须支持：

- timeout；
- idempotency key；
- retry policy；
- circuit breaker；
- cancellation；
- structured error；
- approval interrupt；
- receipt 持久化；
- Tool Call 与 Tool Result 成对事件。

### 7.3 Cancel、Pause、Resume

- Cancel 是终止语义，不能伪装为可恢复 Pause；
- Approval 使用非终止 Interrupt；
- Resume 必须携带明确目标和 payload；
- Tool Result 重放必须使用 Receipt 防止重复副作用；
- Checkpoint 必须声明持久化范围和跨实例能力；
- Engine 不支持某能力时必须诚实声明。

### 7.4 Tool Reliability Conformance

每次 Tool Call 在 `tool.call.begin/end` 中记录 metadata-only 的可靠性声明：

| 语义 | 条件 | 崩溃恢复含义 |
|---|---|---|
| `replay_safe` | 只读或无副作用 | 可以安全重放 |
| `effectively_once` | Receipt + Transport 幂等均成立 | 提交前后两个重复窗口均被关闭 |
| `at_least_once` | 有 Receipt，但外部 Transport 不保证幂等 | Receipt 前崩溃可能重复外部副作用 |
| `best_effort` | 无 Receipt，或副作用合同未知 | 不自动抬高可靠性承诺 |

Harness 不声明跨网络、跨存储事务意义的 `exactly-once`。Conformance 必须覆盖：

1. 外部调用前崩溃；
2. 外部调用成功、Receipt 提交前崩溃；
3. Receipt 提交后、Graph Checkpoint 前崩溃；
4. Checkpoint 已提交、Run 终态事件前崩溃。

Local Tool、MCP、Sandbox、Approval 和 Subagent 均使用同一套声明与故障窗口测试；
其中外部副作用路径只有在 Transport 实际消费稳定幂等键时，才能升级为
`effectively_once`。

---

## 8. Context Engine

### 8.1 Context 分层

```text
Stable Prompt
  |- Platform safety and runtime contract
  |- Agent identity and role
  |- Organization policy references
  `- Stable tool/skill guidance summary

Dynamic Context
  |- Current user input
  |- Working Context
  |- Core Memory Blocks
  |- Retrieved long-term memory
  |- Retrieved knowledge
  |- Loaded Skill body
  |- Recent uncompacted turns
  `- Compaction checkpoint
```

Stable Prompt 和动态 Context 必须分开构建、分别计算 Hash 和缓存命中信息。

### 8.2 ContextEngine 接口

```python
class ContextEngine(Protocol):
    async def plan(self, request: ContextRequest) -> ContextPlan: ...
    async def compact(self, request: CompactionRequest) -> ContextCheckpoint: ...
    async def recover(self, failure: ContextOverflow) -> ContextPlan: ...
```

### 8.3 Token Budget

不能使用所有模型统一的固定 Token 常数。预算来源优先级：

1. Model Profile 明确上下文窗口；
2. Provider `/models` 或平台模型目录；
3. 受审核的静态模型元数据；
4. 最低安全默认值并记录 fallback。

预算建议分区：

```text
context_window
  = stable_prompt
  + core_memory
  + retrieved_context
  + recent_turns
  + working_context
  + tool_schema
  + reserved_output
  + safety_buffer
```

### 8.4 主动压缩与紧急压缩

主动压缩：在预算达到阈值前执行，避免临近窗口才处理。

紧急压缩：模型明确返回 context overflow 时，执行更强压缩并只重试一次，避免无限循环。

压缩约束：

- 不拆分 Tool Call/Tool Result；
- 保留最近完整尾部；
- 保留 opaque ID、金额、日期、版本和审批编号；
- 压缩前整理未持久化事实；
- 完整 Transcript 不删除；
- 压缩结果作为 ContextCheckpoint 持久化；
- 关键约束在压缩后重新注入；
- 记录输入 Token、输出 Token、压缩范围和摘要模型。

#### 8.4.1 现有 `runtime_compaction.py` 差距盘点

"复用 compaction pipeline"不等于"已完成"。逐条对照：

| 压缩约束 | 现有实现 | 差距 |
|---|---|---|
| 不拆分 Tool Call/Tool Result | 满足（按完整消息边界切） | 无 |
| 保留最近完整尾部 | 满足 | 无 |
| 保留 opaque ID/金额/日期/版本/审批编号 | 部分（有语义摘要提示词，无字段级保证） | 需要后校验（正则抽关键 ID 比对压缩前后存在性） |
| 压缩前整理未持久化事实 | 不满足 | 需新增 pre-compaction flush 节点 |
| 完整 Transcript 不删除 | 满足 | 无 |
| 压缩结果作为 ContextCheckpoint 持久化 | 部分（有压缩记录，无 Checkpoint 语义） | 需挂接 §8.2 ContextCheckpoint |
| 关键约束压缩后重注入 | 不满足 | 需新增 post-compaction reinjection |
| 记录输入/输出 Token、范围、摘要模型 | 部分 | 补齐字段 |

Phase 2 的"复用"指：管线骨架、消息边界切分、语义摘要提示词直接复用；pre-flush、后校验、重注入为新增节点。

### 8.5 Working Context

Working Context 不是长期 Memory，包含：

- 当前目标；
- 已确认约束；
- 未解决问题；
- 当前计划；
- 已验证事实；
- Artifact 引用；
- 最近 Tool 失败与降级状态。

其生命周期限定在 Session/Task，可被压缩、重建和审计。

---

## 9. Memory Runtime

### 9.1 作用域

```text
Run State       单次运行
Session Memory  单会话短期连续性
Agent Memory    同一 Agent 跨会话
User Memory     同一用户跨 Agent（受权限控制）
Org Memory      组织规则和共享事实（严格写权限）
```

### 9.2 Core Memory Block

```python
class CoreMemoryBlock(BaseModel):
    id: str
    name: str
    description: str
    value: str
    max_chars: int
    scope: MemoryScope
    writable_by: list[str]
    source_refs: list[str]
    version: int
    sensitivity: str
```

常驻 Block 数量和总预算必须有限；超预算内容进入按需检索。

### 9.3 Memory 写入管线

```text
Candidate Extraction
  -> Source Validation
  -> Normalize
  -> Deduplicate
  -> Conflict Resolution
  -> Permission Check
  -> add / update / delete / ignore
  -> Audit Event
```

禁止：

- 直接把整段聊天写入长期 Memory；
- 无来源事实写入组织级 Memory；
- 模型自行修改无写权限 Block；
- 将敏感 Tool Result 未脱敏写入跨用户 Memory；
- 删除或覆盖 Memory 而不保留审计记录。

---

## 10. MCP 与 Skill Runtime

### 10.1 统一 Capability Registry

```python
class CapabilityDescriptor(BaseModel):
    id: str
    kind: Literal["mcp", "skill", "builtin", "sandbox"]
    name: str
    description: str
    version: str
    risk_level: str
    load_policy: Literal["always", "on_demand", "explicit"]
    dependencies: list[CapabilityRef]
```

### 10.2 MCP 生命周期

```text
Revision binding
  -> Resolve pinned server version
  -> Credential reference resolution
  -> Health/probe
  -> tools/list cache
  -> Schema validation
  -> Tool exposure/filter/prefix
  -> Runtime invocation
  -> Receipt/Trace
```

### 10.3 MCP 降级策略

| 场景 | Draft 调试 | Revision/正式运行 |
|---|---|---|
| 可选 MCP 不可用 | 告警并降级 | 按 Policy 决定降级或失败 |
| 必需 MCP 不可用 | 可无工具调试并明确提示 | 阻止激活或运行失败 |
| 单个 Tool Schema 无效 | 隔离该 Tool | 发布门禁失败 |
| Tool Timeout | 结构化错误，可有限重试 | 记录 Receipt 和 Trace |
| MCP 熔断 | 模型收到可解释降级信息 | 观测中心产生告警 |

### 10.4 Skill 渐进披露

```text
Level 0：名称 + 一句话描述
Level 1：Manifest + 使用条件 + Tool/权限需求
Level 2：完整 SKILL.md
Level 3：引用资源、脚本或数据，仅执行时加载
```

Skill Runtime 必须复用现有包校验、安全解压、Loader 和 Sandbox 编排，不把 Skill 管理面搬进 SDK。

---

## 11. Approval、Policy 与 Sandbox

### 11.1 Tool Policy 决策

```python
class ToolDecision(BaseModel):
    action: Literal["allow", "deny", "require_approval"]
    reason: str
    policy_ref: str
    risk_level: str
    disclosure: str
```

决策输入至少包括：

- Tenant/User/Agent；
- Tool 和版本；
- 参数摘要；
- 数据敏感等级；
- 是否有外部副作用；
- 网络目的地；
- 当前环境；
- Revision Policy；
- 历史 Receipt。

### 11.2 审批恢复流程

```text
Tool Call
  -> Policy: require_approval
  -> persist checkpoint
  -> emit approval.requested
  -> create ApprovalTask
  -> INTERRUPT
  ... minutes/hours later ...
  -> approval decision
  -> RuntimeAdapter.resume
  -> validate revision/checkpoint/receipt
  -> execute once
  -> persist ToolReceipt
  -> continue graph
```

Tool Receipt 负责消除“已提交 Receipt 后的 Graph 重放”。对于“外部调用已成功、
Receipt 尚未提交时进程崩溃”的窗口，Harness 使用稳定的 `run_id + call_id`
派生幂等键，但只向显式声明 `idempotency_mode=transport` 的 MCP Binding 透传，
不会把内部字段注入 Tool 参数而破坏 Schema。支持该合同的 Transport/Gateway
必须将幂等键传到真正执行副作用的服务并持久化去重结果；旧 Transport 保持兼容，
但在该窗口仍是 at-least-once，不能被平台包装成 exactly-once。

### 11.3 Sandbox Backend

Harness 不直接绑定 E2B 对象，统一使用 Sandbox Backend：

```python
class SandboxBackend(Protocol):
    @property
    def capabilities(self) -> SandboxBackendCapabilities: ...
    async def create(self, spec: SandboxSpec) -> SandboxHandle: ...
    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult: ...
    async def collect_artifacts(self, handle: SandboxHandle) -> list[ArtifactRef]: ...
    async def close(self, handle: SandboxHandle) -> None: ...
```

一期优先：

- 保留 Harness 只读本地 Sandbox 作为最低安全模式；
- 接入现有通用 `ksadk.sandbox`；
- E2B 作为优先远程后端；
- 后续扩展 KOP/平台私有 Backend。

Backend 必须声明真实能力，而不是只暴露一个统一类名。当前合同至少区分：

- 文件系统边界：无隔离、受控工作区、临时工作区、远程 Sandbox；
- 网络控制：无控制、命令面限制、仅准入拒绝、后端强制执行；
- 是否具备进程边界、按请求超时、协作取消、Artifact 收集、确定性清理、
  执行审计和跨进程重连。

`Sandbox Backend Conformance` 使用同一组行为探针验证声明与实现一致：

1. 创建与执行结果遵守统一合同；
2. 声明支持按请求超时的后端必须形成明确超时结果；
3. 声明支持取消的后端必须终止子进程并记录审计，不能只取消等待者；
4. Artifact 只能从声明的工作区收集，关闭后 Handle 必须稳定拒绝执行；
5. 网络策略在创建阶段明确拒绝或由后端强制执行，不能静默放行；
6. 未配置真实远程模板、凭证和网络条件时，E2B/KOP 用例标记为跳过，
   不以 Fake SDK 测试冒充远程隔离 E2E。

当前本地 `SubprocessSandboxBackend` 诚实声明为“临时工作区 + 宿主子进程
边界 + 网络准入拒绝”，不宣称容器/VM 级文件系统、内核或网络隔离；
`LocalReadOnlySandboxBackend` 依靠极窄只读命令面和路径解析提供最低安全模式。

现有 SDK 侧 `ksadk.sandbox` 通过 `SessionSandboxBackendAdapter` 接入 Harness
异步合同。适配层不复制 Session 实现，只负责生命周期、结果和错误语义转换：

- `LocalProcessSandboxBackend` 声明为受控工作区与宿主子进程边界；其工作区
  不随 `kill()` 销毁，因此不声明确定性资源清理；
- `E2BSandboxBackend` 声明为远程 Sandbox；通过 E2B 后台 Command Handle
  执行命令，取消 Harness Task 时先向远端进程发送 `kill()`，因此声明协作
  取消；工作目录由适配层初始化，Artifact 通过远端 Filesystem API 递归枚举，
  只返回目录内的普通文件相对路径，并排除目录外路径与符号链接；
- E2B 适配器显式装配 `SandboxAuditLog` 时才声明执行审计；成功、超时、
  SDK 异常和 Harness Task 取消均按 `run_id + handle_id` 留痕，未装配持久化
  审计库时不宣称可审计；
- E2B 适配器通过 `SandboxResumeToken` 恢复跨进程 Sandbox 会话。Token
  只保存 `backend_id`、稳定 `handle_id` 和厂商 Sandbox ID，不保存
  环境变量、凭证或 Secret；恢复方必须重新提供 `SandboxSpec`，使策略
  与密钥继续由 Revision/Secret 事实源决定。本合同只承诺恢复 Sandbox
  会话，不宣称可恢复崩溃时正在执行的后台进程；
- E2B 当前只有 `allow_internet_access` 布尔开关，不能表达按域名 allowlist。
  禁止联网时声明后端强制控制，允许全量联网时声明无细粒度网络控制；任何
  `network_egress=(domain, ...)` 请求均显式拒绝，避免把全量联网伪装成白名单；
- 真实 E2B Conformance 由 `KSADK_REAL_SANDBOX_E2E=1` 与
  `KSADK_SANDBOX_TEMPLATE_ID` 双重门控。未满足远程模板、凭证和网络条件时
  只运行适配合同单测并明确 skip，不用 Fake SDK 结果替代远程 E2E 结论；
  启用后会额外验证远端命令取消、工作目录 Artifact 枚举与
  跨 Adapter 会话恢复。

---

## 12. 生命周期闭环

### 12.1 状态模型

```text
Draft
  -> Validated
  -> Revision Submitted
  -> Built
  -> Awaiting Approval
  -> Deployed
  -> Active
  -> Running
```

失败状态必须显式：

- ValidationFailed；
- BuildFailed；
- ApprovalRejected；
- DeployFailed；
- ActivationFailed；
- RuntimeUnhealthy；
- Disabled；
- Superseded；
- RolledBack。

### 12.2 Revision 编译

```text
AgentRevision
  -> validate refs
  -> resolve pinned dependencies
  -> compile HarnessSpec
  -> compute content hash
  -> build Agent Bundle
  -> run Harness Conformance subset
  -> generate Build Manifest
```

### 12.3 Build Manifest

至少记录：

```json
{
  "revisionRef": "agent-revision://...",
  "harnessVersion": "...",
  "engine": "managed-langgraph",
  "modelProfileRef": "model-profile://...@...",
  "mcpRefs": [],
  "skillRefs": [],
  "policyRefs": [],
  "contentHash": "sha256:...",
  "artifactDigest": "sha256:..."
}
```

不记录 Secret，只记录 Secret Reference 和版本。

### 12.4 Deploy 与 Activate

Local MVP 必须真实完成：

```text
Build Bundle
  -> Create local runtime instance
  -> Start Harness process
  -> Health check
  -> Register local route
  -> Mark deployment deployed
  -> Activate route
  -> Invoke through active deployment
```

不能只把数据库状态设为 `ACTIVE` 就宣称部署完成。

正式平台场景由 `agentengine-server` 负责 Runtime 生命周期、Route、Registry、Hosted Control 和 Auth；`ksadk-python` 负责 SDK/CLI/数据面 Harness 和本地运行。

---

## 13. 可观测性与 Artifact

### 13.1 事件层级

```text
Run
  |- Turn
  |   |- Context Build
  |   |- Model Call
  |   |- Tool Call
  |   `- Policy Decision
  |- Compaction
  |- Memory Read/Write
  |- Approval
  `- Artifact
```

### 13.2 必需事件

- `run.started/completed/failed/canceled/interrupted/resumed`
- `turn.started/completed`
- `context.planned/compacted/recovered`
- `model.call.started/completed/failed`
- `tool.call.started/completed/failed`
- `policy.decision`
- `approval.requested/resolved`
- `memory.read/write/conflict`
- `usage.reported`
- `artifact.created`
- `checkpoint.created`

### 13.3 观测约束

- Tool Call Begin/End 必须成对；
- 每个事件有 tenant/user/agent/session/run/turn 标识；
- Token Usage 区分输入、缓存输入、输出、压缩；
- 不记录 Reasoning 原文；
- Tool 参数和结果按数据策略脱敏；
- Artifact 记录来源 Run、Tool、Revision 和 Digest；
- Context Trace 记录 section、token、hash，不默认记录敏感正文；
- 事件必须支持顺序恢复和幂等写入。

---

## 14. 多 Agent 作为可选策略

### 14.1 Strategy 接口

```python
class ExecutionStrategy(Protocol):
    def compile(self, spec: HarnessSpec) -> ExecutionPlan: ...
```

一期策略：

1. `single-agent`：默认；
2. `plan-execute`：规划与执行分离；
3. `plan-execute-review`：增加独立审查；
4. `custom-imported`：仅兼容导入路径。

### 14.2 与模板的关系

模板定义业务蓝图和能力槽位，可推荐策略，但不把技术拓扑写成模板核心描述。

例如“财务分析 Agent”模板应描述：

- 预算和实际支出分析目标；
- 必需的预算/支出数据能力；
- 推荐分析 Skill；
- 输出 Artifact；
- 评测集；
- 权限和审批策略。

它可以使用 `single-agent` 或 `plan-execute-review`，策略由创建者或平台策略选择。

### 14.3 现有财务编排迁移

当前 `ksadk.orchestration` 的财务 Manager/Executor/Reviewer 实现应拆分为：

- 通用 `SubtaskContract`；
- 通用 `ExecutionReport`；
- 通用 `AuditReport`；
- 财务 Executor Plugin；
- 财务 Reviewer Plugin；
- `plan-execute-review` Graph Compiler。

---

## 15. Harness Conformance

### 15.1 目的

Conformance 不是要求所有 Engine 原生能力相同，而是验证：

1. 相同外部契约；
2. 事件语义一致；
3. 不支持的能力诚实声明；
4. Tool、Cancel、Resume 和错误行为可预测；
5. 平台不会因 Runner 差异破坏安全和审计。

### 15.2 测试矩阵

```python
@pytest.mark.parametrize(
    "engine_factory",
    [managed_harness, codex, adk, external_langgraph],
)
def test_runtime_conformance(engine_factory):
    verify_start_and_terminal_event()
    verify_event_ordering()
    verify_tool_call_pairing()
    verify_cancel_honesty()
    verify_resume_capability_honesty()
    verify_session_isolation()
    verify_error_mapping()
    verify_secret_redaction()
```

### 15.3 必测场景

- 无 Tool 对话；
- 单 Tool、多 Tool、并行 Tool；
- Tool Timeout 和 Provider 失败；
- MCP 不可用与可选能力降级；
- Approval Interrupt/Resume；
- Cancel active turn；
- Context overflow recovery；
- Compaction 后继续 Tool Call；
- 进程重启恢复；
- Session/Tenant 隔离；
- Artifact 创建和 Digest；
- Secret/PII 脱敏；
- Revision 与 Bundle 可重复加载；
- Engine 升级回归。
- Tool 可靠性声明不高于实际 Receipt/Transport 能力；
- 外部调用前、Receipt 前、Checkpoint 前和终态前四个崩溃窗口；
- 已拒绝审批的 Receipt 重放不得再次创建审批任务。

---

## 16. 建议代码结构

在不一次性大搬迁的前提下逐步收口：

```text
ksadk/harness/
|- spec.py
|- state.py
|- compiler.py
|- runtime.py
|
|- engine/
|  |- base.py
|  `- langgraph.py
|
|- loop/
|  |- reason.py
|  |- retry.py
|  |- interrupt.py
|  `- recovery.py
|
|- context/
|  |- engine.py
|  |- builder.py
|  |- budget.py
|  |- compaction.py
|  |- working_context.py
|  `- sections.py
|
|- memory/
|  |- blocks.py
|  |- pipeline.py
|  `- store_adapter.py
|
|- capabilities/
|  |- registry.py
|  |- mcp.py
|  |- skills.py
|  `- disclosure.py
|
|- policy/
|  |- tools.py
|  |- approval.py
|  `- sandbox.py
|
|- observability/
|  |- events.py
|  |- tracing.py
|  |- usage.py
|  `- artifacts.py
|
`- conformance/
   |- contract.py
   |- fixtures.py
   `- suite.py
```

已有模块先通过 Adapter 接入，验证稳定后再决定是否移动文件，避免大爆炸式重构。

职责切分约定：`loop/` 下是与执行引擎无关的纯逻辑单元（reason 一轮的判定、重试策略、恢复规则），图节点只是调用它们的薄封装。这同时服务 Conformance——引擎无关逻辑可以在无 LangGraph 的环境单测。`reason.py` 不 import LangGraph；`engine/langgraph.py` 才 import。

---

## 17. 分阶段实施计划

### Phase 0：冻结契约与基线

目标：先定义“什么是 KsADK Harness”。

交付：

- `HarnessSpec` v1；
- `HarnessState` v1；
- `ExecutionEngine` 接口；
- RuntimeEvent 增量事件定义；
- Capability Matrix；
- 当前五条执行路径基线测试；
- Harness Conformance 最小套件。

验收：

- 不修改 Studio 用户流程；
- 现有 Runner 测试不回退——门禁命令固定为：`.venv/bin/python -m pytest tests/ -q` 全绿（基线 3221 passed；已知环境性失败 `test_runtime_common_packaging` 需 `uv`，本机豁免）+ `npm --prefix ksadk/studio/react-ui test` + `npm --prefix ksadk/studio/react-ui run test:ui` 全绿；
- Spec 不包含 LangGraph 类型（用架构测试守卫）；
- Secret Scan 通过。

### Phase 1：统一默认 Agent Loop

目标：形成真正可用的 `ManagedLangGraphEngine`。

交付：

- Revision -> HarnessSpec Compiler；
- 单 Agent 默认 Graph；
- 实时 Model/Tool 事件；
- Retry/Timeout/Cancel；
- SQLite/Postgres Checkpointer Adapter；
- Interrupt/Resume；
- Studio Draft Playground 切换到 Harness；
- StudioAgentRuntime 进入兼容/迁移状态。

验收：

- 无 Tool 和单 MCP Tool E2E；
- Tool Event 成对实时出现；
- Cancel 真实中断；
- 进程重启可从 Checkpoint 恢复；
- Studio 与 CLI 使用同一 HarnessSpec。

### Phase 2：Context、Session 与 Memory 收口

目标：复用现有 Conversation 能力，形成默认 ContextEngine。

交付：

- Conversation preprocessing 接入；
- Stable Prompt Section；
- Working Context；
- 动态 Token Budget；
- 主动/紧急压缩；
- 压缩前整理和压缩后重注入；
- Session/Transcript 持久化；
- Core Memory Block；
- 长期 Memory 检索与受控写入。

验收：

- 长会话不会因固定阈值错误压缩；
- Tool Pair 不被拆分；
- 压缩后关键金额、日期、ID 保留；
- 不同用户 Session 和 Memory 隔离；
- Memory 写入有来源、权限和审计。

### Phase 3：MCP、Skill、Approval 与 Sandbox

目标：统一 Capability Runtime 和高风险工具治理。

交付：

- Capability Registry；
- MCP 健康缓存、重连、熔断、降级；
- Skill 四级渐进披露；
- Tool Policy；
- Approval Interrupt/Resume；
- Tool Receipt 幂等；
- 通用 Sandbox Backend 接入；
- Artifact 回传。

验收：

- 可选 MCP 故障不拖垮 Agent；
- 必需能力在正式发布时门禁；
- 高风险 Tool 审批后仅执行一次；
- Skill 正文按需加载；
- Sandbox 越界、网络和写入策略可验证。

### Phase 4：生命周期与正式使用闭环

目标：Revision 到正式对话真正闭环。

交付：

- Build Manifest；
- Local Runtime Instance；
- Health Check；
- Local Route；
- Deploy/Activate；
- 正式 Agent Conversation；
- Rollback；
- 部署观测。

验收：

```text
Revision
  -> Build
  -> Local Deployment
  -> Activate
  -> Open Agent
  -> New Session
  -> Invoke
```

每一步对应真实产物和状态，不能只修改 UI 标签。

### Phase 5：多 Agent 与兼容 Runner

目标：把多 Agent 和外部 Runner 建成可选能力，而不是默认复杂度。

交付：

- Execution Strategy Registry；
- `plan-execute`；
- `plan-execute-review`；
- 财务编排迁移；
- Codex/ADK/External LangGraph Capability Matrix；
- Studio 导入已有项目 Beta；
- 完整跨 Engine Conformance。

验收：

- 单 Agent 模板不包含无关多 Agent 字符串；
- 同一模板可选择不同策略；
- 兼容 Runner 不支持的能力在 UI 中诚实展示；
- 普通创建流程仍不展示 Runner 下拉框。

---

## 18. 迁移与兼容策略

### 18.1 StudioAgentRuntime

迁移期间保留，但标记为 Legacy/Compatibility：

1. 首先让 Draft Playground 支持按开关使用新 Harness；
2. 对相同请求运行 Shadow/Parity Test；
3. 事件和输出稳定后默认切换；
4. Active Deployment 只允许新 Harness；
5. 删除重复的 Prompt、Tool 和 Event 逻辑。

**Parity 判定标准（必须先定义再开跑）**：只比较两件事——

- 事件结构 Parity：事件种类集合、顺序（成对性、首尾事件）、`seq_id` 单调性；
- 最终 Artifact 字段 Parity：结构化输出（verdict、metrics 等）的 schema 一致。

**明确放弃**逐字文本 Parity（两个引擎的模型调用温度与编排顺序不同，逐字比较永远不绿）。Parity 测试的模型调用走录制回放（replay fixture），不双份真实调用，控制成本。

### 18.2 现有 Runner

- 不删除 ADK、LangGraph、Codex Adapter；
- CLI/SDK 继续支持；
- Studio 普通创建不展示；
- 通过“导入已有项目”进入；
- 每个 Adapter 输出真实 Capability Matrix；
- 不强制修改用户已编译 LangGraph Graph。

### 18.3 数据迁移

- 旧 Session/Transcript 通过 Adapter 读取；
- 新 Checkpoint 记录 Harness/Engine/Schema 版本；
- 已发布 Revision 不原地修改；
- 重新 Build 产生新 Build ID；
- Harness 升级需要 Compatibility Test；
- 不保证跨重大 Engine 版本复用私有 Checkpoint，应提供 Transcript Replay 回退路径。

---

## 19. 安全与合规

1. HarnessSpec、Bundle、日志和测试 Fixture 不包含真实 API Key；
2. Checkpoint 可能包含消息和 Tool Result，必须支持加密、TTL 和租户隔离；
3. 反序列化限制允许类型，禁止不受控对象恢复；
4. Tool 参数和结果按 Policy 脱敏；
5. Memory 记录来源、敏感等级、权限和删除审计；
6. 高风险 Tool 默认考虑 Approval、Disclosure、Receipt 和 Audit；
7. Sandbox 默认最小权限；
8. Skill 包执行前完成 Manifest、Hash、路径和解压校验；
9. MCP Credential 使用 Secret Reference；
10. 开源代码复用必须记录 License、版本和修改说明。

---

## 20. 质量门禁与验证

### 20.1 单元测试

- HarnessSpec 校验；
- Context Budget；
- Tool Pair；
- Memory Conflict；
- Policy Decision；
- Event Ordering；
- Secret Redaction。

### 20.2 集成测试

- Model + MCP；
- Model + Skill；
- MCP failure degradation；
- Approval resume；
- Sandbox artifact；
- Checkpoint restore；
- Studio Revision compile。

### 20.3 E2E

- Studio 自然语言创建 Agent；
- 模板创建并补齐缺失 MCP；
- Revision/Build/Deploy/Activate；
- 正式对话新建多个 Session；
- 每个用户会话隔离；
- 高风险 Tool 审批；
- Run/Tool/Token/Artifact Trace；
- Rollback 到旧 Revision。

### 20.4 性能指标

Phase 1 起必须先有采集点再谈数值——所有指标从 RuntimeEvent 时间戳派生（事件已含 `seq_id` 与创建时间），不做额外埋点：

- Context Build 延迟：`context.planned` 事件与 turn 开始的时间差；
- 首 Token 延迟：`run.started` 到首个 `text.delta` 的时间差；
- Tool 调用额外开销：`tool.call.begin` 与前一事件 `model.call.completed` 的时间差；
- Checkpoint 写入延迟：`checkpoint.created` 事件自身耗时字段；
- Resume 延迟：`run.resumed` 与 resume 请求接收的时间差；
- 压缩频率与成本：`context.compaction.*` 事件计数与 `usage.reported` 中的压缩 Token；
- Prompt Cache 命中率：`usage.reported` 的 cached_input_tokens / input_tokens；
- MCP 熔断恢复时间：`capability` 降级与恢复事件的时间差；
- Artifact 上传时间：`artifact.created` 事件自身耗时字段。

目标数值在 Phase 1 完成后用真实基线数据回填，本文不预设。

---

## 21. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| 同时维护多条执行路径 | 行为和 Bug 不一致 | Phase 1 优先统一 Studio 默认路径 |
| LangGraph 类型泄露 | 平台被框架绑定 | HarnessSpec/Engine 接口隔离 |
| Checkpoint 过大 | 存储与恢复慢 | 大结果外置、TTL、压缩和引用 |
| 多模型 Tool Calling 不一致 | 运行不稳定 | Model Conformance 与 Provider Policy |
| MCP 故障放大 | Agent 整体失败 | 可选/必需分类、缓存、熔断和降级 |
| Memory 污染 | 错误事实长期传播 | 来源、权限、冲突、版本和审计 |
| 审批后重复调用 | 产生业务副作用 | Tool Receipt 和幂等键 |
| 多 Agent 过早复杂化 | 延误主闭环 | 默认 Single Agent，策略后置 |
| Fork Codex 双栈成本 | 维护负担增加 | 先 Adapter/PoC，不作为默认主线 |
| UI 状态先于真实能力 | 演示误导 | 状态必须由后端产物和健康检查派生 |

---

## 22. MVP 定义

**MVP = Phase 4 完成时的状态。** Skill 能力是 Phase 3 交付物，MVP 依赖它但不在 Phase 1/2 验收中出现。

KsADK Harness MVP 不是“页面上能发送一句话”，而是满足以下真实闭环：

### 22.1 创建与编译

- Studio AI/模板/手动创建生成同一种 AgentDraft；
- Draft 提交不可变 Revision；
- Revision 编译为 HarnessSpec；
- HarnessSpec 能构建 Bundle。

### 22.2 运行

- 默认单 Agent Loop；
- 支持多轮 Session；
- 支持至少一个真实 MCP；
- 支持 Skill 描述和正文按需加载；
- 支持 Cancel；
- 支持 Approval Interrupt/Resume；
- 支持基础 Context 压缩；
- 支持失败降级。

### 22.3 生命周期

按 §12.4 的 Local MVP 清单执行（Build Bundle → Create local runtime instance → Start Harness process → Health check → Register local route → Mark deployed → Activate route → Invoke through active deployment），每一步对应真实产物，不重复罗列。

### 22.4 治理

- Session 按用户隔离；
- Tool Policy 生效；
- Run/Turn/Tool/Token 可观测；
- Artifact 有来源；
- Harness Conformance 通过。

不纳入首个 MVP 的内容：

- Studio 普通用户多 Runner 选择；
- 任意复杂自定义多 Agent 图；
- Skill 自进化；
- 完整图记忆；
- 所有外部 Runner 完全行为一致；
- 生产级多地域调度。

---

## 23. 推荐优先级

当前最优先的不是新增更多 Studio 页面，也不是先扩展多 Agent，而是：

```text
P0  HarnessSpec + ExecutionEngine + Conformance
P0  Studio Playground 切换到 ManagedLangGraphEngine
P0  Revision -> Build -> Local Deploy -> Activate -> Invoke
P1  Session/Context/Compaction 收口
P1  MCP/Skill Capability Runtime
P1  Approval Interrupt/Resume + Tool Receipt
P1  Run/Turn/Tool/Usage/Artifact 观测
P2  Core Memory Block 与受控长期 Memory
P2  多 Agent Execution Strategy
P2  外部 Runner Studio 导入
```

最终目标是：

> Studio 默认创建的所有 Managed Agent 都运行在同一个 KsADK Harness 契约上；LangGraph 负责执行机制，KsADK 负责企业 Agent 的上下文、能力、治理和生命周期；Codex、ADK 和外部 LangGraph 作为兼容 Engine 保留，并由同一套 RuntimeAdapter 与 Conformance 约束。
