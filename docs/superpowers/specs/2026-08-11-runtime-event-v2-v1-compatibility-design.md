# RuntimeEvent 身份化流式语义与 schema v2 / v1 短期兼容设计

> 日期：2026-08-11
>
> 状态：已按 Kimi 首轮意见修订，待复审
>
> 代码库：`ksadk-python`、`ksadk-web`
>
> 支持基线：Google ADK 2.6.3+、A2A v1.0、LangGraph v3 event streaming、Codex app-server v2

## 1. 评审摘要

本设计解决 KsADK 在同一次 subagent 任务包含多次 LLM 调用时可能重复输出的问题，同时让
当前刚发布的 RuntimeEvent v1 获得一个短期、可删除的兼容窗口。

最终决策是：

> `RuntimeEvent(schema_version=2)` 是唯一 canonical event、唯一持久化事实源和唯一 reducer
> 输入；`RuntimeEventV1` 只由 canonical event 单向投影产生，不接受 v1 写入，不迁移历史
> v1 事件，也不保留
> 第二套 reducer。

本期只要求修改 `ksadk-python` 和 `ksadk-web`。现有 AgentEngine server、云控制台会话管理、
`RunAgent`、`ListSessionEvents`、`SubscribeRunEvents`、checkpoint/恢复接口保持公开契约不变。

请评审者重点判断：

1. `scope_id + item_id + event_id` 三层身份是否足以覆盖 ADK、LangGraph、Codex 与 A2A。
2. v1 单向投影是否真正隔离，是否存在隐性双写或第二 reducer。
3. 不升级 AgentEngine server 时，Responses、事件回放和长任务恢复是否仍保持现有行为。
4. v1 的 `snapshot_only` 默认投影与 `identity_replace` 声明式投影是否足以保护新旧消费者。
5. v1 删除门槛是否足够明确，能否避免兼容层无限期存在。

## 2. 背景与现状

KsADK `0.8.0` 已建立 RuntimeEvent v1、RuntimeAdapter、RuntimeEventStore、AG-UI/A2UI、
Responses/SSE、session replay 和运行控制基础。`0.8.1` 进一步让 ADK、LangGraph 与 Codex
共用运行状态和交互投影。这些工作是本设计的基础，不需要推倒重来。

当前缺口位于内容身份和 mutation 语义：

- RuntimeEvent v1 信封只有 `invocation_id`、`event_id`、`seq_id`，没有 `scope_id`、
  `item_id`、`part_id` 和 `append/replace/complete`。
- v1 parser 按 `(invocation_id, phase)` 聚合文本。同一 invocation 内的多次 LLM call
  会进入同一个文本桶。
- ADK runner 当前通过 per-author snapshot、`startswith()` 和文本前缀差计算补偿
  partial/final 与 A2A 累积快照。
- v1 的 `TEXT_DELTA` 与 `TEXT_COMPLETED` 无法明确表达“同一个 item 的 provisional delta”
  和“该 item 的权威 completed snapshot”。

因此问题不应继续定义为“给 per-agent 去重增加更多条件”。更准确的定义是：

> Source adapter 过早丢失了上游的 response/message/item/artifact 身份和操作语义，
> 下游被迫从文本内容反推身份。

## 3. 目标与非目标

### 3.1 目标

- 根治同一 subagent 在一次任务中多次 LLM 调用时的重复输出。
- ADK、LangGraph、Codex 和 A2A 共用同一个 canonical event 与 reducer。
- live、持久化 replay、断线重连和 completed snapshot 得到相同 projection。
- 相同 `event_id` 重放只生效一次；不同 item 即使正文完全相同也全部保留。
- Responses、AG-UI/A2UI、A2A、Web 和会话事件均从 reducer projection 产生。
- 当前 RuntimeEvent v1 消费者可以短期继续读取 v1 wire。
- 不升级 AgentEngine server 时，现有公网 API 和云控制台会话管理保持可用。
- 兼容实现集中在可独立删除的模块，不能向 source adapter、reducer 和 Web 核心扩散。

### 3.2 非目标

- 不迁移或重放升级前保存的 RuntimeEvent v1 历史数据。
- 不接受第三方或旧组件向 canonical store 写入 RuntimeEvent v1。
- 不支持 Google ADK 2.6.3 以下版本。
- 不用一个 checkpoint 抽象覆盖所有框架：LangGraph graph checkpoint、ADK invocation
  resume、Codex thread resume 继续保留各自真实语义。
- 不把 HITL 等同于 A2UI。审批、结构化输入和 A2UI surface 是不同 projection。
- 不要求 A2A agent 暴露其内部 LLM call；A2A agent 可以完全没有 LLM。
- 不提供文本 hash、公共前缀、后缀重叠或相邻消息去重 fallback。
- 本期不修改 AgentEngine server 的数据库模型、API schema 或云控制台代码。

## 4. 核心原则

### 4.1 三种身份不可混用

1. `scope_id`：一次 agent、subagent、graph node、Codex turn 或远端 A2A task 的执行实例。
2. `item_id`：一条 message、reasoning、tool call/result、artifact 或 data item。
3. `event_id`：一次 canonical mutation occurrence，用于幂等和 replay；排序由持久化 `seq` 负责。

一个 `item_id` 的生命周期只覆盖一个 message、artifact、tool call 或 tool result；同一 item 内的
text/image/file/data block 使用 `part_id`，不能把多个 message 塞进一个 item，也不能用 part 代替 item。

`agent_name`、`author` 和模型名只作 attribution metadata，不参与聚合键或幂等键。
`model_call_id` 是可选 provenance，不是通用主键，因为 A2A agent 不一定存在模型调用。

### 4.2 一个事实源、一个 reducer

```text
source event
  -> source adapter
  -> RuntimeEvent(schema_version=2)
  -> canonical store
  -> StreamReducer
  -> projection patch / snapshot
  -> Responses | AG-UI/A2UI | A2A | Web | SessionEvent | RuntimeEvent v1
```

以下结构明确禁止：

```text
source adapter -> v1 store + v2 store
v1 parser      -> v1 projection
v2 reducer     -> v2 projection
```

禁止原因是双写、双 reducer 和双 replay 会让 live 与历史恢复再次漂移，并显著增加后续删除成本。

