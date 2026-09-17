# Skill Runtime 评测可观测性设计

> 适用范围：KsADK Skill Runtime 的发现、加载、执行、产物和结果消费证据。
> 本文记录 `feat/skill-observability` 的实现契约与跨系统边界；不修改 EvalSmith、`RuntimeEvent v1`、SSE、Studio replay 或现有 exporter 配置。

## 目标与边界

Skill 评测需要的是可归因的运行事实，而不只是最终文本。KsADK 为此定义独立的 `SkillEvent v1`，在运行边界记录候选、选择、包处理、加载、执行、产物、清理和结果消费。它不是 `RuntimeEvent v1` 的扩展，也不替代 Agent 的业务轨迹。

本期采用中等深度接入：类型化事件、核心边界埋点、Sandbox JSONL envelope 和 Sandbox 外的 OTEL 重建。Sandbox 不接收 Collector 地址、`traceparent` 或 OTLP 凭证，也不直接上报 OTLP。

```text
Agent Runtime
  binding / candidates / selection receipt / result-consumption link
                  |
                  v
KsADK execute_skills -> Skill Runtime -> Sandbox sidecar JSONL
       |                    |                    |
       |                    |                    +-- session/runtime/kill facts only
       |                    +-- package/load/execute/artifact facts
       v
outer event sink -> validate + redact -> SkillEvent v1 -> OTEL projection
                                                        |
                                                        v
                                      existing OTLP / Langfuse / Cloud Monitor path
```

## 证据契约

`ksadk.skills.events` 提供以下不可变输入和可序列化事件：

| 类型 | 用途 | 关键字段 |
|------|------|------|
| `BoundSkillRef` | 一个候选 Skill 在授权 Space 中的身份 | 完整 `SkillRef`、`space_id` |
| `SkillBinding` | 一次请求的不可变授权候选快照 | `binding_snapshot_id`、候选集合 |
| `SkillExecutionContext` | 受信任外层请求关联，不是模型工具参数 | `run_id`、`trace_id`、binding、`decision_id`、selection receipt、agent step、selected IDs |
| `SkillInvocationPlan` | 外层绑定的完整执行实例清单 | `binding_snapshot_id`、`decision_id`、完整 `SkillRef`、`skill_invocation_id` |
| `SkillEvent` | 独立的 v1 运行事实 | 时间、状态、完整 `SkillRef`、`skill_invocation_id`、`runtime_id`、关联字段、错误分类、属性 |
| `SandboxSkillEventEnvelope` | Sandbox 返回的 JSONL 记录 | `SkillEvent` 的受控序列化形式 |

`SkillRef` 必须以 `skill_id`、`version_id`、`version`、`name` 和内容摘要为主进行归因；名称只用于显示，不能代替版本或 Space 身份。每个从加载开始参与执行的 Skill 使用一个 `skill_invocation_id` 串联加载、执行和产物。

`SkillEvent v1` 的事件名固定如下。新增语义必须通过新 schema 版本或明确的字段演进完成，不能改写既有名称的含义。

| 阶段 | 事件 |
|------|------|
| 候选与选择 | `skill.candidates.resolved`、`skill.selection.completed`、`skill.selection.skipped` |
| 包与加载 | `skill.package.cache_hit`、`skill.package.downloaded`、`skill.package.hash_verified`、`skill.package.extracted`、`skill.manifest.parsed`、`skill.load.started`、`skill.load.completed`、`skill.load.failed` |
| Sandbox 与执行 | `sandbox.session.created`、`sandbox.session.cleaned_up`、`sandbox.session.cleanup_failed`、`skill.execution.started`、`skill.execution.completed`、`skill.execution.failed` |
| 输出与消费 | `skill.artifact.created`、`skill.result.consumed`、`sandbox.envelope.rejected` |

### SkillBinding 与 SkillInvocationPlan

