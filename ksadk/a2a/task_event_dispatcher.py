"""Runtime-shared durable dispatcher for AgentEngine A2A task events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from ksadk.a2a.task_event_outbox import A2ATaskEventOutbox

logger = logging.getLogger(__name__)


class A2ATaskEventSink(Protocol):
    async def append_task_events(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class A2ATaskEventDispatcher:
    """One Runtime-owned outbox dispatcher shared by all Space clients."""

    def __init__(
        self,
        outbox: A2ATaskEventOutbox,
        task_sink: A2ATaskEventSink,
        *,
        retry_interval_seconds: float = 1.0,
        task_sink_timeout_seconds: float = 5.0,
    ) -> None:
        if retry_interval_seconds <= 0:
            raise ValueError("retry_interval_seconds must be positive")
        if task_sink_timeout_seconds <= 0:
            raise ValueError("task_sink_timeout_seconds must be positive")
        self._outbox = outbox
        self._task_sink = task_sink
        self._retry_interval_seconds = retry_interval_seconds
        self._task_sink_timeout_seconds = task_sink_timeout_seconds
        self._drain_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._background_task: asyncio.Task[None] | None = None
        self._started = False
        self._last_error: str | None = None

    @property
    def outbox(self) -> A2ATaskEventOutbox:
        return self._outbox

    @property
    def degraded(self) -> bool:
        return self._last_error is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def ensure_ready(self) -> None:
        await self._outbox.initialize()

    async def ensure_writable(self) -> None:
        await self.ensure_ready()
        await self._outbox.ensure_writable()

    async def start(self) -> None:
        await self.ensure_ready()
        await self.ensure_writable()
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._background_task = asyncio.create_task(
            self._run(),
            name="ksadk-a2a-task-event-dispatcher",
        )
        self._wake_event.set()

    async def stop(self, *, flush_timeout_seconds: float = 5.0) -> None:
        if flush_timeout_seconds < 0:
            raise ValueError("flush_timeout_seconds cannot be negative")
        self._stop_event.set()
        task = self._background_task
        self._background_task = None
        self._started = False
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if flush_timeout_seconds == 0:
            return
        try:
            await asyncio.wait_for(
                self.drain(raise_on_error=False),
                timeout=flush_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("A2A task event outbox flush timed out; pending batches were retained")

    async def enqueue(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> str:
        await self.ensure_writable()
        batch_id = str(
            await self._outbox.enqueue(
                platform_task_id=platform_task_id,
                events=events,
            )
        )
        self._wake_event.set()
        return batch_id

    async def drain(self, *, raise_on_error: bool) -> int:
        await self.ensure_ready()
        delivered = 0
        async with self._drain_lock:
            while True:
                batches = await self._outbox.pending(limit=100)
                if not batches:
                    self._last_error = None
                    return delivered
                for batch in batches:
                    try:
                        await asyncio.wait_for(
                            self._task_sink.append_task_events(
                                platform_task_id=batch.platform_task_id,
                                events=batch.events,
                            ),
                            timeout=self._task_sink_timeout_seconds,
                        )
                    except Exception as exc:
                        error = str(getattr(exc, "error_code", "") or type(exc).__name__)
                        self._last_error = error
                        await self._outbox.record_failure(batch.batch_id, error)
                        if raise_on_error:
                            raise
                        logger.warning(
                            "A2A task event batch remains in local outbox: task=%s batch=%s",
                            batch.platform_task_id,
                            batch.batch_id,
                        )
                        return delivered
                    await self._outbox.acknowledge(batch.batch_id)
                    delivered += 1

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self.drain(raise_on_error=False)
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._retry_interval_seconds,
                )
            except TimeoutError:
                pass
            finally:
                self._wake_event.clear()


__all__ = ["A2ATaskEventDispatcher", "A2ATaskEventSink"]
