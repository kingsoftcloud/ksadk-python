"""RuntimeAdapter 平台接口 (goal-03 / G0.3 冻结稿)。

**只做签名与语义冻结,不实现具体 adapter**(具体 adapter 是后续 A4/A6,如 ADK/LangGraph
adapter)。本模块定义三层结构与六动词签名,供 Runtime 产生端、server 持久化、A2A
真实 cancel 等共同消费。

结构(H2 §4.2):

- :class:`BaseRuntime`:表达 Runtime **原生能力**(现有 ``BaseRunner`` 的演进目标,不推倒)。
- :class:`RuntimeAdapter`:把原生能力映射为**平台接口**(六动词)。
- :class:`RuntimeRegistry`:按 ``runtime_type`` 注册 adapter(替代 ``runners/factory.py``
  的 if/elif 分发)。

六动词语义契约(2026-07-21 友商代码核验,见 docstring 各处):

1. ``stream`` 返回 :class:`~ksadk.events.runtime_event.RuntimeEvent` 事件流(对接 G0.2),
   模型 = **事件流 + 独立命令/恢复通道**:审批回包走 ``resume``/``submit``,不在事件流
   回写(非 duplex stream)——codex/ADK/LangGraph 都是这个模型。
2. ``cancel`` 是状态机,返回 :class:`CancelResult` 枚举(不是 bool——Wegent 实测 bool
   区分不了"记 pending"和"真 interrupt")。
3. ``resume`` 拆 :class:`ResumeTarget`(恢复目标)与 :class:`ResumePayload`(回包)两个
   参数,不混成 union(ADK 区分 invocation_id 目标 vs function response 回包;Codex 区分
   resume_thread_id vs Op 回包)。
4. ``checkpoint`` 粒度用 :class:`CheckpointCapability` 声明(不承诺所有 runtime 同等能力,
   诚实暴露 capability matrix)。
5. ``start`` 带 session/tenant 维度(:class:`StartRequest` 的 ``user_id`` + ``session_id``),
   不只是 prompt。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import Enum
from typing import Any, AsyncIterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ksadk.events.runtime_event import RuntimeEvent
from ksadk.runtime.launch import RuntimeLaunchContext, RuntimeServices

# ---------------------------------------------------------------------------
# cancel 状态机
# ---------------------------------------------------------------------------


class CancelResult(str, Enum):
    """cancel 的结果状态机。

    Wegent ``cancel()`` 实测:无活跃 turn 记 pending 也返回 true、成功 interrupt 也返回
    true,**bool 区分不了两种结果**——因此必须用枚举显式区分。
    """

    INTERRUPTED_ACTIVE_TURN = "interrupted_active_turn"
    """有活跃 turn,已真实 interrupt。"""

    PENDING_CANCEL_RECORDED = "pending_cancel_recorded"
    """无活跃 turn,已记录 pending cancel,下一个 turn 开始时被消费。"""

    NOT_RUNNING = "not_running"
    """目标不在运行(无可取消的活跃/pending run)。"""

    FAILED = "failed"
    """取消动作本身失败(如底层 runtime 报错)。"""


class PauseResult(str, Enum):
    """Non-terminal pause capability result.

    Pause is deliberately separate from :class:`CancelResult`: an adapter must
    never claim a run is resumable after applying destructive cancel semantics.
    """

    PAUSED_ACTIVE_TURN = "paused_active_turn"
    NOT_SUPPORTED = "not_supported"
    NOT_RUNNING = "not_running"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# resume 目标与回包(两件拆开的的事)
# ---------------------------------------------------------------------------


class ResumeTarget(BaseModel):
    """恢复目标:回到哪里。

    - ``invocation_id``:精确到某次调用(ADK forward-only resume)。
    - ``thread_id``:某条会话线程(Codex resume_thread_id)。
    - ``checkpoint_id``:某个 checkpoint(LangGraph time-travel)。
    """

    kind: Literal["invocation_id", "thread_id", "checkpoint_id"]
    id: str


class ResumePayload(BaseModel):
    """恢复回包:带着什么输入恢复(可为空——纯粹续跑)。

    - ``tool_result``:工具执行结果(function response)。
    - ``approval_decision``:审批决定(对应 ``approval.requested`` 的回包,走命令通道)。
    - ``hitl_answer``:human-in-the-loop 回答。
    - ``free_text``:自由文本输入。
    """

    kind: Literal["tool_result", "approval_decision", "hitl_answer", "free_text"]
    call_id: Optional[str] = None
    """对应的 ``approval.requested`` / ``tool.call`` 的 id(如有)。"""
    data: Any = None


# ---------------------------------------------------------------------------
# checkpoint 能力声明
# ---------------------------------------------------------------------------


class CheckpointCapability(BaseModel):
    """checkpoint 粒度声明。诚实暴露,不承诺所有 runtime 同等能力。

    与现有 ``BaseRunner.describe_checkpoint_capability`` 对齐演进。
    """

    supported: bool
    granularity: Literal["delta", "snapshot", "none"]
    """delta(增量)vs snapshot(快照)vs none。"""
    rollback_scope: Literal["turn", "invocation", "none"]
    """可按 turn 还是 invocation 回滚。"""
    fork_supported: bool
    """是否支持从某 checkpoint fork 出新分支。"""
    durable: bool
    """是否持久化(跨进程/重启保留)。"""
    shared_across_pods: bool
    """是否跨 pod 共享(K8s 多副本可读同一 checkpoint)。"""
    reason: str = ""


class CheckpointDescriptor(BaseModel):
    """一次 checkpoint 的描述(checkpoint 动词的返回值)。"""

    checkpoint_id: str
    invocation_id: str
    capability: CheckpointCapability
    ref: dict[str, Any] = Field(default_factory=dict)
    """runtime 私有引用(如 LangGraph checkpoint 的 (thread_id, checkpoint_id) 元组)。"""


# ---------------------------------------------------------------------------
# start 请求与 run 句柄
# ---------------------------------------------------------------------------


CONVERSATION_PREPROCESSING_METADATA_KEY = "conversation_request"
"""StartRequest metadata key for the shared conversation preprocessing contract."""

RESUME_START_REQUEST_NATIVE_KEY = "_conversation_start_request"
"""Ephemeral adapter-private key carrying current request context across attach/resume."""


class ConversationPreprocessingRequest(BaseModel):
    """Transport-neutral input for the existing conversation preprocessing path.

    Protocol adapters keep their wire-specific fields out of the runner payload and
    place the canonical conversation inputs here.  ``RunnerRuntimeAdapter`` then
    reuses the same history, attachment, model-policy, platform-context, ambient
    KB/memory and tracing preparation as the normal RunAgent/Responses entrypoints.

    The model is additive and allows unknown fields so a newer protocol adapter can
    talk to an older runtime without losing forward-compatible metadata.
    """

    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]] = Field(default_factory=list)
    model_metadata: dict[str, Any] = Field(default_factory=dict)
    model_options: dict[str, Any] = Field(default_factory=dict)
    state_delta: dict[str, Any] = Field(default_factory=dict)
    instructions: Optional[str] = None
    request_metadata: dict[str, Any] = Field(default_factory=dict)
    custom_metadata: dict[str, Any] = Field(default_factory=dict)
    account_id: Optional[str] = None
    response_id: Optional[str] = None


class StartRequest(BaseModel):
    """start 的输入:带 session/tenant 维度,不只是 prompt。"""

    input: Any
    """用户消息或结构化输入。"""
    user_id: str
    session_id: str
    agent_id: Optional[str] = None
    model: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def conversation_preprocessing(self) -> Optional[ConversationPreprocessingRequest]:
        """Return the opt-in shared preprocessing request, if one was supplied."""

        raw = self.metadata.get(CONVERSATION_PREPROCESSING_METADATA_KEY)
        if raw is None:
            return None
        return ConversationPreprocessingRequest.model_validate(raw)


class RunHandle(BaseModel):
    """一次 run 的不透明句柄(start 返回,后续 stream/cancel/resume/checkpoint/close 用)。"""

    run_id: str
    """= invocation_id。"""
    session_id: str
    runtime_type: str
    native_ref: dict[str, Any] = Field(default_factory=dict)
    """adapter 私有引用(如 ADK 的 (app_name, user_id, session_id)、Codex 的 thread_ref)。"""


# ---------------------------------------------------------------------------
# BaseRuntime:Runtime 原生能力
# ---------------------------------------------------------------------------


class BaseRuntime(ABC):
    """Runtime 原生能力面(现有 ``BaseRunner`` 的演进目标,不推倒)。

    ``BaseRunner`` 的 ``request_cancel`` / ``describe_checkpoint_capability`` /
    ``invoke`` / ``stream`` 是原生能力的现状;``BaseRuntime`` 是其平台化抽象。
    具体 runner(ADK/LangGraph/...)以 ``BaseRuntime`` 表达原生能力,再由
    :class:`RuntimeAdapter` 映射为平台六动词。
    """

    runtime_type: str = "unknown"

    @abstractmethod
    def native_capabilities(self) -> dict[str, Any]:
        """原生能力声明(cancel / checkpoint / resume / session continuity 等)。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# RuntimeAdapter:平台六动词
