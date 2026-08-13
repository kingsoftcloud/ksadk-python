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
import json
import logging
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, cast

from ksadk.conversations.runtime_input import _runner_name
from ksadk.conversations.runtime_observability import (
    _conversation_span_scope,
    _set_conversation_input_attributes,
    _set_conversation_output_attributes,
    _set_conversation_span_attributes,
    _set_conversation_usage_attributes,
)
from ksadk.events.runtime_event import EventType, RuntimeEvent
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
from ksadk.runtime_context import platform_invocation_scope
from ksadk.tools.gateway import approval_interrupt_info_from_result

logger = logging.getLogger(__name__)

_STREAM_STOP = object()
_ResumeKey = tuple[str, str, str, str, str]


async def _anext_or_stop(gen: AsyncIterator[Any]) -> Any:
    """取下一个 chunk;流结束返回 _STREAM_STOP sentinel(便于竞速)。"""
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return _STREAM_STOP


def _a2ui_surface_event(chunk: Any) -> tuple[str, dict[str, Any]] | None:
    """Recognize a validated A2UI tool envelope and make it a first-class event.

    The dynamic ``generate_a2ui`` tool returns official v0.9 operations as a
    JSON tool result. Tool results are otherwise opaque to the runtime, which
    would leave AG-UI with nothing to project until a page reload reconstructs
    history. Convert exactly that envelope at the runtime boundary so it is
    streamed, persisted, and replayed like every other A2UI surface.
    """

    if not isinstance(chunk, dict):
        return None
    value = chunk.get("tool_output", chunk.get("output"))
    if value is not None and hasattr(value, "content"):
        value = value.content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, Mapping):
        return None
    operations_raw = value.get("a2ui_operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        return None
    operations = [dict(operation) for operation in operations_raw if isinstance(operation, Mapping)]
    if not operations:
        return None

    known: list[tuple[str, str]] = []
    for operation in operations:
        for key, event_type in (
            ("createSurface", EventType.A2UI_SURFACE_BEGIN),
            ("updateComponents", EventType.A2UI_SURFACE_UPDATE),
            ("updateDataModel", EventType.A2UI_SURFACE_UPDATE),
            ("deleteSurface", EventType.A2UI_SURFACE_END),
        ):
            detail = operation.get(key)
            if isinstance(detail, Mapping) and isinstance(detail.get("surfaceId"), str):
                surface_id = detail["surfaceId"].strip()
                if surface_id:
                    known.append((surface_id, event_type))
                    break
    if not known:
        return None
    surface_ids = {surface_id for surface_id, _event_type in known}
    if len(surface_ids) != 1:
        logger.warning("ignoring A2UI tool result with multiple surfaces")
        return None
    surface_id = known[0][0]
    event_type = (
        EventType.A2UI_SURFACE_BEGIN
        if any(kind == EventType.A2UI_SURFACE_BEGIN for _surface_id, kind in known)
        else known[0][1]
    )
    return event_type, {"surface_id": surface_id, "operations": operations}


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


class RunnerRuntimeAdapter(RuntimeAdapter):
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
            raise RuntimeError(
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
            raise RuntimeError(
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
        raise RuntimeError(
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
            yield self._event(
                handle,
                EventType.RUN_CANCELED,
                {
                    "status": "cancelled",
                    "cancel_result": CancelResult.PENDING_CANCEL_RECORDED.value,
                },
            )
            return

        if run.skip_runner:
            run.done = True
            self._active_runs.pop(handle.run_id, None)
            yield self._event(
                handle,
                EventType.RUN_COMPLETED,
                self._completion_payload(handle, status="already_resumed"),
            )
            return

        if run.resume_key is not None:
            self._consumed_resumes.add(run.resume_key)

        yield self._event(handle, EventType.RUN_STARTED, {"status": "in_progress"})

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
                if event.event_type == EventType.APPROVAL_REQUESTED:
                    approval_interrupted = True
                if event.event_type in {
                    EventType.RUN_COMPLETED,
                    EventType.RUN_FAILED,
                    EventType.RUN_CANCELED,
                    EventType.RUN_INTERRUPTED,
                }:
                    terminal_event_seen = True
                if event.event_type in {EventType.RUN_FAILED, EventType.RUN_CANCELED}:
                    return

            if run.interrupt_event.is_set() and not terminal_event_seen:
                yield self._event(
                    handle,
                    EventType.RUN_CANCELED,
                    {
                        "status": "cancelled",
                        "cancel_result": CancelResult.INTERRUPTED_ACTIVE_TURN.value,
                    },
                )
            elif approval_interrupted and not terminal_event_seen:
                yield self._event(
                    handle,
                    EventType.RUN_INTERRUPTED,
                    {"status": "input_required"},
                )
            elif not terminal_event_seen:
                yield self._event(
                    handle,
                    EventType.RUN_COMPLETED,
                    self._completion_payload(
                        handle,
                        status="completed",
                        metrics=run.completion_metrics,
                    ),
                )
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
            merged.update({
                "input": override.get("input"),
                "session_id": handle.session_id,
                "invocation_id": handle.run_id,
                "metadata": {
                    **(dict(base_metadata) if isinstance(base_metadata, Mapping) else {}),
                    **dict(override.get("metadata") or {}),
                },
            })
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

    async def _map_runner_stream(
        self, handle: RunHandle, runner_input: dict
    ) -> AsyncIterator[RuntimeEvent]:
        run = self._active_runs.get(handle.run_id)
        interrupt = run.interrupt_event if run is not None else None
        prepared_start = run.__dict__.get("_prepared_start") if run is not None else None
        invocation_context = (
            prepared_start.context if isinstance(prepared_start, PreparedRuntimeStart) else None
        )
        scope = (
            platform_invocation_scope(invocation_context)
            if invocation_context is not None
            else nullcontext()
        )
        runner_name = _runner_name(self._runner)
        accumulated_output = ""
        usage: dict[str, Any] = {}
        runner_gen: Optional[AsyncIterator[Any]] = None
        async with _conversation_span_scope(runner_name) as span:
            if isinstance(prepared_start, PreparedRuntimeStart):
                _set_conversation_span_attributes(
                    span,
                    agent_id=str(handle.native_ref.get("agent_id") or "agent"),
                    user_id=str(handle.native_ref.get("user_id") or "user"),
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    runner_name=runner_name,
                    model=prepared_start.context.model,
                    response_id=prepared_start.response_id,
                )
                _set_conversation_input_attributes(span, prepared_start.input_text)
            try:
                with scope:
                    canonical_stream = getattr(self._runner, "stream_runtime_events", None)
                    stream_result = (
                        canonical_stream(runner_input)
                        if callable(canonical_stream)
                        else self._runner.stream(runner_input)
                    )
                    if inspect.iscoroutine(stream_result):
                        # runner.stream 若声明为 async def -> AsyncIterator(非 async generator),
                        # 调用返回 coroutine,需 await 得到迭代器。
                        stream_result = await stream_result
                    runner_gen = cast(AsyncIterator[Any], stream_result)
                    while True:
                        # 竞速:下一个 runner chunk vs cancel 中断事件。
                        chunk_task = asyncio.ensure_future(_anext_or_stop(runner_gen))
                        if run is not None:
                            run.chunk_task = chunk_task
                        wait_set = {chunk_task}
                        interrupt_task = (
                            asyncio.ensure_future(interrupt.wait())
                            if interrupt is not None
                            else None
                        )
                        if interrupt_task is not None:
                            wait_set.add(interrupt_task)
                        done, pending = await asyncio.wait(
                            wait_set, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in pending:
                            task.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        if interrupt_task is not None and interrupt_task in done:
                            # cancel 中断:安全关闭 runner 流(同一 task)并停止。
                            chunk_task.cancel()
                            await asyncio.gather(chunk_task, return_exceptions=True)
                            return
                        try:
                            chunk = chunk_task.result()
                        except asyncio.CancelledError:
                            if interrupt is not None and interrupt.is_set():
                                return
                            raise
                        finally:
                            if run is not None:
                                run.chunk_task = None
                        if chunk is _STREAM_STOP:
                            return
                        if isinstance(chunk, RuntimeEvent):
                            if chunk.event_type in {
                                EventType.TEXT_DELTA,
                                EventType.TEXT_COMPLETED,
                            } and chunk.phase == "final_answer":
                                text = self._coerce(chunk.payload.get("text"))
                                if chunk.event_type == EventType.TEXT_COMPLETED:
                                    accumulated_output = text
                                else:
                                    accumulated_output += text
                            elif chunk.event_type == EventType.USAGE_REPORTED:
                                usage.update(chunk.payload)
                        if isinstance(chunk, dict):
                            chunk_type = str(chunk.get("type") or "")
                            if chunk_type == "final" and run is not None:
                                for source_key, target_key in (
                                    ("duration_ms", "duration_ms"),
                                    ("started_at", "started_at"),
                                    ("completed_at", "completed_at"),
                                    ("metrics_source", "source"),
                                ):
                                    if chunk.get(source_key) is not None:
                                        run.completion_metrics[target_key] = chunk[source_key]
                            if chunk_type in {"final", "text", "text_delta"}:
                                text = self._coerce(
                                    chunk.get("delta") or chunk.get("output") or chunk.get("data")
                                )
                                if text:
                                    if chunk_type == "final" or chunk.get("replace"):
                                        accumulated_output = text
                                    else:
                                        accumulated_output += text
                            raw_usage = chunk.get("usage")
                            if isinstance(raw_usage, dict):
                                usage.update(raw_usage)
                        event = self._chunk_to_event(handle, run, chunk)
                        if event is not None:
                            yield event
                        a2ui_surface = _a2ui_surface_event(chunk)
                        if a2ui_surface is not None:
                            event_type, payload = a2ui_surface
                            yield self._event(handle, event_type, payload)
            finally:
                if accumulated_output:
                    _set_conversation_output_attributes(span, accumulated_output)
                _set_conversation_usage_attributes(span, usage)
                if runner_gen is not None:
                    aclose = getattr(runner_gen, "aclose", None)
                    if callable(aclose):
                        try:
                            await aclose()
                        except Exception:  # noqa: BLE001
                            pass
                if run is not None:
                    run.cancellation_ack.set()

    def _chunk_to_event(
        self, handle: RunHandle, run: Optional[_ActiveRun], chunk: Any
    ) -> Optional[RuntimeEvent]:
        if isinstance(chunk, RuntimeEvent):
            # Outer adapter owns the public lifecycle envelope.  A native
            # Runtime may emit its own RUN_STARTED with private identifiers;
            # suppress that duplicate and rebind all other canonical events
            # to the public handle without flattening their payloads.
            if chunk.event_type == EventType.RUN_STARTED:
                return None
            return RuntimeEvent.create(
                chunk.event_type,
                agent_id=str(handle.native_ref.get("agent_id") or "agent"),
                user_id=str(handle.native_ref.get("user_id") or "user"),
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=self._next_seq(),
                phase=chunk.phase,
                payload=dict(chunk.payload),
                event_id=chunk.event_id,
                timestamp=chunk.timestamp,
            )
        if not isinstance(chunk, dict):
            return self._event(
                handle, EventType.TEXT_DELTA, {"text": str(chunk)}, phase="commentary"
            )
        chunk_type = chunk.get("type")
        if chunk_type in ("reasoning", "reasoning_delta", "thinking"):
            text = self._coerce(
                chunk.get("delta")
                or chunk.get("content")
                or chunk.get("output")
                or chunk.get("data")
            )
            if not text:
                return None
            event_type = (
                EventType.REASONING_COMPLETED
                if chunk.get("status") in ("completed", "done")
                else EventType.REASONING_DELTA
            )
            return self._event(
                handle,
                event_type,
                {"text": text},
                phase="commentary",
            )
        if chunk_type in ("tool_call", "tool_start"):
            call_id = str(
                chunk.get("tool_call_id")
                or chunk.get("call_id")
                or chunk.get("run_id")
                or chunk.get("id")
                or ""
            )
            name = str(chunk.get("tool_name") or chunk.get("name") or "tool")
            return self._event(
                handle,
                EventType.TOOL_CALL_BEGIN,
                {
                    "call_id": call_id or name,
                    "name": name,
                    "args": chunk.get("tool_args", chunk.get("args")),
                },
            )
        if chunk_type in ("tool_result", "tool_end"):
            call_id = str(
                chunk.get("tool_call_id")
                or chunk.get("call_id")
                or chunk.get("run_id")
                or chunk.get("id")
                or ""
            )
            name = str(chunk.get("tool_name") or chunk.get("name") or "tool")
            tool_args = chunk.get("tool_args", chunk.get("args"))
            result = chunk.get("tool_output", chunk.get("output"))
            approval_detail = approval_interrupt_info_from_result(
                result,
                fallback_tool_name=name,
                tool_args=tool_args,
                run_id=call_id or None,
            )
            if approval_detail is not None:
                return self._approval_requested_event(
                    handle,
                    run,
                    detail=approval_detail,
                    call_id=call_id,
                )
            return self._event(
                handle,
                EventType.TOOL_CALL_END,
                {
                    "call_id": call_id or name,
                    "name": name,
                    "result": result,
                    "error": chunk.get("error"),
                },
            )
        if chunk_type in ("interrupt", "approval", "approval_required"):
            raw_detail = chunk.get("interrupt_info") or chunk.get("detail") or {}
            detail = dict(raw_detail) if isinstance(raw_detail, Mapping) else {}
            call_id = str(
                chunk.get("call_id")
                or chunk.get("approval_id")
                or chunk.get("id")
                or ""
            )
            return self._approval_requested_event(
                handle,
                run,
                detail=detail,
                call_id=call_id,
            )
        if chunk_type == "checkpoint":
            raw_metadata = chunk.get("metadata")
            metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
            raw_agentengine = metadata.get("agentengine")
            agentengine: dict[str, Any] = (
                raw_agentengine if isinstance(raw_agentengine, dict) else {}
            )
            framework = str(agentengine.get("framework") or self._runtime_type)
            framework_ref = agentengine.get("framework_ref") or {}
            runtime_ref = (
                framework_ref.get(framework) if isinstance(framework_ref, dict) else {}
            ) or {}
            checkpoint_id = str(
                runtime_ref.get("checkpoint_id") if isinstance(runtime_ref, dict) else ""
            )
            if not checkpoint_id:
                return None
            handle.native_ref["checkpoint_id"] = checkpoint_id
            known_checkpoint_ids = handle.native_ref.setdefault("known_checkpoint_ids", [])
            if checkpoint_id not in known_checkpoint_ids:
                known_checkpoint_ids.append(checkpoint_id)
            handle.native_ref["framework_ref"] = framework_ref
            if isinstance(runtime_ref, dict):
                handle.native_ref.update(runtime_ref)
            return self._event(
                handle,
                EventType.CHECKPOINT_CREATED,
                {
                    "checkpoint_id": checkpoint_id,
                    "granularity": self._checkpoint_capability().granularity,
                    "run_id": str(agentengine.get("run_id") or handle.run_id),
                    "framework": framework,
                    "framework_ref": framework_ref,
                    "resume_target": framework_ref,
                },
            )
        if chunk_type == "graph_update":
            return self._event(
                handle,
                EventType.RUN_PROGRESS,
                {
                    "status": "in_progress",
                    "node": str(chunk.get("node") or ""),
                    "state_update": self._coerce(chunk.get("output")),
                },
            )
        if chunk_type == "usage":
            raw_usage = chunk.get("usage")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            return self._event(
                handle,
                EventType.USAGE_REPORTED,
                {
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                    "cached_tokens": int(usage.get("cached_tokens") or 0),
                    "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                    "source": str(usage.get("source") or self._runtime_type),
                },
            )
        if chunk_type == "error":
            error = self._coerce(chunk.get("message") or chunk.get("error"))
            return self._event(
                handle,
                EventType.RUN_FAILED,
                {
                    "status": "failed",
                    "error": error or "runner failed",
                },
            )
        if chunk_type == "final":
            return self._event(
                handle,
                EventType.TEXT_COMPLETED,
                {"text": self._coerce(chunk.get("output"))},
                phase="final_answer",
            )
        text = self._coerce(chunk.get("delta") or chunk.get("output") or chunk.get("data"))
        if not text:
            return None
        payload: dict[str, Any] = {"text": text}
        if chunk.get("replace"):
            payload["replace"] = True
        return self._event(handle, EventType.TEXT_DELTA, payload, phase="commentary")

    def _approval_requested_event(
        self,
        handle: RunHandle,
        run: Optional[_ActiveRun],
        *,
        detail: Mapping[str, Any],
        call_id: str,
    ) -> RuntimeEvent:
        """Convert one framework/tool approval to the canonical runtime event."""

        approval_id = str(
            detail.get("approval_request_id") or detail.get("id") or call_id or ""
        )
        resolved_call_id = str(call_id or approval_id)
        if run is not None and approval_id:
            run.pending_approvals.add(approval_id)
        if approval_id:
            pending_approval_ids = handle.native_ref.setdefault("pending_approval_ids", [])
            if approval_id not in pending_approval_ids:
                pending_approval_ids.append(approval_id)
        return self._event(
            handle,
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": approval_id,
                "call_id": resolved_call_id,
                "kind": "tool",
                "detail": dict(detail),
            },
        )

    def _event(
        self,
        handle: RunHandle,
        event_type: str,
        payload: dict,
        *,
        phase: Optional[str] = None,
    ) -> RuntimeEvent:
        return RuntimeEvent.create(
            event_type,
            agent_id=str(handle.native_ref.get("agent_id") or "agent"),
            user_id=str(handle.native_ref.get("user_id") or "user"),
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            seq_id=self._next_seq(),
            phase=phase,
            payload=payload,
        )

    def _completion_payload(
        self,
        handle: RunHandle,
        *,
        status: str,
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": status, **dict(metrics or {})}
        framework_ref = handle.native_ref.get("framework_ref")
        if isinstance(framework_ref, Mapping) and framework_ref:
            payload["agentengine"] = {
                "run_id": handle.run_id,
                "framework": self._runtime_type,
                "framework_ref": dict(framework_ref),
            }
        return payload

    @staticmethod
    def _coerce(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)


__all__ = ["RunnerRuntimeAdapter", "_RunnerAsBaseRuntime"]
