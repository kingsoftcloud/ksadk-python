# KsADK Harness 长任务 Context 与 Memory 增强技术方案

> 文档状态：实现基线（仓内能力已收口，持续真实环境验证）
> 适用分支：`feat-ksadk-harness`
> 更新日期：2026-08-31

## 1. 文档目的

本文定义 KsADK Harness 在长任务场景下的 Context 与 Memory 增强方案。

目标不是简单地在对话过长时生成摘要，而是保证 Agent 在持续数小时、经历多轮工具调用、审批、中断和恢复后，仍然能够：

1. 准确保留目标、计划、约束、决策和未完成事项；
2. 在历史压缩后保留关键事实、工具证据和审批状态；
3. 对长期 Memory 的写入、更新、删除和冲突处理进行权限与来源治理；
4. 解释每次模型调用使用了哪些 Context、为什么使用以及消耗了多少 Token；
5. 使用统一 RuntimeEvent v2 向 Studio、评测和可观测系统输出运行事实。

本文是 [KsADK Harness 统一运行时技术方案](./KsADK-Harness统一运行时技术方案.md) 在 Context 与 Memory 方向的专项细化，不替代 Harness 总体方案。

## 2. 核心结论

### 2.1 总体路线

采用“LangGraph 执行底座 + KsADK Harness 平台协议”的分层方式：

```text
LangGraph
  负责 Graph State、Checkpoint、Interrupt、Resume 和节点调度

KsADK Harness
  负责 Prompt、Context、Memory、Capability、Policy、Lifecycle、
  RuntimeEvent v2、Conformance 和企业治理边界

Studio / 平台控制面
  负责配置、展示、组织权限、审批操作、资产治理和运行分析
```

LangGraph 是默认执行引擎，但不是 KsADK 的公共领域协议。HarnessSpec、ContextPlan、MemoryRecord 和 RuntimeEvent 不得包含 LangGraph 专属类型。

### 2.2 开源参考原则

可以借鉴并在许可证允许的前提下复用局部实现，但不把任何一个开源 Agent 项目整套复制为 KsADK Harness。

| 参考项目 | 主要借鉴内容 | 不直接采用的部分 |
|---|---|---|
| OpenClaw | Stable Prompt 与动态 Context 分离、压缩前整理、Skill 渐进披露 | 不把本地 `MEMORY.md` 作为企业事实源 |
| Hermes | Working Context、Session、Memory、Skill 分层；主动与紧急压缩 | 不采用固定 Token 常数，不引入完整 Runner |
| Letta / MemGPT | 有名称、描述、预算和写权限的 Core Memory Block | 不让全部 Memory Block 永久常驻 Prompt |
| Mem0 | 候选提取、去重、冲突与 add/update/delete 决策 | 不把 Mem0 存储协议变成 KsADK 公共协议 |
| OpenHands | Workspace、Sandbox 隔离和按需能力装载 | 不引入完整 Agent Runtime |
| LangGraph | Thread scoped state、Checkpoint、Store、Interrupt/Resume | 不要求业务 Agent 直接依赖 LangGraph 类型 |
| Claude Code / claw-code 的公开分析 | Prompt Section、缓存边界、Working Notes、Tool Pair、Parity Test | 不复制未公开 Prompt 和实现细节 |

代码复用前必须逐项确认具体版本的许可证、NOTICE、修改记录、安全风险和后续维护责任。设计思想借鉴与源码复用必须分别记录。

## 3. 分支与仓库边界

### 3.1 本分支负责

`feat-ksadk-harness` 负责 SDK 与 Runtime 数据面能力：

- Context 构建、预算、裁剪、压缩与恢复；
- Working Context 生命周期；
- Memory 检索、受控写入、冲突和审计；
- LangGraph 默认引擎接入；
- RuntimeEvent v2 输出；
- Session、Transcript、Checkpoint 和 Artifact 引用；
- Harness Conformance 与长任务测试。

