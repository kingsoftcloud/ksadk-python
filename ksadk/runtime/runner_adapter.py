"""RunnerRuntimeAdapter — 把现有 ``BaseRunner`` 对齐到 G0.3 ``RuntimeAdapter`` (goal-07)。

不推倒现有 runner(ADK/LangGraph 的 stream/invoke 已在跑),在它们之上实现平台六动词。
**cancel 不再是空壳**:实现冻结的 cancel 状态机——活跃 turn interrupt(关闭流)/
无活跃 turn 记 pending / **级联丢弃 pending 工具审批** / 返回 :class:`CancelResult`。

框架差异(resume 目标、checkpoint 粒度)由子类钩子 ``_resume_native`` 与
``_checkpoint_capability`` 诚实声明(ADK forward-only vs LangGraph time-travel)。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, cast

from pydantic import JsonValue

from ksadk.events.canonical import (
    ApprovalRequest,
    InteractionRequested,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunInterrupted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
    stable_scope_id,
)
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import (
    RESUME_START_REQUEST_NATIVE_KEY,
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.runtime.preprocessing import PreparedRuntimeStart, prepare_runtime_start
from ksadk.runtime.runner_loading import ensure_runner_loaded

logger = logging.getLogger(__name__)

# 保留既有 monkeypatch patch 点:stream_mapping 在调用时经本模块属性解析。
from ksadk.conversations.runtime_observability import (  # noqa: E402,F401
    _conversation_span_scope,
)

# dict-chunk 退化路径的流竞速/事件映射实现拆至 _runner_adapter 子包(纯移动,行为不变)。
from ksadk.runtime._runner_adapter.stream_mapping import (  # noqa: E402
    _STREAM_STOP,
    _RunnerStreamMappingMixin,
)

_ResumeKey = tuple[str, str, str, str, str]


def _coerce_literal(value: Any, allowed: tuple[str, ...], default: str) -> Any:
    """把 value 收窄到 allowed 之一(不在则回退 default),供 Literal 字段使用。"""
    return value if value in allowed else default


class _RunnerAsBaseRuntime(BaseRuntime):
    """把 ``BaseRunner`` 包装为 G0.3 ``BaseRuntime``(原生能力面)。"""

    def __init__(self, runner: BaseRunner, runtime_type: str) -> None:
        self._runner = runner
        self.runtime_type = runtime_type

    @property
    def runner(self) -> BaseRunner:
        return self._runner

    def native_capabilities(self) -> dict[str, Any]:
        caps = getattr(self._runner, "get_runtime_capabilities", None)
        if callable(caps):
            try:
                return dict(caps())
            except Exception:  # noqa: BLE001
                pass
        return {"Framework": self.runtime_type}


@dataclass
class _ActiveRun:
    """一次进行中的 run(adapter 用于 cancel 追踪)。"""

    invocation_id: str
    session_id: str
    stream: Optional[AsyncIterator[RuntimeEvent]] = None
    pending_approvals: set[str] = field(default_factory=set)
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancellation_ack: asyncio.Event = field(default_factory=asyncio.Event)
    chunk_task: Optional[asyncio.Task[Any]] = None
    resume_key: Optional[_ResumeKey] = None
    resume_fingerprint: Any = None
    skip_runner: bool = False
    done: bool = False
    completion_metrics: dict[str, Any] = field(default_factory=dict)
    # dict-chunk 退化路径:追踪已 ItemStarted 的 item key,避免重复发 Started。
    started_items: set[tuple[str, str]] = field(default_factory=set)
    # dict-chunk 退化路径:final_answer message 的 item_id,供 RunCompleted.output_refs 引用。
    final_answer_item_id: Optional[str] = None


class RunnerRuntimeAdapter(_RunnerStreamMappingMixin, RuntimeAdapter):
    """把 ``BaseRunner`` 映射为平台六动词的通用 adapter。"""

    def __init__(self, runner: BaseRunner, *, runtime_type: str) -> None:
        super().__init__(_RunnerAsBaseRuntime(runner, runtime_type))
        self._runner = runner
        self._runtime_type = runtime_type
        self._active_runs: dict[str, _ActiveRun] = {}
        self._known_runs: set[str] = set()
        self._run_sessions: dict[str, str] = {}
        self._pending_cancels: set[str] = set()
        self._resume_decisions: dict[_ResumeKey, Any] = {}
        self._consumed_resumes: set[_ResumeKey] = set()
        self._seq = 0
        # 可观测:最近一次 cancel 级联丢弃的 pending 审批集(contract test 断言用)。
        self.last_cancel_dropped_approvals: set[str] = set()

    # ---- 框架钩子(子类按需 override) ----

    def capabilities(self) -> RuntimeCapabilityMatrix:
        """诚实矩阵:cancel 经 asyncio 任务打断(emulated,过 conformance);
        resume/checkpoint 依赖 runner 声明的原生 checkpoint;attach/durable_restore
        依赖 ``attach_runtime_handle`` seam 与跨进程持久化,内存表不算数。
        """

        def _unavailable(reason: str) -> RuntimeCapability:
            return RuntimeCapability(supported=False, mode="unavailable", reason=reason)

        checkpoint_capability = self._checkpoint_capability()
        attach_seam = callable(getattr(self._runner, "attach_runtime_handle", None))
        checkpoint_supported = bool(checkpoint_capability.supported)
        durable_supported = bool(
            checkpoint_capability.durable
            and checkpoint_capability.shared_across_pods
            and attach_seam
        )
        return RuntimeCapabilityMatrix(
            cancel=RuntimeCapability(
                supported=True,
                mode="emulated",
                reason="runner_stream_task_interrupt",
            ),
            pause=_unavailable("runtime_no_native_pause"),
            resume=(
                RuntimeCapability(supported=True, mode="native")
                if checkpoint_supported
                else _unavailable("runtime_no_native_checkpoint")
            ),
            submit_interaction=_unavailable("runtime_no_live_interaction_channel"),
            attach=(
                RuntimeCapability(supported=True, mode="native")
                if attach_seam
                else _unavailable("runner_no_durable_attach_seam")
            ),
            steer=_unavailable("runtime_no_native_steer"),
            inject=_unavailable("runtime_no_native_inject"),
            checkpoint=(
                RuntimeCapability(supported=True, mode="native")
                if checkpoint_supported
                else _unavailable("runtime_no_native_checkpoint")
            ),
            durable_restore=(
                RuntimeCapability(supported=True, mode="native")
                if durable_supported
                else _unavailable("durable_restore_requires_cross_process_checkpoint")
            ),
            # A generic Runner cannot claim interaction delivery merely from a
            # checkpoint capability.  ADK is forward-only and only the
            # LangGraph specialization below binds a checkpoint to the
            # original interrupt identity.
            interaction_mode="unavailable",
        )

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        if not self.capabilities().durable_restore.supported:
            raise UnsupportedControlError(
                f"{self._runtime_type} has no cross-process checkpoint backend for run "
                f"{handle.run_id!r}"
            )
        return await self.attach(handle)

    def _checkpoint_capability(self) -> CheckpointCapability:
        """诚实暴露 checkpoint 粒度。默认读 runner.describe_checkpoint_capability。"""
        raw = {}
        describe = getattr(self._runner, "describe_checkpoint_capability", None)
        if callable(describe):
            try:
                raw = dict(describe())
            except Exception:  # noqa: BLE001
                raw = {}
        supported = bool(raw.get("Supported"))
        granularity = _coerce_literal(
            raw.get("Granularity"),
            ("delta", "snapshot", "none"),
            "snapshot" if supported else "none",
        )
        rollback_scope = _coerce_literal(
            raw.get("RollbackScope"),
            ("turn", "invocation", "none"),
            "invocation" if supported else "none",
        )
        return CheckpointCapability(
            supported=supported,
            granularity=granularity,
            rollback_scope=rollback_scope,
            fork_supported=bool(raw.get("ForkSupported", False)),
            durable=bool(raw.get("Durable", False)),
            shared_across_pods=bool(raw.get("SharedAcrossPods", False)),
            reason=str(raw.get("Reason") or ""),
        )

    async def _resume_native(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> Optional[dict]:
        """框架原生 resume:返回注入下一次 ``stream()`` 的 runner_input 覆盖。

        返回的 dict 会被 :meth:`_build_runner_input` 合并,以框架 runner 真正消费的
        ``checkpoint_resume`` + ``framework_ref`` 形式驱动真实恢复(而非仅写
        ``native_ref`` 摆设)。默认通用形式;ADK/LangGraph 子类覆盖为各自框架结构。
        返回 ``None`` 表示本框架无可注入的 resume 输入(退化为普通 stream)。
        """
        return {
            "checkpoint_resume": True,
            "run_id": target.id,
            "framework_ref": {self._runtime_type: {f"{target.kind}": target.id}},
            "input": payload.data if payload else None,
        }

    # ---- 六动词 ----

    async def preflight(self) -> None:
        """Load the framework agent before a streaming HTTP response commits."""

        ensure_runner_loaded(self._runner, runtime_type=self._runtime_type)

    async def start(self, request: StartRequest) -> RunHandle:
        ensure_runner_loaded(self._runner, runtime_type=self._runtime_type)
        run_id = str(request.metadata.get("invocation_id") or self._next_invocation_id())
        prepared_start = await prepare_runtime_start(request, self._runner)
        handle = RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type=self._runtime_type,
            native_ref={"user_id": request.user_id, "agent_id": request.agent_id},
        )
        self._known_runs.add(run_id)
        self._run_sessions[run_id] = request.session_id
        self._active_runs[run_id] = _ActiveRun(invocation_id=run_id, session_id=request.session_id)
        # 暂存 start 输入,供 stream() 使用。
        self._active_runs[run_id].__dict__["_start_request"] = request
        if prepared_start is not None:
            self._active_runs[run_id].__dict__["_prepared_start"] = prepared_start
        return handle

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        return self._stream_events(handle)

    def is_handle_attached(self, handle: RunHandle) -> bool:
        return (
            handle.runtime_type == self._runtime_type
            and handle.run_id in self._known_runs
            and self._run_sessions.get(handle.run_id) == handle.session_id
        )

    async def attach(self, handle: RunHandle) -> RunHandle:
        """Restore a persisted handle through an explicit native runner seam.

        Merely repopulating ``_known_runs`` would make validation pass without
        restoring framework state.  A runner therefore has to expose
        ``attach_runtime_handle(handle)`` and prove that its durable backend can
        resolve the referenced session/checkpoint.
        """
        if self.is_handle_attached(handle):
            return handle
        if handle.runtime_type != self._runtime_type:
            raise ValueError(
                f"run handle runtime {handle.runtime_type!r} does not match "
                f"adapter {self._runtime_type!r}"
            )
        attach = getattr(self._runner, "attach_runtime_handle", None)
        if not callable(attach):
            raise UnsupportedControlError(
                f"runner for {self._runtime_type!r} has no durable "
                "attach_runtime_handle capability"
            )
        restored = attach(handle)
        if inspect.isawaitable(restored):
            restored = await restored
        if restored is False or restored is None:
            raise RuntimeError(
                f"runner could not restore persisted run {handle.run_id!r} "
                f"for session {handle.session_id!r}"
            )
        if isinstance(restored, RunHandle) and restored != handle:
            raise ValueError("native runner attached a different run handle")
        self._known_runs.add(handle.run_id)
        self._run_sessions[handle.run_id] = handle.session_id
        return handle

    async def cancel(self, handle: RunHandle) -> CancelResult:
        run_id = handle.run_id
        run = self._active_runs.get(run_id)
        # 活跃 turn = 正在 streaming(run.stream 非 None 且未 done);
        # start 过但未 streaming / 已结束 → 无活跃 turn(记 pending 或 not_running)。
        is_active = run is not None and not run.done and run.stream is not None
        if not is_active:
            if run_id in self._known_runs:
                # 无活跃 turn 但 invocation 已知 → 记 pending,下个 turn 消费。
                self._pending_cancels.add(run_id)
                return CancelResult.PENDING_CANCEL_RECORDED
            return CancelResult.NOT_RUNNING
        # 有活跃 turn:interrupt + 级联丢弃 pending 审批。
        assert run is not None  # is_active 蕴含 run 非 None
        try:
            await self._interrupt_active_run(run)
            # 级联丢弃该 turn 的 pending 工具审批(先快照供观测)。
            self.last_cancel_dropped_approvals = set(run.pending_approvals)
            run.pending_approvals.clear()
            run.done = True
            self._active_runs.pop(run_id, None)
            self._pending_cancels.discard(run_id)
            return CancelResult.INTERRUPTED_ACTIVE_TURN
        except Exception:  # noqa: BLE001
            logger.exception("cancel active run %s 失败", run_id)
            return CancelResult.FAILED

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> RunHandle:
        if (
            handle.runtime_type != self._runtime_type
            or handle.run_id not in self._known_runs
            or self._run_sessions.get(handle.run_id) != handle.session_id
        ):
            raise ValueError(f"unknown run handle for {self._runtime_type}: {handle.run_id!r}")
        if handle.run_id in self._pending_cancels:
            return handle
        self._require_native_checkpoint_capability()
        self._known_runs.add(handle.run_id)
        prepared_start: PreparedRuntimeStart | None = None
        raw_start_request = handle.native_ref.pop(RESUME_START_REQUEST_NATIVE_KEY, None)
        if isinstance(raw_start_request, Mapping):
            prepared_start = await prepare_runtime_start(
                StartRequest.model_validate(raw_start_request), self._runner
            )
        # _resume_native 返回的 runner_input 覆盖存到 run 上,下一次 stream() 经
        # _build_runner_input 消费,以框架原生方式真驱动恢复。
        override = await self._resume_native(handle, target, payload)
        resume_key = (
            handle.run_id,
            handle.session_id,
            target.kind,
            target.id,
            str(payload.call_id or "") if payload is not None else "",
        )
        resume_fingerprint = (
            (payload.kind, copy.deepcopy(payload.data)) if payload is not None else None
        )
        existing_fingerprint = self._resume_decisions.get(resume_key, _STREAM_STOP)
        if existing_fingerprint is not _STREAM_STOP:
            if existing_fingerprint != resume_fingerprint:
                raise ValueError(f"conflicting resume for checkpoint {target.id!r}")
            current = self._active_runs.get(handle.run_id)
            if current is not None and not current.done and current.resume_key == resume_key:
                return handle
            if resume_key in self._consumed_resumes:
                self._active_runs[handle.run_id] = _ActiveRun(
                    invocation_id=handle.run_id,
                    session_id=handle.session_id,
                    resume_key=resume_key,
                    resume_fingerprint=resume_fingerprint,
                    skip_runner=True,
                )
                return handle

        current = self._active_runs.get(handle.run_id)
        if current is not None and not current.done and current.resume_key is not None:
            if current.resume_key == resume_key:
                if current.resume_fingerprint == resume_fingerprint:
                    return handle
                raise ValueError(f"conflicting resume for checkpoint {target.id!r}")
            raise ValueError(f"run {handle.run_id!r} already has an active or pending resume")

        run = _ActiveRun(
            invocation_id=handle.run_id,
            session_id=handle.session_id,
            resume_key=resume_key,
            resume_fingerprint=resume_fingerprint,
        )
        if prepared_start is not None:
            run.__dict__["_prepared_start"] = prepared_start
        if override:
            run.__dict__["_resume_input_override"] = override
        self._resume_decisions[resume_key] = resume_fingerprint
        self._active_runs[handle.run_id] = run
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        capability = self._require_native_checkpoint_capability()
        checkpoint_id = str(handle.native_ref.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            raise UnsupportedControlError(
                f"{self._runtime_type} runner has no native checkpoint for run "
                f"{handle.run_id!r}"
            )
        return CheckpointDescriptor(
            checkpoint_id=checkpoint_id,
            invocation_id=handle.run_id,
            capability=capability,
            ref=dict(handle.native_ref),
        )

    def _require_native_checkpoint_capability(self) -> CheckpointCapability:
        """Fail closed when a runner cannot create or resume a native checkpoint."""
        capability = self._checkpoint_capability()
        if capability.supported:
            return capability
        detail = capability.reason or "runner does not expose framework checkpoints"
        raise UnsupportedControlError(
            f"{self._runtime_type} native checkpoint capability is unavailable: {detail}"
        )

    async def close(self, handle: RunHandle) -> None:
        run = self._active_runs.pop(handle.run_id, None)
        if run is not None:
            await self._interrupt_active_run(run)
            run.done = True
        resume_keys = {
            key
            for key in self._resume_decisions
            if key[0] == handle.run_id and key[1] == handle.session_id
        }
        for key in resume_keys:
            self._resume_decisions.pop(key, None)
        self._consumed_resumes.difference_update(resume_keys)
        self._pending_cancels.discard(handle.run_id)
        self._known_runs.discard(handle.run_id)
        self._run_sessions.pop(handle.run_id, None)
        close = getattr(self._runner, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    # ---- 内部:cancel 中断 ----

    async def _interrupt_active_run(self, run: _ActiveRun) -> None:
        """中断活跃 turn:置 interrupt 事件,由 stream 消费侧的竞速循环取消在途 chunk task。

        机制链(经实证,见 test_adapter_contract.test_cancel_active_turn_returns_interrupted):
        ``interrupt_event.set()`` → :meth:`_map_runner_stream` 的 ``asyncio.wait`` 竞速命中 →
        ``chunk_task.cancel()`` → ``CancelledError`` 传入 runner 生成器正在 in-flight 的
        ``__anext__`` await(ADK ``run_async`` 的 LLM/tool 调用就在其中),从而真实打断执行,
        而非仅在事件间停止消费。

        前提与边界(诚实声明):ADK / LangGraph 均**不暴露原生 cancel 方法**(Runner 只有
        run/run_async/close 等),因此 asyncio 任务取消是**唯一且正确**的中断机制。它要求
        runner 的执行发生在其 stream async generator 的 ``__anext__`` 内(ADK/LG 流式即如此);
        若某 runner 把实际工作放到与 ``__anext__`` 解耦的后台 task,则不在本机制覆盖范围。
        不在此跨 task 对正被另一 task 迭代的 async generator 直接 aclose(不可靠)。
        """
        run.interrupt_event.set()
        if run.stream is None:
            run.cancellation_ack.set()
            return
        if run.chunk_task is not None:
            run.chunk_task.cancel()
        await asyncio.wait_for(run.cancellation_ack.wait(), timeout=2)

    # ---- 内部:stream → RuntimeEvent ----

    def _next_invocation_id(self) -> str:
        self._seq += 1
        return f"inv_{self._runtime_type}_{self._seq}"

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _stream_events(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        run = self._active_runs.get(handle.run_id)
        if run is None:
            run = _ActiveRun(invocation_id=handle.run_id, session_id=handle.session_id)
            self._active_runs[handle.run_id] = run

        # 消费 pending cancel:start 时若已记 pending,立即中断该 turn。
        if handle.run_id in self._pending_cancels:
            self._pending_cancels.discard(handle.run_id)
            yield self._make_run_canceled(handle, reason=CancelResult.PENDING_CANCEL_RECORDED.value)
            return

        if run.skip_runner:
            run.done = True
            self._active_runs.pop(handle.run_id, None)
            yield self._make_run_completed(handle)
            return

        if run.resume_key is not None:
            self._consumed_resumes.add(run.resume_key)

        yield self._make_run_started(handle)

        request = run.__dict__.get("_start_request")
        runner_input = self._build_runner_input(handle, request)
        gen: Optional[AsyncIterator[RuntimeEvent]] = None
        terminal_event_seen = False
        approval_interrupted = False
        try:
            gen = self._map_runner_stream(handle, runner_input)
            run.stream = gen
            async for event in gen:
                yield event
                if event.event_type == "interaction.requested":
                    approval_interrupted = True
                    # Track pending approval for canonical streams that bypass
                    # _chunk_to_event (e.g. stream_canonical_events).
                    if isinstance(event, InteractionRequested):
                        call_id = str(event.interaction_id or "")
                        if call_id:
                            run.pending_approvals.add(call_id)
                            pending_ids = handle.native_ref.setdefault("pending_approval_ids", [])
                            if call_id not in pending_ids:
                                pending_ids.append(call_id)
                if event.event_type in {
                    "run.completed",
                    "run.failed",
                    "run.canceled",
                    "run.interrupted",
                }:
                    terminal_event_seen = True
                if event.event_type in {"run.failed", "run.canceled"}:
                    return

            if run.interrupt_event.is_set() and not terminal_event_seen:
                yield self._make_run_canceled(
                    handle, reason=CancelResult.INTERRUPTED_ACTIVE_TURN.value
                )
            elif approval_interrupted and not terminal_event_seen:
                yield self._make_run_interrupted(handle, reason="input_required")
            elif not terminal_event_seen:
                yield self._make_run_completed(handle, run=run, metrics=run.completion_metrics)
        finally:
            run.stream = None
            run.done = True
            self._active_runs.pop(handle.run_id, None)

    def _build_runner_input(self, handle: RunHandle, request: Optional[StartRequest]) -> dict:
        # resume 覆盖优先:_resume_native 注入的 checkpoint_resume + framework_ref
        # 直接作为 runner 输入,驱动框架原生恢复。
        run = self._active_runs.get(handle.run_id)
        override = run.__dict__.get("_resume_input_override") if run is not None else None
        prepared_start = run.__dict__.get("_prepared_start") if run is not None else None
        if override is not None:
            merged = (
                dict(prepared_start.runner_input)
                if isinstance(prepared_start, PreparedRuntimeStart)
                else {}
            )
            base_metadata = merged.get("metadata")
            merged.update(
                {
                    "input": override.get("input"),
                    "session_id": handle.session_id,
                    "invocation_id": handle.run_id,
                    "metadata": {
                        **(dict(base_metadata) if isinstance(base_metadata, Mapping) else {}),
                        **dict(override.get("metadata") or {}),
                    },
                }
            )
            for key, value in override.items():
                if key not in ("input", "metadata"):
                    merged[key] = value
            return merged

        if isinstance(prepared_start, PreparedRuntimeStart):
            return dict(prepared_start.runner_input)

        metadata: Dict[str, Any] = {}
        request_config: Dict[str, Any] = {}
        input_value: Any = ""
        if request is not None:
            input_value = request.input
            metadata = dict(request.metadata or {})
            request_config = dict(request.config or {})
        return {
            **request_config,
            "input": input_value,
            "session_id": handle.session_id,
            "invocation_id": handle.run_id,
            "metadata": metadata,
        }

    # ---- canonical event construction helpers ----

    def _interaction_requested_from_approval(
        self,
        handle: RunHandle,
        run: Optional[_ActiveRun],
        *,
        detail: Mapping[str, Any],
        call_id: str,
    ) -> list[RuntimeEvent]:
        """把 ToolGateway 结果中携带的审批请求转为 canonical InteractionRequested。"""

        approval_id = str(detail.get("approval_request_id") or detail.get("id") or call_id or "")
        resolved_call_id = str(call_id or approval_id)
        if run is not None and approval_id:
            run.pending_approvals.add(approval_id)
        if approval_id:
            pending_approval_ids = handle.native_ref.setdefault("pending_approval_ids", [])
            if approval_id not in pending_approval_ids:
                pending_approval_ids.append(approval_id)
        framework = self._runtime_type
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        interaction_id = resolved_call_id or stable_item_id(framework, run_id, "interaction")
        item_id = stable_item_id(framework, run_id, "interaction")
        detail_value: JsonValue = (
            cast(JsonValue, dict(detail)) if isinstance(detail, Mapping) else None
        )
        return [
            InteractionRequested(
                **self._canonical_kwargs(
                    handle,
                    scope_id=scope_id,
                    item_id=item_id,
                    event_type="interaction.requested",
                    part_id="interaction",
                ),
                interaction_id=interaction_id,
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id=resolved_call_id or None,
                    kind="tool",
                    detail=detail_value,
                ),
            )
        ]

    def _make_source(self, handle: RunHandle, *, protocol: str | None = None) -> SourceRef:
        # SourceRef.framework 是封闭枚举;测试 fixture 或自定义 runtime_type 落到
        # 通用 "ksadk",原生框架名原样保留。
        framework = self._runtime_type
        if framework not in {"adk", "langgraph", "codex", "a2a", "ksadk"}:
            framework = "ksadk"
        return SourceRef(
            framework=framework,
            protocol=protocol,
            native_run_id=handle.run_id,
            metadata={
                "agent_id": str(handle.native_ref.get("agent_id") or "agent"),
                "user_id": str(handle.native_ref.get("user_id") or "user"),
                "session_id": handle.session_id,
                "invocation_id": handle.run_id,
            },
        )

    def _canonical_kwargs(
        self,
        handle: RunHandle,
        *,
        scope_id: str,
        item_id: str,
        event_type: str,
        part_id: str,
    ) -> dict[str, Any]:
        """Build common EventEnvelope kwargs for the dict-chunk degraded path."""
        framework = self._runtime_type
        run_id = handle.run_id
        # TODO(runtime-event-v2): dict chunk 退化路径,chunk_ordinal 用 seq counter;
        # LangGraph/Codex 切 stream_canonical_events 后清理
        n = self._next_seq()
        return {
            "schema_version": 2,
            "event_id": stable_event_id(
                framework, scope_id, item_id, event_type, part_id, run_id, n
            ),
            "seq": n,
            "timestamp": time.time(),
            "run_id": run_id,
            "scope_id": scope_id,
            "source": self._make_source(handle),
        }

    def _make_run_started(self, handle: RunHandle) -> RunStarted:
        framework = self._runtime_type
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        item_id = stable_item_id(framework, run_id, "$run")
        return RunStarted(
            schema_version=2,
            event_id=stable_event_id(framework, scope_id, item_id, "run.started", "run", run_id, 0),
            seq=self._next_seq(),
            timestamp=time.time(),
            run_id=run_id,
            scope_id=scope_id,
            source=self._make_source(handle),
            status="running",
        )

    def _make_run_completed(
        self,
        handle: RunHandle,
        *,
        run: Optional[_ActiveRun] = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> RunCompleted:
        framework = self._runtime_type
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        item_id = stable_item_id(framework, run_id, "$run")
        output_refs: tuple[OutputRef, ...] = ()
        if run is not None and run.final_answer_item_id:
            output_refs = (
                OutputRef(
                    scope_id=scope_id,
                    item_id=run.final_answer_item_id,
                    part_id="text-0",
                ),
            )
        source = self._make_source(handle)
        if metrics:
            source = source.model_copy(
                update={"metadata": {**source.metadata, "metrics": dict(metrics)}}
            )
        return RunCompleted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, item_id, "run.completed", "run", run_id, 0
            ),
            seq=self._next_seq(),
            timestamp=time.time(),
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            status="completed",
            output_refs=output_refs,
        )

    def _make_run_canceled(self, handle: RunHandle, *, reason: str | None = None) -> RunCanceled:
        framework = self._runtime_type
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        item_id = stable_item_id(framework, run_id, "$run")
        return RunCanceled(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, item_id, "run.canceled", "run", run_id, 0
            ),
            seq=self._next_seq(),
            timestamp=time.time(),
            run_id=run_id,
            scope_id=scope_id,
            source=self._make_source(handle),
            status="canceled",
            reason=reason,
        )

    def _make_run_interrupted(
        self, handle: RunHandle, *, reason: str | None = None
    ) -> RunInterrupted:
        framework = self._runtime_type
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        item_id = stable_item_id(framework, run_id, "$run")
        return RunInterrupted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, item_id, "run.interrupted", "run", run_id, 0
            ),
            seq=self._next_seq(),
            timestamp=time.time(),
            run_id=run_id,
            scope_id=scope_id,
            source=self._make_source(handle),
            status="interrupted",
            reason=reason,
        )

    @staticmethod
    def _coerce(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)


__all__ = ["RunnerRuntimeAdapter", "_RunnerAsBaseRuntime"]
