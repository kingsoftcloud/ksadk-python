# RuntimeEvent v2 与 v1 短期兼容设计

> 日期：2026-08-11
>
> 状态：待 Kimi 与维护者评审
>
> 代码库：`ksadk-python`、`ksadk-web`
>
> 支持基线：Google ADK 2.6.3+、A2A v1.0、LangGraph v3 event streaming、Codex app-server v2

## 1. 评审摘要

本设计解决 KsADK 在同一次 subagent 任务包含多次 LLM 调用时可能重复输出的问题，同时让
当前刚发布的 RuntimeEvent v1 获得一个短期、可删除的兼容窗口。

最终决策是：

> RuntimeEvent v2 是唯一 canonical event、唯一持久化事实源和唯一 reducer 输入；
> RuntimeEvent v1 只由 v2 单向投影产生，不接受 v1 写入，不迁移历史 v1 事件，也不保留
> 第二套 reducer。

本期只要求修改 `ksadk-python` 和 `ksadk-web`。现有 AgentEngine server、云控制台会话管理、
`RunAgent`、`ListSessionEvents`、`SubscribeRunEvents`、checkpoint/恢复接口保持公开契约不变。

请评审者重点判断：

1. `scope_id + item_id + event_id` 三层身份是否足以覆盖 ADK、LangGraph、Codex 与 A2A。
2. v1 单向投影是否真正隔离，是否存在隐性双写或第二 reducer。
3. 不升级 AgentEngine server 时，Responses、事件回放和长任务恢复是否仍保持现有行为。
4. v1 `TEXT_COMPLETED` 保留完整 snapshot、由新 parser 执行 replace 的选择是否合理。
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
3. `event_id`：一次 transport occurrence，用于幂等、排序和 replay。

`agent_name`、`author` 和模型名只作 attribution metadata，不参与聚合键或幂等键。
`model_call_id` 是可选 provenance，不是通用主键，因为 A2A agent 不一定存在模型调用。

### 4.2 一个事实源、一个 reducer

```text
source event
  -> source adapter
  -> RuntimeEvent v2
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
RuntimeEvent v2 / RunProjection -> RuntimeEvent v1
```

不提供：

```text
RuntimeEvent v1 -> RuntimeEvent v2
```

因此 v1 兼容模块不进入 source adapter、canonical store、reducer 或 final output selection。

## 5. RuntimeEvent v2 合同

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

- `seq` 是 session 级单调序号，与现有 `ListSessionEvents`、`ListSessionMessages` 和
  `SubscribeRunEvents.AfterSeqId` 共用一个游标空间。
- event 必须先由 session store 原子分配 `seq` 并持久化，再发布给 live subscriber；
  不能先向 Web 发送临时序号，再在存储层换号。
- source 原生顺序放在 `SourceRef.native_cursor`；需要 run 内诊断顺序时使用可选 `run_seq`，
  不能取代 session `seq`。
- 同一 `event_id` 重放是 no-op。
- `item.updated(op=append)` 只向已打开 item 追加。
- `item.updated(op=replace)` 权威覆盖 provisional 内容。
- `item.completed.snapshot` 覆盖 provisional 内容并关闭 item。
- item 完成或失败后继续更新属于 conformance error。
- `run.completed.output_refs` 是最终用户可见输出的唯一选择依据。

## 6. Canonical StreamReducer