### 4.3 v1 是只读投影，不是内部兼容模式

兼容只覆盖输出：

```text
RuntimeEvent(schema_version=2) / RunProjection -> RuntimeEventV1
```

不提供：

```text
RuntimeEventV1 -> RuntimeEvent(schema_version=2)
```

因此 v1 兼容模块不进入 source adapter、canonical store、reducer 或 final output selection。

## 5. RuntimeEvent schema v2 合同

> 命名原则：核心领域类型**不带代数后缀**（`RuntimeEvent`、`RuntimeEventStore`、
> `StreamReducer`），协议版本只体现在 `schema_version=2` 上；带 `V1` 后缀的名字只出现在
> 兼容模块（见 §8）。这样后续删除兼容层不需要把 `RuntimeEventV2` 全仓改回 `RuntimeEvent`。

v2 使用 Pydantic discriminated union。以下代码只描述稳定接口，实际实现按 envelope、
content value object、event union 和 reducer 分文件。

```python
class SourceRef(BaseModel):
    framework: Literal["adk", "langgraph", "codex", "a2a", "ksadk"]
    native_event_id: str | None = None
    native_cursor: str | None = None
    native_run_id: str | None = None
    native_item_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class EventEnvelope(BaseModel):
    schema_version: Literal[2] = 2
    event_id: str
    seq: int
    timestamp: float
    run_id: str
    run_seq: int | None = None
    scope_id: str
    parent_scope_id: str | None = None
    source: SourceRef


ItemKind = Literal[
    "message",
    "reasoning",
    "tool_call",
    "tool_result",
    "artifact",
    "status",
    "data",
]


class ItemStarted(EventEnvelope):
    event_type: Literal["item.started"]
    item_id: str
    item_kind: ItemKind
    phase: Literal["commentary", "final_answer"] | None = None
    initial: ContentSnapshot | None = None


class ItemUpdated(EventEnvelope):
    event_type: Literal["item.updated"]
    item_id: str
    item_kind: ItemKind
    op: Literal["append", "replace"]
    update: ContentUpdate


class ItemCompleted(EventEnvelope):
    event_type: Literal["item.completed"]
    item_id: str
    item_kind: ItemKind
    snapshot: ContentSnapshot


class RunCompleted(EventEnvelope):
    event_type: Literal["run.completed"]
    output_refs: tuple[OutputRef, ...]


InteractionKind = Literal["approval", "structured_input"]


class InteractionRequested(EventEnvelope):
    event_type: Literal["interaction.requested"]
    interaction_id: str
    interaction_kind: InteractionKind
    request: InteractionRequest


class InteractionResolved(EventEnvelope):
    event_type: Literal["interaction.resolved"]
    interaction_id: str
    interaction_kind: InteractionKind
    response: InteractionResponse


ContinuationKind = Literal[
    "graph_checkpoint",
    "invocation_resume",
    "thread_resume",
    "task_resume",
]


class ContinuationCreated(EventEnvelope):
    event_type: Literal["continuation.created"]
    continuation_id: str
    continuation_kind: ContinuationKind
    resumable: bool
    ref: JsonObject


class ContinuationResumed(EventEnvelope):
    event_type: Literal["continuation.resumed"]
    continuation_id: str
    continuation_kind: ContinuationKind
    resume_attempt_id: str
```

完整 union 还包括 `RunStarted`、`ItemFailed`、`RunFailed`、`RunInterrupted`、取消、
context compaction 和 usage 事件。`ContentUpdate`、`ContentSnapshot`、`OutputRef`、
`InteractionRequest/Response` 同样使用 discriminated union/value object，不接受任意
Python 对象进入持久化事件。

`ContinuationKind` 只统一“平台需要展示和调用一个恢复入口”的外层合同，不抹平原生能力：

- LangGraph 使用真实 `graph_checkpoint`；
- ADK 使用 forward-only `invocation_resume`；
- Codex 使用 `thread_resume`；
- 通用长任务使用 `task_resume`。

`ref` 是 adapter 私有且受类型/序列化约束的恢复引用，不允许前端修改后回传并覆盖服务端事实。

其他映射约束：

- `part_id` 属于 typed `ContentUpdate/ContentSnapshot`，在同一 item 内标识 text、reasoning、
  image、file 或 data block。
- tool call 与 tool result 是两个 item，使用不同 `item_id`，通过稳定 `call_id` 关联。
- A2UI surface 使用 `item_kind=data`，稳定 `surface_id` 作为 source-native item identity；
  A2UI user action 使用 `interaction.*`，A2UI 仍只是协议 projection，不等同于 HITL。
- A2A Task 状态映射 run/status lifecycle，artifact 映射 artifact item，不新增一套 A2A 专属
  reducer。

约束：

- `item_id` 必须从 source-native identity **确定性派生**，live、完整 replay 和 cursor replay
  对同一个 source item 必须得到完全相同的值。禁止在 adapter 中使用 UUID、进程内计数器、
  时间戳、正文 hash、`author` 或运行时随机数生成 `item_id`。
- `scope_id` 同样从 source run/invocation/task 与 branch/namespace 确定性派生。
- `event_id` 标识一次 canonical mutation，而不是直接照抄可能被 partial/final 复用的 native event
  id。它由 framework、scope、native occurrence id/cursor、canonical event type、part 与稳定 chunk
  ordinal 确定性编码；同一 partial 重投得到相同 `event_id`，同 item 的 append/replace/complete
  得到不同 `event_id`。禁止在重试时重新生成随机 id。
- 如果 source 没有单独的 item id，adapter 必须使用该 source 可稳定重建的复合键，例如原生
  response/event id、namespace、task/artifact/message id 与 content block index；如果连稳定复合键
  都不存在，则该 source 只能提供 terminal snapshot，不得伪装成可精确 replay 的增量流。
- `seq` 是 session 级单调序号，与现有 `ListSessionEvents`、`ListSessionMessages` 和
  `SubscribeRunEvents.AfterSeqId` 共用一个游标空间。
- event 必须先由 session store 原子分配 `seq` 并持久化，再发布给 live subscriber；
  不能先向 Web 发送临时序号，再在存储层换号。
