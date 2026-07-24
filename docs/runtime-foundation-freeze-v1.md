# G0 地基冻结稿 v1(拍板用)

> 状态:**已拍板冻结**(2026-07-23,G0 三样冻结稿评审通过后进入 05-09 实现,据此落地;字段/签名不再改,只允许 additive 演进)。
> 依据:H2-下半年迭代规划-v2-2026-07.md §4.2/4.3;goals/01、02、03;友商实证(codex `EventMsg`、ADK `Event`/`EventActions`、Wegent `cancel()`)。
> 三样:G0.2 RuntimeEvent schema / G0.3 RuntimeAdapter 签名 / G0.1 create_runtime_app 结构。
> 关联:G0.3 的 `stream` 返回类型消费 G0.2 的事件族;G0.3 与 A2A contract freeze 同期拍板。

---

## G0.2 RuntimeEvent v1 schema

### 设计约束(友商证伪,必须遵守)

1. **带相位字段**:区分 `commentary`(过程解说)vs `final_answer`(最终答案)。Hosted UI 与 CodexRuntime 都依赖。codex 用 `AgentMessage` vs `AgentReasoning` 分离;我们用统一 `phase` 字段。
2. **工具审批是一等事件**(`approval.*`),不是普通 text。codex `ExecApprovalRequest`、ADK `requested_tool_confirmations` 均为一等。
3. **additive 设计 + `schema_version`**:只增字段/事件类型,不改既有字段语义。
4. **事件只定义一次**:Runtime 产生 → server 持久化 → gateway 透传 → UI/协议 adapter 消费。

### 信封(envelope,每个事件都有)

```python
class RuntimeEvent(BaseModel):
    schema_version: Literal[1] = 1        # additive 演进锚点
    event_id: str                          # 全局唯一,幂等/去重/回放用
    event_type: str                        # 见下方事件族,如 "text.delta" / "approval.requested"
    timestamp: float                       # epoch 秒(producer 侧)
    # 多维度归属(tenant/session/run)
    agent_id: str
    user_id: str
    session_id: str
    invocation_id: str                     # 单次 run/turn id
    seq_id: int                            # session 级单调游标(对齐现有 ConversationEvent.seq_id),
                                           # 支撑 SubscribeSessionEvents 跨 invocation replay
    phase: Literal["commentary", "final_answer"] | None = None   # 仅 text/reasoning 类用
    payload: dict[str, Any] = {}           # 按 event_type 的负载(见下)
```

### 事件族(event_type 枚举,v1 冻结)

| 族 | event_type | 关键 payload 字段 | 相位 |
|---|---|---|---|
| **text** | `text.delta` / `text.completed` | `text`, `message_id` | commentary/final_answer |
| **reasoning** | `reasoning.delta` / `reasoning.completed` | `text`, `summary` | commentary(恒) |
| **tool** | `tool.call.begin` / `tool.call.end` | `call_id`, `name`, `args`(begin) / `result`, `error`, `duration_ms`(end) | — |
| **artifact** | `artifact.created` / `artifact.updated` | `name`, `version`, `uri`, `mime` | — |
| **approval** | `approval.requested` / `approval.resolved` | `approval_id`, `call_id`, `kind`(tool/permission/input), `detail`(requested), `decision`(resolved) | — |
| **run** | `run.started` / `run.progress` / `run.interrupted` / `run.completed` / `run.failed` / `run.canceled` | `status`, `progress?`, `error?`, `cancel_result?` | — |
| **checkpoint** | `checkpoint.created` / `checkpoint.resumed` | `checkpoint_id`, `granularity`(delta/snapshot), `resume_target?` | — |
| **usage** | `usage.reported` | `input_tokens`, `output_tokens`, `total_tokens`, `cached_tokens`, `reasoning_tokens` | — |
| **A2UI** | `a2ui.surface.begin` / `a2ui.surface.update` / `a2ui.surface.end` / `a2ui.interaction` / `a2ui.action` | `surface_id`, `block_id?`, `catalog?`, `data?` | — |
| **remote A2A** | `a2a.task.created` / `a2a.task.status` / `a2a.task.artifact` | `task_id`, `origin`(remote agent url/space), `status?`, `artifact?` | — |

**A2UI 对齐说明**:事件族以 `a2ui.surface/interaction/action` 冻结;A2UI canonical 的
`a2ui.block.*` 独立事件与 renderer registry 是 goal-13/14 的渲染层映射,本 schema 在
`surface.update` payload 里承载 block 数据,不在 v1 信封另开 `a2ui.block.*` 顶层类型——
若 goal-13 论证需要顶层 `a2ui.block.*`,作为 **additive 新增事件类型**加入,不改本冻结。

**审批回包走向(关键)**:`approval.requested` 在事件流上;**审批决定(`approval.resolved`)
主要经独立命令/恢复通道(G0.3 `resume`/`submit`)回流**,事件流上的 `approval.resolved`
仅作回放/审计记录——codex/ADK/LangGraph 都是"事件流 + 独立命令通道",不是 duplex stream。