# ---------------------------------------------------------------------------


class RuntimeAdapter(ABC):
    """把 :class:`BaseRuntime` 的原生能力映射为平台接口(六动词)。

    签名冻结:以下六个方法的名字、参数、返回类型在 v1 冻结,只允许 additive 演进
    (新增方法/可选参数),不允许改既有签名。
    """

    def __init__(self, runtime: BaseRuntime) -> None:
        self._runtime = runtime

    @property
    def runtime(self) -> BaseRuntime:
        return self._runtime

    async def preflight(self) -> None:
        """Validate that this adapter can accept a new run without creating one.

        This is deliberately an additive lifecycle hook rather than a seventh
        platform verb.  HTTP streaming routes use it before committing a 200
        response, so a lazy runner import or configuration failure is returned
        as a normal request error instead of a detached, half-open SSE stream.
        Implementations must not allocate a run handle or start model work.
        """

        return None

    @abstractmethod
    async def start(self, request: StartRequest) -> RunHandle:
        """启动一次 run,返回句柄。"""
        raise NotImplementedError

    @abstractmethod
    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        """订阅 run 的结构化事件流(对接 G0.2 RuntimeEvent)。

        注意:这是**事件流 + 独立命令/恢复通道**模型——审批回包/工具结果走
        :meth:`resume`,不在本事件流上回写(非 duplex stream)。
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, handle: RunHandle) -> CancelResult:
        """请求取消。返回状态机结果;成功 cancel 级联丢弃该 turn 的 pending 审批。"""
        raise NotImplementedError

    async def pause(self, handle: RunHandle) -> PauseResult:
        """Pause an active turn without invalidating its resumable state.

        This additive hook defaults to an honest unsupported result.  It must
        not fall back to ``cancel`` because cancellation is terminal for some
        runtimes (notably Codex).
        """

        return PauseResult.NOT_SUPPORTED

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        """Submit input to a live interaction without restarting the stream.

        Runtimes whose HITL model ends the current stream should continue to
        use :meth:`resume`; live JSON-RPC approval requests use this command
        channel instead.
        """

        raise RuntimeError(f"{type(self).__name__} does not support live interaction input")

    @abstractmethod
    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> RunHandle:
        """恢复 run。``target`` 是恢复目标,``payload`` 是回包(可为空)。"""
        raise NotImplementedError

    async def attach(self, handle: RunHandle) -> RunHandle:
        """Attach a persisted handle in this process before ``resume``/``stream``.

        A handle restored from durable task metadata is not evidence that the
        underlying runner exists in this process.  Adapters which support
        cross-process recovery must implement this seam using their framework's
        durable checkpoint/session API.  The default deliberately fails closed.
        """
        raise RuntimeError(
            f"{type(self).__name__} does not support attaching persisted run "
            f"{handle.run_id!r}; durable runtime restore is unavailable"
        )

    def is_handle_attached(self, handle: RunHandle) -> bool:
        """Return whether ``handle`` is already attached to this adapter process."""
        return False

    @abstractmethod
    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        """在当前位置创建 checkpoint,返回描述(粒度见 capability)。"""
        raise NotImplementedError

    @abstractmethod
    async def close(self, handle: RunHandle) -> None:
        """释放 run 持有的运行期资源。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# RuntimeRegistry
