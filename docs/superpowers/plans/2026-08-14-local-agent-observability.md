# 本地 Agent 观测持久化、外导与轨迹展示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在本地部署的 Agent 中建立一份可追加、可回放的规范轨迹，支持导出版本化 Session Log、在 Studio 实时展示，并通过脱敏后的 OTLP/HTTP 安全外导到云端。

**Architecture:** 继续使用 `RuntimeEvent -> RuntimeEventStore -> SessionEvent -> SessionService/SQLite` 作为唯一持久化事实源。SQLite 提交成功后用进程内通知唤醒实时订阅者，历史补拉和断线恢复仍以持久化 `seq_id` 为准；Session Log、轨迹 UI 和 OTel Span 都是该事件日志的投影。云端外导只接收策略过滤后创建的安全 Span，队列、批处理和传输复用 OTel SDK。

**Tech Stack:** Python 3.10+、Pydantic、SQLite、Click、FastAPI/SSE、React/TypeScript、OpenTelemetry SDK、OTLP/HTTP protobuf、pytest、Vitest。

---

## 1. 结论与范围

### 1.1 当前 `master` 的准确判断

本计划的现状基线固定为 `master@261392b57b2210c2fd10ecd5f1a01bbd6d6ab59f`；当前 checkout 是否包含后续评测分支改动，不改变以下 master 能力判断。

KsADK 不是“轨迹数据已经完整，只缺页面”，而是已具备部分底座：

| 已有能力 | 代码证据 | 仍缺能力 |
| --- | --- | --- |
| 类型化 RuntimeEvent | `ksadk/events/runtime_event.py` | Turn/Step/Model Call 边界和稳定关联字段 |
| SQLite 逐事件追加 | `ksadk/events/store.py`、`ksadk/sessions/local_service.py` | Studio 尚未统一使用；只有接入该路径的运行才持久化 |
| 游标查询、回放和订阅 | `RuntimeEventStore.list/replay/subscribe_*` | Studio 尾页 + 实时流协议、客户端去重和缺口补拉 |
| Studio Trace Explorer | `ksadk/studio/react-ui/src/pages/ObservabilityPage.tsx` | Session Event 账本、Turn/Step/Call/TTFT 视图和实时跟尾 |
| OTLP/HTTP 批量外导 | `ksadk/tracing/setup.py` | 外发前 allowlist/脱敏、数据策略、未知字段阻断和健康计数 |

当前最需要修复的是 `ksadk/studio/event_store.py`：每次事件追加都会读写完整 Run JSON，并调用 `OtlpTraceStore.sync()` 重建完整 OTLP JSON。长轨迹的主要写放大来自这里，而不是 SQLite 的单行追加。

### 1.2 DeepSeek Harness 参考边界

核对基线：`deepseek-ai/deepseek-harness@47f943859bef60e4160492346772ded9b24f765a`，2026-08-13。

采用：

- 类型化、仅追加的 Session Event 日志作为唯一真源。
- 显式 Turn/Step 边界和稳定序号。
- 连续 text/reasoning delta 的无损 packed-row 编码，以及 Step 结束时的完整消息投影。
- 持久化与 UI/Telemetry 投影分离。
- 历史尾页 + 实时流、序号去重、缺口补拉、暂停跟尾；实时通知只负责唤醒，持久化 cursor 负责正确性。
- 中断尾部保留并追加明确的 `interrupted` 终态。

首版不采用：

- 插件 seam 系统。
- JSONL/Zstd 与 SQLite 双持久化后端。
- Zstd 和持久化 UI projection cache。
- 未经长轨迹压测证明必要的虚拟列表。
- Harness 的“内存事件先可见、后台再 flush”顺序；KsADK 保持 SQLite 提交成功后才通知页面。
- 逐 Session 批写接口。KsADK 先移除 Studio 的 O(n) 全文件重写并保留现有 SQLite 单行事务；只有 10k 事件基准显示持久化开销超过运行耗时 5%，或单次追加 p95 超过 10ms，才新增 `append_events()`。

Harness 的后台 Job 状态是进程内状态，不作为 KsADK 持久化设计依据。其开发者预览实现也不替代 KsADK 自己的迁移、删除、保留和单写者约束。

Harness 的 Trajectory UI 从共享 Session Event window 组装业务记录，不单独持久化轨迹表；其 SessionPersistence 可选择 JSONL/Zstd 或 SQLite。Harness 的 OTel 后端当前主要投影 OTLP LogRecord，而本计划继续将生命周期 RuntimeEvent 投影为 OTel Span，以满足 Trace Explorer 的父子拓扑和耗时查询；不直接复制 Harness 的遥测 wire format。

### 1.3 首版边界

首版支持：本地 ADK/LangGraph/Codex Runtime 产生的 `RuntimeEvent`，版本化 JSONL，Studio 本地查询/SSE，四级外导策略，以及 OTLP/HTTP Collector。

首版不支持：远端 A2A 内部轨迹补造、多进程同时写同一 SQLite、云端 Trace 存储服务、Zstd、离线导入回放、生产流量采样平台。远端 A2A 未返回轨迹时继续标记 `UNAVAILABLE`。

## 2. 目标数据流与契约

```text
Runtime adapters
      -> canonical RuntimeEvent
      -> RuntimeEventStore
      -> SessionService / local SQLite
          |-> fixed-watermark Session Log JSONL exporter
          |-> post-commit notification -> Trajectory projection -> tail page + SSE -> Studio
          `-> outbound policy + redaction
                -> safe OTel Span projection
                -> existing BatchSpanProcessor
                -> OTLP/HTTP Collector -> cloud backends
