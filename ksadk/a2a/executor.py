"""A2ARuntimeExecutor — 把 RuntimeAdapter 桥接进 A2A 请求生命周期。

契约 §7.2:A2A ``context_id`` ↔ Runtime ``session_id``;``canceled`` ↔
``RuntimeAdapter.cancel(invocation_id)``。executor 不在此自造 cancel,而是委托
:class:`~ksadk.a2a.task_adapter.A2ARuntimeTaskAdapter`(其内部走 RuntimeAdapter.cancel,
G0.3 已冻结)。

wire 对象在 a2a-sdk 1.1.0 是 protobuf(``a2a_pb2``);文本用 ``Part(text=...)``,
消息用 ``Message(role=Role.ROLE_AGENT, parts=[...])``。
"""

from __future__ import annotations

from ksadk.a2a.inbound_kernel import ManagedKernelRequestError

import inspect
import json
import logging
import os
import time
from typing import Any, Literal

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus
from a2a.utils.errors import TaskNotCancelableError

from ksadk.a2a.resume_store import A2AResumePayloadKind
from ksadk.a2a.tool_activity import PublicToolActivityPolicy, PublicToolActivityProducer
from ksadk.events.canonical import (
    ApprovalRequest,
    ContinuationCreated,
    EventEnvelope,
    InteractionRequested,
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    StructuredInputRequest,
)
from ksadk.events.content import TextContent
from ksadk.events.identity import stable_item_id
from ksadk.runtime import CancelResult, RunHandle

logger = logging.getLogger(__name__)

#: ADK v2 A2A 集成扩展标记 URI(adk.dev/a2a/a2a-extension)。
#: 在 Task/status metadata 里塞入该 key 且值非空时,ADK ``RemoteA2aAgent`` 走
#: ``_handle_a2a_response_v2``——处理每个 ``artifact_update``(含 append=True 增量),
#: 不再像 v1 那样只放行首块+末块、丢弃中间增量(remote_a2a_agent.py:546/762)。
#: ksadk 自研 executor 必须带这个标记,否则编排侧 sub-agent 流式被压成"一次性"。
ADK_V2_INTEGRATION_EXTENSION_URI = "https://google.github.io/adk-docs/a2a/a2a-extension/"

#: 扩展标记值(与 google.adk ``A2aAgentExecutor._get_invocation_metadata`` 对齐)。
ADK_V2_INTEGRATION_METADATA: dict[str, Any] = {
    ADK_V2_INTEGRATION_EXTENSION_URI: {"adk_agent_executor_v2": True}
}

_OUTPUT_SNAPSHOT_METADATA = {"ksadk_output_snapshot": True}
_ArtifactKind = Literal["text", "thinking"]


def _thought_part(text: str) -> Part:
    """构造可被 ADK RemoteA2aAgent 识别为 thought 的 A2A Part。"""
    return Part(text=text, metadata={"adk_thought": True})


class _ArtifactStreamEmitter:
    """Bound text update volume while retaining the final artifact chunk."""

    FLUSH_INTERVAL_SECONDS = 0.5
    MAX_PENDING_CHARS = 512

    def __init__(self, updater: TaskUpdater, task_id: str, *, clock=None) -> None:
        self._updater = updater
        self._task_id = task_id
        self._clock = clock or time.monotonic
        self._emitted: dict[tuple[_ArtifactKind, tuple[str, str]], int] = {}
        self._pending: tuple[_ArtifactKind, str, bool, tuple[str, str], float] | None = None
        self.source_updates = 0
        self.wire_updates = 0

    async def push(
        self,
        kind: _ArtifactKind,
        text: str,
        *,
        replace_snapshot: bool = False,
        identity: tuple[str, str],
    ) -> None:
        self.source_updates += 1
        now = self._clock()
        if self._pending is None:
            self._pending = (kind, text, replace_snapshot, identity, now)
            return
        pending_kind, pending_text, pending_replace, pending_identity, started = self._pending
        switched = (pending_kind, pending_identity) != (kind, identity)
        first_visible = (pending_kind, pending_identity) not in self._emitted
        if (
            switched
            or first_visible
            or pending_replace
            or replace_snapshot
            or len(pending_text) + len(text) >= self.MAX_PENDING_CHARS
            or now - started >= self.FLUSH_INTERVAL_SECONDS
        ):
            await self._flush_pending(last_chunk=switched)
            self._pending = (kind, text, replace_snapshot, identity, now)
        else:
            self._pending = (kind, pending_text + text, False, identity, started)

    async def close(self) -> None:
        await self._flush_pending(last_chunk=True)

    async def _flush_pending(self, *, last_chunk: bool = False) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            kind, text, replace_snapshot, identity, _ = pending
            await self._emit(kind, text, replace_snapshot, identity, last_chunk=last_chunk)

    async def _emit(
        self,
        kind: _ArtifactKind,
        text: str,
        replace_snapshot: bool,
        identity: tuple[str, str],
        *,
        last_chunk: bool,
    ) -> None:
        key = (kind, identity)
        self._emitted[key] = self._emitted.get(key, 0) + 1
        is_reasoning = kind == "thinking"
        artifact_id = stable_item_id("a2a-output", self._task_id, *identity, kind)
        part = (
            _thought_part(text)
            if is_reasoning
            else Part(
                text=text,
                metadata=(dict(_OUTPUT_SNAPSHOT_METADATA) if replace_snapshot else None),
            )
        )
        await self._updater.add_artifact(
            parts=[part],
            artifact_id=artifact_id,
            name="reasoning" if is_reasoning else "response",
            append=False if replace_snapshot else self._emitted[key] > 1,
            last_chunk=last_chunk,
        )
        self.wire_updates += 1