### 交付物(goal-02 实现时)

`ksadk/events/runtime_event.py`(类型 + 序列化/反序列化 + `SCHEMA_VERSION`)+
`ksadk_runtime_common/schemas/runtime_event_v1.json`(JSON Schema)+
`tests/events/`(序列化 roundtrip + conformance fixture)。**本 goal 只冻结,不改 runtime.py 发事件。**

---

## G0.3 RuntimeAdapter 签名

### 结构(三层)

- `BaseRuntime`:表达 Runtime **原生能力**(现有 `BaseRunner` 演进,不推倒)。
- `RuntimeAdapter`:把原生能力映射为**平台接口**(六动词)。
- `RuntimeRegistry`:按 `runtime_type` 注册 adapter(替代 `runners/factory.py` 的 if/elif 分发)。

### 六动词签名(冻结)

```python
class RuntimeAdapter(ABC):
    @abstractmethod
    async def start(self, request: StartRequest) -> RunHandle: ...
    @abstractmethod
    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]: ...
    @abstractmethod
    async def cancel(self, handle: RunHandle) -> CancelResult: ...
    @abstractmethod
    async def resume(
        self, handle: RunHandle,
        target: ResumeTarget, payload: ResumePayload | None,
    ) -> RunHandle: ...
    @abstractmethod
    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor: ...
    @abstractmethod
    async def close(self, handle: RunHandle) -> None: ...
```

### 五条语义契约(签名/文档中明确,已友商核验)

1. **stream 返回结构化 `RuntimeEvent` 事件流**(对接 G0.2,含相位/工具/审批类型)。
   模型 = **事件流 + 独立命令/恢复通道**:审批回包走 `resume`/`submit`,不在事件流回写(非 duplex)。
2. **cancel 是状态机,返回 `CancelResult` 枚举**(不是 bool——Wegent 实测 bool 区分不了
   "记 pending" 和 "真 interrupt"):

   ```python
   class CancelResult(str, Enum):
       INTERRUPTED_ACTIVE_TURN = "interrupted_active_turn"   # 有活跃 turn,真 interrupt
       PENDING_CANCEL_RECORDED = "pending_cancel_recorded"   # 无活跃 turn,记 pending 下次消费
       NOT_RUNNING = "not_running"                           # 无可取消目标
       FAILED = "failed"                                     # 取消动作本身失败
   ```
   级联语义:成功 cancel 同时丢弃该 turn 的 pending 审批。
3. **resume 拆两个参数**(不混成 union——ADK 区分 invocation_id 目标 vs function response 回包;
   Codex 区分 resume_thread_id vs Op 回包):

   ```python
   class ResumeTarget(BaseModel):   # 恢复目标
       kind: Literal["invocation_id", "thread_id", "checkpoint_id"]
       id: str

   class ResumePayload(BaseModel):  # 回包内容,可为空(纯粹恢复续跑)
       kind: Literal["tool_result", "approval_decision", "hitl_answer", "free_text"]
       call_id: str | None = None    # 对应 approval.requested / tool.call 的 id
       data: Any = None
   ```
4. **checkpoint 粒度用 capability 声明**(不承诺所有 runtime 同等能力,诚实暴露 matrix):

   ```python
   class CheckpointCapability(BaseModel):
       supported: bool
       granularity: Literal["delta", "snapshot", "none"]
       rollback_scope: Literal["turn", "invocation", "none"]
       fork_supported: bool
       durable: bool
       shared_across_pods: bool
       reason: str = ""
   ```
   (与现有 `BaseRunner.describe_checkpoint_capability` 对齐演进。)
5. **start 带 session/tenant 维度**:

   ```python
   class StartRequest(BaseModel):
       input: Any                       # 用户消息/结构化输入
       user_id: str
       session_id: str
       agent_id: str | None = None
       model: str | None = None
       config: dict[str, Any] = {}
       metadata: dict[str, Any] = {}

   class RunHandle(BaseModel):        # 不透明句柄
       run_id: str                    # = invocation_id
       session_id: str
       runtime_type: str
       native_ref: dict[str, Any] = {}  # adapter 私有引用
   ```

### 交付物(goal-03 实现时)

`ksadk/runtime/adapter.py`(RuntimeAdapter/BaseRuntime/RuntimeRegistry + 上述类型)+
`tests/runners/test_adapter_interface.py`(签名级测试)。**本 goal 只冻结签名,不实现具体 adapter(那是 A4/A6)。**

---

## G0.1 create_runtime_app 结构

### 现状(实证)

`ksadk/server/app.py` 4722 行、47 条平铺路由;`app = FastAPI()`(L377)、`set_runner()`(L503)
+ `_resolve_active_runner()` 读全局 runner;模块级可变态 `_runner_loaded` + 4 个
`_DETACHED_*` 结构(L83-87,detached SSE / resume 映射,**cancel/resume 回归风险集中点**)。

### 目标结构