### 3.2 本分支不直接负责

以下属于 Studio 或平台控制面：

- Token 构成和压缩历史页面；
- Memory 查看、纠正、删除和锁定页面；
- 组织级 Memory 审批和资产治理；
- 云端 Registry、跨租户共享和完整权限后台；
- Studio 页面交互和视觉实现。

Harness 需要提供稳定的数据与事件合同，由 Studio 和控制面消费，不在本分支复制 UI 逻辑。

## 4. 当前实现基线

当前分支已经存在独立的 `ksadk.harness` 包，并与 Studio 领域模型解除反向依赖。

### 4.1 已具备能力

| 能力 | 当前实现 |
|---|---|
| Stable Prompt 分离 | `HarnessContextEngine.stable_prompt()` 与独立 Hash |
| 动态 Context 规划 | 复用 `ContextPlanner`、`ContextPlan` 和 `ContextAssembler` |
| 动态 Token Budget | 根据 Model Profile 提供的窗口构建预算 |
| Tool Pair 保护 | 通过 Context Item 原子分组避免拆分 Tool Call/Result |
| 主动与紧急压缩框架 | `plan / compact / recover`，紧急恢复限制重试次数 |
| 关键事实保护 | 对 ID、金额、日期、版本和审批编号进行压缩后校验与重注入 |
| Working Context | 目标、计划、约束、问题、事实、Artifact 和工具失败状态 |
| Core Memory | 限制常驻数量和总 Token 预算 |
| Memory Scope | Agent、User、Workspace/Org 等作用域与租户隔离 |
| Memory 受控写入 | 来源、权限、敏感标签、去重和冲突检查 |
| Memory Provider | 本地 SQLite Provider 和统一 Coordinator |
| Checkpoint/恢复 | Managed LangGraph Engine 的 Checkpoint 基础 |
| Runtime 事件 | Harness 内部事件兼容层和 RuntimeEvent v2 平台适配 |

### 4.2 当前实施状态

已落地的基础能力：

1. ContextPlan、ContextManifest 与 Planned/Projected/Actual Token 闭环；
2. RuntimeEvent v2 信封及 v1/v2 兼容投影；
3. CompactionRecord、压缩前受控 Memory Flush 和压缩后校验；
4. WorkingContextPatch、乐观版本与 Transcript 确定性状态重建；
5. MemoryRecord 来源、TTL、Sensitivity、写策略、纠错、遗忘和锁定语义；
6. 关键词与可选向量融合、多样性控制和 Token 装箱；
7. 固定长任务数据集、保真指标与发布门禁基础；
8. Context Trace、Token Report 等稳定 Insights 查询合同；
9. LangGraph Store 到 MemoryProvider 的适配层。

后续需要跨服务或真实环境继续验证的项目：

1. 将 Insights 稳定投影接入可持久化、多实例的平台事件存储和 Studio 页面；
2. 100 条冻结 Memory 金标集已经落地，继续在目标模型网关运行完整真实模型评测；
3. 在真实 LangGraph Store 与企业 Memory Service 环境验证跨进程冷恢复；
4. 使用生产授权数据扩大语义召回、Reranker 和多模态来源评测；
5. 由平台控制面完成组织权限、Memory 审批和治理闭环。

## 5. 目标架构

```text
Model Call
  |
  v
ContextEngine
  |- Stable Prompt
  |- Working Context
  |- Recent Turns
  |- Retrieved Memory
  |- Retrieved Knowledge
  |- Skill Context
  |- Tool / Artifact Evidence
  `- Context Budget + Compaction
  |
  |- ContextPlan       规划选择什么
  |- AssembledInput    实际准备发送什么
  `- ContextManifest  可观测和可审计快照
  |
  v
Model Provider
  |
  `- ActualUsage       Provider 返回的实际 Token 和缓存数据

MemoryEngine
  |- Core Memory
  |- Episodic Memory
  |- Semantic Memory
  |- Procedural Memory
  |- Candidate Extraction
  |- Deduplicate / Conflict
  |- Access Policy
  `- Pluggable MemoryBackend
```