```python
class StreamReducer:
    def apply(self, event: RuntimeEventV2) -> ProjectionPatch: ...
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

- 重复 `item.started` 只有在 `event_id` 相同时幂等，否则报错。
- 缺少 start 但上游直接给出完整 item 时，adapter 先合成 start，再发 complete。
- completed snapshot 与 provisional 内容不一致时，snapshot 获胜，并记录 reconcile 指标。
- `run.completed` 到达时若仍有 open item，产生结构化错误，不静默拼接。
- reducer 不读取 `author`，不比较文本内容，不识别框架专有 partial 标志。
- live 与 replay 都只调用同一个 `apply()`；不存在另一套 history parser。

## 7. Source adapter 映射

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
- terminal、reconnect 或订阅重建时使用 `GetTask` snapshot 校正。
- 对无 identity 的重复 append 不做文本推断，也不承诺 exactly-once。

## 8. RuntimeEvent v1 兼容投影

### 8.1 模块边界

兼容逻辑只能存在于一个模块：

```text
ksadk/events/v1_compat.py
```

它只允许依赖 v2 schema、`ProjectionPatch` 和 `RunProjection`，其他模块不得依赖其内部状态。
source adapter、store 和 reducer 不得 import v1 compatibility module。

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

### 8.3 text/reasoning 映射

- `item.updated(op=append)` -> `TEXT_DELTA` / `REASONING_DELTA`。
- `item.updated(op=replace)` -> 对应 delta event，并携带 `operation=replace`。
- `item.completed` -> `TEXT_COMPLETED` / `REASONING_COMPLETED`，`text` 保留完整权威 snapshot，
  并携带 `operation=replace`、`item_id` 与 `scope_id`。
- 新版 v1 parser 按 `(invocation_id, scope_id, item_id, part_id, phase)` 聚合，并让 completed
  snapshot 覆盖 provisional 内容，不再 append 全文。
- 新版 v1 parser 记录已消费 `event_id`；相同 event 重放是 no-op。
- 对缺少新增字段的 v1 event，新版 parser 使用旧 key，只作为短期 read fallback；该 fallback
  不进入 v2 store，也不用于旧历史迁移。

选择保留 completed 完整 snapshot，而不是只输出增量后缀，原因是完整 snapshot 可以修正丢包、
重连和 source provisional 内容错误。自定义 v1 客户端如果无条件 append completed 全文，需要升级
其 parser；当前官方 parser 和 `ksadk-web` 会在本期一并修正。

### 8.4 其他事件映射

- tool item 使用已有 `call_id`，同时追加 `scope_id/item_id`。
- artifact 使用稳定 artifact name/id 与 version，不按正文合并。
- approval、checkpoint、usage、A2UI、A2A task 和 run lifecycle 保留现有 v1 event type，追加
  v2 source reference，不改变既有必填 payload key。
- v1 projector 不重新决定 final answer；它只投影 `run.completed.output_refs` 已选择的结果。

### 8.5 Python API 与版本发现

- 现有 `ksadk.events.RuntimeEvent` 和 RuntimeEvent v1 parser 在一个过渡版本内保留。
- 新 canonical 类型显式命名为 `RuntimeEventV2`，内部 adapter、store 和 reducer 只使用 v2。
- v1 reader 通过 `v1_compat` projector 获取事件；`RuntimeEventStore` 不增加 `version=1`
  分支，也不返回 v1/v2 混合列表。
- `RunAgent`、Responses、AG-UI 和 A2A 使用各自标准 wire，不增加 RuntimeEvent 版本参数。
- capability 可追加 `RuntimeEventVersions=[1, 2]` 和 `RuntimeEventDefault=2`，但旧 AgentEngine
  server 忽略这些字段时，不影响 Responses 和会话管理接口。

### 8.6 兼容承诺

兼容承诺包括：

- v1 JSON 信封可反序列化；
- 现有 event type 和必填 payload key 保留；
- 官方 v1 parser、AG-UI/A2UI projection 和 Web 在升级后得到正确结果；
- 未使用新增身份字段的只读消费者仍可读取事件。

兼容承诺不包括：

- 旧版自定义 parser 继续无条件 append completed snapshot 仍得到正确文本；
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
RuntimeEvent v2。AgentEngine server 可以继续代理和解析当前 wire。

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

runtime 仍按现有事件类型接受 `EventTypes` 过滤。进入存储查询前，兼容层把公开过滤值翻译为
canonical 类型，例如 `run_checkpoint -> continuation.created`；过滤、精确 `Total` 和分页针对
投影后的公开事件集合计算，不能先按 v2 全量分页再丢弃隐藏事件。

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
- 旧 Hosted UI 可继续消费 v1/Responses wire，并获得源头输出修复收益。

本期不能宣称：

- AgentEngine server control-plane mirror 已原生保存 RuntimeEvent v2；
- server 本地产生的全部事件都有真实 subagent/item identity；
- v1 projector 和 Web legacy ingress 已可删除；
- 未升级的自定义 v1 parser 已理解 completed snapshot 的 replace 语义。

## 10. ksadk-web 与 Studio 边界

- Web 核心 store 按 `run_id/scope_id/item_id/part_id` 索引 projection。
- Web 只消费 reducer patch 或带 identity 的会话 projection，不维护 `last_text`。
- 相同 event id 重放是 no-op；不同 event id 的相同正文不能删除。
- `replace` 覆盖指定 part；`completed` 只关闭 item 并应用权威 snapshot。
- Responses、AG-UI 和 session replay 最终进入同一个 Web projection reducer。
- server legacy ingress adapter 是唯一允许合成 item id 的位置。
- `ksadk-python` 内的 React Studio 和独立 `ksadk-web` 复用同一组 cross-language golden
  fixtures，避免两个前端重新实现不同语义。

旧的以下逻辑在新 identity 路径接通后删除：

- per-agent/per-author active text；
- `startswith()`、公共前缀、后缀重叠和正文 hash 去重；
- “上一条消息是否 streaming”的 item identity 推断；
- partial/final 按相邻位置猜测属于同一消息。

## 11. 持久化、回放与隐私

- canonical RuntimeEvent v2 继续复用现有 `SessionEvent` 表和 session service 作为物理载体，
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
- 升级前 v1 历史事件不迁移；旧会话可继续通过现有 legacy session message 投影只读查看，
  但不承诺进入 v2 replay。
- raw source payload 默认不永久保存 reasoning 或敏感数据。
- 调试 raw payload 只能短期、受配置、脱敏保存，并遵守现有 audit 与访问控制。

## 12. 错误处理与可观测性

以下情况产生结构化 conformance error，并记录 framework、scope 和 item：

- append-before-start；
- update-after-complete；
- 相同 item id 出现不兼容 item kind；
- source sequence 倒序或不允许的 gap；
- run completion 时仍有 open item；
- completed snapshot 与 provisional 内容不一致。

最后一种由 reducer 使用 completed snapshot 自动校正，并增加：

```text
stream_projection_reconciled_total{source,reason}
```

协议违规增加：

```text
stream_conformance_error_total{source,reason}
```

兼容层使用情况增加：

```text
runtime_event_v1_projection_total{event_type}
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