- source 原生顺序放在 `SourceRef.native_cursor`；需要 run 内诊断顺序时使用可选 `run_seq`，
  不能取代 session `seq`。
- `event_id` 的幂等域是 `(session_id, event_id)`；canonical store 在分配 `seq` 和发布前以唯一
  索引完成去重。同一 session 内同一 `event_id` 重放是 no-op，不同 session 可以复用 source id。
- `item.updated(op=append)` 只向已打开 item 追加。
- `item.updated(op=replace)` 权威覆盖 provisional 内容。
- `item.completed.snapshot` 覆盖 provisional 内容并关闭 item。
- item 完成或失败后继续更新属于 conformance error。
- `run.completed.output_refs` 是最终用户可见输出的唯一选择依据。

## 6. Canonical StreamReducer

```python
class StreamReducer:
    def apply(self, event: RuntimeEvent) -> ProjectionPatch: ...
    def snapshot(self) -> RunProjection: ...
```

Reducer 维护：

```text
run_id
  -> scope_id
       -> item_id
            -> ItemProjection
```

规则：

- `event_id` 负责 producer occurrence 的持久化幂等，`seq` 负责 session 顺序和断线游标，二者
  不互相替代。历史全量去重由 store 的 `(session_id, event_id)` 唯一索引负责，reducer 不保存
  无上界的 event id 集合。
- live reducer 只维护 `last_seq` 和固定上限 1024 条的 recent-event LRU，用于吸收发布/订阅边界
  的短期重复；`seq <= last_seq` 时不再应用，若该 seq 仍在 LRU 内但内容冲突则报 conformance
  error。更老的 seq 由 store 的唯一约束保证不可冲突。因此幂等辅助状态上界为 O(1024)，其余
  内存仅随当前 projection 与 open item 增长。
- 重复 `item.started` 只有在 `event_id` 相同时幂等，否则报错。
- 缺少 start 但上游直接给出完整 item 时，adapter 先合成 start，再发 complete。
- completed snapshot 与 provisional 内容不一致时，snapshot 获胜，并记录 reconcile 指标。
- `run.completed` 到达时若仍有 open item，产生结构化错误，不静默拼接。
- reducer 不读取 `author`，不比较文本内容，不识别框架专有 partial 标志。
- live 与 replay 都只调用同一个 `apply()`；不存在另一套 history parser。

## 7. Source adapter 映射

所有 adapter 先实现同一条 identity contract；下表中的值是 `item_id` 的确定性输入，不是仅供
观测的 metadata：

| Source | item identity 的确定性来源 |
| --- | --- |
| ADK | `invocation_id + branch/node path + event.id` |
| LangGraph | `namespace + LLM run/message id + content block id/index` |
| Codex | `thread_id + turn_id + native item id` |
| A2A artifact | `context_id + task_id + artifact_id` |
| A2A message | `context_id + task_id + message_id` |

adapter 使用稳定、带类型标签的编码生成 canonical `item_id`；相同输入必须逐字节得到相同结果。
`output_refs` 只能引用这些确定性 id，禁止在 `run.completed` 时重新分配或按数组位置猜测。

### 7.1 Google ADK 2.6.3+

- `scope_id` 使用 invocation 与 ADK node/branch 执行路径。
- 单次 model response 的 `item_id` 使用 `(invocation_id, event.id)`。
- 同一 `event.id` 的 `partial=True` 映射为 append update。
- 同一 `event.id` 的 `partial=False` 映射为 completed snapshot，不作为新正文 append。
- 下一次 LLM call 的新 `event.id` 创建新 item。
- function call/response 使用原生 event/call identity 创建 tool item。
- `is_final_response()` 只关闭当前 agent response，不直接关闭整个 run。

接入完成后删除 ADK runner 中的 per-author text/thought snapshot map 和文本前缀判断。

### 7.2 LangGraph v3

- 使用 `stream_events(..., version="v3")` canonical event stream。
- `scope_id` 保留 namespace 中的执行路径。
- message 使用 LLM `run_id/message_id`，content block 使用原生 block identity。
- `message-start/content-block-*/message-finish` 映射 item lifecycle。
- values、updates、messages、tools 和 lifecycle 保持不同 typed channel。
- LangGraph source seq 仅作为 source cursor；KsADK 仍分配 canonical `seq`。

### 7.3 Codex app-server v2

- `scope_id = thread_id + turn_id`。
- 保留原生 item id、content part id、turn id 和 thread id。
- `item/started`、delta、`item/completed` 直接映射 item lifecycle。
- completed item 是权威 snapshot，不降级为无 identity 的完整文本 append。
- reasoning、command、MCP tool、file change 和 agent message 保持不同 item kind。

### 7.4 A2A v1.0

- task scope 使用 `context_id + task_id`。
- artifact item 使用 `task_id + artifact_id`。
- `append=true` 映射 append，`append=false` 映射 replace/create。
- `last_chunk=true` 只关闭 artifact，内容操作仍由 `append` 决定。
- message-only response 使用 A2A `message_id` 作为 item identity。
- terminal Task snapshot 是第三方 A2A 的最终权威状态。

A2A core artifact update 没有通用 event id/seq，push 又允许重复。因此：

- KsADK 对端可通过声明式 A2A extension metadata 传递 producer `event_id/seq`。
- 不支持 extension 的第三方 agent，live update 只作 provisional preview。
- terminal、reconnect 或订阅重建时**必须**调用 `GetTask`，以 Task/artifact snapshot 校正
  provisional projection；若无法获得终态 snapshot，本次 run 不得标记为已一致回放。
- 对无 identity 的重复 append 不做文本推断，也不承诺 exactly-once。

## 8. RuntimeEvent v1 兼容投影

### 8.1 模块边界

兼容逻辑只能存在于一个模块：

```text
ksadk/events/v1_compat.py
```

v1 的旧信封类型也收敛到这里，命名为 `RuntimeEventV1`（带后缀的是 legacy，不是 canonical）：

```python
# ksadk/events/v1_compat.py

class RuntimeEventV1(BaseModel): ...

RuntimeEventV1ProjectionMode = Literal["snapshot_only", "identity_replace"]

def project_to_v1(
    event: RuntimeEvent,
    *,
    mode: RuntimeEventV1ProjectionMode = "snapshot_only",
) -> tuple[RuntimeEventV1, ...]: ...
```

