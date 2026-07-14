# ADK 恢复机制与 ksadk 集成设计

> 版本：1.0 | 日期：2026-07-14
> 依赖：`google-adk>=1.34.0,<2.0.0`（[pyproject.toml](../pyproject.toml)）
> 恢复最低版本门槛：`GOOGLE_ADK_RESUME_MIN_VERSION`（默认 `1.16.0`）

## 1 背景与目标

ksadk 已有一套框架无关的长任务恢复基础设施（checkpoint 事件、API 接口、session 持久化、
tool receipt 幂等）。Google ADK 自 v1.14.0 起引入 `ResumabilityConfig` 和 `invocation_id`
恢复机制，可以在工作流中断后从上次停止处继续执行。

本文档描述 ksadk ADKRunner 如何桥接 ADK 原生恢复能力到 ksadk 的长任务恢复体系，使 ADK
框架 agent 支持 `ListSessionCheckpoints` / `ResumeRun` / `SubscribeRunEvents` 等完整 API。

**设计原则**：ADKRunner 只做运行时消费，不实现 Skill 管理面逻辑；恢复抽象层
（`_normalize_checkpoint_resume_input`、`append_run_checkpoint_event`、
`append_run_resume_event`）是框架无关的，ADK 是与 LangGraph 同构的公民。

---

## 2 ADK 原生恢复机制

### 2.1 核心概念

ADK 的"恢复"（Resumability）指：agent 工作流因网络断开、进程崩溃等意外中断后，
从上次停止处继续执行，避免从头重跑。

- **invocation**：一次 `runner.run_async()` 调用，从用户消息开始到最终响应结束，
  可能包含多次 LLM 调用、多次工具调用、多次 agent 转移——全程共享同一个 `invocation_id`。
- **invocation_id**：在 `InvocationContext` 创建时一次性生成（`"e-" + uuid`），
  所有 event 的 `event.invocation_id` 均指向同一个值，从第一个 event 到最后一个 event 不变。

### 2.2 执行模型

```
  ┌─────────────────────────── invocation ───────────────────────────┐
  │ invocation_id = "e-abc123"（全程不变）                            │
  │                                                                   │
  │  ┌── agent_call_1 ──┐  ┌── agent_call_2 ──┐  ┌── agent_call_3 ─┐│
  │  │ ┌─ step_1 ─┐     │  │ ┌─ step_1 ─┐     │  │                 ││
  │  │ │call_llm  │     │  │ │call_llm  │     │  │  call_llm      ││
  │  │ │call_tool │     │  │ │call_tool │     │  │  → final resp   ││
  │  │ │call_llm  │     │  │ │call_llm  │     │  │                 ││
  │  │ └──────────┘     │  │ └──────────┘     │  └─────────────────┘│
  │  └──────────────────┘  └──────────────────┘                     │
  └──────────────────────────────────────────────────────────────────┘
```

一个 invocation 内的所有 event 共享同一个 `invocation_id`，这是 ksadk 只需建立一次
映射的关键依据。

### 2.3 启用方式

在 `App` 对象上配置 `ResumabilityConfig`：

```python
from google.adk.apps import App, ResumabilityConfig

app = App(
    name='my_resumable_agent',
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
runner = Runner(app=app, session_service=session_service)
```

Runner 会将 `app.resumability_config` 传递给 `InvocationContext`，影响
`ctx.is_resumable` 和 `ctx.should_pause_invocation()` 的行为。

### 2.4 恢复方式

恢复时传入原始 invocation 的 `invocation_id`，不传 `new_message`：

```python
async for event in runner.run_async(
    user_id='u_123',
    session_id='s_abc',
    invocation_id='e-abc123',      # 原始 invocation_id
    # new_message 不传
):
    ...
```

### 2.5 恢复原理

ADK 恢复基于 **事件（Events）+ 事件动作（Event Actions）** 的增量追踪：

1. **记录已完成任务**：可恢复工作流执行过程中，每个 agent 的完成状态通过
   `EventActions.agent_state` 和 `EventActions.end_of_agent` 写入事件历史。
2. **中断后重启时恢复状态**：`_setup_context_for_resumed_invocation()` 读取事件历史，
   调用 `ctx.populate_invocation_agent_states()` 恢复每个 agent 的状态。
