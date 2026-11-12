"""Retain cancellation evidence for native LangGraph model messages."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler


class NativeModelTasks(BaseCallbackHandler):
    """Correlate v3 message starts with the async task that emitted them.

    LangChain's ainvoke can be canceled inside agenerate's gather before
    on_llm_error or a terminal message frame is dispatched.
    """

    run_inline = True

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._pending_calls: set[str] = set()

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, **kwargs) -> None:
        self._pending_calls.add(str(run_id))

    def on_llm_end(self, response, *, run_id: UUID, **kwargs) -> None:
        self._pending_calls.discard(str(run_id))

    def on_llm_error(self, error, *, run_id: UUID, **kwargs) -> None:
        self._pending_calls.discard(str(run_id))

    def on_stream_event(self, event: Mapping[str, Any], *, run_id: UUID, **kwargs: Any) -> None:
        kind = event.get("event")
        if kind in ("message-finish", "error"):
            self._tasks.pop(str(run_id), None)
        elif kind == "message-start":
            try:
                task = asyncio.current_task()
            except RuntimeError:  # synchronous model running outside an event loop
                return
            if task is not None:
                self._tasks[str(run_id)] = task

    def was_canceled(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and task.cancelled()

    def needs_recovery_answer(self) -> bool:
        """A fresh business answer can replace an interrupted model attempt.

        Before its first chunk, ainvoke's canceled gather can omit both stream
        start and terminal callbacks. Pending calls are answer-selection evidence
        only: they MUST NOT authorize closing unexplained open stream lanes.
        ``was_canceled`` remains the stronger requirement for that operation.
        """
        return bool(self._pending_calls) or any(task.cancelled() for task in self._tasks.values())