`v1_compat` 只允许依赖 canonical `RuntimeEvent`(v2)、`ProjectionPatch` 和 `RunProjection`，
其他模块不得依赖其内部状态。source adapter、store 和 reducer 不得 import v1 compatibility
module。

### 8.2 信封兼容

保留 v1 现有字段：

```text
schema_version=1
event_id
event_type
timestamp
agent_id
user_id
session_id
invocation_id
seq_id
phase
payload
```

v2 身份和操作通过 v1 允许的 additive payload key 暴露：

```json
{
  "schema_version": 1,
  "event_type": "text.delta",
  "invocation_id": "run_123",
  "payload": {
    "text": "hello",
    "scope_id": "scope_123",
    "item_id": "item_123",
    "part_id": "part_0",
    "operation": "append",
    "source_event_id": "native_123"
  }
}
```

### 8.3 text/reasoning 投影模式

当前官方 `RuntimeEventParser` 会把 `TEXT_DELTA` 与 `TEXT_COMPLETED` 都 append 到
`(invocation_id, phase)`；当前 `ksadk-web` 也按 invocation 合并并使用正文前缀/后缀启发式。
因此把 completed 改成 full-snapshot replace 后仍称为“旧消费者兼容”是不成立的。v1 projector
必须显式提供以下两种互斥模式：

| 模式 | 适用消费者 | text/reasoning 行为 |
| --- | --- | --- |
| `snapshot_only` | 未声明 operation 能力的旧/第三方消费者；也是默认值 | 丢弃 `item.updated` 内容，只在 `item.completed` 发一个 full-snapshot `TEXT_COMPLETED` / `REASONING_COMPLETED`；旧 append parser 最终只追加一次，但不获得 raw RuntimeEvent 增量预览 |
| `identity_replace` | 已同车升级并声明支持 identity/operation 的官方消费者 | append update 发 delta；replace update 发带 `operation=replace` 的 delta；completed 发带 `operation=replace` 的 full snapshot |

两种模式都不改变 Responses、AG-UI、A2A 的流式体验：这些标准 wire 直接从 canonical reducer
projection 生成，不经过 raw v1 projector。不得把 `op=replace` 悄悄投影成旧 v1 delta；旧 key
无法寻址时必须走默认 `snapshot_only`，以 full-snapshot completed 作为唯一正文事件。

`identity_replace` 的新版 v1 parser：

- 按 `(invocation_id, scope_id, item_id, part_id, phase)` 聚合；
- append 只追加目标 part，replace 与 completed snapshot 覆盖目标 part；
- 按 `event_id` 幂等；
- 对缺少新增字段的历史 v1 event 使用旧 key，只作为短期 read fallback，不进入 canonical store。

消费者若未通过 capability 或显式调用参数声明 `identity_replace`，projector 必须选择
`snapshot_only`。不允许依据 User-Agent、前端版本字符串或正文形态猜测能力。

### 8.4 v1 直接消费者迁移清单

本次代码审计发现的生产消费路径必须全部纳入同车改造或验证：

| 消费路径 | 当前风险 | 本期目标与门禁 |
| --- | --- | --- |
| `ksadk.events.RuntimeEventParser`，含 replay/CLI | completed 无条件 append，且 key 只有 invocation/phase | 移入 `v1_compat`；支持 `identity_replace` 与 event id 幂等；旧 wire fixture 回归 |
| `ksadk.runtime.runner_adapter` | 直接选择 v1 final output | 改为直接消费 canonical projection/output refs，不经过 v1 |
| `ksadk.runtime.conversation_execution` / Responses | 仍用 snapshot/prefix 修正语义事件 | 改为 canonical patch -> Responses；删除正文前缀判断；Responses golden 保持 wire 不变 |
| `ksadk.agui.agent` | completed 通过前缀裁剪 | 改为 canonical patch -> AG-UI；运行同 item replace/completed fixture |
| `ksadk.a2a.executor` / `event_adapter` | completed 通过前缀裁剪，artifact 有独立累计状态 | 改为 canonical patch -> A2A artifact lifecycle；用 terminal `GetTask` 校正 |
| `ksadk.studio.run_service` 与 conversation message projection | 直接识别 v1 event type | 改为 canonical/session projection；live/replay 同 fixture |
| `ksadk/studio/react-ui` | `message.completed` 已按 snapshot replace，但正文仍按整个 run 聚合、没有 item identity | 按 run/scope/item/part 消费 Studio projection；活动页与可观测页共用 golden fixture |
| `ksadk-web` 的 `session-events.js` | 按 invocation 合并，并用正文启发式 | 切到 identity-aware reducer；仅 legacy server ingress 可合成 identity |
| `ksadk-python` embedded Agent UI | 使用 `ksadk-web` 的 `build:ksadk` 静态产物 | 发布同一 `ksadk-web` 版本后重新同步 `ksadk/server/static`；运行静态合同与本地 UI E2E |
| `agentengine-hosted-ui` | 生产壳固定消费 `@kingsoftcloud/ksadk-web` npm 包 | 升级到同一已发布版本；运行仓库合同测试、`build:hosted` 与预发 RunAgent/replay/reconnect E2E |

`ksadk.codex.runtime`、`ksadk.harness.runtime` 是 v1 事件生产/适配路径，不是 completed 的最终
聚合消费者；它们也必须迁移到 source adapter 输出 canonical event，不能继续产生旁路 v1 事实。
AgentEngine server 不在此表中，因为它不消费 raw RuntimeEvent；其边界在 §9.1 单独验证。

### 8.5 其他事件映射

- tool item 使用已有 `call_id`，同时追加 `scope_id/item_id`。
- artifact 使用稳定 artifact name/id 与 version，不按正文合并。
- approval、checkpoint、usage、A2UI、A2A task 和 run lifecycle 保留现有 v1 event type，追加
  v2 source reference，不改变既有必填 payload key。
- v1 projector 不重新决定 final answer；它只投影 `run.completed.output_refs` 已选择的结果。

### 8.6 Python API 与版本发现