| 契约 | 解决的问题 | 关键字段 | 产生时机 | 评测使用方式 |
|------|------|------|------|------|
| `SkillBinding` | 本次请求允许考虑哪些 Skill | `binding_snapshot_id`、`candidates`；每个候选包含完整 `SkillRef` 和 `space_id` | Agent Runtime 查询授权候选后，在模型决策前固化 | 作为候选覆盖、Space 归属和版本归因的基准快照 |
| `SkillInvocationPlan` | 本次请求最终准备调用哪些 Skill | `binding_snapshot_id`、`decision_id`、`entries`；每个 entry 包含 `SkillRef` 和唯一 `skill_invocation_id` | 选择完成后，由 `selected_skill_ids` 从 `SkillBinding` 派生 | 作为 Sandbox 和 Skill Runtime 的可信执行清单，串联 load、execution、artifact、consumed 事件 |

两者不是同一个概念：`SkillBinding` 是授权候选集合，`SkillInvocationPlan` 是选择后的执行实例集合。Plan 只能从 Binding 派生；不在 Binding 中的 Skill ID、重复选择或重复 invocation ID 必须拒绝。它们都不是 Agent checkpoint，也不应由模型工具参数提供。

### SkillEvent 事件释义

#### 候选与选择

| 事件 | 释义 | 主要关联字段 | 评测用途 |
|------|------|------|------|
| `skill.candidates.resolved` | 本次请求已解析出授权候选集合 | `binding_snapshot_id`、`run_id`、`trace_id`、`candidate_count`、`candidate_skill_ids` | 检查候选覆盖是否完整 |
| `skill.selection.completed` | Agent 已从候选集合中完成 Skill 选择 | `binding_snapshot_id`、`decision_id`、`selected_skill_ids` | 检查选择正确性、选择精度和决策归因 |
| `skill.selection.skipped` | 选择阶段被跳过 | `binding_snapshot_id`、`decision_id`（如有） | 区分未选择、未启用和选择失败；当前 schema 已定义，但当前代码未找到实际生产点 |

#### 包与加载

| 事件 | 释义 | 主要关联字段 | 评测用途 |
|------|------|------|------|
| `skill.package.cache_hit` | 本地缓存中已找到 Skill 包 | `skill_ref`、`skill_invocation_id`、`cache_hit` | 检查缓存命中和重复下载情况 |
| `skill.package.downloaded` | Skill 包已从 Skill Service 下载完成 | `skill_ref`、`skill_invocation_id`、时间区间 | 检查下载成功率和耗时 |
| `skill.package.hash_verified` | 实际包内容摘要与 `SkillRef.content_hash` 一致 | `skill_ref`、`skill_invocation_id` | 检查版本和供应链完整性 |
| `skill.package.extracted` | 包已通过安全检查并解压到受控目录 | `skill_ref`、`skill_invocation_id` | 检查包是否可安全展开 |
| `skill.manifest.parsed` | `SKILL.md` / manifest 已成功解析 | `skill_ref`、`skill_invocation_id`、`has_description` | 检查元数据是否可解析 |
| `skill.load.started` | 开始将包加载为运行时 Skill | `skill_ref`、`skill_invocation_id` | 加载生命周期起点 |
| `skill.load.completed` | Skill 已加载，可交给执行器 | `skill_ref`、`skill_invocation_id` | 判断 Skill 是否 loadable |
| `skill.load.failed` | 加载过程失败 | `skill_ref`、`skill_invocation_id`、`error_category` | 判断加载健康度和失败分类 |

#### Sandbox 与执行