3. **工具结果复用**：已成功执行的工具结果已存在于事件历史中，
   恢复后 ADK 不会重新调用这些工具。

#### 多智能体恢复策略

| 智能体类型 | 恢复策略 | 关键字段 |
|-----------|---------|---------|
| SequentialAgent | 从 `current_sub_agent` 继续序列 | `agent_states["seq_agent"]["current_sub_agent"]` |
| LoopAgent | 从 `current_sub_agent` + `times_looped` 继续循环 | `agent_states["loop_agent"]` |
| ParallelAgent | 只运行未完成的子智能体 | `end_of_agents[sub_name]` |
| 自定义 Agent | 开发者通过 `BaseAgentState` + `WorkflowStep` 自定义 | `agent_states[agent_name]["step"]` |

#### 暂停条件

`InvocationContext.should_pause_invocation(event)` 判断是否暂停：
- 必须满足 `ctx.is_resumable == True`
- 当前 event 包含 `long_running_tool_ids`（长时间运行函数工具）
- 暂停后，调用方需要保存 `invocation_id`，后续通过传入该 ID 恢复

### 2.6 ResumeMode 语义差异

ADK 与 LangGraph 的恢复语义本质不同，ksadk 不抹平这个差异：

| | LangGraph | ADK |
|---|---|---|
| ResumeMode | `time_travel` | `invocation_id` |
| 能力 | 回档到**任意** checkpoint_id 重跑 | 只能**续跑被中断的** invocation（forward-only） |

控制面按 `ResumeMode` 分支即可。ADK 的 checkpoint 只是一个"最新恢复点"标记，
不携带独立的回档语义。

### 2.7 限制

1. **工具调用至少执行一次，恢复时可能执行多次**：开发者需自行保证幂等
2. **不支持恢复前修改工作流**：停止后不能增删智能体再恢复
3. **不支持从 ADK Web UI 或 CLI 恢复**：仅 API Server / Runner API
4. **自定义 Agent 需手动实现 `BaseAgentState`**：默认不支持
5. **LLM 推理不可中断**：单次 LLM 调用是原子操作，无法 mid-inference checkpoint

---

## 3 架构总览

### 3.1 模块边界

| 模块 | 文件 | 职责 |
|------|------|------|
| ADKRunner | [adk_runner.py](../ksadk/runners/adk_runner.py) | Runner 构造、invocation_id 采集、checkpoint 写入、恢复入口、能力声明 |
| 会话连续性 | [continuity.py](../ksadk/sessions/continuity.py) | ADKSessionAdapter 连续性级别判定 |
| 恢复抽象层 | [runtime.py](../ksadk/conversations/runtime.py) | `_normalize_checkpoint_resume_input`、`append_run_checkpoint_event`、`append_run_resume_event`（框架无关，ADK 与 LangGraph 共用） |
| 服务端 API | [app.py](../ksadk/server/app.py) | ResumeRun、ListSessionCheckpoints、GetCheckpointResumePreview、SubscribeRunEvents、GetAgentUiBootstrap |
| Session 存储 | local_service / postgres_service | `ksadk_states` / `ksadk_events` 表，无 schema 变更 |

### 3.2 恢复链路总览

```
控制台                         ksadk server (app.py)              ADKRunner (adk_runner.py)
  │                                │                                   │
  │── GetAgentUiBootstrap ────────►│                                   │
  │   读 RuntimeCapabilities       │── runner.get_runtime_capabilities()►│
  │   + CheckpointResumeCapability │                                   │
  │◄── 返回 capability ───────────│                                   │
  │                                │                                   │
  │── ListSessionCheckpoints ─────►│                                   │
  │   读 run_checkpoint 事件       │── _apply_adk_only_latest_resumable │
  │   只展示最新可恢复点           │                                   │
  │◄── checkpoint 列表 ───────────│                                   │
  │                                │                                   │
  │── ResumeRun ──────────────────►│                                   │
  │   Stream=true                  │── 终态检查                         │
  │                                │   终态 → _action_response(noop)   │
  │                                │   非终态 → _detached_streaming     │
  │                                │   resume_input 下发 ──────────────►│
  │                                │                                   │── _resolve_resume_invocation_id
  │                                │                                   │   framework_ref 或 session binding
  │                                │                                   │── runner.run_async(invocation_id=...)
  │◄── SSE 流 ────────────────────│◄── wrapped event stream ──────────│
  │                                │                                   │
  │── SubscribeRunEvents ─────────►│                                   │
  │   AfterSeqId 增量拉取           │── session service 事件轮询         │
  │◄── SSE 补流 + [DONE] ─────────│                                   │
```