- 核心类型直接用 canonical 名：`ksadk.events.RuntimeEvent` 升级后即代表 schema_version=2 的
  canonical schema，**不保留**旧 v1 Python 类名 alias，也不引入 `RuntimeEventV2` 这种临时名。
- 兼容目标只覆盖 **v1 wire/JSON**，不覆盖旧 Python 顶层导入语义。需要显式使用旧类型的调用方
  改为 `from ksadk.events.v1_compat import RuntimeEventV1`；v1 parser 同样收敛进 `v1_compat`。
  鉴于 v1 刚发布、使用者很少，这个 import 破坏是可接受的一次性成本，换来核心类型长期不带
  代数后缀。
- v1 reader 通过 `v1_compat.project_to_v1` 获取事件；`RuntimeEventStore` 不增加 `version=1`
  分支，也不返回 v1/v2 混合列表。
- `RunAgent`、Responses、AG-UI 和 A2A 使用各自标准 wire，不增加 RuntimeEvent 版本参数。
- capability 追加 `RuntimeEventVersions=[1, 2]`、`RuntimeEventDefault=2`、
  `RuntimeEventV1ProjectionModes=["snapshot_only", "identity_replace"]` 和
  `RuntimeEventV1ProjectionDefault="snapshot_only"`，由 capability 声明（而非类型名）表达
  协议版本和消费能力；旧 AgentEngine server 忽略这些字段时，不影响 Responses 和会话管理接口。

### 8.7 兼容承诺

兼容承诺包括：

- v1 JSON 信封可反序列化；
- 现有 event type 和必填 payload key 保留；
- 官方 v1 parser、AG-UI/A2UI projection、Web、embedded Studio 与 Hosted UI 按上表同车验证后
  得到正确结果；
- 未声明 operation 能力的只读消费者通过默认 `snapshot_only` 得到正确终态文本。

兼容承诺不包括：

- 旧版自定义 parser 同时消费 delta 与 full-snapshot completed 仍得到正确文本；该组合不会再发给
  未声明能力的调用方；
- 升级前保存的 v1 日志可迁移到 v2；
- v1 producer 可以写入 v2 canonical store。

## 9. 不升级 AgentEngine server 的行为

### 9.1 保持不变的公开接口

以下接口不改 URL、不新增必填参数、不删除或改名字段：

- `RunAgent`
- `ListSessionEvents`
- `ListSessionMessages`
- `SubscribeRunEvents`
- `ListSessionCheckpoints`
- `GetCheckpointResumePreview`
- `ResumeRun`
- `CancelRun`

`RunAgent(Stream=true)` 继续输出标准 Responses 或 Chat Completions SSE，不直接输出
RuntimeEvent。当前代码核验后的 Responses 调用链固定为：

```text
AgentEngine RunAgent
  -> ConversationRuntimeService.run/_stream_runtime
  -> ConversationRuntimeService._consume_runtime_stream
  -> ChatService.stream_responses
  -> ksadk-python POST /v1/responses
  -> iter_runtime_conversation_semantic_events
  -> Responses SSE serializer
  -> AgentEngine 原样转发 chunk，并仅用 Responses event 聚合 transcript
```

`RuntimeEvent -> semantic event -> Responses SSE` 的转换发生在 `ksadk-python`。AgentEngine 的
`_update_stream_text` 只识别 `response.output_text.delta`、`response.content_part.delta` 与
`response.completed`（以及 Chat Completions choices），**从不解析** `text.delta`、
`text.completed` 或 RuntimeEvent v1/v2 信封。因此 raw v1 completed 语义变化不会迫使 server
同步升级；若未来 server 开始解析 RuntimeEvent，此结论自动失效，必须重新走兼容评审。

这里的“不升级 server”只承诺 `RunAgent` 主链路和已有公开管理接口的 wire 不变，不等于 server
已具备 canonical identity。`ListSessionEvents`、`SubscribeRunEvents` 属于 session projection
链路，必须按下一节单独做 contract fixture，不能用 RunAgent 的纯 Responses 边界替代验证。

### 9.2 会话事件投影

runtime 对 `ListSessionEvents` 和 `SubscribeRunEvents` 继续返回当前结构：

```text
EventId / SessionId / InvocationId / EventType / Content /
StateDelta / SeqId / Metadata / Timestamp
```

新增 v2 身份放入 `Metadata.RuntimeItem`：

```json
{
  "RuntimeItem": {
    "RunId": "run_123",
    "ScopeId": "scope_123",
    "ItemId": "item_123",
    "PartId": "part_0",
    "Operation": "replace",
    "SourceEventId": "native_123"
  }
}
```

AgentEngine server runtime-source 路径可以透明转发这些 additive metadata。云控制台如果只查询
`run_checkpoint`、恢复状态和会话管理字段，不需要修改。

runtime 仍按现有事件类型接受 `EventTypes` 过滤，但过滤键必须同时约束 canonical event type
与 `continuation_kind`，不能把 `run_checkpoint` 宽化成所有 `continuation.created`：

| legacy 过滤值 | canonical predicate | legacy 投影 |
| --- | --- | --- |
| `run_checkpoint` | `event_type=continuation.created AND continuation_kind=graph_checkpoint` | `run_checkpoint` |
| `run_resume` | `event_type=continuation.resumed AND continuation_kind=graph_checkpoint` | `run_resume` |
| 无对应旧值 | `continuation_kind IN {invocation_resume, thread_resume, task_resume}` | 不进入旧 checkpoint 列表；由 continuation capability/view 暴露 |

`ListSessionCheckpoints` 使用相同 predicate；这里只有 LangGraph `graph_checkpoint` 是真实
checkpoint。否则 ADK `invocation_resume`、Codex `thread_resume` 或通用 `task_resume` 会被云控制台
误显示为 graph checkpoint。过滤、精确 `Total` 和分页针对投影后的公开事件集合计算，不能先按
v2 全量分页再丢弃隐藏事件，也不能只按 event type 过滤后再忽略 kind。

### 9.3 server control fallback

当前 server 自己生成的 `assistant_stream_snapshot`、`assistant_message` 和 `run_status` 没有 v2
item identity。新版 Web 在单一 legacy ingress adapter 中使用确定性映射：