async def _enqueue_initial_task(context: RequestContext, event_queue: EventQueue) -> None:
    """先入队初始 ``Task`` 对象,再发状态更新(a2a-sdk 1.1.0 生命周期要求:
    TaskStatusUpdateEvent 之前必须已有 Task)。``current_task`` 为续跑任务时直接用,
    否则新建 submitted 任务。Task metadata 带 ADK v2 扩展标记,让 RemoteA2aAgent
    走 v2 handler 保留全部流式增量。
    """
    task = getattr(context, "current_task", None)
    if task is None:
        task = Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            metadata=dict(ADK_V2_INTEGRATION_METADATA),
        )
    await event_queue.enqueue_event(task)


class _InputRequired(Exception):
    """Runtime 请求用户输入的信号(内部用于跳出 execute,task 停 input-required)。"""


class _RunCanceled(Exception):
    """Runtime 已取消本次执行,executor 不得再发 completed。"""


def _require_resume_capability(task_adapter: Any) -> None:
    """当 runtime adapter 显式声明 typed capability matrix 时,校验 resume 是否 supported。

    只有 adapter **覆写**了 ``capabilities()`` 才执行强校验(声明 unsupported 必须
    fail-closed);沿用基类默认矩阵的旧版/第三方 adapter 不受影响,避免把
    "未迁移到 v1 matrix" 误判为 "声明不支持"。
    """

    from ksadk.runtime.adapter import RuntimeAdapter

    runtime_adapter = getattr(task_adapter, "runtime_adapter", None)
    declared = getattr(type(runtime_adapter), "capabilities", None)
    if declared is None or declared is RuntimeAdapter.capabilities:
        return
    matrix = declared(runtime_adapter)
    if not matrix.resume.supported:
        from ksadk.kernel.errors import UnsupportedControlError

        raise UnsupportedControlError(
            f"runtime capability matrix declares resume unsupported: {matrix.resume.reason}"
        )