---

## 4 ADKRunner 实现细节

### 4.1 可恢复性检测：`_resolve_resumability`

检测 ADK Runner 是否启用可恢复性，按优先级返回 `_ResolvabilityResult`：

```python
@dataclass
class _ResolvabilityResult:
    enabled: bool
    source: str   # "agent_module" | "env" | "auto_persistent_session" | "default"
    app: Any      # 用户模块导出的 App 对象（如果有）
```

优先级链：

1. **agent 模块导出 `app`**：模块属性 `app` 或 `application` 是 `App` 实例且
   `resumability_config.is_resumable == True` → 直接使用该 App（保留 plugins 等自定义配置）
2. **环境变量**：`KSADK_ADK_RESUMABLE=1|true|yes` → 自动包装为 App
3. **持久化 session backend 自动启用**：ShortTermMemory backend 非 local 时自动启用
4. **默认不启用**

### 4.2 版本兼容性检查：`_check_adk_resume_compatibility`

在构造 Runner 前，检查当前 `google-adk` 版本是否满足恢复最低要求：

- 读取 `GOOGLE_ADK_RESUME_MIN_VERSION` 环境变量（默认 `1.16.0`）
- 使用 `packaging.version.Version` 比较，低于门槛时强制关闭恢复
- 记录 `_resume_disabled_reason` 供 capability 声明回传

```python
def _check_adk_resume_compatibility(self) -> tuple[bool, str]:
    adk_ver = self._get_adk_version()
    if adk_ver is None:
        return False, "google-adk version unknown, ..."
    from packaging.version import Version
    if Version(adk_ver) < Version(self._adk_resume_min_version):
        return False, f"google-adk {adk_ver} < {self._adk_resume_min_version}, ..."
    return True, ""
```

### 4.3 Runner 构造：`_build_runner`

根据可恢复性检测结果构造 ADK Runner：

- **版本检查失败**：即使 `resumable.enabled=True`，也降级为非可恢复 Runner，
  记录 `_resume_disabled_reason`
- **用户提供 App**：直接传 `app=resumable.app`，保留用户自定义配置
- **自动包装**：`App(name=..., root_agent=..., resumability_config=ResumabilityConfig(is_resumable=True))`
- **不可恢复**：`Runner(agent=self._agent, ...)`，保持现有行为

### 4.4 invocation_id 采集与映射存储

#### 采集方式

从 `run_async()` 返回的第一个 event 中取出 `invocation_id`，建立 ksadk invocation_id →
ADK invocation_id 的映射。采集在 event 迭代器包装函数中完成，不使用实例属性。

两个包装函数：

- **`_collect_adk_invocation_id`**：可恢复模式下使用。采集 invocation_id + 持久化映射 +
  在可恢复边界写入 checkpoint 事件。接收 `checkpoint_run_id` 参数用于恢复模式下沿用
  原始 RunId 保证 checkpoint 时间线连贯。
- **`_collect_adk_invocation_id_if_present`**：不可恢复模式下使用。仅采集 invocation_id
  并持久化映射，不写 checkpoint。

#### 并发安全

- **不使用实例属性**：早期实现用 `self._last_adk_invocation_id` 存储最后一个
  invocation_id，但 ADKRunner 是进程级全局单例，并发处理 session A、B 时 B 会覆盖 A
  的值。已移除实例属性，改用函数内局部变量（`captured_adk_invocation_id` 或
  `local_adk_invocation_id`）。
- **映射加锁**：`_persist_invocation_mapping` 的读改写（get → 修改 invocation_map → set）
  在 `self._invocation_map_lock`（`asyncio.Lock`）保护下执行，防止同一 session 并发
  运行丢失映射。

#### 映射存储

```python
async def _persist_invocation_mapping(self, *, session_id, ksadk_invocation_id, adk_invocation_id):
    async with self._invocation_map_lock:
        binding = await core.get_binding_by_session_id(session_id, "adk")
        invocation_map = dict(binding.get("invocation_map") or {})
        invocation_map[ksadk_invocation_id] = adk_invocation_id
        await core.set_binding_by_session_id(session_id, "adk", {
            "external_session_id": str(session_id),
            "internal_session_id": str(session_id),
            "invocation_map": invocation_map,
        })
```