```text
scope_id = InvocationId
item_id = InvocationId + ":legacy-assistant"
assistant_stream_snapshot -> replace
assistant_message -> completed snapshot
```

该映射只适用于 server 已经聚合成单个 assistant output 的 legacy 事件，不比较文本内容，
不使用 `startswith()`，也不进入 Python canonical store。

### 9.4 本期可达到与不可达到的结果

本期可以达到：

- ADK 多 LLM call 重复输出在 source/runtime 层根治；
- 新版 Web/Studio 的 live、runtime replay 和刷新恢复统一；
- 现有 AgentEngine API、云控制台 checkpoint 管理和长任务恢复保持可用；
- 未升级 Hosted UI 仍可通过 Responses live 与 `snapshot_only`/legacy session view 得到正确终态，
  但不获得 raw RuntimeEvent 增量 replay；本期放量目标仍是升级到与 `ksadk-web` 相同的已验证版本。

本期不能宣称：

- AgentEngine server control-plane mirror 已原生保存 `RuntimeEvent(schema_version=2)`；
- server 本地产生的全部事件都有真实 subagent/item identity；
- v1 projector 和 Web legacy ingress 已可删除；
- 未升级的自定义 v1 parser 已理解 completed snapshot 的 replace 语义。

## 10. ksadk-web、Studio 与 Hosted UI 边界

- Web 核心 store 按 `run_id/scope_id/item_id/part_id` 索引 projection。
- Web 只消费 reducer patch 或带 identity 的会话 projection，不维护 `last_text`。
- 相同 event id 重放是 no-op；不同 event id 的相同正文不能删除。
- `replace` 覆盖指定 part；`completed` 只关闭 item 并应用权威 snapshot。
- Responses、AG-UI 和 session replay 最终进入同一个 Web projection reducer。
- server legacy ingress adapter 是唯一允许合成 item id 的位置。
- `ksadk-python` 的独立 React Studio 必须消费带 identity 的 Studio projection；它不依赖
  `@kingsoftcloud/ksadk-web`，但必须复用同一组 cross-language golden fixtures。
- `ksadk-python` embedded Agent UI 与 `agentengine-hosted-ui` 都消费 `ksadk-web` 构建产物；前者
  同步 `build:ksadk` 到 `ksadk/server/static`，后者通过固定 npm 版本消费 `build:hosted`。
- Hosted UI 的 SSO、权限、workspace/terminal 壳可以独立演进，但不得 fork stream/session parser。

旧的以下逻辑在新 identity 路径接通后删除：

- per-agent/per-author active text；
- `startswith()`、公共前缀、后缀重叠和正文 hash 去重；
- “上一条消息是否 streaming”的 item identity 推断；
- partial/final 按相邻位置猜测属于同一消息。

## 11. 持久化、回放与隐私

- canonical RuntimeEvent 继续复用现有 `SessionEvent` 表和 session service 作为物理载体，
  使用 `schema_version=2` marker；不新建第二张 event log 表。
- `SessionEvent` 是存储 envelope，不等同于 legacy `ListSessionEvents` 返回结构。公开结构在读取时
  从 v2 投影产生。
- Python runtime 不再为同一 assistant output 同时持久化 v2 item event、RuntimeEvent v1、
  `assistant_stream_snapshot` 和 `assistant_message` 四份事实。
- checkpoint、approval、run status 等现有 domain event 逐步映射为 v2 continuation、interaction
  和 run lifecycle event；兼容接口在读取时恢复旧 `EventType/Content/Metadata`。
- canonical `seq` 由 session store 分配，并由 `(session_id, seq)` 唯一约束保护。
- replay 从 v2 event log 重建 reducer，不读取 v1 projector 状态。
- v1 response 在读取时或流式 projection 时生成，不单独持久化。
- 升级后同一物理 session 可能包含旧 v1 run 和新 v2 run，这是明确接受的过渡窗口：
  - canonical reducer/replay 只重建带 `schema_version=2` 的 run，不把旧 v1 output 猜测转换成
    canonical item；
  - `ListSessionEvents` / `ListSessionMessages` 的 legacy read view 合并“旧记录的既有 read
    projection”和“新 v2 记录的 v1/session projection”，并按共享物理 `seq` 排序，所以旧 run
    文本不能在升级后消失；
  - `AfterSeqId` 始终使用同一 session seq 空间，不能为新旧 schema 分配两个游标；
  - 新 v2 turn 可以使用 legacy message projection 作为会话上下文，但它的输出与执行恢复仍只写 v2。
- 跨升级时仍在运行的 v1 run 不允许接入 v2 replay/resume。发布前必须 drain 或显式取消活跃
  v1 run；漏网请求返回 `409 legacy_run_not_resumable`，不能静默显示成“恢复成功但文本消失”。
- 因此“不迁历史”表示不把旧 event 伪造为 v2 canonical log，不表示旧会话 UI 不再可读。
- raw source payload 默认不永久保存 reasoning 或敏感数据。
- 调试 raw payload 只能短期、受配置、脱敏保存，并遵守现有 audit 与访问控制。

## 12. 错误处理与可观测性

conformance error 不能只打日志；live 与 replay 必须从已持久化的同一组 canonical recovery event
得到相同终态。无效 source event 本身不发布为内容，恢复规则固定为：

| 违规 | canonical 恢复与 projection |
| --- | --- |
| append-before-start | 如果 adapter 同时持有完整原生 item/snapshot，先合成确定性 `item.started` 再 complete；只有孤立 delta 时拒绝该 delta，持久化对应 `item.failed` 与 `run.failed` |
| update-after-complete | 保留已完成 snapshot，拒绝后续内容，持久化 `run.failed` |
| 同一 item id 出现不兼容 item kind | 持久化该 `item.failed` 与 `run.failed`，不把两种内容合并 |
| source sequence 倒序或 gap | 有权威 terminal snapshot 的 A2A/Codex source 先拉取并 reconcile；无法校正则 `run.failed` |
| `run.completed` 时仍有 open item | 为每个 open item 按确定顺序合成 `item.failed`，把原完成事件转换为 `run.failed` |
| completed snapshot 与 provisional 内容不一致 | snapshot 获胜并正常完成，记录 reconcile 指标，不视为 fatal |