### 13.2 v1 compatibility scenarios

1. 每一种现有 v1 event type 都有 v2 -> v1 contract fixture。
2. v1 JSON 信封和既有必填 payload key 保持不变。
3. delta 后的 completed full snapshot 在新版 v1 parser 中执行 replace，不重复 append。
4. 不带 item identity 的 v1 event 仍可由 legacy parser path 读取。
5. v1 projector 不产生单独持久化记录。
6. v1 projector 输出重放两次时，官方 parser 结果保持幂等。

### 13.3 AgentEngine server compatibility scenarios

使用固定 JSON/SSE fixture 验证，不要求修改 server 代码：

1. `RunAgent` 非流式 envelope 不变。
2. `RunAgent(Stream=true)` 仍是标准 Responses/Chat Completions SSE。
3. `ListSessionEvents` 既有字段不变，只追加 `Metadata.RuntimeItem`。
4. `SubscribeRunEvents` 保留 heartbeat、`AfterSeqId` 和 `[DONE]`。
5. `ListSessionCheckpoints`、preview、resume 和 cancel 请求/响应不变。
6. server control fallback 事件通过 deterministic legacy ingress 得到单个 assistant item。
7. 云控制台使用 `EventTypes=["run_checkpoint"]` 的 fixture 不受 output item 变化影响。

### 13.4 验证门禁

- targeted adapter/reducer/v1 projection tests；
- 完整 Python test suite；
- ruff 与类型检查；
- `ksadk-python` Studio React 测试和 production build；
- `ksadk-web` unit/contract tests 和 production build；
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

- `v1_compat` projector；
- v1 parser fallback；
- Web server-legacy ingress；
- v1 contract fixtures 和 capability 声明。

