"""A2ARuntimeExecutor — 把 RuntimeAdapter 桥接进 A2A 请求生命周期。

契约 §7.2:A2A ``context_id`` ↔ Runtime ``session_id``;``canceled`` ↔
``RuntimeAdapter.cancel(invocation_id)``。executor 不在此自造 cancel,而是委托
:class:`~ksadk.a2a.task_adapter.A2ARuntimeTaskAdapter`(其内部走 RuntimeAdapter.cancel,
G0.3 已冻结)。

wire 对象在 a2a-sdk 1.1.0 是 protobuf(``a2a_pb2``);文本用 ``Part(text=...)``,
消息用 ``Message(role=Role.ROLE_AGENT, parts=[...])``。
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Literal

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus
from a2a.utils.errors import TaskNotCancelableError

from ksadk.a2a.resume_store import A2AResumePayloadKind
from ksadk.events import EventType, RuntimeEvent
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
    """按连续类型分段输出 A2A artifact，并正确终止每个 artifact stream。"""

    def __init__(self, updater: TaskUpdater, task_id: str) -> None:
        self._updater = updater
        self._task_id = task_id
        self._active_kind: _ArtifactKind | None = None
        self._segments: dict[_ArtifactKind, int] = {"text": 0, "thinking": 0}
        self._emitted: dict[tuple[_ArtifactKind, int], int] = {}
        self._pending: tuple[_ArtifactKind, str, bool, int] | None = None

    async def push(
        self,
        kind: _ArtifactKind,
        text: str,
        *,
        replace_snapshot: bool = False,
    ) -> None:
        if self._pending is not None:
            switched = self._pending[0] != kind
            await self._emit(self._pending, last_chunk=switched)
        if self._active_kind != kind:
            self._segments[kind] += 1
            self._active_kind = kind
        self._pending = (kind, text, replace_snapshot, self._segments[kind])

    async def close(self) -> None:
        if self._pending is None:
            return
        await self._emit(self._pending, last_chunk=True)
        self._pending = None

    async def _emit(
        self,
        pending: tuple[_ArtifactKind, str, bool, int],
        *,
        last_chunk: bool,
    ) -> None:
        kind, text, replace_snapshot, segment = pending
        key = (kind, segment)
        self._emitted[key] = self._emitted.get(key, 0) + 1
        is_reasoning = kind == "thinking"
        base_id = (
            f"{self._task_id}-reasoning"
            if is_reasoning
            else f"{self._task_id}-response"
        )
        artifact_id = base_id if segment == 1 else f"{base_id}-{segment}"
        part = (
            _thought_part(text)
            if is_reasoning
            else Part(
                text=text,
                metadata=(
                    dict(_OUTPUT_SNAPSHOT_METADATA)
                    if replace_snapshot
                    else None
                ),
            )
        )
        await self._updater.add_artifact(
            parts=[part],
            artifact_id=artifact_id,
            name="reasoning" if is_reasoning else "response",
            append=False if replace_snapshot else self._emitted[key] > 1,
            last_chunk=last_chunk,
        )


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


class A2ARuntimeExecutor(AgentExecutor):
    """在 A2A 请求生命周期内执行 RuntimeAdapter。

    start/stream/resume/cancel 全部委托 ``A2ARuntimeTaskAdapter``；协议层不再
    保留 Runner fallback，因此所有入口共享同一 RuntimeEvent 合同。
    """

    def __init__(
        self,
        task_adapter: Any,
        include_reasoning: bool = False,
    ) -> None:
        if task_adapter is None:
            raise TypeError("task_adapter is required")
        self.task_adapter = task_adapter
        self.include_reasoning = include_reasoning

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
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

        handle: RunHandle | None = None
        try:
            # §7.2:当前任务处于 input-required 时,本条消息是续跑回包(checkpoint/resume)。
            if not is_resume:
                await _enqueue_initial_task(context, event_queue)
            # start_work 不透传 metadata;直接 update_status 以带 ADK v2 扩展标记
            # (RemoteA2aAgent 读 task/status metadata 决定走 v2 全增量 handler)。
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
                output = await self._run_runtime(context, updater, handle)
            else:
                handle = await self.task_adapter.start_task(
                    task_id=str(context.task_id or ""),
                    context=context,
                    input_data=context.get_user_input(),
                )
                output = await self._run_runtime(context, updater, handle)
            # completed 携带全文消息:非流式消费端与 text.completed 投影(§ event_adapter
            # message_to_event final)依赖它拿最终结果;流式消费端的重复由 adk_runner
            # 在 handoff 分支对"增量累积"去重解决(不在此处删消息)。
            completion_message = (
                updater.new_agent_message(parts=[Part(text=output)]) if output else None
            )
            await updater.complete(message=completion_message)
            await self._forget_task(context, handle)
        except _InputRequired:
            # runner 请求输入:task 已停在 input-required(附 resume token),不 complete。
            logger.info("A2A task %s 进入 input-required", context.task_id)
        except _RunCanceled:
            logger.info("A2A task %s 已由 runtime 取消", context.task_id)
            await self._forget_task(context, handle)
        except Exception as exc:  # noqa: BLE001
            logger.error("A2A task execution failed (%s)", type(exc).__name__)
            await updater.failed(
                message=updater.new_agent_message(parts=[Part(text="A2A task execution failed")])
            )
            await self._forget_task(context, handle)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
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
        await updater.cancel(
            message=updater.new_agent_message(parts=[Part(text="Request canceled")])
        )
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
        output_text = ""
        artifacts = _ArtifactStreamEmitter(updater, str(context.task_id))
        reasoning_text = ""
        input_required = False
        input_prompt = "Input required"
        checkpoint_id: str | None = None
        call_id: str | None = None
        payload_kind: A2AResumePayloadKind = "hitl_answer"

        async for event in self.task_adapter.stream_task(handle):
            if not isinstance(event, RuntimeEvent):
                raise TypeError("RuntimeAdapter.stream must yield RuntimeEvent")
            if event.event_type == EventType.RUN_FAILED:
                raise RuntimeError(self._coerce_text(event.payload.get("error")))
            if event.event_type == EventType.RUN_CANCELED:
                await artifacts.close()
                if not self._cancel_was_accepted(context, handle):
                    await updater.cancel(
                        message=updater.new_agent_message(parts=[Part(text="Request canceled")])
                    )
                raise _RunCanceled()
            if event.event_type == EventType.APPROVAL_REQUESTED:
                input_required = True
                payload_kind = "approval_decision"
                call_id = (
                    str(event.payload.get("call_id") or event.payload.get("approval_id") or "")
                    or None
                )
                detail = event.payload.get("detail")
                if isinstance(detail, dict):
                    input_prompt = self._coerce_text(
                        detail.get("prompt") or detail.get("message") or input_prompt
                    )
                continue
            if event.event_type == EventType.CHECKPOINT_CREATED:
                checkpoint_id = str(event.payload.get("checkpoint_id") or "") or None
                continue
            if event.event_type == EventType.RUN_INTERRUPTED:
                input_required = True
                input_prompt = self._coerce_text(
                    event.payload.get("prompt") or event.payload.get("message") or input_prompt
                )
                continue
            if event.event_type not in {
                EventType.TEXT_DELTA,
                EventType.TEXT_COMPLETED,
                EventType.REASONING_DELTA,
                EventType.REASONING_COMPLETED,
            }:
                continue
            text = self._coerce_text(event.payload.get("text"))
            if not text:
                continue
            if event.event_type == EventType.REASONING_COMPLETED:
                if not self.include_reasoning:
                    continue
                if not reasoning_text:
                    delta = text
                    reasoning_text = text
                elif text.startswith(reasoning_text):
                    delta = text[len(reasoning_text) :]
                    reasoning_text = text
                else:
                    delta = text
                    reasoning_text += text
                if delta:
                    await artifacts.push("thinking", delta)
                continue
            if event.event_type == EventType.REASONING_DELTA:
                if not self.include_reasoning:
                    continue
                reasoning_text += text
                await artifacts.push("thinking", text)
                continue
            # TEXT_COMPLETED 是累计全文,去重只发新增 suffix;TEXT_DELTA 默认是增量,
            # 但 runner 显式标记 replace 时是权威快照。
            if event.event_type == EventType.TEXT_COMPLETED:
                if not output_text:
                    delta = text
                    output_text = text
                    replace_snapshot = False
                elif text.startswith(output_text):
                    delta = text[len(output_text) :]
                    output_text = text
                    replace_snapshot = False
                else:
                    delta = text
                    output_text = text
                    replace_snapshot = True
            else:
                delta = text
                replace_snapshot = bool(event.payload.get("replace"))
                output_text = text if replace_snapshot else output_text + text
            if not delta:
                continue
            await artifacts.push("text", delta, replace_snapshot=replace_snapshot)
        if self._cancel_was_accepted(context, handle):
            await artifacts.close()
            raise _RunCanceled()
        await artifacts.close()
        if input_required:
            await self.task_adapter.persist_resume_state(
                task_id=str(context.task_id or ""),
                context=context,
                handle=handle,
                checkpoint_id=checkpoint_id,
                call_id=call_id,
                payload_kind=payload_kind,
            )
            await updater.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED,
                message=updater.new_agent_message(parts=[Part(text=input_prompt)]),
                metadata=dict(ADK_V2_INTEGRATION_METADATA),
            )
            raise _InputRequired()
        return output_text

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