存储位置：`ksadk_states` 表，scope = `runner_binding:adk`，`state_json` 字段内的
`invocation_map` key。无需新增表或字段。

### 4.5 checkpoint 事件写入

#### 可恢复边界识别

| 事件特征 | 是否为可恢复边界 | 理由 |
|---------|----------------|------|
| event 包含 `function_calls`（工具调用请求） | 是 | 工具调用是天然的恢复点 |
| event 包含 `agent_state`（`EventActions.agent_state`） | 是 | 自定义 Agent 显式保存了步骤状态 |
| event 包含 `end_of_agent`（`EventActions.end_of_agent`） | 是 | Agent 完成标记，用于 ParallelAgent 进度追踪 |
| 纯 LLM 文本响应 | 否 | LLM 响应已完成，无需 checkpoint |
| partial 流式 token | 否 | 中间态，不可恢复 |

#### checkpoint_id 生成

每个可恢复边界写入一个递增 seq 的 checkpoint：

```python
checkpoint_id = f"adk-ckpt-{checkpoint_seq}"
```

#### metadata 字段

checkpoint 事件的 metadata 包含以下字段，供消费方做门控和 UI 展示：

| 字段 | 含义 |
|------|------|
| `tool_names` / `tool_call_ids` | 工具调用信息（如有） |
| `agent_state_keys` | agent 状态键（如有） |
| `is_terminal` / `end_of_agent` | 终态标记（如有） |
| `is_resumable` / `resume_status` | 当前是否可恢复 |
| `backend` | session backend 类型（`in_memory` / `sqlite` / `database`） |
| `scope` | 固定 `invocation` |
| `durable` | 是否持久化（`stm_backend is not None and stm_backend != "local"`） |
| `resume_mode` | 固定 `invocation_id` |
| `only_latest_resumable` | 固定 `True`，标识此 checkpoint 只能取最新恢复点 |

#### framework_ref 结构

```python
framework_ref = {
    "adk": {
        "invocation_id": adk_invocation_id,
        "checkpoint_seq": checkpoint_seq,
        "event_id": getattr(event, "id", ""),
        "author": getattr(event, "author", ""),
    }
}
```

恢复时 server 从 checkpoint 事件中取出 `framework_ref.adk.invocation_id` 透传给 runner。

### 4.6 恢复入口：`_prepare_run_events`

`invoke()` 和 `stream()` 共用 `_prepare_run_events` 统一准备事件流，消除双路径复制风险。

恢复模式判定：`input_data["checkpoint_resume"] == True` 时进入恢复路径。

```python
if is_resume and self._resumable:
    adk_invocation_id = await self._resolve_resume_invocation_id(
        input_data=input_data,
        session_id=session_id,
        ksadk_invocation_id=ksadk_invocation_id,
    )
    run_kwargs["invocation_id"] = adk_invocation_id
    # 不传 new_message
else:
    run_kwargs["new_message"] = new_message or self._build_adk_content("[empty message]", [])
    run_kwargs["state_delta"] = self._build_state_delta(input_data) or None
```

`checkpoint_run_id`：恢复模式下沿用原始 RunId 写 checkpoint，保证同一长任务的
checkpoint 时间线连贯；非恢复模式下为空，用 ksadk_invocation_id 作为 run_id。

### 4.7 恢复引用解析：`_resolve_resume_invocation_id`

恢复时需要 ADK invocation_id，解析顺序：

1. **framework_ref**：从 `input_data["framework_ref"]["adk"]["invocation_id"]` 取
2. **session binding fallback**：从 `runner_binding:adk` scope 的 `invocation_map` 中
   按 ksadk invocation_id 查找
3. **找不到时抛 `ValueError`**：消息为 `checkpoint_not_resumable: ADK invocation_id not
   found for session=..., ksadk_invocation_id=...`。**绝不静默降级为新任务。**

```python
if not adk_invocation_id:
    raise ValueError(
        f"checkpoint_not_resumable: ADK invocation_id not found for "
        f"session={session_id}, ksadk_invocation_id={ksadk_invocation_id}. "
        f"The checkpoint data may have been lost or the invocation was "
        f"never persisted."
    )
```