## 6. Context Engine 设计

### 6.1 Context 分层

每次模型调用前，由 ContextEngine 构建 ContextPlan，不直接拼接完整聊天历史。

#### Stable Prompt

变化较少、适合缓存：

- 平台安全和 Runtime 合同；
- Agent 身份、职责和工作边界；
- 输出协议；
- Tool 使用约束；
- Memory 写权限；
- Skill 索引描述，不包含全部正文。

Stable Prompt 必须有独立 Hash。任何 Section 变化都应能定位，便于分析 Prompt Cache 失效原因。

#### Working Context

Working Context 是当前任务的结构化运行状态，不是聊天摘要。

建议目标模型：

```yaml
goal: 当前最终目标
plan: 当前计划
current_step: 当前步骤
confirmed_constraints: 已确认约束
decisions: 已确认决策
open_questions: 未解决问题
verified_facts: 已验证事实
pending_actions: 未完成动作
artifact_refs: 关键产物引用
recent_tool_failures: 最近工具失败与降级
```

审批的权威状态继续由 `HarnessState.pending_approval` 管理；Working Context 只保存必要摘要或引用，避免双重事实源。

Working Context 更新使用结构化 Patch：

```python
class WorkingContextPatch:
    base_version: int
    operations: list[PatchOperation]
    source_event_ids: list[str]
    reason: str
```

禁止模型每轮无条件完全重写 Working Context。Patch 必须保留版本、来源和审计记录。

#### Recent Turns

保留最近完整的：

- 用户消息；
- 模型回复；
- Tool Call 与 Tool Result；
- Approval Request 与 Approval Result；
- Interrupt 与 Resume。

Tool Call/Result、Approval Request/Result 必须作为不可拆分的原子组。

#### Retrieved Context

按需检索：

- Agent、User 和 Org Memory；
- 知识库片段；
- 历史 Artifact；
- 当前任务相关 Skill 正文；
- 必要的历史 Session Episode。

必须先进行租户、用户、组织和资源权限过滤，再做关键词或向量检索。

### 6.2 复用现有模型

不新增与现有实现冲突的平行模型：

```text
ContextItem / ContextPlan
  负责规划层选择、预算和裁剪

AssembledInput
  负责 Runner/Provider 输入投影

ContextManifest
  负责持久化 ContextPlan 的可解释快照

ActualUsage
  负责记录 Provider 实际 Token、缓存和模型信息
```

ContextManifest 建议包含：

```yaml
manifest_id: ctxm_xxx
run_id: run_xxx
scope_id: scope_xxx
model_profile_ref: model-profile://...
stable_prompt_hash: sha256:...
sections:
  - item_id: ctx_xxx
    kind: working_context
    source_refs: [...]
    selected_reason: required
    estimated_tokens: 320
    included: true
budget:
  context_window: 128000
  reserved_output: 8000
  available_input: 116000
projected_tokens: 24500
actual_usage_ref: usage_xxx
```

Manifest 默认保存 Hash、引用、原因和 Token，不重复持久化敏感正文。

### 6.3 Token Budget

上下文窗口来源优先级：

1. Model Profile 明确声明；
2. Provider 模型目录或 `/models`；
3. 受审核的静态模型元数据；
4. 安全默认值，并产生 fallback 观测事件。

预算由 Context Profile 配置，不把比例写死为全局常数：

| Profile | 预算倾向 |
|---|---|
| conversation | Recent Turns |
| research | Knowledge、Memory 和 Evidence |
| execution | Working Context、Tool Result 和 Artifact |
| coding | Workspace、Artifact、Tool Schema |

每次调用记录：

```text
Planned：规划器选择的预算与 Context Item
Projected：组装后本地估算的 Token
Actual：Provider 返回的输入、输出与缓存 Token
```

### 6.4 主动与紧急压缩