| 事件 | 释义 | 主要关联字段 | 评测用途 |
|------|------|------|------|
| `sandbox.session.created` | Sandbox 会话创建成功 | `runtime_id` | 判断执行环境是否建立 |
| `sandbox.session.cleaned_up` | Sandbox 已正常清理 | `runtime_id` | 判断资源释放是否正常 |
| `sandbox.session.cleanup_failed` | Sandbox 清理失败 | `runtime_id`、`error_category` | 检查资源泄漏和运维风险 |
| `skill.execution.started` | Skill 工作流开始执行 | `skill_ref`、`skill_invocation_id` | 执行生命周期起点 |
| `skill.execution.completed` | Skill 工作流执行完成 | `skill_ref`、`skill_invocation_id` | 判断执行是否成功 |
| `skill.execution.failed` | Skill 工作流执行失败 | `skill_ref`、`skill_invocation_id`、`error_code`、`error_category` | 判断失败、超时和命令错误 |
| `skill.execution.completed` + `status=skipped` | 有候选 Skill，但没有可执行入口，执行被跳过 | `skill_ref`、`skill_invocation_id` | 与真正执行成功区分；必须同时检查事件类型和 `status` |

#### 输出、消费与安全校验

| 事件 | 释义 | 主要关联字段 | 评测用途 |
|------|------|------|------|
| `skill.artifact.created` | Skill 创建了受控产物 | `skill_invocation_id`、`artifact_ref`、`artifact_name`、`size_bytes`、`mime_type` | 检查产物是否生成、类型和大小是否符合要求 |
| `skill.result.consumed` | Agent 的持久化步骤实际消费了 Skill 结果 | `skill_invocation_id`、`agent_step_id`、`selection_receipt_id` | 判断结果是否真正进入 Agent 后续流程 |
| `sandbox.envelope.rejected` | Sandbox 返回的 JSONL 事件未通过外层校验 | `runtime_id`、`error_category` | 检测非法 JSON、invocation/ref 错配或伪造事件 |

`skill.execution.completed` 不等于 `skill.result.consumed`。前者只证明 Skill 执行完成，后者还要求 Agent Runtime 能指出实际消费结果的持久化步骤。

### 关联、错误和脱敏

- `trace_id`、`run_id`、`binding_snapshot_id` 和 `decision_id` 仅由受信任的外层 `SkillExecutionContext` 补齐，不由模型调用参数或 Sandbox 声明决定。
- `skill_invocation_id` 对加载、执行、产物和结果消费是必填关联键。Sandbox 返回的记录必须与外层 `SkillInvocationPlan` 中的 invocation 和完整 `SkillRef` 一致；不合法记录产生 `sandbox.envelope.rejected`，不得改变原执行结果。
- `status` 描述事实状态，`error_code` 和 `error_category` 描述失败分类。观测写入、解析和 OTEL 投影失败只作为诊断，不能改变 workflow 的成功、失败、超时或清理语义。
- 事件属性和 OTEL 属性禁止包含 prompt、stdout、stderr、下载 URL、凭证、token、密码、Secret、文件系统路径或产物内容。产物只记录不透明 `artifact_ref`、展示名、大小、MIME 类型或可用性等受控元数据。
- 缺失候选快照、binding、完整版本身份或 invocation 关联时，不补造事件。下游对依赖这些证据的维度应返回 `not_evaluable`，而不是把缺失视为通过或失败。

## 系统职责

| 所有者 | 必须提供 | 不负责 |
|------|------|------|
| Agent Runtime | 请求级 `SkillBinding`；授权候选快照；选择回执及 `decision_id`；将结果消费关联到请求步骤；创建并向 KsADK 闭包传递可信 `SkillExecutionContext` | 伪造 KsADK 包、加载或 Sandbox 内部事件；在模型可见工具 schema 中暴露 context |
| Skill Service | 已授权且不可变的 Skill、Version、内容摘要和 Space 归属；面向运行时的下载能力 | 在 KsADK 中执行 Skill；产生执行或 Sandbox 事件；把下载 URL 暴露给模型、事件或 OTEL |
| KsADK Skill Runtime | Skill Service 消费；缓存、校验、安全解压、manifest、加载、执行和 artifact 事实；事件校验与脱敏；Sandbox envelope 回收；Sandbox 外 OTEL 投影 | Skill 注册/CRUD/版本治理；评测打分；签发平台凭证 |
| Sandbox Service | session 创建和销毁；runtime/session ID；命令执行、超时/kill 状态；受控 event-file 的回收通道 | 解释 `SKILL.md` 的选择或业务语义；直连 OTEL/Langfuse；持有 Collector 凭证 |
| EvalSmith | 本期不在本仓实施。后续消费已经产生的事件和 trace 证据，归一化并评测 | 执行 Skill、签发 Sandbox/Skill Service 凭证、补造缺失事件 |