```

### 2.1 需求 ID

| ID | 要求 | 完成标准 |
| --- | --- | --- |
| OBS-DATA-01 | 规范轨迹事件 | Run/Turn/Step/Model/Tool 生命周期可关联；不支持的框架字段显式缺失，不推测 |
| OBS-DATA-02 | 本地追加持久化 | Studio 和非 Studio Runtime 都写 `RuntimeEventStore`；正常追加不全量扫描历史；同一次运行没有第二份事件真源 |
| OBS-LOG-01 | Session Log | 固定导出水位后分页写 `session` header；连续 text/reasoning delta 无损打包，其余 RuntimeEvent 原样写入；按 `seq_id` 排序，原子发布且默认不覆盖 |
| OBS-LIVE-01 | 实时查询 | SQLite 提交后唤醒 SSE；历史尾页和 SSE 使用同一序号；重连后无重复、无缺口 |
| OBS-UI-01 | 轨迹展示 | 展示 User/Context/Assistant/Tool、Turns、Steps、Calls、TTFT、耗时和终态 |
| OBS-SEC-01 | 数据策略 | `local_only`、`metadata_only`、`redacted_trace`、`full_trace`；SDK 管理的 secret 不持久化，任何 secret 不外发 |
| OBS-OTLP-01 | 云端外导 | 复用 OTLP/HTTP protobuf 和 `BatchSpanProcessor`；默认无网络，失败不影响 Agent |
| OBS-OPS-01 | 可运维性 | 提供成功、失败、阻断和最近时间；不暴露 endpoint credential/Header 值 |

### 2.2 `RuntimeEvent v1` additive 扩展

新增事件类型：

```python
TURN_STARTED = "turn.started"
TURN_COMPLETED = "turn.completed"
STEP_STARTED = "step.started"
STEP_COMPLETED = "step.completed"
MODEL_CALL_BEGIN = "model.call.begin"
MODEL_CALL_FIRST_TOKEN = "model.call.first_token"
MODEL_CALL_END = "model.call.end"
```

新增可选信封字段：

```python
turn_id: str | None = None
step_id: str | None = None
parent_event_id: str | None = None
trace_id: str | None = None
span_id: str | None = None
```

字段保持 optional 以兼容旧 v1 JSON，但新产生的 Run/Turn/Step/Model/Tool 事件必须携带同一个 invocation 级 `trace_id`（32 位小写 hex）。`conversation_execution` 或 Studio 在创建 `StartRequest` 时确定一次：`local_only` 使用本地 correlation ID，启用外导时使用真实 safe root Span 的 trace ID；RuntimeAdapter 只透传，Studio `RunRecord.trace_id` 使用同一值。`span_id` 仅在事件已关联真实 OTel Span 时填写，安全投影缺失时由 OTel SDK 创建，不伪造并写回事实源。

事件 payload 最低契约：

| 事件 | 必填 payload | 说明 |
| --- | --- | --- |
| `turn.started` | `turn_index` | 一次 invocation 对应一个 Turn；恢复后的新 invocation 使用新 Turn |
| `turn.completed` | `turn_index`, `status`, `duration_ms` | `completed/failed/cancelled/interrupted` |
| `step.started` | `step_index` | 一次模型请求及其产生的工具调用属于同一步 |
| `step.completed` | `step_index`, `status`, `duration_ms` | 不完整边界不伪造为 completed |
| `model.call.begin` | `model_call_id`, `model` | 模型请求开始 |
| `model.call.first_token` | `model_call_id`, `ttft_ms` | 每个模型调用最多一条 |
| `model.call.end` | `model_call_id`, `status`, `duration_ms` | Usage 继续复用 `usage.reported` |

Adapter 只有拿到真实边界时才发 Model/Step 事件。没有原生回调的 Runtime 首版只发 Turn 和现有 Tool/Text 事件，页面显示“Model Call 边界不可用”，不得根据 OTel Span 反向补造规范事件。

### 2.3 Session Log v1

参考 Harness 的“首行不可变 SessionHeader，后续为原始事件或 packed chunk record”，但保留 KsADK `RuntimeEvent` 原始字段，**不宣称可由 Harness reader 直接读取**。每行一个 JSON 对象，使用 UTF-8 和 `\n`：

```json
{"agent_id":"agent_demo","created_at":"2026-08-14T00:00:00Z","exported_through_seq_id":1,"id":"ses_demo","schema":"ksadk.session-log/v1","type":"session","version":1}
{"agent_id":"agent_demo","event_id":"evt_demo","event_type":"run.started","invocation_id":"run_demo","payload":{"status":"in_progress"},"schema_version":1,"seq_id":1,"session_id":"ses_demo","timestamp":0,"user_id":"local-user"}
{"type":"reasoning-chunks","seq0":2,"data":{"base":{"agent_id":"agent_demo","event_type":"reasoning.delta","invocation_id":"run_demo","phase":"commentary","session_id":"ses_demo","step_id":"step_1","turn_id":"turn_1","user_id":"local-user"},"event_ids":["evt_2","evt_3","evt_4"],"timestamps":[1.0,1.1,1.2],"texts":["先","分析","问题"]}}
```

导出开始时固定 `exported_through_seq_id`，随后只分页读取不超过该水位的事件，因此导出期间新产生的事件不会混入快照。三个及以上、关联字段一致的连续 `text.delta` 或 `reasoning.delta` 打包为一条物理记录；解码后必须逐字段还原原始 RuntimeEvent。完整 Session 导出要求逻辑事件 `seq_id` 连续；按 invocation 过滤时保留原始 `seq_id`，只要求严格递增，不重新编号。Session Log 是 SQLite 事实源的一次原子快照，不是第二个可写事实源；v1 verifier 同时读取 packed rows 和原始 RuntimeEvent 行。首版不实现导入、继续 append 或 Zstd。

轨迹展示使用同一逻辑事件流做有状态投影：Turn/Step 生命周期只建立分组，model begin/first-token/end、reasoning/text、usage 合并为一个 Assistant Message，tool begin/end 合并为 Tool。实时 delta 更新当前 Message，Step 完成后以完整 text/reasoning 结果结算；不得把 delta 或生命周期边界渲染成同级业务行。

### 2.4 外导策略

| 策略 | 网络 | 内容 |
| --- | --- | --- |
| `local_only` | 禁止创建外部 Exporter | 本地事件与 Session Log |
| `metadata_only` | 允许 | ID、类型、状态、时间、耗时、计数和受控资源标签 |
| `redacted_trace` | 允许 | metadata 加 allowlist 后的截断内容摘要 |
| `full_trace` | 显式授权后允许 | 允许的业务内容；credential、Authorization/Cookie、环境变量和 secret 仍禁止 |

未知事件类型、未知外发字段或递归脱敏失败统一返回 `EXPORT_BLOCKED`。任何策略都不能降级为明文兜底。

本地 SQLite 和 Session Log 用于完整诊断，可以包含用户输入、模型输出和工具参数/结果；目录权限设为 `0700`，导出文件设为 `0600`。KsADK 自己管理的 credential、Authorization/Cookie header 和环境变量不得写入 RuntimeEvent。四级策略只控制网络外导内容，不用云端脱敏后的残缺数据替换本地事实源。

### 2.5 需求与验收映射

| 用户目标 | 需求 ID | 实现任务 | 最小验收命令 |
| --- | --- | --- | --- |
| 观测数据准备与本地追加持久化 | OBS-DATA-01、OBS-DATA-02 | Task 1-3.5 | `uv run pytest tests/events/test_runtime_event.py tests/events/test_runtime_event_store.py tests/runtime/test_trajectory_events.py tests/studio/test_run_service.py tests/studio/test_event_store.py -q` |
| Session Log 文件导出 | OBS-LOG-01 | Task 4-5 | `uv run pytest tests/observability/test_session_log.py tests/cli/test_cmd_observe.py -q` |
| 历史尾页、实时流与 Studio 展示 | OBS-LIVE-01、OBS-UI-01 | Task 6-7 | `uv run pytest tests/studio/test_observability_api.py tests/observability/test_trajectory.py -q`；`cd ksadk/studio/react-ui && npm run test:ui && npm run build` |
| 脱敏后通过 OTel 外导 | OBS-SEC-01、OBS-OTLP-01、OBS-OPS-01 | Task 8-9 | `uv run pytest tests/observability/test_policy.py tests/observability/test_otel_projection.py tests/test_tracing_setup_otlp.py -q` |
| 删除、恢复、性能与联合验收 | 全部 | Task 10 | `uv run pytest tests/observability/test_local_observability_e2e.py -q`；预发 Collector E2E 和定向 secret scan |

## 3. 文件结构

| 文件 | 动作 | 责任 |
| --- | --- | --- |
| `ksadk/events/runtime_event.py` | 修改 | 新事件类型、关联字段和 conformance |
| `ksadk/events/store.py` | 修改 | 新字段映射、幂等追加和提交后订阅唤醒 |
| `ksadk/sessions/base.py` | 修改 | 明确 SessionEvent ID 与 cursor 唯一性契约 |
| `ksadk/sessions/local_service.py` | 修改 | SQLite 事件约束和单事件事务 |
| `ksadk/sessions/in_memory.py` | 修改 | 与持久化后端一致的事件 ID 幂等约束 |
| `ksadk/runtime/runner_adapter.py` | 修改 | Runtime 能确认的 Turn/Step/Model Call 边界 |
| `ksadk/studio/run_service.py` | 修改 | Studio RuntimeEvent 先写规范 Store，再做页面投影 |
| `ksadk/studio/event_store.py` | 修改 | 只保存 RunRecord；读取旧 `events` 兼容，但不再追加或同步完整 OTLP |
| `ksadk/studio/service.py` | 修改 | 组装 LocalSessionService/RuntimeEventStore |
| `ksadk/observability/session_log.py` | 新增 | JSONL 导出和校验 |
| `ksadk/observability/trajectory.py` | 新增 | RuntimeEvent 到页面记录的纯函数投影 |
| `ksadk/observability/policy.py` | 新增 | 数据策略、递归 allowlist、阻断错误 |
| `ksadk/observability/otel_projection.py` | 新增 | 只从已脱敏字段创建安全 Span |
| `ksadk/cli/cmd_observe.py` | 新增 | `observe export` 薄 CLI |
| `ksadk/cli/__init__.py` | 修改 | 注册 observe 命令和帮助 |
| `ksadk/studio/api.py` | 修改 | 历史尾页、实时 SSE、导出和健康接口 |
| `ksadk/tracing/setup.py` | 修改 | 策略门禁、安全 scope 过滤、OTLP 健康计数 |
| `ksadk/studio/react-ui/src/pages/trajectory.ts` | 新增 | 客户端归并、去重、缺口检测 |
| `ksadk/studio/react-ui/src/pages/TrajectoryView.tsx` | 新增 | 轨迹账本、Turns/Calls/Duration |
| `ksadk/studio/react-ui/src/pages/ObservabilityPage.tsx` | 修改 | 在 Trace 详情中挂载轨迹 Tab |

## 4. 分阶段执行

### Task 1: 冻结 RuntimeEvent 轨迹契约

**Files:**
- Modify: `ksadk/events/runtime_event.py`
- Modify: `ksadk/events/store.py`
- Modify: `tests/events/test_runtime_event.py`
- Test: `tests/events/test_runtime_event_store.py`

- [ ] **Step 1: 写关联字段和新事件的失败测试**

```python
def test_model_call_event_round_trips_through_session_event():
    event = RuntimeEvent.create(
        EventType.MODEL_CALL_BEGIN,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="run-1",
        seq_id=0,
        turn_id="turn-1",
        step_id="step-1",
        trace_id="0" * 32,
        span_id="1" * 16,
        payload={"model_call_id": "model-call-1", "model": "demo-model"},
    )
    restored = session_event_to_runtime_event(runtime_event_to_session_event(event))
    assert restored == event
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/events/test_runtime_event.py tests/events/test_runtime_event_store.py -q`

Expected: FAIL，提示 `EventType.MODEL_CALL_BEGIN` 或 `turn_id` 尚不存在。

- [ ] **Step 3: 最小实现 additive 字段和 payload 校验**

在 `EventType`、`ALL_EVENT_TYPES`、`EVENT_PAYLOAD_REQUIRED_KEYS`、`RuntimeEvent` 和 `create()` 中加入第 2.2 节的精确字段。`runtime_event_to_session_event()` 把关联字段写入 `metadata["correlation"]`，反向映射完整恢复；字段缺失保持 `None`。

```python
correlation = {
    key: value
    for key, value in {
        "turn_id": event.turn_id,
        "step_id": event.step_id,
        "parent_event_id": event.parent_event_id,
        "trace_id": event.trace_id,
        "span_id": event.span_id,
    }.items()
    if value is not None
}
```

- [ ] **Step 4: 补兼容测试**

旧 v1 JSON 不含关联字段时仍可解析；未知 event type 继续由 conformance 拒绝；新字段序列化使用 snake_case，不改变既有字段。

- [ ] **Step 5: 运行相关测试**

Run: `uv run pytest tests/events/test_runtime_event.py tests/events/test_runtime_event_store.py tests/events/test_replay_parser.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add ksadk/events/runtime_event.py ksadk/events/store.py tests/events/test_runtime_event.py tests/events/test_runtime_event_store.py
git commit -m "feat(observability): add trajectory event contract"
```

### Task 2: 在 RuntimeAdapter 发出真实 Turn/Step/Model Call 边界

**Files:**
- Modify: `ksadk/runtime/runner_adapter.py`
- Modify: `ksadk/runtime/conversation_execution.py`
- Modify: `ksadk/runners/langgraph_runner.py`
- Modify: `ksadk/codex/runtime.py`
- Create: `tests/runtime/test_trajectory_events.py`
- Modify: `tests/runtime/test_conversation_execution.py`
- Create: `tests/runners/test_langgraph_observability.py`
- Modify: `tests/runners/test_codex_runtime_adapter.py`

- [ ] **Step 1: 写 Turn 生命周期失败测试**

断言成功、失败、取消和中断流都满足：`run.started -> turn.started -> ... -> turn.completed -> run terminal`，并且同一 Turn 的事件共享 `turn_id`。

```python
assert [event.event_type for event in events[:2]] == [
    EventType.RUN_STARTED,
    EventType.TURN_STARTED,
]
assert events[-2].event_type == EventType.TURN_COMPLETED
assert events[-2].payload["status"] == "completed"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/runtime/test_trajectory_events.py tests/runtime/test_conversation_execution.py tests/runners/test_langgraph_observability.py tests/runners/test_codex_runtime_adapter.py -q`

Expected: FAIL，当前流没有 Turn 事件。

- [ ] **Step 3: 在共享 Adapter 生命周期发 Turn 事件**

`conversation_execution` 创建 `trace_id = uuid4().hex` 并写入 `StartRequest.metadata`；Studio 传入自己的 `RunRecord.trace_id`。Runner/Codex Adapter 在 `start()` 时复制到 `handle.native_ref`，所有 `_event()` 自动透传。使用 `turn_id = f"turn_{handle.run_id}"`；以 `time.monotonic()` 计算 duration。所有终态分支通过一个 `_turn_completed_event(status)` helper 收口，确保每个已开始 Turn 只结束一次。测试断言同一 invocation 的 RuntimeEvent 与 Studio RunRecord 使用同一 trace ID。

- [ ] **Step 4: 为有原生模型边界的 Adapter 发 Step/Model Call 事件**

LangGraph 的 `astream_events()` 已暴露 `on_chat_model_start/stream/end`。在 `LangGraphRunner` 中把这三类原生事件转成 `model_call_begin/model_call_first_token/model_call_end` chunk，并在 begin/end 同时发 `step_start/step_end`；`model_call_id` 使用原生 `run_id`，`step_id = f"step_{model_call_id}"`，`step_index` 按 start 顺序递增。共享 Adapter 只做 chunk 到第 2.2 节 RuntimeEvent 的确定性映射。

```python
model_call_id = str(event.get("run_id") or "")
if event_kind == "on_chat_model_start" and model_call_id:
    model_started_at[model_call_id] = time.monotonic()
    step_index = len(model_started_at)
    yield {"type": "step_start", "step_id": f"step_{model_call_id}", "step_index": step_index}
    yield {
        "type": "model_call_begin",
        "model_call_id": model_call_id,
        "model": str(event.get("name") or "chat-model"),
    }