# ---------------------------------------------------------------------------


RuntimeAdapterFactory = Callable[[RuntimeLaunchContext], RuntimeAdapter]


class RuntimeRegistry:
    """按 ``runtime_type`` 注册/创建 :class:`RuntimeAdapter`。

    替代 ``runners/factory.py`` 的 if/elif 分发:新 runtime 通过 ``register``
    注册,不再改 factory 分支。
    """

    def __init__(self) -> None:
        self._factories: dict[str, RuntimeAdapterFactory] = {}

    def register(self, runtime_type: str, factory: RuntimeAdapterFactory) -> None:
        if not isinstance(runtime_type, str) or not runtime_type.strip():
            raise ValueError("runtime type must be a non-empty string")
        key = runtime_type.strip().lower()
        if key in self._factories:
            raise ValueError(f"duplicate runtime type: {runtime_type!r}")
        if not callable(factory):
            raise TypeError(f"runtime factory must be callable: {factory!r}")
        self._factories[key] = factory

    def get(self, runtime_type: str) -> RuntimeAdapterFactory:
        key = runtime_type.strip().lower()
        try:
            return self._factories[key]
        except KeyError:
            raise KeyError(
                f"missing runtime type: {runtime_type!r}; registered: {sorted(self._factories)}"
            ) from None

    def create(self, context: RuntimeLaunchContext) -> RuntimeAdapter:
        """使用不可变启动上下文创建一个新的 Adapter 实例。"""

        adapter = self.get(context.runtime_type)(context)
        if not isinstance(adapter, RuntimeAdapter):
            raise TypeError(
                f"runtime factory must return RuntimeAdapter, got {type(adapter).__name__}"
            )
        return adapter

    def registered_types(self) -> list[str]:
        return sorted(self._factories)


__all__ = [
    "BaseRuntime",
    "CancelResult",
    "PauseResult",
    "CheckpointCapability",
    "CheckpointDescriptor",
    "ResumePayload",
    "ResumeTarget",
    "RunHandle",
    "RuntimeAdapter",
    "RuntimeAdapterFactory",
    "RuntimeLaunchContext",
    "RuntimeRegistry",
    "RuntimeServices",
    "StartRequest",
]