`run.failed` 携带结构化 `ConformanceError {code, source, scope_id, item_id, source_ref}`；不得携带
正文、tool 参数或用户输入。fatal recovery event 必须先持久化再投影，v1/session/Responses 端只从
该 canonical 失败终态生成各自 wire，不允许 Web 自行补一个不同的失败消息。

snapshot 自动校正增加：

```text
stream_projection_reconciled_total{source,reason}
```

协议违规增加：

```text
stream_conformance_error_total{source,reason}
```

兼容层使用情况增加：

```text
runtime_event_v1_projection_total{event_type,mode}
legacy_session_event_without_item_identity_total{event_type,source}
```

指标不得包含正文、用户输入、tool 参数或敏感 metadata。

## 13. 测试与验收

### 13.1 canonical golden scenarios

1. ADK subagent 在 tool 前后进行两次 LLM call，各自 partial + full final，只显示一次。
2. 同一 agent 连续产生两个正文完全相同但 item id 不同的 response，两个都保留。
3. 多个同名 subagent invocation 交错输出而不串流。
4. LangGraph message/reasoning/tool block 交错，按 namespace 与 item identity 投影。
5. Codex delta 丢失后，completed snapshot 修复最终内容而不重复。
6. A2A 多 artifact、append、replace、lastChunk 和 message-only response。
7. 相同 event id 重放是 no-op；不同 event id 的相同 delta 均生效。
8. append-before-start、update-after-complete 和 incompatible item kind 产生确定错误。
9. live、完整 replay、cursor replay 的最终 `RunProjection` 完全一致。
10. `run.completed.output_refs` 只选择最终用户可见输出。
11. 同一 source fixture 在不同进程中产生逐字节相同的 `scope_id/item_id/output_refs`，不得依赖 UUID。
12. 每一种 fatal conformance error 的 live 与 replay 都产生相同的 `item.failed/run.failed` 终态。

### 13.2 v1 compatibility scenarios

1. 每一种现有 v1 event type 都有 v2 -> v1 contract fixture。
2. v1 JSON 信封和既有必填 payload key 保持不变。
3. `snapshot_only` 不发正文 delta，只发一个 full-snapshot completed；未升级 append parser 的
   最终文本正确。
4. `identity_replace` 的 delta 后 full-snapshot completed 在新版 v1 parser 中执行 replace，不重复。
5. 未声明 capability、未知调用方和显式 legacy 调用均选择 `snapshot_only`。
6. `op=replace` 绝不作为不带 identity/operation 的旧 delta 静默输出。
7. 不带 item identity 的历史 v1 event 仍可由 legacy parser path 读取。
8. v1 projector 不产生单独持久化记录。
9. v1 projector 输出重放两次时，官方 parser 结果保持幂等。

### 13.3 AgentEngine server compatibility scenarios

使用固定 JSON/SSE fixture 验证，不要求修改 server 代码：

1. `RunAgent` 非流式 envelope 不变。
2. `RunAgent(Stream=true)` 仍是标准 Responses/Chat Completions SSE。
3. `ListSessionEvents` 既有字段不变，只追加 `Metadata.RuntimeItem`。
4. `SubscribeRunEvents` 保留 heartbeat、`AfterSeqId` 和 `[DONE]`。
5. `ListSessionCheckpoints`、preview、resume 和 cancel 请求/响应不变。
6. server control fallback 事件通过 deterministic legacy ingress 得到单个 assistant item。
7. 云控制台使用 `EventTypes=["run_checkpoint"]` 时只得到 `graph_checkpoint`，不会混入
   `invocation_resume` / `thread_resume` / `task_resume`；`Total` 与分页精确。
8. AgentEngine `_update_stream_text` 的 fixture 只接收 Responses/Chat Completions event；raw
   `text.completed` 不进入 `RunAgent` server 聚合层。
9. 混合 schema session 的旧 run 与新 run 都能通过 legacy list view 回显，且共用 `AfterSeqId`。
10. 活跃 v1 run 跨升级 resume 返回明确 409，而不是生成空白或部分 v2 replay。

### 13.4 客户端同车验证矩阵

| 客户端/消费面 | 输入 wire | 必须验证 |
| --- | --- | --- |
| `ksadk-web` 单测 | Responses SSE、identity session projection、legacy session projection | 多 LLM call、多 item 同文、replace/completed、刷新、切换 session、cursor replay |
| `ksadk-python` React Studio | Studio run/session projection | `chatProtocol`、活动页、可观测页按 item 聚合；Studio 单测与 production build |
| `ksadk-python` embedded Agent UI | 同一 `ksadk-web build:ksadk` 产物 | 静态同步合同、本地 RunAgent -> replay -> reconnect |
| `agentengine-hosted-ui` | 发布后的同一 `@kingsoftcloud/ksadk-web` 包 + AgentEngine Actions | 依赖版本合同、`npm test`、`build:hosted`、预发 RunAgent/replay/reconnect/resume/cancel |
| AG-UI/A2UI | canonical projection -> AG-UI/A2UI | 文本 item lifecycle、approval、structured input、surface/action 独立性 |
| A2A | canonical projection -> Task/artifact/message | append/replace/lastChunk、重复 push、断线后强制 `GetTask` 校正 |
| 云控制台会话管理 | AgentEngine list/checkpoint Actions | session list、事件分页、checkpoint kind 过滤、preview/resume；不要求聊天渲染 |

`agentengine-hosted-ui` 当前固定消费 npm 包而不是复制 parser；发布顺序必须是：先合并并发布
`ksadk-web`，再升级 Hosted UI 的依赖并构建镜像，同时用同一版本重新生成 embedded Agent UI
静态资源。独立 React Studio 同车运行相同 golden fixtures。四个消费面未全部通过时不得放量。

### 13.5 验证门禁

- targeted adapter/reducer/v1 projection tests；
- 完整 Python test suite；
- ruff 与类型检查；
- `ksadk-python` React Studio 测试/build，以及 embedded Agent UI 静态同步合同；
- `ksadk-web` unit/contract tests 和 production build；
- `agentengine-hosted-ui` dependency contract、unit tests、`build:hosted` 和预发 smoke/E2E；
- ADK 2.6.3+、LangGraph v3、Codex app-server v2、A2A v1.0 fixture matrix；
- AgentEngine server 当前接口 golden compatibility suite；
- 允许时补一次真实 Hosted RunAgent -> replay -> reconnect -> resume E2E。