`invoke()` 和 `stream()` 中调用 `_prepare_run_events` 处不包裹 try/except，
`ValueError` 直接传播给上层 conversation runtime 处理。

### 4.8 零事件防护

恢复一个已结束的 invocation 时，ADK 可能返回零个 event。早期代码在循环后直接访问
`event` 变量触发 `UnboundLocalError`。现在使用 `last_event = None` 初始化并跟踪：

```python
last_event = None
async for event in wrapped_async:
    events_list.append(event)
    last_event = event
    ...
# 循环结束后安全访问 last_event
if last_event is not None and hasattr(last_event, "content") and last_event.content:
    ...
```

### 4.9 能力声明

#### `describe_checkpoint_capability`

```python
{
    "Supported": True,
    "Backend": "adk_invocation" | "adk_invocation+sqlite" | "adk_invocation+postgres",
    "Scope": "invocation",
    "Durable": stm_backend is not None and stm_backend != "local",
    "SharedAcrossPods": stm_backend == "database",
    "ResumeMode": "invocation_id",
    "Reason": "ADK ResumabilityConfig enabled; resume via invocation_id",
}
```

不可恢复时 `Supported=False`，`ResumeMode="forward_only"`，`Reason` 回传
`_resume_disabled_reason`（版本不兼容时为版本信息）。

#### `get_runtime_capabilities`

`SessionContinuity.Level` 随 backend 持久化程度降级：

- **durable**（sqlite / database backend）：`Level = "runtime"` — invocation 状态跨
  进程可恢复
- **非 durable**（in_memory / local backend）：`Level = "semantic"` — invocation 状态
  在内存中，进程重启 / 跨 Pod 全丢，恢复只在同一进程生命周期内有效

```python
is_durable = stm_backend is not None and stm_backend != "local"
level = "runtime" if is_durable else "semantic"
```

`Reason` 同步区分：
- durable：`"ADK ResumabilityConfig enabled with durable session backend, ..."`
- 非 durable：`"ADK ResumabilityConfig enabled but session state is in-memory; resume only works within the same process lifetime"`

### 4.10 ADKSessionAdapter 连续性判定

[continuity.py](../ksadk/sessions/continuity.py) 中的 `ADKSessionAdapter.continuity_status`
同步降级：

```python
if is_resumable:
    is_durable = stm_backend is not None and stm_backend != "local"
    level = SessionContinuityLevel.RUNTIME if is_durable else SessionContinuityLevel.SEMANTIC
    path = "adk_resume"
elif has_native_session:
    level = SessionContinuityLevel.SEMANTIC
    path = "native_session"
else:
    level = SessionContinuityLevel.SEMANTIC
    path = "replay"
```

### 4.11 LiteLLM JSON 补丁

`_apply_json_patch` 对 `google.adk.models.lite_llm._message_to_generate_content_response`
做手术式补丁，在最终组装的工具调用参数解析处捕获 `JSONDecodeError`：

```python
try:
    result = _original_fn(message, **forward_kwargs)
except _stdlib_json.JSONDecodeError:
    logger.warning("ADKRunner: caught JSONDecodeError in args parsing, ...")
    return _genai_types.GenerateContentResponse(candidates=[])
```

此补丁不替换 json 模块，不影响 ADK 流式参数完整性检测（`try: json.loads(args) except
json.JSONDecodeError: pass`）。兼容不同 ADK 版本（按原始函数签名动态决定转发哪些 kwargs）。

---

## 5 服务端 API 行为

### 5.1 ResumeRun

[app.py](../ksadk/server/app.py) 中的 `resume_run_action` 处理流程：

1. **查找 checkpoint**：从 session events 中按 `RunId` + `CheckpointId` 定位
2. **终态检查**：`_checkpoint_resume_disabled_detail` 判断 checkpoint 是否已终态
   - **终态**（`is_terminal=True`）：写 `run_resume` + `run_status(completed)` 审计事件，
     返回 `_action_response`（普通 JSON，不是 SSE）：

     ```json
     {"status": "noop", "Reason": "...", "CheckpointId": "...", "RunId": "...", "ResumeAttemptId": "..."}
     ```

     即使 `Stream=True` 也返回 JSON，避免 SSE 客户端解析失败。
   - **非终态但不可恢复**：抛 `409 checkpoint_not_resumable`