#### 主动压缩

达到 Context Profile 的软阈值时触发：

1. 选择已经闭合的历史片段；
2. 压缩前执行受控 Memory Flush；
3. 生成带 SourceRef 的 Episode；
4. 更新 Working Context；
5. 保留最近完整尾部和全部未完成操作；
6. 生成 CompactionRecord；
7. 校验后重注入关键事实。

#### 紧急压缩

模型明确返回 Context Overflow 时触发：

1. 永远保留安全规则、当前目标和审批状态；
2. 永远保留未闭合工具和审批原子组；
3. 优先移除闲聊、重复和低价值内容；
4. 使用更强压缩 Profile；
5. 重新计算 Token；
6. 最多自动重试一次；
7. 仍无法安全保留关键信息时明确失败，不静默丢弃。

#### CompactionRecord

```yaml
compaction_id: cmp_xxx
run_id: run_xxx
trigger: proactive
before_tokens: 92000
after_tokens: 31000
compacted_event_range: [120, 480]
preserved_event_ids: [...]
summary_artifact_ref: artifact://...
memory_candidate_refs: [...]
summary_model_ref: model-profile://...
quality_checks:
  tool_pairs_complete: true
  approvals_preserved: true
  goal_preserved: true
  critical_facts_preserved: true
```

完整 Transcript 不删除。CompactionRecord 只是 Context 投影变化记录，不是新的聊天事实源。

## 7. Memory Engine 设计

### 7.1 Memory 类型

| 类型 | 示例 | 生命周期 |
|---|---|---|
| Core | 用户角色、固定偏好、Agent 身份 | 长期、有限预算 |
| Episodic | 某次预算分析的过程与结论 | TTL 可配置 |
| Semantic | 有来源的业务口径和稳定事实 | 长期、强来源要求 |
| Procedural | 完成某类任务的流程经验 | 随 Agent/Skill 版本治理 |

Session 内 Working Context 不归类为长期 Memory。

### 7.2 扩展现有 MemoryRecord

以 `ksadk.memory.models.MemoryRecord` 为唯一 SDK 领域模型，按兼容方式补齐：

- `source_event_ids`；
- `source_artifact_refs`；
- `confidence`；
- `sensitivity`；
- `expires_at`；
- `write_policy`；
- `version` 与状态；
- 冲突和 supersede 引用。

不在 Harness 包内重新定义另一套 MemoryRecord。

### 7.3 写回流水线

```text
Turn / Run 完成
  -> Candidate Extraction
  -> Schema Validation
  -> Source Validation
  -> Sensitivity Classification
  -> Scope / Permission Check
  -> Normalize
  -> Deduplicate
  -> Conflict Resolution
  -> Add / Update / Delete / Ignore
  -> Optional User Confirmation
  -> Commit
  -> RuntimeEvent v2 + Audit
```

基本规则：

- 模型猜测不得写成事实；
- 时效数据必须有来源和 TTL；
- 组织级 Memory 默认需要用户明确指令或审批；
- 敏感 Tool Result 未脱敏不得写入跨用户作用域；
- 冲突不得静默覆盖；
- 删除和更新必须保留审计链；
- 完整聊天内容不得直接作为长期 Memory 写入。

### 7.4 Core Memory Block

Core Memory 复用现有 MemoryRecord 和 CoreMemoryRequest，不另建存储协议。

每个 Block 需要：

- 名称和用途描述；
- 最大 Token；
- Scope；
- 读写权限；
- SourceRef；
- 版本和敏感级别；
- 是否允许常驻 Context。

只有通过权限检查、被配置为常驻且未超过总预算的 Block 才进入 Context。

### 7.5 检索策略

```text
Tenant / ACL / Scope Filter
  -> Time / Status / TTL Filter
  -> Keyword + Vector Search
  -> Rerank
  -> Deduplicate
  -> Diversity Control
  -> Token Budget Truncation
  -> ContextPlan
```