任何门禁失败都不能宣称重复输出问题已经根治。

## 14. 兼容层删除策略

兼容代码从第一天就标记 deprecated，但不按主观日期直接删除。全部满足以下条件后，在后续
breaking release 删除：

1. 当前 Hosted UI 和 Studio 均使用 identity-aware projection。
2. AgentEngine server 已原生透传/保存 v2 identity，或确认始终消费稳定的非 RuntimeEvent wire。
3. 云控制台 checkpoint/session 管理 contract suite 连续通过。
4. `runtime_event_v1_projection_total` 在所有可观测环境连续 30 天为零，或维护者明确接受
   剩余调用方中断。
5. `legacy_session_event_without_item_identity_total` 连续 30 天为零。
6. v1 projector、legacy ingress 和相关 fixture 能在一个独立提交中完整删除，不影响 adapter、
   reducer、store、Responses、AG-UI/A2UI 或 A2A 测试。

删除时只移除：

- `v1_compat.py`（含 `RuntimeEventV1`、`project_to_v1` projector）；
- v1 parser fallback；
- Web server-legacy ingress；
- v1 contract fixtures 和 capability 声明。

由于核心类型从一开始就叫 `RuntimeEvent`（版本由 `schema_version` 表达），删除**不涉及**任何
`RuntimeEventV2 -> RuntimeEvent` 式的全仓改名。不得再次修改 canonical adapter/reducer 才能完成
删除；如果删除时必须改核心，说明本期隔离失败。

## 15. 实施分段

本节只定义评审边界，不代替后续 implementation plan。

1. 建立 `RuntimeEvent(schema_version=2)`、typed content、conformance tests 和纯 reducer。
2. 建立 v1 单向 projector 与 current-server contract fixtures。
3. 接入 ADK adapter，先验收多 LLM call 复现用例。
4. 接入 LangGraph、Codex 与 A2A adapter。
5. 将 canonical store、replay 和 final output selection 切到 reducer。
6. 将 Responses、AG-UI/A2UI、A2A 和 session events 切到 reducer projection。
7. 将 React Studio 与 `ksadk-web` 切到 identity-aware projection，并保留单一 legacy ingress。
8. 发布同一 `ksadk-web` 版本，升级 Hosted UI 依赖并同步 embedded Agent UI 静态资源。
9. 运行跨协议、全部 Web 消费面与云控制台 contract/E2E 后删除 per-author 和文本启发式逻辑。
10. 更新 RuntimeEvent capability、兼容说明和弃用指标，不修改 AgentEngine 公共 API。

## 16. 风险与回滚

### 16.1 主要风险

- ADK 某些自定义/A2A subagent 丢失原生 event id，adapter 无法构造稳定 item identity。
- A2A 第三方 push 不携带 extension，无法提供 exactly-once live update。
- server control fallback 缺少真实 item identity，只能恢复成 invocation 级 composite item。
- v1 `identity_replace` 需要官方 parser、Web、React Studio、embedded Agent UI 与 Hosted UI
  同车升级；未声明能力的调用方必须锁定 `snapshot_only`。
- 并发 run 共用 session `seq` 时，任何绕过 persist-before-publish 的路径都会破坏 cursor replay。
- 混合 schema session 的 legacy read view 若漏合并旧记录，会让旧 run 在 UI 中消失；活跃 v1 run
  若未 drain，会无法安全接入 v2 resume。

### 16.2 回滚原则

- source adapter 以 framework 维度接入，可单独关闭某个新 adapter 并回退到稳定 Responses
  非流式 snapshot 或 v1 `snapshot_only`，但不能回退到文本前缀去重。
- v1 projector 可保持开启，不影响 canonical store。
- 失败版本不得同时写 v1/v2 两套 store；回滚应用版本时使用升级前新建的测试会话验证，
  不承诺跨 schema 回放。
- checkpoint/resume 数据与 output item event 分开，事件投影回滚不能删除真实 checkpoint。

## 17. Kimi 评审问题

请给出明确的“同意 / 建议修改 / 阻塞”结论，并重点回答：

1. 内部直接采用 `RuntimeEvent(schema_version=2)`、v1 仅做单向投影，是否比继续 additive 扩展 v1 更能降低
   后续维护成本？
2. `scope_id/item_id/event_id` 是否缺少必须的一层身份，例如 response id 或 content block id？
3. ADK `(invocation_id, event.id)` 是否足以区分同一次任务里的多次 LLM call 和同名 subagent？
4. 未声明能力默认 `snapshot_only`、官方同车消费者用 `identity_replace`，是否彻底关闭了
   `TEXT_COMPLETED` 被旧 parser 二次 append 的窗口？
5. §9.1 已核验的 Responses 调用链与 §9.2 session projection 分界，是否足以证明本期无需升级
   AgentEngine server？
6. canonical store 只存 v2、legacy read view 合并旧 run、活跃 v1 run 不允许跨版本恢复的窗口，
   是否清楚且可执行？
7. A2A 无 extension 的 at-least-once 限制是否表达准确，是否需要更严格的 terminal snapshot
   reconciliation 规则？
8. v1 projector、Web legacy ingress，以及 ksadk-web -> embedded Studio / Hosted UI 的同车发布
   门禁是否足够窄且可执行？
9. 以上方案是否遗漏 approval、structured input、A2UI activity、tool lifecycle 或长任务恢复的
   关键交互？
10. 是否仍有任何直接 raw RuntimeEvent 消费者或 server 解析路径未被 §8.4 / §9.1 枚举？

## 18. 评审通过标准

评审通过需要同时确认：

- v2 identity/lifecycle 能覆盖四类 source adapter；
- v1 仅是单向、无状态或可重建的输出投影；
- canonical store 和 reducer 始终只有一套；
- server 不升级不会破坏当前管理与恢复接口；
- 兼容层具有可观测使用量、明确删除门槛和独立删除路径；
- golden scenarios 足以证明“合法相同文本不误删、partial/final 不重复、live/replay 一致”。