class A2ARuntimeExecutor(AgentExecutor):
    """在 A2A 请求生命周期内执行 RuntimeAdapter。

    start/stream/resume/cancel 全部委托 ``A2ARuntimeTaskAdapter``；协议层不再
    保留 Runner fallback，因此所有入口共享同一 RuntimeEvent 合同。
    """

    def __init__(
        self,
        task_adapter: Any,
        include_reasoning: bool = False,
        public_tool_policy: PublicToolActivityPolicy | None = None,
    ) -> None:
        if task_adapter is None:
            raise TypeError("task_adapter is required")
        self.task_adapter = task_adapter
        self.include_reasoning = include_reasoning
        self.public_tool_policy = public_tool_policy or PublicToolActivityPolicy()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        select = getattr(self.task_adapter, "for_context", None)
        if callable(select):
            selected = select(context)
            if selected is not self.task_adapter:
                return await A2ARuntimeExecutor(selected, self.include_reasoning, self.public_tool_policy).execute(context, event_queue)
        current_task = getattr(context, "current_task", None)
        durable_context_id = str(
            getattr(current_task, "context_id", "") or context.context_id or "unknown-context"
        )
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "unknown-task",
            context_id=durable_context_id,
        )
        is_resume = (
            current_task is not None
            and getattr(getattr(current_task, "status", None), "state", None)
            == TaskState.TASK_STATE_INPUT_REQUIRED
        )
        from ksadk.kernel.ingress import kernel_route_active

        if kernel_route_active() and not is_resume:
            # The A2A SDK requires the initial Task before any status update.
            # Kernel execution reports progress through TaskUpdater just like
            # the legacy adapter path, so it must establish the same wire
            # lifecycle first or clients discard working/completed updates.
            await _enqueue_initial_task(context, event_queue)
            await self._kernel_execute(context, updater)
            return

        interaction_response: Any = None
        # Third-party/local adapters written before durable context mapping do not
        # necessarily provide this optional lifecycle hook.
        prepare_context = getattr(self.task_adapter, "prepare_context", None)
        if callable(prepare_context):
            await prepare_context(context)
        if is_resume:
            interaction_response = self.task_adapter.answer_from_context(context)
            # Invalid resume tokens/decisions are request errors. Validate before emitting
            # working so the durable Task remains input-required and retryable.
            await self.task_adapter.validate_resume_task(
                context.task_id or "",
                context,
                answer=interaction_response,
            )
            # 诚实 capability:runtime 声明 resume unsupported 时 fail-closed,
            # 不允许协议层吞掉 matrix 并假装续跑成功。
            _require_resume_capability(self.task_adapter)

        handle: RunHandle | None = None
        try:
            # §7.2:当前任务处于 input-required 时,本条消息是续跑回包(checkpoint/resume)。
            if not is_resume:
                await _enqueue_initial_task(context, event_queue)
            # start_work 不透传 metadata;直接 update_status 以带 ADK v2 扩展标记
            # (RemoteA2aAgent 读 task/status metadata 决定走 v2 全增量 handler)。
            server_admitted = bool(getattr(self.task_adapter, "server_admitted", False))
            if not server_admitted:
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    metadata=dict(ADK_V2_INTEGRATION_METADATA),
                )
            if is_resume:
                handle = await self.task_adapter.resume_task(
                    context.task_id or "",
                    context,
                    answer=interaction_response,
                )
                if server_admitted:
                    await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=dict(ADK_V2_INTEGRATION_METADATA))
                await self._run_runtime(context, updater, handle)
            else:
                handle = await self.task_adapter.start_task(
                    task_id=str(context.task_id or ""),
                    context=context,
                    input_data=context.get_user_input(),
                )
                if server_admitted:
                    await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=dict(ADK_V2_INTEGRATION_METADATA))
                await self._run_runtime(context, updater, handle)
            # Every output is already a durable, identity-bearing Task artifact.
            # A fresh status Message would introduce a third output identity for
            # the same answer. Complete the Task without copying artifact text.
            await updater.complete()
            await self._forget_task(context, handle)
        except _InputRequired:
            # runner 请求输入:task 已停在 input-required(附 resume token),不 complete。
            logger.info("A2A task %s 进入 input-required", context.task_id)
        except _RunCanceled:
            logger.info("A2A task %s 已由 runtime 取消", context.task_id)
            await self._forget_task(context, handle)
        except ManagedKernelRequestError:
            # In particular, a lost admission reply must not erase an existing
            # input-required task or declare an accepted remote execution failed.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("A2A task execution failed (%s)", type(exc).__name__)
            await updater.failed(
                message=updater.new_agent_message(parts=[Part(text="A2A task execution failed")])
            )
            await self._forget_task(context, handle)

    async def _kernel_execute(self, context: RequestContext, updater: TaskUpdater) -> None:
        """kernel 路径（灰度 opt-in）：A2A task -> AgentControlCommand -> receipt。

        mutation 只走 kernel.submit；A2A task 事件 shape 保留，cursor 源自同一
        Session seq（SessionEventSubscription.after_seq）。
        """
        from ksadk.kernel import ingress as _kernel_ingress

        task_id = str(context.task_id or "")
        try:
            session_id = await self._prepare_kernel_session(context, task_id=task_id)
            trusted = _kernel_ingress.trusted_context(
                source_kind="a2a",
                source_ref=task_id,
                session_id=session_id,
                operations=("enqueue",),
            )
            command = _kernel_ingress.map_a2a_task(
                session_id=session_id,
                idempotency_key=task_id,
                content={"input": context.get_user_input()},
                task_id=task_id,
                trusted=trusted,
            )
            receipt = await _kernel_ingress.submit_command(command, permit=trusted.permit)
            if receipt.status not in ("accepted", "duplicate"):
                await updater.failed(
                    message=updater.new_agent_message(
                        parts=[Part(text=f"agent kernel rejected command: {receipt.status}")]
                    )
                )
                return
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                metadata=dict(ADK_V2_INTEGRATION_METADATA),
            )
            output_text = ""
            async for _seq, projected in _kernel_ingress.subscribe_projected(
                session_id,
                trusted=trusted,
                after_seq=int(receipt.accepted_seq or 0),
                projector=_a2a_envelope_projection,
            ):
                if projected is None:
                    continue
                kind, value = projected
                if kind == "delta":
                    output_text += value
                elif kind == "completed":
                    output_text = value or output_text
            completion = (
                updater.new_agent_message(parts=[Part(text=output_text)]) if output_text else None
            )
            await updater.complete(message=completion)
        except Exception as exc:  # noqa: BLE001
            logger.error("A2A kernel ingress failed (%s)", type(exc).__name__)
            await updater.failed(
                message=updater.new_agent_message(parts=[Part(text="A2A task execution failed")])
            )

    async def _prepare_kernel_session(self, context: RequestContext, *, task_id: str) -> str:
        """Resolve the durable A2A context and create its shared-log session.

        The legacy RuntimeAdapter path calls ``prepare_context`` from
        ``start_task``.  Kernel routing bypasses ``start_task``, so it must do
        the same verified external-context mapping before constructing the
        command.  The command admission and worker then write to this exact
        session in the Kernel-bound service.
        """

        session_id = ""
        prepare_context = getattr(self.task_adapter, "prepare_context", None)
        if callable(prepare_context):
            prepared = prepare_context(context)
            if inspect.isawaitable(prepared):
                prepared = await prepared
            session_id = str(prepared or "")
        if not session_id:
            session_id = str(context.context_id or task_id)
        if not session_id:
            raise RuntimeError("A2A_SESSION_ID_REQUIRED")

        from ksadk.kernel.bootstrap import get_agent_kernel_runtime
        from ksadk.sessions import resolve_session_service

        runtime = get_agent_kernel_runtime()
        runtime_config = getattr(runtime, "config", None)
        service = getattr(runtime_config, "session_service", None)
        if service is None:
            service = resolve_session_service()

        agent_id = (
            os.environ.get("KSADK_A2A_AGENT_ID", "").strip()
            or str(getattr(runtime_config, "agent_instance_id", "") or "").strip()
            or os.environ.get("AGENT_INSTANCE_ID", "").strip()
        )
        if not agent_id:
            raise RuntimeError("A2A_AGENT_ID_REQUIRED")
        trusted_tenant = getattr(self.task_adapter, "_trusted_tenant", None)
        user_id = (
            str(trusted_tenant(context) or "").strip()
            if callable(trusted_tenant)
            else str(getattr(getattr(context, "call_context", None), "tenant", "") or "").strip()
        ) or "anonymous"

        existing = await service.get_session_metadata(session_id)
        if existing is None:
            existing = await service.create_session(agent_id, user_id, session_id=session_id)
        if str(getattr(existing, "agent_id", "") or "") != agent_id:
            raise RuntimeError("A2A_SESSION_AGENT_CONFLICT")
        return session_id

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        select = getattr(self.task_adapter, "for_context", None)
        if callable(select):
            selected = select(context)
            if selected is not self.task_adapter:
                return await A2ARuntimeExecutor(selected, self.include_reasoning, self.public_tool_policy).cancel(context, event_queue)
        # §7.4:cancel 统一由 adapter 提供。有 RuntimeAdapter → 尊重其 CancelResult,
        # 只有底层真取消(CANCELLED)才把协议 Task 置 canceled;其余状态如实抛
        # TaskNotCancelableError 拒绝,不伪造终态。无 adapter 时底层状态未知,
        # 同样拒绝取消。
        current_task = getattr(context, "current_task", None)
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "unknown-task",
            context_id=str(
                getattr(current_task, "context_id", "") or context.context_id or "unknown-context"
            ),
        )
        result = await self.task_adapter.cancel_task(context.task_id or "", context)
        # 只有底层真的接受了取消(已中断活跃 turn,或已登记 pending cancel)才把
        # 协议 Task 置 canceled;其他结果如实拒绝,包括未来新增的 UNSUPPORTED。
        if result not in (
            CancelResult.INTERRUPTED_ACTIVE_TURN,
            CancelResult.PENDING_CANCEL_RECORDED,
        ):
            result_value = getattr(result, "value", result)
            raise TaskNotCancelableError(
                message=f"underlying runtime cancel did not apply: {result_value}"
            )
        message = updater.new_agent_message(parts=[Part(text="Request canceled")])
        await updater.cancel(message=message)
        from ksadk.a2a.kernel_task_adapter import ManagedKernelA2ATaskAdapter
        from ksadk.a2a.protocol_errors import _CONFIRMED_KERNEL_CANCEL

        if (isinstance(self.task_adapter, ManagedKernelA2ATaskAdapter)
                and result == CancelResult.INTERRUPTED_ACTIVE_TURN):
            # This adapter returns only after observing a canonical cancelled fact.
            # Preserve its acknowledgement if the upstream producer closed the queue.
            status = TaskStatus(state=TaskState.TASK_STATE_CANCELED, message=message)
            status.timestamp.GetCurrentTime()
            context.call_context.state[_CONFIRMED_KERNEL_CANCEL] = (context.task_id, status)
        clear_resume_state = getattr(self.task_adapter, "clear_resume_state", None)
        if callable(clear_resume_state):
            result = clear_resume_state(context.task_id or "", context)
            if inspect.isawaitable(result):
                await result

    async def _run_runtime(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        handle: RunHandle,
    ) -> str:
        stream_started = time.perf_counter()
        run_completed_at: float | None = None
        output_items: dict[tuple[str, str], dict[str, str]] = {}
        output_refs = None
        artifacts = _ArtifactStreamEmitter(updater, str(context.task_id))
        tools = PublicToolActivityProducer(self.public_tool_policy)
        input_required = False
        input_prompt = "Input required"
        checkpoint_id: str | None = None
        call_id: str | None = None
        input_schema: dict[str, Any] | None = None
        interaction_metadata: dict[str, Any] | None = None
        payload_kind: A2AResumePayloadKind = "hitl_answer"

        async for event in self.task_adapter.stream_task(handle):
            if not isinstance(event, EventEnvelope):
                raise TypeError("RuntimeAdapter.stream must yield RuntimeEvent")
            if getattr(event, "item_kind", None) in {"tool_call", "tool_result"}:
                await artifacts.close()
            await tools.emit(event, updater)
            if isinstance(event, RunCompleted):
                output_refs = event.output_refs
                run_completed_at = time.perf_counter()
            if isinstance(event, RunFailed):
                raise RuntimeError(self._coerce_text(event.error.message))
            if isinstance(event, RunCanceled):
                await artifacts.close()
                if not self._cancel_was_accepted(context, handle):
                    await updater.cancel(
                        message=updater.new_agent_message(parts=[Part(text="Request canceled")])
                    )
                raise _RunCanceled()
            if isinstance(event, InteractionRequested):
                input_required = True
                if isinstance(event.request, ApprovalRequest):
                    from ksadk.a2a.interaction_wire import approval_metadata

                    interaction_metadata = approval_metadata(event.request)
                    payload_kind = "approval_decision"
                    call_id = str(event.request.call_id or event.interaction_id or "") or None
                    detail = event.request.detail
                    if isinstance(detail, dict):
                        input_prompt = self._coerce_text(
                            detail.get("prompt") or detail.get("message") or input_prompt
                        )
                elif isinstance(event.request, StructuredInputRequest):
                    interaction_metadata = None
                    payload_kind = "hitl_answer"
                    call_id = str(event.interaction_id or "") or None
                    input_schema = dict(event.request.schema_)
                    input_prompt = self._coerce_text(event.request.prompt or input_prompt)
                    native_run_id = str(event.source.native_run_id or "").strip()
                    if native_run_id:
                        checkpoint_id = native_run_id
                continue
            if isinstance(event, ContinuationCreated):
                checkpoint_id = event.continuation_id
                continue
            if isinstance(event, RunInterrupted):
                input_required = True
                input_prompt = self._coerce_text(event.reason or input_prompt)
                continue
            if isinstance(event, ItemUpdated):
                if event.item_kind == "reasoning":
                    if not self.include_reasoning:
                        continue
                    if not isinstance(event.update, TextContent):
                        continue
                    text = event.update.text
                    if not text:
                        continue
                    await artifacts.push(
                        "thinking",
                        text,
                        replace_snapshot=(event.op == "replace"),
                        identity=(event.scope_id, event.item_id),
                    )
                    continue
                if event.item_kind == "message":
                    if not isinstance(event.update, TextContent):
                        continue
                    text = event.update.text
                    if not text and event.op != "replace":
                        continue
                    key = (event.scope_id, event.item_id)
                    parts = output_items.setdefault(key, {})
                    part_id = event.update.part_id
                    parts[part_id] = (
                        text if event.op == "replace" else parts.get(part_id, "") + text
                    )
                    replace_snapshot = event.op == "replace" or next(reversed(parts)) != part_id
                    await artifacts.push(
                        "text",
                        "".join(parts.values()) if replace_snapshot else text,
                        replace_snapshot=replace_snapshot,
                        identity=key,
                    )
                    continue
                continue
            if isinstance(event, (ItemCompleted, ItemSnapshotReplaced)):
                if event.item_kind == "reasoning":
                    if not self.include_reasoning:
                        continue
                    text = self._snapshot_text(event)
                    if not text:
                        continue
                    await artifacts.push(
                        "thinking",
                        text,
                        replace_snapshot=True,
                        identity=(event.scope_id, event.item_id),
                    )
                    continue
                if event.item_kind == "message":
                    key = (event.scope_id, event.item_id)
                    parts = {
                        part.part_id: part.text
                        for part in event.snapshot.parts
                        if isinstance(part, TextContent)
                    }
                    if not parts and key not in output_items:
                        continue
                    output_items[key] = parts
                    await artifacts.push(
                        "text", "".join(parts.values()), replace_snapshot=True, identity=key
                    )
                    continue
                continue
        if self._cancel_was_accepted(context, handle):
            await artifacts.close()
            raise _RunCanceled()
        await artifacts.close()
        logger.info(
            "A2A artifact timing task=%s source_updates=%d wire_updates=%d "
            "elapsed_ms=%.1f post_run_terminal_ms=%.1f",
            context.task_id,
            artifacts.source_updates,
            artifacts.wire_updates,
            (time.perf_counter() - stream_started) * 1000,
            ((time.perf_counter() - run_completed_at) * 1000)
            if run_completed_at is not None else 0.0,
        )
        if input_required:
            await self.task_adapter.persist_resume_state(
                task_id=str(context.task_id or ""),
                context=context,
                handle=handle,
                checkpoint_id=checkpoint_id,
                call_id=call_id,
                payload_kind=payload_kind,
            )
            message_metadata = (
                interaction_metadata
                if interaction_metadata is not None
                else ({"input_schema": input_schema} if input_schema is not None else None)
            )
            await updater.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED,
                message=updater.new_agent_message(
                    parts=[Part(text=input_prompt)], metadata=message_metadata
                ),
                metadata=dict(ADK_V2_INTEGRATION_METADATA),
            )
            raise _InputRequired()
        if output_refs:
            return "".join(
                output_items.get((ref.scope_id, ref.item_id), {}).get(ref.part_id, "")
                if ref.part_id is not None
                else "".join(output_items.get((ref.scope_id, ref.item_id), {}).values())
                for ref in output_refs
            )
        return "".join(text for parts in output_items.values() for text in parts.values())

    def _cancel_was_accepted(self, context: RequestContext, handle: RunHandle) -> bool:
        was_cancel_accepted = getattr(self.task_adapter, "was_cancel_accepted", None)
        return bool(
            callable(was_cancel_accepted)
            and was_cancel_accepted(str(context.task_id or ""), context, handle)
        )

    async def _forget_task(self, context: RequestContext, handle: RunHandle | None) -> None:
        forget_task = getattr(self.task_adapter, "forget_task", None)
        if handle is not None and callable(forget_task):
            result = forget_task(str(context.task_id or ""), context, handle)
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def _snapshot_text(event: ItemCompleted | ItemSnapshotReplaced) -> str:
        """Extract text from the first TextContent part of an ItemCompleted snapshot."""
        if not event.snapshot.parts:
            return ""
        part = event.snapshot.parts[0]
        return part.text if isinstance(part, TextContent) else ""

    @classmethod
    def _coerce_text(cls, payload: Any) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            for key in ("output", "delta", "data"):
                value = payload.get(key)
                if value is not None:
                    return cls._coerce_text(value)
            return json.dumps(payload, ensure_ascii=False, default=str)
        return str(payload)


__all__ = ["A2ARuntimeExecutor"]


def _a2a_envelope_projection(envelope) -> tuple[str, str] | None:
    """Session envelope -> A2A 文本投影；cursor 仍用 envelope.seq。"""

    payload = envelope.payload or {}
    if envelope.event_type == "run.completed":
        return "completed", str(payload.get("output_text") or "")
    text = str(payload.get("delta") or payload.get("text") or "")
    if text:
        return "delta", text
    return None
