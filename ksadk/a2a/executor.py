"""A2A executor that adapts a KsADK runner to the A2A server contract."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TextPart

logger = logging.getLogger(__name__)


class KsAgentExecutor(AgentExecutor):
    """Execute a KsADK runner inside the A2A request lifecycle."""

    def __init__(self, runner: Any, prefer_stream: bool = True) -> None:
        self.runner = runner
        self.prefer_stream = prefer_stream

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "unknown-task",
            context_id=context.context_id or "unknown-context",
        )

        try:
            await updater.start_work()
            runner_input = self._build_runner_input(context)
            output = await self._run_runner(context, updater, runner_input)
            completion_message = (
                updater.new_agent_message(parts=[Part(root=TextPart(text=output))])
                if output
                else None
            )
            await updater.complete(message=completion_message)
        except Exception as exc:
            logger.exception("A2A task execution failed")
            error_text = self._coerce_text(exc)
            await updater.failed(
                message=updater.new_agent_message(
                    parts=[Part(root=TextPart(text=error_text))]
                )
            )

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "unknown-task",
            context_id=context.context_id or "unknown-context",
        )
        await updater.cancel(
            message=updater.new_agent_message(
                parts=[Part(root=TextPart(text="Request canceled"))]
            )
        )

    async def _run_runner(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        runner_input: dict[str, Any],
    ) -> str:
        stream = getattr(self.runner, "stream", None)
        if self.prefer_stream and callable(stream):
            return await self._run_streaming(context, updater, stream, runner_input)

        result = await self.runner.invoke(runner_input)
        text = self._coerce_text(result)
        if text:
            await updater.add_artifact(
                parts=[Part(root=TextPart(text=text))],
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
            if chunk_type == "final":
                final_text = (
                    self._coerce_text(chunk.get("output"))
                    if isinstance(chunk, dict)
                    else ""
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
                            parts=[Part(root=TextPart(text=suffix))],
                            artifact_id=artifact_id,
                            name="response",
                            append=bool(emitted_chunks > 1),
                            last_chunk=last_chunk,
                        )
                    continue
                else:
                    output_text = final_text
                    await updater.add_artifact(
                        parts=[Part(root=TextPart(text=final_text))],
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
                parts=[Part(root=TextPart(text=text))],
                artifact_id=artifact_id,
                name="response",
                append=bool(emitted_chunks > 1),
                last_chunk=last_chunk,
            )

        return output_text

    async def _with_last_flag(
        self,
        iterator: AsyncIterator[Any],
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

    @staticmethod
    def _build_runner_input(context: RequestContext) -> dict[str, Any]:
        metadata = dict(context.metadata)
        state = metadata.get("state", {})
        if not isinstance(state, dict):
            state = {}

        return {
            "input": context.get_user_input(),
            "task_id": context.task_id,
            "context_id": context.context_id,
            "session_id": context.context_id,
            "state": dict(state),
            "branch": metadata.get("branch", ""),
            "metadata": metadata,
        }

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
