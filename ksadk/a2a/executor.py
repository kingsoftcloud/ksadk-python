"""A2ARuntimeExecutor — 把 ksadk runner 桥接进 A2A 请求生命周期 (goal-05)。

契约 §7.2:A2A ``context_id`` ↔ Runtime ``session_id``;``canceled`` ↔
``RuntimeAdapter.cancel(invocation_id)``。executor 不在此自造 cancel,而是委托
:class:`~ksadk.a2a.task_adapter.A2ARuntimeTaskAdapter`(其内部走 RuntimeAdapter.cancel,
G0.3 已冻结)。

wire 对象在 a2a-sdk 1.1.0 是 protobuf(``a2a_pb2``);文本用 ``Part(text=...)``,
消息用 ``Message(role=Role.ROLE_AGENT, parts=[...])``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus
from a2a.utils.errors import TaskNotCancelableError

from ksadk.events import EventType, RuntimeEvent
from ksadk.runtime import CancelResult, RunHandle

logger = logging.getLogger(__name__)


async def _enqueue_initial_task(context: RequestContext, event_queue: EventQueue) -> None:
    """先入队初始 ``Task`` 对象,再发状态更新(a2a-sdk 1.1.0 生命周期要求:
    TaskStatusUpdateEvent 之前必须已有 Task)。``current_task`` 为续跑任务时直接用,
    否则新建 submitted 任务。
    """
    task = getattr(context, "current_task", None)
    if task is None:
        task = Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        )
    await event_queue.enqueue_event(task)


class _InputRequired(Exception):
    """runner 请求用户输入的信号(内部用于跳出 execute,task 停 input-required)。"""


class _RunCanceled(Exception):
    """Runtime 已取消本次执行,executor 不得再发 completed。"""


class A2ARuntimeExecutor(AgentExecutor):
    """在 A2A 请求生命周期内执行 ksadk runner。

    - sync: ``runner.invoke``。
    - streaming: ``runner.stream``,逐 chunk 发 artifact。
    - cancel: 委托 ``task_adapter.cancel_task``(内部走 RuntimeAdapter.cancel)。
    """

    def __init__(self, runner: Any, task_adapter: Any = None, prefer_stream: bool = True) -> None:
        self.runner = runner
        self.task_adapter = task_adapter
        self.prefer_stream = prefer_stream

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
        if is_resume and self.task_adapter is not None:
            interaction_response = self.task_adapter.answer_from_context(context)
            # Invalid resume tokens/decisions are request errors. Validate before emitting
            # working so the durable Task remains input-required and retryable.
            self.task_adapter.validate_resume_task(
                context.task_id or "",
                context,
                answer=interaction_response,
            )

        handle: RunHandle | None = None
        try:
            # §7.2:当前任务处于 input-required 时,本条消息是续跑回包(checkpoint/resume)。
            if not is_resume:
                await _enqueue_initial_task(context, event_queue)
            await updater.start_work()
            runner_input = self._build_runner_input(context)
            if is_resume:
                if self.task_adapter is None:
                    raise RuntimeError("runtime task adapter is required to resume A2A task")
                handle = await self.task_adapter.resume_task(
                    context.task_id or "",
                    context,
                    answer=interaction_response,
                )
                output = await self._run_runtime(context, updater, handle)
            elif self.task_adapter is not None:
                handle = await self.task_adapter.start_task(
                    task_id=str(context.task_id or ""),
                    context=context,
                    input_data=runner_input.get("input"),
                )
                output = await self._run_runtime(context, updater, handle)
            else:
                output = await self._run_runner(context, updater, runner_input)
            completion_message = (
                updater.new_agent_message(parts=[Part(text=output)]) if output else None
            )
            await updater.complete(message=completion_message)
            self._forget_task(context, handle)
        except _InputRequired:
            # runner 请求输入:task 已停在 input-required(附 resume token),不 complete。
            logger.info("A2A task %s 进入 input-required", context.task_id)
        except _RunCanceled:
            logger.info("A2A task %s 已由 runtime 取消", context.task_id)
            self._forget_task(context, handle)
        except Exception as exc:  # noqa: BLE001
            logger.error("A2A task execution failed (%s)", type(exc).__name__)
            await updater.failed(
                message=updater.new_agent_message(parts=[Part(text="A2A task execution failed")])
            )
            self._forget_task(context, handle)

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
        if self.task_adapter is None:
            raise TaskNotCancelableError(message="runtime task adapter is not configured")
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

    async def _run_runner(
        self, context: RequestContext, updater: TaskUpdater, runner_input: dict[str, Any]
    ) -> str:
        stream = getattr(self.runner, "stream", None)
        if self.prefer_stream and callable(stream):
            return await self._run_streaming(context, updater, stream, runner_input)

        result = await self.runner.invoke(runner_input)
        text = self._coerce_text(result)
        if text:
            await updater.add_artifact(
                parts=[Part(text=text)],
                artifact_id=f"{context.task_id}-response",
                name="response",
                last_chunk=True,
            )
        return text

    async def _run_streaming(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        stream: Any,
        runner_input: dict[str, Any],
    ) -> str:
        output_text = ""
        emitted_chunks = 0
        artifact_id = f"{context.task_id}-response"

        async for chunk, last_chunk in self._with_last_flag(stream(runner_input)):
            chunk_type = chunk.get("type") if isinstance(chunk, dict) else None
            if chunk_type == "input_required":
                raise RuntimeError(
                    "runner emitted input_required without a RuntimeAdapter execution path"
                )
            if chunk_type == "final":
                final_text = (
                    self._coerce_text(chunk.get("output")) if isinstance(chunk, dict) else ""
                )
                if not final_text:
                    continue
                if not output_text:
                    output_text = final_text
                elif final_text.startswith(output_text):
                    suffix = final_text[len(output_text) :]
                    output_text = final_text
                    if suffix:
                        emitted_chunks += 1
                        await updater.add_artifact(
                            parts=[Part(text=suffix)],
                            artifact_id=artifact_id,
                            name="response",
                            append=bool(emitted_chunks > 1),
                            last_chunk=last_chunk,
                        )
                    continue
                else:
                    output_text = final_text
                    await updater.add_artifact(
                        parts=[Part(text=final_text)],
                        artifact_id=artifact_id,
                        name="response",
                        append=False,
                        last_chunk=last_chunk,
                    )
                continue

            text = self._coerce_text(chunk)
            if not text:
                continue
            output_text += text
            emitted_chunks += 1
            await updater.add_artifact(
                parts=[Part(text=text)],
                artifact_id=artifact_id,
                name="response",
                append=bool(emitted_chunks > 1),
                last_chunk=last_chunk,
            )

        return output_text

    async def _with_last_flag(
        self, iterator: AsyncIterator[Any]
    ) -> AsyncIterator[tuple[Any, bool]]:
        try:
            previous = await anext(iterator)
        except StopAsyncIteration:
            return
        while True:
            try:
                current = await anext(iterator)
            except StopAsyncIteration:
                yield previous, True
                return
            yield previous, False
            previous = current

    def _build_runner_input(self, context: RequestContext) -> dict[str, Any]:
        metadata = dict(getattr(context, "metadata", None) or {})
        state = metadata.get("state", {})
        if not isinstance(state, dict):
            state = {}
        # §7.2: A2A context_id ↔ Runtime session_id。
        return {
            "input": context.get_user_input(),
            "task_id": context.task_id,
            "context_id": context.context_id,
            "session_id": context.context_id,
            "state": dict(state),
            "branch": metadata.get("branch", ""),
            "metadata": metadata,
        }

    async def _run_runtime(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        handle: RunHandle,
    ) -> str:
        output_text = ""
        artifact_id = f"{context.task_id}-response"
        emitted_chunks = 0
        pending_text: str | None = None
        input_required = False
        input_prompt = "Input required"
        checkpoint_id: str | None = None
        call_id: str | None = None
        payload_kind = "hitl_answer"

        async def emit_text(text: str, *, last_chunk: bool) -> None:
            nonlocal emitted_chunks
            emitted_chunks += 1
            await updater.add_artifact(
                parts=[Part(text=text)],
                artifact_id=artifact_id,
                name="response",
                append=emitted_chunks > 1,
                last_chunk=last_chunk,
            )

        async for event in self.task_adapter.stream_task(handle):
            if not isinstance(event, RuntimeEvent):
                raise TypeError("RuntimeAdapter.stream must yield RuntimeEvent")
            if event.event_type == EventType.RUN_FAILED:
                raise RuntimeError(self._coerce_text(event.payload.get("error")))
            if event.event_type == EventType.RUN_CANCELED:
                if pending_text is not None:
                    await emit_text(pending_text, last_chunk=False)
                    pending_text = None
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
            if pending_text is not None:
                await emit_text(pending_text, last_chunk=False)
            pending_text = text
            output_text += text
        if self._cancel_was_accepted(context, handle):
            if pending_text is not None:
                await emit_text(pending_text, last_chunk=False)
            raise _RunCanceled()
        if pending_text is not None:
            await emit_text(pending_text, last_chunk=True)
        if input_required:
            metadata = self.task_adapter.build_resume_metadata(
                handle=handle,
                checkpoint_id=checkpoint_id,
                call_id=call_id,
                payload_kind=payload_kind,
            )
            await updater.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED,
                message=updater.new_agent_message(parts=[Part(text=input_prompt)]),
                metadata=metadata,
            )
            raise _InputRequired()
        return output_text

    def _cancel_was_accepted(self, context: RequestContext, handle: RunHandle) -> bool:
        was_cancel_accepted = getattr(self.task_adapter, "was_cancel_accepted", None)
        return bool(
            callable(was_cancel_accepted)
            and was_cancel_accepted(str(context.task_id or ""), context, handle)
        )

    def _forget_task(self, context: RequestContext, handle: RunHandle | None) -> None:
        forget_task = getattr(self.task_adapter, "forget_task", None)
        if handle is not None and callable(forget_task):
            forget_task(str(context.task_id or ""), context, handle)

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