3. **构造 resume_input**：从 checkpoint 事件提取 `framework` / `framework_ref` / `metadata`，
   生成 `resume_attempt_id`，透传给 runner
4. **执行恢复**：
   - `Stream=True`：`_detached_streaming_response` 后台流式执行
   - `Stream=False`：`invoke_conversation_once` 同步执行

### 5.2 ListSessionCheckpoints

`list_session_checkpoints_action` 处理流程：

1. 从 session events 中提取所有 `run_checkpoint` 事件
2. **`_apply_adk_only_latest_resumable`**：对 metadata 含 `only_latest_resumable=True` 的
   checkpoint，按 RunId 分组，只保留 SeqId 最大的为 `IsResumable=True`，其余置
   `IsResumable=False` + `ResumeDisabledReason="新的恢复点已生成，此恢复点暂停恢复能力"`
3. 应用 resume audit（`ResumeCount` / `CheckpointStatus` / `LastResumedAt`）
4. 支持 `OnlyResumable` / `Framework` / `RunId` 过滤和分页

### 5.3 GetCheckpointResumePreview

对单个 checkpoint 调用 `_check_adk_latest_resumable` 验证是否为最新恢复点，构建 preview
（`CanResume` / `ExpectedAction` / `NextNode` / `Risk.DuplicateSideEffectRisk` /
`ToolReceipts`）。

### 5.4 SubscribeRunEvents

`subscribe_run_events_action` 是一个 SSE 长轮询端点，从 session service 增量拉取事件：

- 按 `InvocationId` 过滤，从 `AfterSeqId` 开始增量查询
- 遇到 `run_status` 终态事件时发送 `[DONE]` 并结束
- 无新事件时查全量确认 run 是否已有终态（断线重连兜底）

**已知限制**：当前只读本地 session service 事件，没有代理到 runtime SSE 事件源。
如果 runtime resume 事件未被写入 session service，客户端断线 / 刷新后可能订阅不到恢复
事件（详见第 8 节）。

### 5.5 GetAgentUiBootstrap

返回 `RuntimeCapabilities`（runner.get_runtime_capabilities()）和
`CheckpointResumeCapability`（从 RuntimeCapabilities 中提取 ResumeRun/Checkpoint），
控制台据此决定恢复入口是否可见。

```python
checkpoint_resume_capability = {
    "Supported": bool((runtime_capabilities.get("ResumeRun") or {}).get("Supported") ...),
    "Checkpoint": runtime_capabilities.get("Checkpoint") or {},
    "ResumeRun": runtime_capabilities.get("ResumeRun") or {},
}
```

### 5.6 恢复审计事件归属

恢复审计（`run_resume` 事件）只有一个 owner：**conversation runtime**。

`_prepare_conversation_turn` 在检测到 checkpoint resume input 时调用
`append_run_resume_event` + `append_run_status_event(resuming)`。ADKRunner 内不再调用
`append_run_resume_event`，避免一次恢复写入两条审计记录。

---

## 6 数据存储设计

### 6.1 无需新增表或字段

| 数据 | 存储位置 | scope |
|------|---------|-------|
| invocation_id 映射 | `ksadk_states.state_json` → `invocation_map` | `runner_binding:adk` |
| checkpoint 事件 | `ksadk_events` 表 | event_type=`run_checkpoint` |
| resume 事件 | `ksadk_events` 表 | event_type=`run_resume` |
| run status 事件 | `ksadk_events` 表 | event_type=`run_status` |

### 6.2 环境变量

| 环境变量 | 用途 | 默认值 |
|---------|------|--------|
| `KSADK_ADK_RESUMABLE` | 显式控制是否启用 ADK 恢复 | 空（不启用） |
| `GOOGLE_ADK_RESUME_MIN_VERSION` | 恢复最低版本门槛，低于此版本自动降级 | `1.16.0` |

启用优先级：agent 模块 `resumability_config` > `KSADK_ADK_RESUMABLE=1` > 持久化 session
backend 自动启用。

### 6.3 恢复失败语义

| 场景 | 行为 |
|------|------|
| invocation_id 不在 framework_ref 也不在 session binding | 抛 `ValueError("checkpoint_not_resumable: ...")`，传播给上层 |
| checkpoint 已终态（`is_terminal=True`） | 返回 JSON `{"status": "noop"}`，不启动 SSE |
| checkpoint 非终态但 `IsResumable=False` | 抛 `409 checkpoint_not_resumable` |
| google-adk 版本低于门槛 | 恢复降级关闭，`_resume_disabled_reason` 回传 capability |
| 恢复已结束的 invocation（零事件） | 不抛异常，返回空响应（`last_event = None` 防护） |