每条召回记录需要在 ContextManifest 或 Trace 中说明：

- Memory ID；
- 召回原因；
- 分数；
- SourceRef；
- 估算 Token；
- 是否最终进入模型输入。

### 7.6 可替换 Backend

```python
class MemoryBackend(Protocol):
    async def search(self, request: MemorySearchRequest) -> MemorySearchResult: ...
    async def add(self, record: MemoryRecord) -> MemoryRecord: ...
    async def update(self, record: MemoryRecord) -> MemoryRecord: ...
    async def delete(self, memory_id: str, *, reason: str) -> None: ...
```

默认使用 KsADK Provider/Coordinator，可通过适配器接入：

- LangGraph Store；
- Mem0；
- Letta；
- 企业内部 Memory Service。

KsADK MemoryRecord 和权限语义始终是公共协议，第三方 Backend 不能反向污染领域模型。

## 8. RuntimeEvent v2 与可观测合同

Harness 内部可以暂时保留 v1 兼容事件，但向平台输出必须统一为 RuntimeEvent v2。

建议覆盖以下语义：

| 语义 | v2 表达内容 |
|---|---|
| Context Build | ContextManifest 引用、Section Token、选择原因 |
| Context Compact | CompactionRecord、触发原因、前后 Token 和校验结果 |
| Memory Recall | Memory ID、Scope、分数、是否注入 |
| Memory Write | Operation、来源、Scope、决策和审计引用 |
| Memory Conflict | 新旧记录、冲突原因和处理结果 |
| Model Call | 模型、输入输出 Token、缓存使用和 ContextManifest 引用 |

事件必须包含 `run_id`、`scope_id`、`parent_scope_id` 和有序序号，使 Studio 能展示单 Agent 与多 Agent 的 Context、Memory 和 Token 层级。

不得继续向平台扩展无类型约束的任意 `event_type + payload`。

## 9. 与默认 Agent Loop 的接线

```text
load_session
  -> restore_checkpoint
  -> recall_core_memory
  -> retrieve_memory_and_knowledge
  -> patch_working_context
  -> build_context_plan
  -> emit_context_manifest
  -> call_model
  -> execute_or_approve_tools
  -> update_working_context
  -> compact_if_needed
  -> extract_memory_candidates
  -> controlled_memory_write
  -> checkpoint
  -> complete_run
```

关键要求：

- Checkpoint 保存路由位置和必要状态；
- Transcript 保存完整事件事实；
- Working Context 可由 Transcript 重建；
- 大 Tool Result 和 Artifact 只保存引用与摘要；
- 恢复后不得重复执行已有 Tool Receipt 的副作用工具；
- Context/Memory 后置失败不得篡改已完成的模型或工具结果。

## 10. 实施计划与进度

### P0：Context 可解释化（已完成基础实现）

交付：

- ContextManifest；
- Planned/Projected/Actual Token；
- RuntimeEvent v2 Context/Usage 投影；
- CompactionRecord 完整字段；
- 压缩前 Flush 和压缩后校验；
- ContextManifest 与敏感正文分离。

验收：

- 每次模型调用可解释 Context 来源和 Token 构成；
- Tool Pair 和审批对不会被拆分；
- 压缩后关键 ID、金额、日期和约束保留；
- Context Overflow 最多自动恢复一次。

### P1：Working Context 与 Memory 质量（已完成基础实现）

交付：

- WorkingContextPatch 与版本控制；
- MemoryRecord 兼容扩展；
- TTL、Sensitivity、SourceRef；
- 候选提取和冲突决策增强；
- Core Memory 写权限；
- 用户纠错、删除和锁定所需 Runtime API 合同。

验收：

- 不同用户、Agent、Session 和组织作用域隔离；
- 模型猜测不进入长期事实；
- 冲突不静默覆盖；
- Memory 更新和删除可以完整审计。

### P2：检索与长任务评测（基础实现已完成，真实模型评测待扩展）

交付：