elif event_kind == "on_chat_model_stream" and model_call_id in model_started_at:
    data = event.get("data") or {}
    chunk = data.get("chunk") if isinstance(data, Mapping) else None
    has_content = bool(
        getattr(chunk, "content", None) or getattr(chunk, "reasoning_content", None)
    )
    if has_content and model_call_id not in first_token_seen:
        first_token_seen.add(model_call_id)
        yield {
            "type": "model_call_first_token",
            "model_call_id": model_call_id,
            "ttft_ms": int((time.monotonic() - model_started_at[model_call_id]) * 1000),
        }
elif event_kind == "on_chat_model_end" and model_call_id in model_started_at:
    duration_ms = int((time.monotonic() - model_started_at.pop(model_call_id)) * 1000)
    yield {
        "type": "model_call_end",
        "model_call_id": model_call_id,
        "status": "completed",
        "duration_ms": duration_ms,
    }
    first_token_seen.add(model_call_id)
    yield {
        "type": "step_end",
        "step_id": f"step_{model_call_id}",
        "status": "completed",
        "duration_ms": duration_ms,
    }
```

- [ ] **Step 5: 覆盖不支持边界的 Runtime**

ADK、Codex 和 Fake Adapter 首版只发 Turn 和现有事件；断言它们不产生虚假的 Step/Model Call，能力摘要使用 `model_call_boundaries=False`。后续只有接入各自真实模型回调后才能改为 `True`。

- [ ] **Step 6: 运行测试并提交**

Run: `uv run pytest tests/runtime/test_trajectory_events.py tests/runtime/test_conversation_execution.py tests/runners/test_langgraph_observability.py tests/runners/test_codex_runtime_adapter.py -q`

Expected: PASS。

```bash
git add ksadk/runtime/runner_adapter.py ksadk/runtime/conversation_execution.py ksadk/runners/langgraph_runner.py ksadk/codex/runtime.py tests/runtime/test_trajectory_events.py tests/runtime/test_conversation_execution.py tests/runners/test_langgraph_observability.py tests/runners/test_codex_runtime_adapter.py
git commit -m "feat(observability): emit turn and model boundaries"
```

### Task 3: 让 Studio 使用规范 RuntimeEvent 事实源

**Files:**
- Modify: `ksadk/studio/service.py`
- Modify: `ksadk/studio/run_service.py`
- Modify: `ksadk/studio/event_store.py`
- Modify: `ksadk/studio/otel_trace.py`
- Test: `tests/studio/test_run_service.py`
- Test: `tests/studio/test_event_store.py`

- [ ] **Step 1: 写“每个事件一行且不重写 OTLP”的失败测试**

使用 `LocalSessionService(tmp_path / "sessions.sqlite")` 和 Fake Runtime 运行 100 个 delta。断言 `RuntimeEventStore.list(session_id)` 返回连续事件，`.agentkit/runs/<run_id>.json` 只包含 `record`，且运行中没有 `.agentkit/traces/<trace_id>.otlp.json` 被每个 delta 重写。

```python
events = await runtime_events.list(record.session_id, invocation_id=record.id)
assert [event.seq_id for event in events] == list(range(events[0].seq_id, events[-1].seq_id + 1))
run_payload = json.loads((tmp_path / ".agentkit/runs" / f"{record.id}.json").read_text())
assert set(run_payload) == {"record"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/studio/test_run_service.py tests/studio/test_event_store.py -q`

Expected: FAIL；当前 Run JSON 含 `events`，并在每次 `_write()` 调用 `trace_store.sync()`。

- [ ] **Step 3: 在 StudioService 组装现有 SessionService**

使用 `LocalSessionService(project_dir=str(self.workspace.root))` 和 `RuntimeEventStore`，注入 `StudioRunService`。不要新增数据库或 Studio 专用事件表。

```python
self.session_service = LocalSessionService(project_dir=str(self.workspace.root))
self.runtime_events = RuntimeEventStore(self.session_service)
self.run_service = StudioRunService(
    self.workspace,
    self.runtime_executor,
    event_store=self.event_store,
    runtime_events=self.runtime_events,
)
```

- [ ] **Step 4: RuntimeEvent 先持久化再投影**

创建 `StartRequest` 时把 `record.trace_id` 写入 metadata；Adapter 产生的事件必须回带该值。将 `persist()` 改成 async：先校验 `runtime_event.trace_id == record.trace_id`，再 `await runtime_events.append_one(runtime_event)`，最后把持久化后得到的 `seq_id` 投影为临时 `RunEvent` 供现有 `on_event` 回调使用。RunRecord 的 `id` 必须继续作为 `invocation_id`；取消/失败补写也走同一方法。

```python
async def persist(runtime_event: RuntimeEvent) -> RunEvent:
    stored = await self.runtime_events.append_one(runtime_event)
    event_type, data = project_runtime_event(stored)
    projected = RunEvent(id=stored.seq_id, run_id=record.id, type=event_type, data=data)
    if on_event is not None:
        on_event(projected)
    return projected
```

- [ ] **Step 5: RunEventStore 退化为 RunRecord store**

`create/save/get/list_runs/delete_*` 保留；新写入只保存 `{"record": ...}`。`_read()` 兼容读取旧文件的 `events`，但 `save()` 不再把旧 events 写回，也不再调用 `trace_store.sync()`。`append/events/recover_interrupted` 的新调用全部迁移后删除；旧 Run 文件保持可读，不做破坏性批量迁移。

- [ ] **Step 6: OTLP 本地投影改为按需或终态生成**

`OtlpTraceStore` 增加从规范 RuntimeEvent 构建 Trace 的入口；Trace API 首次读取时投影，或 Run 进入终态时只写一次。不得在 delta append 上调用 `sync()`。

- [ ] **Step 7: 中断恢复改读规范事件**

Studio 启动时，对非终态 RunRecord 按 `session_id + invocation_id` 查最后一个 RuntimeEvent。存在终态则同步 RunRecord；不存在则追加 `run.interrupted`，再更新 RunRecord。已持久化历史不截断。

- [ ] **Step 8: 运行测试并提交**

Run: `uv run pytest tests/studio/test_run_service.py tests/studio/test_event_store.py tests/events/test_runtime_event_store.py -q`

Expected: PASS。

```bash
git add ksadk/studio/service.py ksadk/studio/run_service.py ksadk/studio/event_store.py ksadk/studio/otel_trace.py tests/studio/test_run_service.py tests/studio/test_event_store.py
git commit -m "refactor(studio): persist runs through runtime event store"
```

### Task 3.5: 优化幂等追加并增加提交后通知

**Files:**
- Modify: `ksadk/events/store.py`
- Modify: `ksadk/sessions/base.py`
- Modify: `ksadk/sessions/local_service.py`
- Modify: `ksadk/sessions/in_memory.py`
- Modify: `tests/events/test_runtime_event_store.py`
- Modify: `tests/test_sessions_service.py`

- [ ] **Step 1: 写正常追加不扫描历史的失败测试**

使用记录 `get_events()` 调用次数的 SessionService，连续追加两个不同 `event_id`。断言正常路径不会为了查重读取完整事件列表；重复相同 `event_id` 返回原事件，重复 ID 但内容不同继续报 collision。

```python
await store.append_one(make_event("evt-1"))
await store.append_one(make_event("evt-2"))
assert service.get_events_calls == 0

duplicate = await store.append_one(make_event("evt-1"))
assert duplicate.event_id == "evt-1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/events/test_runtime_event_store.py -q`

Expected: FAIL；当前 `_event_by_id()` 在每次正常追加前调用 `get_events()`，事件越多扫描越长。

- [ ] **Step 3: 改为 INSERT 优先的幂等路径并约束 cursor**

删除 `reserve_once()` 正常路径的预读。先调用 `SessionService.append_event()`；只有唯一键冲突或并发写入异常时才按 `event_id` 读取原事件并执行 `_assert_same_event()`。在 `BaseSessionService.append_event()` 明确“同一 backend namespace 的事件 ID 唯一、同一 Session 的 cursor 唯一”契约。这一优化依赖所有可写 SessionService 对事件 ID 提供唯一性：SQLite 已有 `id PRIMARY KEY`，PostgreSQL 已有 `(namespace, id) PRIMARY KEY`，本任务为 `InMemorySessionService` 补同样的重复 ID 拒绝。`LocalSessionService` 另为 `(session_id, seq_id)` 增加唯一索引，继续在同一事务中分配序号和插入事件；创建索引前若发现旧库已有重复 cursor，必须明确失败，不得静默删除或重排历史。首版仍维持单进程写者，不增加 busy retry。

```python
try:
    stored = await self._service.append_event(
        event.session_id,
        runtime_event_to_session_event(event),
    )
except Exception:
    existing = await self._event_by_id(event.session_id, event.event_id)
    if existing is None:
        raise
    self._assert_same_event(existing, event)
    return existing, False
```

- [ ] **Step 4: 写提交后唤醒和竞态测试**

订阅者从 cursor 读取完历史后等待。事件提交成功必须立即唤醒订阅者；在“最后一次查询结束”和“开始等待”之间追加事件也不能丢通知。持久化失败不得更新 revision 或唤醒订阅者。

```python
subscriber = asyncio.create_task(anext(store.subscribe_session("session-1", timeout=1)))
await asyncio.sleep(0)
stored = await store.append_one(make_event("evt-live"))
assert await asyncio.wait_for(subscriber, timeout=1) == stored
```

- [ ] **Step 5: 用 revision + `asyncio.Condition` 替换固定轮询**

`RuntimeEventStore` 按 Session 保存进程内 revision 和 `asyncio.Condition`。`reserve_once()` 仅在新事件持久化成功后递增 revision 并 `notify_all()`，保证直接使用 reservation 路径的写入也能唤醒订阅。订阅循环先记录 revision，再按 cursor 查询 SQLite；无事件时只有 revision 未变化才等待，避免查询与 wait 之间的丢唤醒。通知只是优化延迟，进程重启、重连和竞态后的正确性仍由 `list(after_seq_id=...)` 保证。

```python
revision = self._session_revisions[session_id]
events = await self.list(session_id, after_seq_id=last)
if events:
    for event in events:
        last = event.seq_id
        yield event
    continue

condition = self._session_conditions[session_id]
async with condition:
    if self._session_revisions[session_id] == revision:
        try:
            await asyncio.wait_for(condition.wait(), timeout=remaining)
        except TimeoutError:
            return
```

不引入 Redis、消息队列、SQLite notification 或通用事件总线。多进程写同一 SQLite 继续不在首版范围内。

- [ ] **Step 6: 运行测试并提交**

Run: `uv run pytest tests/events/test_runtime_event_store.py tests/test_sessions_service.py -q`

Expected: PASS。

```bash
git add ksadk/events/store.py ksadk/sessions/base.py ksadk/sessions/local_service.py ksadk/sessions/in_memory.py tests/events/test_runtime_event_store.py tests/test_sessions_service.py
git commit -m "perf(events): wake subscribers after durable append"
```

### Task 4: 实现版本化 Session Log 导出与校验

**Files:**
- Create: `ksadk/observability/__init__.py`
- Create: `ksadk/observability/session_log.py`
- Create: `tests/observability/test_session_log.py`

- [ ] **Step 1: 写固定水位、成功、缺口和原子发布失败测试**

```python
def make_event(event_id: str, event_type: str, status: str) -> RuntimeEvent:
    return RuntimeEvent.create(
        event_type,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="run-1",
        seq_id=0,
        event_id=event_id,
        payload={"status": status},
    )


@pytest.mark.asyncio
async def test_export_session_log_is_ordered_and_verifiable(tmp_path):
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", "session-1")
    store = RuntimeEventStore(service)
    await store.append_one(make_event("evt-1", EventType.RUN_STARTED, "in_progress"))
    await store.append_one(make_event("evt-2", EventType.RUN_COMPLETED, "completed"))

    target = tmp_path / "session.jsonl"
    result = await export_session_log(service, "session-1", target)

    assert result.event_count == 2
    assert verify_session_log(target).event_count == 2
    lines = [json.loads(line) for line in target.read_text().splitlines()]
    assert lines[0]["type"] == "session"
    assert lines[0]["schema"] == "ksadk.session-log/v1"
    assert lines[0]["exported_through_seq_id"] == 2
    assert [line["event_id"] for line in lines[1:]] == ["evt-1", "evt-2"]
```

再覆盖：导出固定水位后并发追加的事件不进入文件；单页上限固定且 10k 事件不会一次载入内存；目标存在默认拒绝；过滤 invocation 保留原 seq；不支持 schema、Header session id 与事件不一致、完整 Session 序号不连续时校验失败；异常时目标文件不存在。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/observability/test_session_log.py -q`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现固定模型和错误类型**

```python
SESSION_LOG_SCHEMA = "ksadk.session-log/v1"

@dataclass(frozen=True)
class SessionLogResult:
    path: Path
    event_count: int
    first_seq_id: int | None
    last_seq_id: int | None
    exported_through_seq_id: int | None

class SessionLogError(ValueError):
    pass
```

- [ ] **Step 4: 实现固定水位的分页流式导出**

先用 `RuntimeEventStore.list(session_id, limit=1)` 读取当前尾事件并固定 `exported_through_seq_id`；空 Session 使用 `None`。随后按最多 500 个 `seq_id` 的窗口向前读取，且始终设置 `before_seq_id <= exported_through_seq_id + 1`。按 invocation 导出时在每个窗口内过滤，不得先对全局尾页应用 invocation 过滤，否则会漏掉更早的匹配事件。导出期间追加的更大序号不进入本次文件。

```python
cursor = 0
while cutoff is not None and cursor < cutoff:
    window_end = min(cursor + 500, cutoff)
    events = await store.list(
        session_id,
        after_seq_id=cursor,
        before_seq_id=window_end + 1,
    )
    for event in events:
        if invocation_id is None or event.invocation_id == invocation_id:
            write_json_line(file, event.to_dict())
    cursor = window_end
```

Header 写入相同的 `exported_through_seq_id`。完整 Session 校验要求第一条至该水位连续；invocation 过滤文件只要求实际导出事件严格递增，不重新编号。

- [ ] **Step 5: 实现确定性 JSONL 和原子不覆盖发布**

使用标准库 `json`、`tempfile`、`os.link` 和 `os.unlink`。序列化固定为 `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"`。Header 取 Session metadata，事件行直接使用 `RuntimeEvent.to_dict()`。临时文件与目标同目录并设为 `0600`；fsync 文件后用 hard link 原子发布，目标存在时 `FileExistsError -> SessionLogError`；随后 fsync 父目录，finally 删除临时文件。若文件系统不支持 hard link，明确返回 `SESSION_LOG_ATOMIC_PUBLISH_UNSUPPORTED`，不得退化为覆盖写。

- [ ] **Step 6: 实现流式校验**

逐行读取，不把完整文件载入内存。Header 必须是第一行且 `type=session/schema=ksadk.session-log/v1/version=1`，`exported_through_seq_id` 必须为空或非负整数；其余每行通过 `RuntimeEvent.from_dict()` 校验，Session id 必须与 Header 一致。event `seq_id` 严格递增且不得超过导出水位，未过滤的完整 Session 必须连续并结束在该水位；返回实际 count/range/cutoff。

- [ ] **Step 7: 运行测试并提交**

Run: `uv run pytest tests/observability/test_session_log.py tests/events/test_runtime_event_store.py -q`

Expected: PASS。

```bash
git add ksadk/observability/__init__.py ksadk/observability/session_log.py tests/observability/test_session_log.py
git commit -m "feat(observability): export versioned session logs"
```

### Task 5: 增加 `agentengine observe export` CLI

**Files:**
- Create: `ksadk/cli/cmd_observe.py`
- Modify: `ksadk/cli/__init__.py`
- Create: `tests/cli/test_cmd_observe.py`
- Modify: `tests/cli/test_cli_alignment.py`

- [ ] **Step 1: 写 CLI 失败测试**

```python
result = runner.invoke(
    cli,
    [
        "observe",
        "export",
        "--session-id",
        "session-1",
        "--output",
        str(target),
        "--format",
        "json",
    ],
)
assert result.exit_code == 0
assert json.loads(result.output)["eventCount"] == 2
```

覆盖缺失 Session、目标已存在、不可写目录、`--invocation-id` 过滤和 `--format pretty|json`。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_cmd_observe.py tests/cli/test_cli_alignment.py -q`

Expected: FAIL，`observe` 命令未注册。

- [ ] **Step 3: 实现薄 CLI**

```python
@click.group("observe")
def observe() -> None:
    """查询和导出本地 Agent 观测数据。"""

@observe.command("export")
@click.option("--session-id", required=True)
@click.option("--invocation-id")
@click.option("--output", "output_path", required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=click.Choice(["pretty", "json"]), default="pretty")
def export_command(session_id: str, invocation_id: str | None, output_path: Path, output_format: str) -> None:
    configure_ui_runtime(output_mode=output_format)
    result = asyncio.run(
        export_session_log(
            LocalSessionService(project_dir=str(Path.cwd())),
            session_id,
            output_path,
            invocation_id=invocation_id,
        )
    )
    payload = {
        "path": str(result.path),
        "eventCount": result.event_count,
        "firstSeqId": result.first_seq_id,
        "lastSeqId": result.last_seq_id,
        "exportedThroughSeqId": result.exported_through_seq_id,
    }
    if is_json_output():
        emit_json(payload)
    else:
        print_success(f"已导出 {result.event_count} 条事件")
        print_kv("文件", str(result.path))
```

错误通过现有 CLI error 规范转成固定错误码；不提供 `--force`，避免首版覆盖文件。

- [ ] **Step 4: 注册根帮助**

将 `observe` 加入 `ROOT_HELP_COMMANDS`、`SHORT_HELP_MAP`、彩色帮助行和 `_register_optional_command(cli, "ksadk.cli.cmd_observe", "observe")`。

- [ ] **Step 5: 运行测试并提交**

Run: `uv run pytest tests/cli/test_cmd_observe.py tests/cli/test_cli_alignment.py -q`

Expected: PASS。

```bash
git add ksadk/cli/cmd_observe.py ksadk/cli/__init__.py tests/cli/test_cmd_observe.py tests/cli/test_cli_alignment.py
git commit -m "feat(cli): export local session logs"
```

### Task 6: 提供历史尾页和实时 SSE

**Files:**
- Create: `ksadk/observability/trajectory.py`
- Modify: `ksadk/studio/api.py`
- Modify: `ksadk/studio/service.py`
- Create: `tests/studio/test_observability_api.py`
- Create: `tests/observability/test_trajectory.py`

- [ ] **Step 1: 写历史尾页契约测试**

```python
response = client.get("/api/v1/sessions/session-1/events?limit=3")
assert response.status_code == 200
assert [item["seqId"] for item in response.json()["items"]] == [8, 9, 10]
assert response.json()["page"] == {
    "oldestSeqId": 8,
    "latestSeqId": 10,
    "hasMore": True,
}
assert all(item["projectionVersion"] == 1 for item in response.json()["items"])
assert all(item["recordId"] for item in response.json()["items"])
```

`beforeSeqId=8` 返回更旧尾页；Session 不存在返回 404；limit 限制 1..500。

- [ ] **Step 2: 写 SSE 重连测试**

`Last-Event-ID: 7` 优先于 query `afterSeqId`。每帧格式固定为 `id: <seq>\nevent: runtime_event\ndata: <json>\n\n`；发送已有尾部后继续消费 `RuntimeEventStore.subscribe_session()`。断开请求必须取消订阅生成器。再覆盖：SQLite 提交失败时 SSE 不得出现事件；空闲 15 秒产生 `: keepalive\n\n`；keepalive 后追加事件仍从原 cursor 继续且不重复。

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/studio/test_observability_api.py tests/observability/test_trajectory.py -q`

Expected: FAIL，路由和投影不存在。

- [ ] **Step 4: 实现带稳定记录 ID 的纯函数页面投影**

`project_trajectory_event(event)` 返回 `projectionVersion/seqId/eventId/recordId/type/category/turnId/stepId/timestamp/status/durationMs/summary/details`。`projectionVersion` 首版固定为 `1`；`recordId` 按 `tool:{call_id}`、`model:{model_call_id}`、`turn:{turn_id}`、`step:{step_id}` 生成，无法关联时退到 `event:{event_id}`。begin/end 事件共享同一个 `recordId`，前端可以更新展示行而不修改历史事实。`category` 只允许 `user/context/assistant/tool/lifecycle`；不识别事件保留为 `lifecycle`，但不进入模型历史。

- [ ] **Step 5: 实现路由**

```python
@app.get("/api/v1/sessions/{session_id}/events")
async def session_events(session_id: str, before_seq_id: int | None = Query(None, alias="beforeSeqId"), limit: int = Query(100, ge=1, le=500)):
    return await studio.trajectory_page(session_id, before_seq_id=before_seq_id, limit=limit)

@app.get("/api/v1/sessions/{session_id}/events/stream")
async def session_event_stream(session_id: str, request: Request, after_seq_id: int = Query(0, alias="afterSeqId")):
    last = request.headers.get("Last-Event-ID")
    cursor = int(last) if last and last.isdigit() else after_seq_id
    return StreamingResponse(studio.stream_trajectory(session_id, cursor), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
```

`trajectory_page()` 直接复用 `RuntimeEventStore.list(before_seq_id=..., limit=...)`。当前 `LocalSessionService._get_events_sync()` 已实现“按 cursor 过滤 -> 取最新 N 条 -> 按 `seq_id` 升序返回”，因此首版不新增存储查询 API；`hasMore` 只需用同一 `beforeSeqId` 再探测一条更旧事件。

实时流复用 Task 3.5 的 post-commit `asyncio.Condition` 唤醒；每次被唤醒后仍调用 `list(after_seq_id=cursor)` 从 SQLite 读取已提交事实。订阅每 15 秒结束一个空闲等待周期，Studio 在继续订阅前发送 SSE keepalive：

```python
while not await request.is_disconnected():
    async for event in runtime_events.subscribe_session(
        session_id,
        after_seq_id=cursor,
        timeout=15,
    ):
        cursor = event.seq_id
        yield encode_sse(project_trajectory_event(event), event_id=cursor)
    if not await request.is_disconnected():
        yield ": keepalive\n\n"
```

不引入 Redis、消息队列、SQLite notification 或独立实时事件表。通知丢失、进程重启或 EventSource 重连都通过同一个 SQLite cursor 补拉。

- [ ] **Step 6: 增加本地导出 API**

`POST /api/v1/sessions/{session_id}:export` 只接受目标文件名和可选 invocation；目标目录固定在 workspace 的 `.agentkit/exports/`，目录权限为 `0700`，拒绝绝对路径和 `..`。返回相对路径、count 和 range。

- [ ] **Step 7: 运行测试并提交**

Run: `uv run pytest tests/studio/test_observability_api.py tests/observability/test_trajectory.py -q`

Expected: PASS。

```bash
git add ksadk/observability/trajectory.py ksadk/studio/api.py ksadk/studio/service.py tests/studio/test_observability_api.py tests/observability/test_trajectory.py
git commit -m "feat(studio): stream canonical trajectory events"
```

### Task 7: 实现 Harness 风格 Studio 轨迹视图

**Files:**
- Create: `ksadk/studio/react-ui/src/pages/trajectory.ts`
- Create: `ksadk/studio/react-ui/src/pages/trajectory.test.ts`
- Create: `ksadk/studio/react-ui/src/pages/TrajectoryView.tsx`
- Create: `ksadk/studio/react-ui/src/pages/TrajectoryView.test.tsx`
- Modify: `ksadk/studio/react-ui/src/pages/ObservabilityPage.tsx`
- Modify: `ksadk/studio/react-ui/src/studio.css`

- [ ] **Step 1: 写客户端序号归并失败测试**

```typescript
it("deduplicates events and reports a cursor gap", () => {
  const initial = trajectoryState([
    { seqId: 7, recordId: "tool:call-1", status: "running" },
    { seqId: 8, recordId: "event:8" },
  ]);
  expect(mergeTrajectory(initial, { seqId: 8 }).items).toHaveLength(2);
  expect(mergeTrajectory(initial, { seqId: 10 }).gap).toEqual({ afterSeqId: 8, beforeSeqId: 10 });
});

it("folds begin and end events into one stable record", () => {
  const initial = trajectoryState([
    { seqId: 7, recordId: "tool:call-1", status: "running" },
  ]);
  const next = mergeTrajectory(initial, {
    seqId: 8,
    recordId: "tool:call-1",
    status: "completed",
  });
  expect(next.items).toHaveLength(1);
  expect(next.items[0].status).toBe("completed");
});
```

再覆盖补拉 `[9]` 后应用缓存的 `[10]`、prepend 旧页保持稳定 key、content 更新不重排已有记录。

- [ ] **Step 2: 运行 reducer 测试确认失败**

Run: `cd ksadk/studio/react-ui && npm run test:ui -- src/pages/trajectory.test.ts`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现纯 reducer**

状态只包含 `items/bySeq/byRecordId/lastSeqId/gap/hasMore/followTail`。`bySeq` 只负责事实去重，`byRecordId` 负责把同一个 Model/Tool/Turn/Step 的 begin/end 更新为一个稳定展示行。`seqId <= lastSeqId` 且已存在的事件丢弃；大于 `lastSeqId + 1` 的事件暂存并触发补拉；补拉完成后按序应用。更新已有 `recordId` 时保留列表位置和 React key，只替换内容。

```typescript
export type TrajectoryState = {
  items: TrajectoryEvent[];
  bySeq: Map<number, TrajectoryEvent>;
  byRecordId: Map<string, number>;
  lastSeqId: number;
  gap: { afterSeqId: number; beforeSeqId: number } | null;
  hasMore: boolean;
  followTail: boolean;
};
```

- [ ] **Step 4: 写视图交互失败测试**

断言：首屏显示尾页；选中 `Duration/Turns/Calls` 分段控件切换摘要；Tool 行显示状态和耗时；向上滚动把 `followTail` 设为 false；点击“回到最新”恢复；Model 边界缺失显示“不可用”而非 0ms。

- [ ] **Step 5: 实现 TrajectoryView**

账本只渲染 User、Context、Assistant、Tool 和 lifecycle 行，不把页面说明或操作教程放入正文。详情抽屉显示 Input/Output/Timing/Usage；Reasoning 默认折叠。使用现有 Lucide 图标、Tabs/Button/ScrollArea 组件和 8px 以下圆角。

- [ ] **Step 6: 接入历史和 EventSource**

先请求尾页，再以 `afterSeqId=lastSeqId` 创建 `EventSource`。检测 gap 时暂停应用并调用历史 API 补拉；组件卸载时关闭 EventSource。用户离底部超过 48px 后暂停跟尾。

- [ ] **Step 7: 挂载到 Trace Explorer**

`ObservabilityPage` 的 Trace 详情增加 `Spans | 轨迹` Tab，使用 `activeTrace.sessionId` 和 `activeTrace.runId` 过滤。Trace Span 页保持现状，不复制到新页面。

- [ ] **Step 8: 运行前端验证并提交**

Run: `cd ksadk/studio/react-ui && npm run test:ui`

Expected: PASS。

Run: `cd ksadk/studio/react-ui && npm run build`

Expected: PASS，Vite 产物构建成功。

```bash
git add ksadk/studio/react-ui/src/pages/trajectory.ts ksadk/studio/react-ui/src/pages/trajectory.test.ts ksadk/studio/react-ui/src/pages/TrajectoryView.tsx ksadk/studio/react-ui/src/pages/TrajectoryView.test.tsx ksadk/studio/react-ui/src/pages/ObservabilityPage.tsx ksadk/studio/react-ui/src/studio.css
git commit -m "feat(studio): add live trajectory view"
```

### Task 7.5: 按逻辑 Event 聚合本地 OTLP 内容事件

**Files:**
- Modify: `ksadk/studio/otel_trace.py`
- Modify: `tests/studio/test_otel_trace.py`

- [x] **Step 1: 写内容事件聚合失败测试**

同一 `(run_id, turn_id, step_id, event family)` 下，连续 `thinking/message delta` 只生成一个 OTLP Event。存在 `*.completed` 时使用完整内容和 completed 类型；缺失 completed 时拼接 delta 并保留 delta 类型。不同 Step 不得合并。

- [x] **Step 2: 实现 OTLP 逻辑 Event 投影**

只在 `_root_events()` 投影阶段聚合内容事件，并增加 `agentkit.event.delta_count`。RuntimeEvent SQLite、Session Log packed rows、SSE、Model Span 和 Tool Span 均保持不变。

- [x] **Step 3: 验证**

Run: `uv run pytest tests/studio/test_otel_trace.py tests/studio/test_run_service.py -q`

Expected: Raw OTLP 中每个 Step/Event family 最多一个内容 Event，且拼接文本与原始 delta 一致。

### Task 8: 实现 fail-closed 外导策略和递归脱敏

**Files:**
- Create: `ksadk/observability/policy.py`
- Create: `tests/observability/test_policy.py`
- Modify: `tests/runtime/test_trajectory_events.py`

- [ ] **Step 1: 写四级策略和阻断测试**

```python
def test_redacted_trace_removes_nested_secrets_and_truncates_content():
    event = RuntimeEvent.create(
        EventType.TOOL_CALL_END,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="run-1",
        seq_id=1,
        payload={
            "call_id": "call-1",
            "name": "demo-tool",
            "status": "completed",
            "headers": {"Authorization": "Bearer secret"},
            "result": "x" * 5000,
        },
    )
    safe = project_export_payload(
        event,
        TraceDataPolicy.REDACTED_TRACE,
    )
    assert "headers" not in safe
    assert "secret" not in json.dumps(safe)
    assert len(safe["content_summary"]) <= 1024
```

覆盖：`local_only` 拒绝网络投影；`metadata_only` 不含 content；`full_trace` 仍删除 credential；未知 event type/字段返回 `ExportBlocked(code="EXPORT_BLOCKED")`；循环对象和非 JSON 值阻断。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/observability/test_policy.py tests/runtime/test_trajectory_events.py -q`

Expected: FAIL，policy 模块不存在。

- [ ] **Step 3: 实现策略枚举和错误**

```python
class TraceDataPolicy(str, Enum):
    LOCAL_ONLY = "local_only"
    METADATA_ONLY = "metadata_only"
    REDACTED_TRACE = "redacted_trace"
    FULL_TRACE = "full_trace"

class ExportBlocked(ValueError):
    code = "EXPORT_BLOCKED"
```

- [ ] **Step 4: 实现按事件类型的 allowlist**

allowlist 显式列出第 2.2 节和既有 EventType 的 metadata/content 字段。禁止键大小写归一后匹配：`authorization`、`cookie`、`set-cookie`、`api_key`、`access_token`、`secret_access_key`、`password`、`credential`、`headers`、`env`、`environment`。未知键不外发。

- [ ] **Step 5: 实现递归清理**

只接受 `None/bool/int/float/str/list/dict[str, JSON]`；深度上限 12、列表上限 100、字符串上限 1024。`redacted_trace` 对内容保存前 256 字符加 SHA-256；`full_trace` 保留到 16KiB，但仍应用禁止键和敏感值模式。

- [ ] **Step 6: 锁定本地采集边界**

用带占位 `Authorization/Cookie/env` 的 `StartRequest.metadata` 驱动 Fake Adapter，断言这些 SDK 管理字段没有被复制进 RuntimeEvent；用户输入和 Tool 参数/结果仍原样持久化。不要在 `RuntimeEvent.validate_conformance()` 全局拒绝 `headers` 等业务键，否则会因合法工具输入破坏本地运行。网络外导统一经过 `project_export_payload()`。

- [ ] **Step 7: 运行测试并提交**

Run: `uv run pytest tests/observability/test_policy.py tests/runtime/test_trajectory_events.py -q`

Expected: PASS。

```bash
git add ksadk/observability/policy.py tests/observability/test_policy.py tests/runtime/test_trajectory_events.py
git commit -m "feat(observability): add fail-closed export policy"
```

### Task 9: 复用 OTel SDK 实现安全 Span 外导和健康状态

**Files:**
- Create: `ksadk/observability/otel_projection.py`
- Modify: `ksadk/events/store.py`
- Modify: `ksadk/runtime/conversation_execution.py`
- Modify: `ksadk/studio/run_service.py`
- Modify: `ksadk/tracing/setup.py`
- Modify: `tests/test_tracing_setup_otlp.py`
- Create: `tests/observability/test_otel_projection.py`
- Modify: `tests/events/test_runtime_event_store.py`
- Modify: `tests/runtime/test_trajectory_events.py`
- Modify: `ksadk/studio/api.py`

- [ ] **Step 1: 写默认无网络和显式策略测试**

```python
def test_otlp_endpoint_without_data_policy_does_not_export(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com")
    setup = _reload_setup(monkeypatch)
    setup.setup_tracing(enable_inmemory=False, enable_adk_instrumentation=False)
    assert _FakeHttpOTLPSpanExporter.instances == []
```

设置 `KSADK_OBSERVABILITY_DATA_POLICY=metadata_only` 后创建一个外部 Exporter；`local_only` 和缺失策略都不创建。该行为变更必须在用户文档中写明，不能静默保留旧的原始 Span 外发。

- [ ] **Step 2: 写安全 scope 过滤测试**

Fake batch 同时包含 `ksadk.observability.safe` 和自动插桩 scope；只有 safe scope 进入底层 OTLP Exporter，其他 Span 增加 `blocked_count`。safe Span 含未知 attribute 时整批阻断，不部分发送。

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_tracing_setup_otlp.py tests/observability/test_otel_projection.py -q`

Expected: FAIL，缺少 policy gate 和 safe projector。

- [ ] **Step 4: 实现 RuntimeEventSpanProjector**

仅使用 `project_export_payload()` 的返回值创建 scope 为 `ksadk.observability.safe` 的 Span。不能只拿 `event.trace_id` 构造假的 `NonRecordingSpan`：标准 OTel 父上下文还要求真实 `span_id`，伪造 parent 会让后端出现无来源父节点。

`begin_invocation()` 在 `run.started` 生成前创建真实、无业务内容的 safe root Span，并把其 `SpanContext` 按 `invocation_id` 登记；`conversation_execution` 和 Studio `run_service` 用该真实 context 的 trace ID 填入 `StartRequest`、`RunRecord` 和 RuntimeEvent。`local_only` 不创建 Span，继续只生成本地 32 位 correlation ID。除这个空 root 的分配外，事件属性、子 Span 创建和结束都必须在对应 RuntimeEvent 持久化成功后执行。

`RuntimeEventSpanProjector` 暴露两个入口：`begin_invocation(invocation_id, fallback_trace_id)` 返回 trace/span ID 并持有真实 root Span；`consume(event)` 先执行 `project_export_payload()`，再按下面的固定状态机处理已经持久化的事件。scope 常量固定为 `ksadk.observability.safe`。

- 活跃 Span 表按 `(invocation_id, kind, object_id)` 索引：Run 使用 `invocation_id`，Turn/Step/Model/Tool 分别使用 `turn_id/step_id/model_call_id/call_id`。父级固定为 `Run -> Turn -> Step -> Model|Tool`；缺少 Step 时 Tool 退到 Turn，缺少 Turn 时退到 Run。
- begin 事件用登记的真实父 Span 创建子 Span并保存；first-token 用 `add_event("first_token", timestamp=...)` 写入已存在的 Model Span；end 事件设置状态和安全属性后按事件时间结束并移出表。普通瞬时事件在最近的真实父级下创建同时间起止的 Span。
- end 找不到 begin、但存在真实祖先时，创建同时间起止的 diagnostic 子 Span并设置 `ksadk.boundary.incomplete=true`；Run 终态到达时，把仍未结束的子 Span按终态时间标为 incomplete 后关闭，再结束 root。不存在活跃 root、trace ID 与 root 不一致或 ID 非法时返回 `EXPORT_BLOCKED`，不得重建历史父上下文或生成另一条 Trace。
- `event_id` 是进程内幂等键；已消费事件直接忽略。首版只投影当前进程的在线事件，不从 SQLite 回灌历史 Span；需要离线 backfill 时再单独设计可持久化的 Span 映射。

- [ ] **Step 5: 接到持久化成功路径**

`RuntimeEventStore.append_one()` 仅在 `reserve_once()` 返回 `created=True` 后调用 `submit_runtime_event_for_export(persisted)`。`local_only` 时该函数是 no-op；其他策略同步完成 Span 构造并立即返回，实际网络发送仍由 `BatchSpanProcessor` 排队。策略阻断和 projector 异常只更新健康计数，不改变已持久化事件、不向 Agent 抛错。增加测试证明：真实 root/child trace ID 一致、父子关系正确、first-token 只记录一次、终态关闭残留 Span、幂等 append 不重复 consume，以及 projector 抛错时 append 仍成功。

- [ ] **Step 6: 用过滤包装器保护现有 Exporter**

在 `_LoggingSpanExporter` 外层增加 `SafeScopeSpanExporter`。它不修改 Span，只验证 scope 和 attributes；队列、批次、超时和 shutdown 继续由已有 `BatchSpanProcessor` 负责。不新增线程、内存队列或重试循环。

- [ ] **Step 7: 增加健康快照**

进程内锁保护计数：`enabled/policy/exported_count/failed_count/blocked_count/last_success_at/last_failure_at`。只报告配置的 `queue_capacity`，不抓取 OTel 私有队列获取实时 depth，也不伪造标准 SDK 没有公开的 dropped count。

- [ ] **Step 8: 增加健康 API**

`GET /api/v1/observability/export-health` 返回健康快照；endpoint 只返回 origin/path 的脱敏摘要或完全省略，禁止 Header 和 credential 值。

- [ ] **Step 9: Collector 分流和去重**

默认只启用标准 `OTEL_EXPORTER_OTLP_*` endpoint，由 Collector 分流。若同时配置 CloudMonitor 直连和通用 Collector，初始化时检测重复目的地并拒绝第二路；同一 `event_id` 在进程内只投影一次。

- [ ] **Step 10: 运行测试并提交**

Run: `uv run pytest tests/test_tracing_setup_otlp.py tests/observability/test_otel_projection.py tests/events/test_runtime_event_store.py tests/runtime/test_trajectory_events.py tests/studio/test_observability_api.py -q`

Expected: PASS。

```bash
git add ksadk/observability/otel_projection.py ksadk/events/store.py ksadk/runtime/conversation_execution.py ksadk/studio/run_service.py ksadk/tracing/setup.py ksadk/studio/api.py tests/test_tracing_setup_otlp.py tests/observability/test_otel_projection.py tests/events/test_runtime_event_store.py tests/runtime/test_trajectory_events.py tests/studio/test_observability_api.py
git commit -m "feat(observability): export redacted traces over otlp"
```

### Task 10: 完成删除、恢复、性能和端到端验收

**Files:**
- Modify: `ksadk/studio/service.py`
- Modify: `ksadk/studio/api.py`
- Modify: `tests/studio/test_api.py`
- Modify: `tests/studio/test_observability_api.py`
- Create: `tests/observability/test_local_observability_e2e.py`
- Create: `tests/observability/test_observability_benchmark.py`
- Create: `docs/local-agent-observability.md`

- [ ] **Step 1: 写删除一致性测试**

把 Studio Session 删除入口改为 async：确认没有运行中 Run 后，先调用 `SessionService.delete_session()` 删除 SQLite 事件，再删除 RunRecord 和本地 OTLP projection；任一步失败都返回明确错误并记录剩余资源，不伪装成完整成功。保持现有“Session 不存在返回 404”契约。用户通过 CLI/API 显式导出的 Session Log 是独立诊断文件，不建 export 索引，也不随 Session 自动删除；文档必须提示用户按本地保留策略自行清理。

- [ ] **Step 2: 写进程中断恢复测试**

构造 `turn.started/step.started/tool.call.begin` 后模拟重启。恢复只追加一个真实的 `run.interrupted`；原事件字节与序号不变，重复恢复不再追加第二个终态。Trajectory reducer 将仍开放的 Turn/Step/Tool 显示为 `interrupted/incomplete`，OTel 投影在 Run 终态关闭活跃子 Span 并写 `ksadk.boundary.incomplete=true`，但事实源不伪造未发生的 Tool/Step/Turn end。只有未来需要从中断 Step 精确续跑时，才重新评估 Harness 式 synthetic closer。

- [ ] **Step 3: 写 10k 事件基准**

生成 10k 个 RuntimeEvent，测量 SQLite append 总耗时、单次 p50/p95、数据库大小、post-commit 唤醒延迟和 Session Log 导出时间。额外断言正常追加路径不会随 Session 长度增加而全量扫描历史，导出器的单次读取窗口不超过 500 个 seq。基准只记录数据，不作为普通 CI 的硬时间断言；独立命令输出 JSON。

Run: `uv run pytest tests/observability/test_observability_benchmark.py -q -s`

采用批写的触发条件：持久化耗时超过同一 fake Agent 总运行耗时 5%，或单次追加 p95 超过 10ms。未达到则保持单行事务。

- [ ] **Step 4: 写本地全链路 E2E**

Fake Agent 产生两 Turn、三 Step、两个 Tool、一次失败 Tool 和最终回复。验证 SQLite commit -> post-commit notification -> SSE -> trajectory projection，以及 SQLite -> fixed-watermark JSONL 的 `event_id/seq_id/turn_id/step_id` 一致。导出固定水位后再追加一条事件，断言该事件只出现在 SSE 和下一次导出中。

- [ ] **Step 5: 写 fake Collector 安全 E2E**

在 Tool 参数、结果、Prompt 和 metadata 中放占位 secret；以 `redacted_trace` 外导。断言 Collector 收到 OTLP/HTTP protobuf、可按 trace 还原层级，且 payload 不含占位 secret。Collector 返回 500 和超时后 Agent 仍完成，SQLite/Session Log 不丢事件。

- [ ] **Step 6: 编写用户文档**

`docs/local-agent-observability.md` 必须写清：默认 `local_only`、Session DB/Log 位置、CLI/API、四级策略、OTLP 标准环境变量、Collector 推荐拓扑、删除/保留规则、当前仅单写者，以及旧 OTLP 配置需要显式增加数据策略。

- [ ] **Step 7: 运行完整验证**

Run: `uv run pytest tests/events tests/observability tests/studio/test_event_store.py tests/studio/test_run_service.py tests/studio/test_observability_api.py tests/test_tracing_setup_otlp.py -q`

Expected: PASS。

Run: `cd ksadk/studio/react-ui && npm test && npm run test:ui && npm run build`

Expected: PASS。

Run: `git diff --check`

Expected: 无输出，退出码 0。

- [ ] **Step 8: 定向 secret scan**

Run: `rg -n "(Bearer [A-Za-z0-9._-]{12,}|AKIA[A-Z0-9]{12,}|Ksc-Appkey=|api[_-]?key[=:][^ <])" ksadk tests docs`

Expected: 仅命中测试占位符或文档字段名；任何真实值都必须在提交前移除。

- [ ] **Step 9: 提交**

```bash
git add ksadk/studio/service.py tests/studio/test_api.py tests/observability/test_local_observability_e2e.py tests/observability/test_observability_benchmark.py docs/local-agent-observability.md
git commit -m "test(observability): verify local trace lifecycle"
```

## 5. 里程碑、依赖和退出标准

| 里程碑 | 建议窗口 | 包含任务 | 外部依赖 | 退出条件 |
| --- | --- | --- | --- | --- |
| O1 规范数据和本地事实源 | 2026-08-17 至 2026-08-21 | Task 1-3.5 | 无 | Studio/非 Studio 共用 RuntimeEventStore；正常追加不扫描全量历史；提交后可唤醒订阅；不再逐 delta 重写 Run/OTLP |
| O2 Session Log | 2026-08-24 至 2026-08-27 | Task 4-5 | 无 | 固定水位分页导出/校验、原子发布、CLI 可用、目录 0700/文件 0600 |
| O3 实时轨迹 | 2026-08-24 至 2026-09-04 | Task 6-7 | 无；可与 O2 并行 | 提交后唤醒；尾页/SSE 重连无重复缺口；Studio 展示 Turns/Steps/Calls/TTFT，缺失能力不伪造 |
| O4 安全外导 | 2026-08-24 至 2026-09-04 | Task 8-9 | Collector endpoint、鉴权注入、后端字段映射 | 默认无网络；脱敏失败阻断；预发可查 Trace；Exporter 故障不影响本地运行 |
| O5 联合验收 | 2026-09-07 至 2026-09-11 | Task 10 | 预发 Collector/后端和安全负责人 | 本地/外导 E2E、删除/恢复、secret scan、前后端构建全部通过 |

关键路径：Task 1 -> Task 2 -> Task 3 -> Task 3.5。Task 4/5、Task 6/7、Task 8/9 在 Task 3.5 完成后可并行。O4 外部依赖不可用时，不阻塞 O1-O3 发布，但不得宣称云端外导已打通。

## 6. 发布门禁

- 规范事件：同一 Session 的 `seq_id` 严格递增且存储层唯一；`event_id` 幂等；未知必需事件不被静默忽略。
- 本地存储：正常追加不全量扫描历史；只有 SQLite 提交成功后才通知实时消费者；Agent/Studio 崩溃不截断已写事件。
- Session Log：导出固定水位后新增事件不混入文件；分页读取内存有界；半成品 Session Log 不发布。
- 数据安全：SDK 管理的 credential/Header/env 不被复制进事件；本地原始业务内容受文件权限保护；OTLP 未知字段和脱敏失败 fail closed。
- 故障隔离：Collector 500、超时、队列满和 shutdown flush 超时不改变 Agent 终态或本地事件。
- 页面正确性：断线补拉无重复/缺口；查看旧记录时不自动跳回尾部；缺失 Model Call 边界显示不可用。
- 兼容性：旧 Run JSON 可读；不自动删除或重写用户历史文件；旧 OTLP 自动外发行为变更有明确迁移文档。
- 真实验证：只记录实际执行过的测试。缺少 Collector、鉴权或后端时明确列出条件，不用 fake 结果代替预发 E2E。

## 7. 参考

- [DeepSeek Harness Session 事件模型](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/docs/subsystems/session.zh.md)
- [DeepSeek Harness Session 持久化](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/docs/subsystems/persistence.zh.md)
- [DeepSeek Harness JSONL 持久化](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/session/session-persistence-jsonl/README.zh.md)
- [DeepSeek Harness SQLite 持久化](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/session/session-persistence-sqlite/README.zh.md)
- [DeepSeek Harness Trajectory UI](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/client/ui-trajectory/README.md)