不得再次修改 canonical adapter/reducer 才能完成删除；如果删除时必须改核心，说明本期隔离失败。

## 15. 实施分段

本节只定义评审边界，不代替后续 implementation plan。

1. 建立 RuntimeEvent v2、typed content、conformance tests 和纯 reducer。
2. 建立 v1 单向 projector 与 current-server contract fixtures。
3. 接入 ADK adapter，先验收多 LLM call 复现用例。
4. 接入 LangGraph、Codex 与 A2A adapter。
5. 将 canonical store、replay 和 final output selection 切到 reducer。
6. 将 Responses、AG-UI/A2UI、A2A 和 session events 切到 reducer projection。
7. 将 Studio 与 `ksadk-web` 切到 identity-aware projection，并保留单一 legacy ingress。
8. 运行跨协议 E2E 后删除 per-author 和文本启发式逻辑。
9. 更新 RuntimeEvent capability、兼容说明和弃用指标，不修改 AgentEngine 公共 API。

## 16. 风险与回滚

### 16.1 主要风险

- ADK 某些自定义/A2A subagent 丢失原生 event id，adapter 无法构造稳定 item identity。
- A2A 第三方 push 不携带 extension，无法提供 exactly-once live update。
- server control fallback 缺少真实 item identity，只能恢复成 invocation 级 composite item。
- v1 completed snapshot 的 replace 语义需要官方 parser 与 Web 同步升级。
- 并发 run 共用 session `seq` 时，任何绕过 persist-before-publish 的路径都会破坏 cursor replay。

### 16.2 回滚原则

- source adapter 以 framework 维度接入，可单独关闭某个新 adapter 并回退到稳定 Responses
  非流式 snapshot，但不能回退到文本前缀去重。
- v1 projector 可保持开启，不影响 canonical store。
- 失败版本不得同时写 v1/v2 两套 store；回滚应用版本时使用升级前新建的测试会话验证，
  不承诺跨 schema 回放。
- checkpoint/resume 数据与 output item event 分开，事件投影回滚不能删除真实 checkpoint。

## 17. Kimi 评审问题

请给出明确的“同意 / 建议修改 / 阻塞”结论，并重点回答：

1. 内部直接采用 RuntimeEvent v2、v1 仅做单向投影，是否比继续 additive 扩展 v1 更能降低
   后续维护成本？
2. `scope_id/item_id/event_id` 是否缺少必须的一层身份，例如 response id 或 content block id？
3. ADK `(invocation_id, event.id)` 是否足以区分同一次任务里的多次 LLM call 和同名 subagent？
4. v1 completed 保留完整 snapshot 并要求新版 parser replace，是否是正确兼容取舍？是否存在
   更低成本、同时能修正丢包的投影方式？
5. 旧 AgentEngine server 不升级时，`Metadata.RuntimeItem` 与 Web deterministic legacy ingress
   是否足以覆盖 RunAgent、ListSessionEvents 和 SubscribeRunEvents？
6. canonical store 只存 v2、旧 v1 历史不迁移，是否会影响当前实际用户或发布承诺？
7. A2A 无 extension 的 at-least-once 限制是否表达准确，是否需要更严格的 terminal snapshot
   reconciliation 规则？
8. v1 projector 和 Web legacy ingress 的模块边界是否足够窄，删除门槛是否可执行？
9. 以上方案是否遗漏 approval、structured input、A2UI activity、tool lifecycle 或长任务恢复的
   关键交互？
10. 是否有任何部分会迫使 AgentEngine server 在本期同步升级？如有，请指出具体字段和调用链。

## 18. 评审通过标准

评审通过需要同时确认：

- v2 identity/lifecycle 能覆盖四类 source adapter；
- v1 仅是单向、无状态或可重建的输出投影；
- canonical store 和 reducer 始终只有一套；
- server 不升级不会破坏当前管理与恢复接口；
- 兼容层具有可观测使用量、明确删除门槛和独立删除路径；
- golden scenarios 足以证明“合法相同文本不误删、partial/final 不重复、live/replay 一致”。