---

## 7 端到端流程

### 7.1 首次运行

```
端侧 → RunAgent(user_input, session_id)
  │
  ▼
ksadk ADKRunner.invoke() / stream()
  ├─ load_agent() → _build_runner() 构造 Runner(App(resumability_config=...))
  ├─ _prepare_run_events(is_resume=False)
  │   ├─ run_kwargs = {new_message=..., state_delta=...}
  │   └─ wrapped = _collect_adk_invocation_id(events, ...)
  │
  ├─ async for event in wrapped:
  │    │
  │    ▼ event[0]: invocation_id = "e-abc123"
  │    ├─ 采集到局部变量 captured_adk_invocation_id
  │    ├─ _persist_invocation_mapping(ksadk_inv → "e-abc123")
  │    │   (async with _invocation_map_lock: 读改写)
  │    │
  │    ▼ event[N]: function_calls=[search_database(...)]
  │    ├─ _is_resumable_boundary(event) = True
  │    ├─ checkpoint_seq = 1
  │    ├─ _maybe_write_checkpoint:
  │    │   checkpoint_id = "adk-ckpt-1"
  │    │   framework_ref.adk.invocation_id = "e-abc123"
  │    │   metadata = {backend, scope, durable, resume_mode,
  │    │              only_latest_resumable: True, tool_names, ...}
  │    │
  │    ▼ event[M]: function_calls=[generate_report(...)]
  │    ├─ checkpoint_seq = 2, checkpoint_id = "adk-ckpt-2"
  │    │
  │    ▼ 最终响应
  └─ 返回结果
```

### 7.2 中断后恢复

```
端侧 → ResumeRun(RunId="ksadk-inv-001", CheckpointId="adk-ckpt-2", Stream=true)
  │
  ▼
ksadk server: resume_run_action
  ├─ _find_session_checkpoint → 从 events 定位 checkpoint
  ├─ _checkpoint_resume_disabled_detail:
  │   is_terminal? → NO
  │   IsResumable? → _check_adk_latest_resumable 验证是否最新
  │     (若 adk-ckpt-2 不是最新 → IsResumable=False → 409)
  │
  ├─ 构造 resume_input:
  │   {type: "agentengine.resume_checkpoint",
  │    run_id: "ksadk-inv-001",
  │    checkpoint_id: "adk-ckpt-2",
  │    framework: "adk",
  │    framework_ref: {adk: {invocation_id: "e-abc123", checkpoint_seq: 2, ...}}}
  │
  ├─ conversation runtime:
  │   append_run_resume_event (审计 owner，唯一)
  │   append_run_status_event(status="resuming")
  │   _normalize_checkpoint_resume_input → 下发给 runner
  │
  ▼
ADKRunner._prepare_run_events(is_resume=True)
  ├─ _resolve_resume_invocation_id:
  │   framework_ref.adk.invocation_id = "e-abc123"
  │   (找不到 → ValueError, 不降级为新任务)
  │
  ├─ run_kwargs = {invocation_id: "e-abc123"}  (不传 new_message)
  ├─ runner.run_async(**run_kwargs)
  │   ├─ ADK 从 session 事件历史恢复 invocation "e-abc123" 状态
  │   ├─ 跳过已执行的工具调用，复用已有结果
  │   └─ 从断点处继续执行，产出新 events
  │
  ├─ wrapped = _collect_adk_invocation_id(events, checkpoint_run_id="ksadk-inv-001")
  │   ├─ 采集同一个 invocation_id（不变）
  │   ├─ 继续写 checkpoint（seq 继续递增）
  │   └─ yield events
  │
  ▼
端侧收到 SSE 流 → 运行完成 → [DONE]
```

### 7.3 终态 noop 恢复