### 请求与消费的责任划分

1. Agent Runtime 在模型决策之前固化已授权候选及其 Space，产生 `binding_snapshot_id`；在选择完成后记录 `decision_id` 和选择回执。
2. Agent Runtime 把这些事实以内部 `SkillExecutionContext` 绑定到 `execute_skills` 闭包。`execute_skills` 的模型参数保持 `workflow_prompt` 与 `skill_names`，不接受调用者提供的 run、binding 或 trace 字段。
3. KsADK 在包、加载和执行边界产生事实，Sandbox 只将受控 envelope 写到指定 JSONL 文件；外层验证、脱敏后才使其可观察。
4. Agent Runtime 仅在能够证明某一步使用了此 `SkillRuntimeResult` 后产生 `skill.result.consumed`。KsADK 不应从最终回复文本或时间相邻关系推断“已消费”。

## KsADK 实现切片

当前特性分支已实现以下独立且向后兼容的部分：

- `SkillEvent v1`、`SkillBinding`、`SkillExecutionContext`、`SkillInvocationPlan`、JSONL 解析与敏感属性脱敏位于 `ksadk/skills/events.py`。
- runtime loader 在缓存命中、下载、摘要校验、安全解压、manifest、加载边界写入事件；`PackageStore` 在实际 hash 与 extract 操作处记录独立时段；executor 写入执行和受控 artifact 事实。
- `local_process` 与 E2B backend 为 runtime agent 提供受控事件文件路径，并将回收的 JSONL 作为可选 `SkillRuntimeResult.skill_events` 返回。旧调用在无事件时保持原 `to_dict()` 字段集合。
- `build_execute_skills_tool(..., execution_context=...)` 支持内部上下文补齐、候选/选择事实和受控 `SkillInvocationPlan`；未传 context 的旧注入路径维持原行为。
- `record_skill_result_consumption()` 仅接受 Agent Runtime 已持久化的 step 和已完成 invocation，供真正的请求编排 owner 调用；不会由 KsADK 推断最终文本是否使用了结果。
- `project_skill_events()` 使用现有 OpenTelemetry 当前上下文：生命周期类事件重建为 `execute_skills` 下的 child span，瞬时事实加入当前 span event。仅写入 `ksadk.skill.*` 属性，既有 Langfuse、Cloud Monitor 与 OTLP exporter 的构造和配置不变。

当前实现尚未把下面能力称为已完成：

| 待接入项 | 原因与落点 |
|------|------|
| Agent Runtime 新请求路径 | 需要由 Agent Runtime 创建真实授权 binding、候选快照、选择回执和结果消费链接；旧 ADK 注入不迁移。 |
| Sandbox Service 真机验证 | local/E2B backend 已在 create/kill/cleanup-failure 边界产生 session 事件；真实模板、平台 Sandbox Service 的行为仍需端到端验证。 |
| Agent Runtime 新请求路径 | 当前 checkout 未定位到创建真实授权 binding、候选快照、选择回执和结果消费链接的请求编排 owner；在 owner 确认前不能伪造跨仓接入。 |
| Sandbox Service 真机验证 | local/E2B backend 已以外层 invocation plan 校验 JSONL；真实模板、平台 Sandbox Service 的行为仍需端到端验证。 |
| 真实 E2B 端到端验证 | 仅在已注册 template、授权 binding、预发 Skill Service 和非敏感 OTLP 配置齐备后执行。 |

## OTEL 上报规则