```python
def create_runtime_app(config: RuntimeAppConfig) -> FastAPI: ...

class RuntimeAppConfig(BaseModel):
    runner: BaseRunner                  # 依赖注入,替代 set_runner 全局态
    runtime_type: str = "local"
    route_groups: set[str] | None = None  # 默认全部;HarnessApp 只装数据面 group
    # session_service / event_store / 其他可插拔依赖…
```

### route group 拆分(APIRouter 按域)

| group | 路由 | 数据面/控制面 |
|---|---|---|
| `health_meta` | `/health`、`/list-apps`、`/v1/models`、静态 catch-all | 数据面 |
| `sessions` | `/agentengine/api/v1/{CreateSession,GetSession,ListSessions,DeleteSession,ListSessionEvents,ListSessionMessages,ListSessionCheckpoints}` | 数据面 |
| `sessions_adk_compat` | `/apps/**`(ADK web 兼容 8 条) | 数据面 |
| `run` | `/agentengine/api/v1/RunAgent`、`/run_sse`、`/agentengine/api/v1/SubscribeRunEvents` | 数据面 |
| `openai_compat` | `/v1/chat/completions`、`/v1/responses`、`/v1/models` | 数据面 |
| `workspace` | `/agentengine/api/v1/{Add,Delete,List}WorkspaceFile`、`GetWorkspaceFileContent`、`UploadFile`、`ExportWorkspaceZip`、`AttachmentContent`、`/agentengine/api/v1/ws/**` | 数据面 |
| `models` | `/agentengine/api/v1/ListAgentModels` | 数据面 |
| `feedback` | `/agentengine/api/v1/{Get,Upsert,Delete}ResponseFeedback` | 数据面 |
| `tools` | `/agentengine/api/v1/ListToolReceipts` | 数据面 |
| `ui_bootstrap` | `/agentengine/api/v1/GetAgentUiBootstrap` | 数据面 |
| `control`(cancel/resume) | `/agentengine/api/v1/{CancelRun,ResumeRun,GetCheckpointResumePreview}`、`/builder/app/{app_name}/cancel` | **控制面(不进 HarnessApp)** |
| `builder` | `/builder/app/{app_name}`、`/builder/save` | **控制面** |
| `debug` | `/debug/**`、`/traces` | **控制面** |

**装配纪律**(H2):A2A、Responses、session、files 等**数据面 group 可插拔装配进 HarnessApp;
控制面 action(cancel/resume/builder/debug)不进入 HarnessApp**。不 import 全局 `base_app` 再过滤复制路由。

### 关键回归纪律

- **去 `set_runner` 全局态**:runner 经 `config` 注入 `app.state.runner`,路由用 `Depends(get_runner)`。
- **`_DETACHED_*` 生命周期**:从模块级挪到 `app.state` 持有的 `StreamRegistry` 实例(per-app),
  绑定 app lifespan;**SSE/cancel/detached stream 行为必须与现状逐点一致**(重点回归:3352、3711、
  `_DetachedSSEStream.cancel` L230)。
- **薄兼容壳**:保留 `ksadk/server/app.py` 暴露 `app = create_runtime_app(...)` 与 `set_runner`
  deprecated shim,避免一次性改爆所有调用方(`run_server`、现有测试)。

### 交付物(goal-01 实现时)

`ksadk/server/factory.py`(create_runtime_app + RuntimeAppConfig)+ route group 模块拆分 +
`tests/server/test_app_factory.py`(同一 factory 产出普通/harness app 行为一致)+ 现有 `tests/server/` 全绿。

---

## 三样对齐检查

- G0.3 `stream` 返回类型 = G0.2 `RuntimeEvent` 事件流。✅
- G0.3 `CancelResult` 与现有 `request_cancel`("accepted/not_found/unsupported")的迁移映射,goal-03 实现时给出兼容层。✅
- G0.1 `control` group 的 CancelRun/ResumeRun 最终走 G0.3 `cancel`/`resume`(goal-05/06 接 A2A 时落地)。✅
- G0.3 与 A2A contract freeze(`a2a-center-productization-2026-07.md`)同期拍板。✅

---

## goal-15 试金石验收注记(2026-07-23)

第 4 框架 adapter(**miniflow**)在**零平台改动**下接入并通过与 adk/langgraph/codex 完全相同的
`tests/runners/test_adapter_contract.py` contract test(cancel 状态机 + resume union + 序列化 + 签名)。

**接口收敛证据**:
- `ResumeTarget.kind` 直接复用现有枚举(`checkpoint_id`),未为 miniflow 新增 kind。
- miniflow 的特有 checkpoint 细节经 **`framework_ref.miniflow.snapshot_id`** 承载——证明
  `framework_ref` 扩展点足以吸收框架差异,**不需要为它改冻结签名**。
- 接入仅需注册 `RuntimeRegistry`,未触碰 RuntimeEvent schema / RuntimeAdapter 抽象 / cancel 状态机。

结论:G0.3 签名对"未见过的第 4 框架"收敛成立,接口冻结有效。