```
端侧 → ResumeRun(CheckpointId="ckpt-terminal", Stream=true)
  │
  ▼
server: _checkpoint_resume_disabled_detail
  ├─ is_terminal = True
  ├─ 写 run_resume + run_status(completed) 审计事件
  └─ 返回 _action_response:  ← 普通 JSON，不是 StreamingResponse
      {"status": "noop", "Reason": "该 checkpoint 已是终态", ...}
  │
  ▼
端侧: 收到 JSON (非 SSE)
  ├─ 检查 Code=0, Data.status="noop"
  └─ 刷新 checkpoint 列表，不启动 SSE
```

---

## 8 已知限制与后续工作

### 8.1 SubscribeRunEvents 事件源一致性

**问题**：`ResumeRun` 走 `_detached_streaming_response` 直接从 runner 流式消费事件；
`SubscribeRunEvents` 只从 session service 数据库读已持久化事件。两条路径是否指向同一
事件源，取决于 detached streaming 是否可靠地将所有事件落库。

当前代码没有显式的 runtime → session service 事件镜像，也没有专门的"resume 后断线续订"
测试。如果 runtime resume 事件未被写入 session service，客户端断线 / 刷新后订阅不到恢复
事件。

**后续方向**：让 `SubscribeRunEvents` 解析 `AgentId` 并代理到与 `ResumeRun` 相同的
runtime 事件源，或确保 detached streaming 路径将 resume 事件可靠写入 session service。

### 8.2 真实 resume E2E

本地测试用 mock runner 覆盖了并发串 session、丢失引用、零事件、重复审计、终态 noop 等场景，
但没有真实 ADK + 持久化 session service 的端到端 resume 验证。需要 live ADK 环境才能验证
ADK 原生恢复行为（事件历史读取、工具结果复用、多智能体状态恢复）。

### 8.3 ADK 版本升级

当前锁定 `google-adk>=1.34.0,<2.0.0`。ADK `ResumabilityConfig` 标记为 `@experimental`，
API 可能在 2.x 中变化。升级时需重新验证恢复流程。

### 8.4 工具幂等性

ADK 只保证 at-least-once，对副作用工具（如支付、发邮件）需开发者自行保证幂等。
ksadk 的 Tool Receipt 机制可作为补充，但当前 ADK 恢复路径尚未集成 Tool Receipt。

### 8.5 多 Pod 场景

`invocation_map` 存在 session state 中，跨 Pod 可访问（前提是 session backend 使用
PostgreSQL 等持久化存储）。但 ADK Runner 实例是进程内的，恢复需要确保新 Pod 能拿到
相同的 session 数据和 agent 配置。非 durable backend 下 `Level` 降级为 `semantic`，
消费方不应展示跨 Pod 恢复入口。

---

## 9 测试覆盖

[tests/test_runner.py](../tests/test_runner.py) 和
[tests/test_server_session_app.py](../tests/test_server_session_app.py) 中的相关测试：

| 测试 | 覆盖项 |
|------|--------|
| `test_adk_runner_no_cross_session_invocation_id_corruption` | P1.1：实例属性已移除 |
| `test_adk_runner_concurrent_invocation_ids_no_cross_pollution` | P1.1：并发不串 session |
| `test_adk_runner_invocation_map_lock_prevents_lost_update` | P1.1：映射加锁 |
| `test_adk_runner_resume_raises_on_missing_invocation_id` | P1.2：丢失引用抛错 |
| `test_adk_runner_resolve_resume_from_framework_ref` | P1.2：从 framework_ref 解析 |
| `test_adk_runner_declares_runtime_resume_when_resumable_enabled` | P1.3：Level 随 backend 降级 |
| `test_adk_runner_checkpoint_metadata_includes_backend_info` | P1.3：metadata 含 backend/scope/durable |
| `test_adk_runner_zero_events_resume_no_unbound_error` | P1.4：零事件无 UnboundLocalError |
| `test_adk_runner_checkpoint_metadata_includes_resume_mode_annotation` | P1.4：`only_latest_resumable` |
| `test_apply_adk_only_latest_resumable_marks_older_checkpoints` | P1.4：旧 checkpoint 禁用 |
| `test_adk_runner_resume_does_not_write_duplicate_audit` | P2：不重复写 run_resume |
| `test_resume_run_action_returns_noop_for_terminal_checkpoint` | P1.6：终态 noop 返回 JSON |
| `test_subscribe_run_events_streams_events_appended_after_subscription` | SubscribeRunEvents 基本流 |
| `test_subscribe_run_events_reconnects_without_replaying_consumed_events` | SubscribeRunEvents 断线重连 |