OTEL 是现有可观测管道的投影，不是 `SkillEvent` 的唯一存储或评测结论来源。

- 有生命周期的 package、hash、extract、load、Sandbox session、execution 和 cleanup 事件投影为 child span；candidate、selection、manifest、artifact、result-consumption 与 rejected envelope 投影为当前 `execute_skills` span event。
- parentage 依赖调用时的 OTel current span；`SkillEvent.trace_id` 是业务关联字段，不能用来在 Sandbox 内重建或伪造 trace context。
- `failed`、`timed_out`、`cleanup_failed` 投影为 OTEL error；`skipped` 保持非 error，并携带原因属性。
- OTEL SDK 不可用、span 创建失败或 exporter 失败时必须吞掉观测异常，保留原 `SkillRuntimeResult` 与已有上报链路。

## 评测时如何使用这些契约

评测先验证证据链，再计算指标；不能从最终自然语言回复、相邻时间戳或单条 RuntimeEvent 推断 Skill 事实。

| 评测维度 | 最小契约/事件证据 | 可回答的问题 | 缺失处理 |
|------|------|------|------|
| 候选覆盖 | `SkillBinding`、`skill.candidates.resolved` | 本次请求实际获得了哪些授权候选？ | `not_evaluable` |
| 选择正确性 | Binding、`skill.selection.completed`、`decision_id`、selection receipt | Agent 是否从允许集合中选择了正确 Skill？ | `not_evaluable` |
| Space 与版本归因 | 完整 `SkillRef`、`space_id`、`binding_snapshot_id` | 执行的是否是指定 Space 和固定版本？ | `not_evaluable` |
| 包完整性 | `skill.package.cache_hit/downloaded/hash_verified/extracted` | 包是否获取成功且内容未被替换？ | `not_evaluable` 或失败 |
| 加载健康度 | `skill_invocation_id` 串联的 `skill.load.*`、`skill.manifest.parsed` | Skill 是否可解析、可加载？ | `not_evaluable` 或失败 |
| 执行健康度 | `skill.execution.started/completed/failed` | Skill 是否实际执行、是否超时或失败？ | `not_evaluable` 或失败 |
| Sandbox 生命周期 | `sandbox.session.created`、`runtime_id`、cleanup/kill 事实 | 执行环境是否创建并正常清理？ | `not_evaluable` 或运维失败 |
| 产物可用性 | `skill.artifact.created` 及受控 artifact 元数据 | 是否生成了符合要求的产物？ | `not_evaluable` |
| 结果是否被使用 | `skill.result.consumed`、`agent_step_id`、`selection_receipt_id` | Agent 的哪个持久化步骤实际消费了结果？ | `not_evaluable` |
| Sandbox 事件可信度 | `sandbox.envelope.rejected` | Sandbox 返回的事件是否被外层接受？ | 记录拒绝，不改变原执行结果 |

没有独立可执行入口的纯指令或资源型 Skill，其入口健康维度是 `not_applicable`；这与证据缺失导致的 `not_evaluable` 不同。

推荐的评测判定顺序是：

1. 先验证 `SkillBinding` 和完整 `SkillRef`，确定候选、Space 和版本身份。
2. 再验证 `SkillInvocationPlan`，确认选择结果来自候选集合，且每个调用有唯一 `skill_invocation_id`。
3. 按 invocation ID 检查 package、load、execution、artifact 和 Sandbox 生命周期事件。
4. 只有存在真实 Agent step 关联时，才计算 `skill.result.consumed` 或结果使用指标。
5. 任一依赖证据缺失，返回 `not_evaluable`；不要把缺失证据当成通过或失败。

## 验证与安全

本特性的基础验证应覆盖 schema、关联、脱敏、cache/hash、超时、清理、非法 envelope、local/E2B、旧接口兼容、binding、span 父子关系以及 exporter 不变。真实端到端验证不使用生产凭证或临时下载 URL，测试 fixture 与文档只使用占位符。