- Keyword + Vector 混合检索；
- Rerank、多样性和 Token 截断；
- 长任务数据集；
- 压缩保真和 Memory Precision/Recall 指标；
- 不同模型窗口与 Context Profile 对比；
- Harness Conformance 增量用例。

验收：

- 跨压缩后仍可完成长任务；
- Goal/Constraint/Evidence Retention 达到评审基线；
- Memory Precision/Recall 有可重复的离线测试结果；
- Provider 或 Memory Backend 故障有明确降级语义。

### P3：平台消费与可替换 Backend（查询合同已完成，平台 E2E 待接入）

交付：

- Studio/控制面查询合同；
- MemoryBackend 适配层；
- LangGraph Store 和企业 Memory Service 适配验证；
- Context/Memory Trace 查询；
- 平台端数据保留和脱敏策略。

验收：

- Studio 不需要解析 Harness 私有对象；
- 更换 Backend 不改变 HarnessSpec 和 RuntimeEvent v2；
- Context 与 Memory 数据满足组织权限和审计要求。

## 11. 质量指标

| 指标 | 含义 |
|---|---|
| Goal Retention | 压缩后是否保留最终目标 |
| Constraint Retention | 关键约束保留率 |
| Evidence Fidelity | 摘要与原始证据一致性 |
| Tool Pair Integrity | Tool Call/Result 完整率 |
| Approval Integrity | 审批请求、结果和恢复完整率 |
| Memory Precision | 写入 Memory 中真正有用且正确的比例 |
| Memory Recall | 需要时成功召回的比例 |
| Conflict Accuracy | 冲突识别和处理正确率 |
| Context Efficiency | 有效 Context Token 占输入 Token 的比例 |
| Long-task Success | 经历压缩和恢复后的任务成功率 |

质量指标需要绑定固定测试集、模型版本和 Context Profile，避免不同环境的数据直接比较。

## 12. 安全与合规

- Context 检索必须先鉴权后召回；
- ContextManifest 默认不重复存储敏感正文；
- 长期 Memory 必须保留来源和写入主体；
- 组织 Memory 写入需要更高权限或审批；
- Memory 删除使用可审计的逻辑删除或 tombstone；
- Tool Result 写入 Memory 前必须执行脱敏策略；
- 日志和 RuntimeEvent 不得包含凭证、Cookie、Token 或完整私密文档；
- 引入开源代码前完成许可证、NOTICE、SBOM 和安全检查。

## 13. MVP 完成定义

本专项 MVP 完成需同时满足：

1. 默认 ManagedLangGraphEngine 在每次模型调用前生成 ContextPlan；
2. ContextManifest 可持久化并通过 RuntimeEvent v2 查询；
3. Token 能区分 Planned、Projected 和 Actual；
4. 主动压缩、紧急压缩和恢复路径可验证；
5. Tool Pair、审批状态、目标和关键事实不会因压缩丢失；
6. Working Context 使用结构化 Patch 更新并可从 Transcript 重建；
7. Memory 写入具备来源、Scope、权限、去重、冲突和审计；
8. 至少具备一组跨压缩长任务和 Memory Precision/Recall 测试；
9. Harness 对 Studio 只暴露稳定合同，不依赖 Studio 领域模型；
10. Memory Backend 可替换但不改变 KsADK 公共协议。

## 14. 后续验证优先级

仓内 Context 与 Memory 主能力已经收口，后续按以下顺序完成真实环境闭环：

```text
平台事件存储与 Studio Insights 接入
  -> 目标模型网关运行 100 条 Memory 金标集
  -> 真实 LangGraph Store / 企业 Memory Service 冷恢复 E2E
  -> 生产授权数据的语义召回、Reranker 与多模态评测
  -> 预发发布门禁与回归基线
```

该顺序优先将已实现的 Harness 合同连入真实平台和评测环境，再扩大数据规模与智能程度，避免继续横向增加未经 E2E 验证的抽象。
