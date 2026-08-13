"""Detached streaming and per-app resource lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ksadk.conversations.runtime_persistence import append_run_status_event
from ksadk.server.factory import get_state

from .models import _RUN_TERMINAL_STATUSES
from .projection import _latest_invocation_status

logger = logging.getLogger(__name__)
SSE_HEARTBEAT_INTERVAL_SECONDS = max(
    0.01,
    float(os.getenv("AGENTENGINE_SSE_HEARTBEAT_SECONDS", "15")),
)

_RESERVED_UI_PATHS = {"/", "/chat", "/build", "/deploy"}
_CUSTOM_API_PROXY_ENV_KEYS = ("KSADK_USER_BACKEND_URL", "LUOLUO_USER_BACKEND_URL")
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _observe_detached_task_result(task: asyncio.Task[Any]) -> None:
    """Consume a detached task exception after ``_consume`` has logged it."""
    if task.cancelled():
        return
    task.exception()


class _DetachedSSEStream:
    _MAX_BACKLOG_CHUNKS = 256

    def __init__(
        self,
        source: AsyncIterator[str],
        *,
        invocation_id: str | None = None,
        session_id: str | None = None,
        run_mode: str = "unknown",
        run_trigger: str = "unknown",
    ):
        self._source = source
        self.invocation_id = invocation_id
        self.session_id = session_id
        self._run_mode = run_mode
        self._run_trigger = run_trigger
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        self._backlog: list[str] = []
        self._done = False
        # 后台 task 脱离请求上下文,创建时捕获 per-app registry(goal-01)。
        self._registry = get_state().stream_registry
        self._task = asyncio.create_task(self._consume())
        self._registry.streams.add(self._task)
        self._task.add_done_callback(self._registry.streams.discard)
        self._task.add_done_callback(_observe_detached_task_result)
        if self.invocation_id:
            self._registry.streams_by_invocation[self.invocation_id] = self
            self._task.add_done_callback(
                lambda _task: self._registry.streams_by_invocation.pop(
                    self.invocation_id or "", None
                )
            )

    async def _has_terminal_run_status(self) -> bool:
        if not self.session_id or not self.invocation_id:
            return False
        status = await _latest_invocation_status(
            get_state().resolve_session_service(),
            self.session_id,
            self.invocation_id,
        )
        return status in _RUN_TERMINAL_STATUSES

    async def _consume(self) -> None:
        terminal_fallback_status: str | None = None
        try:
            async for chunk in self._source:
                self._backlog.append(chunk)
                if len(self._backlog) > self._MAX_BACKLOG_CHUNKS:
                    self._backlog = self._backlog[-self._MAX_BACKLOG_CHUNKS :]
                subscribers = list(self._subscribers)
                if not subscribers:
                    continue
                await asyncio.gather(
                    *(subscriber.put(chunk) for subscriber in subscribers),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            terminal_fallback_status = "cancelled"
            raise
        except Exception:
            terminal_fallback_status = "failed"
            logger.exception("Detached SSE stream failed")
            raise
        finally:
            self._done = True
            subscribers = list(self._subscribers)
            if subscribers:
                await asyncio.gather(
                    *(subscriber.put(None) for subscriber in subscribers),
                    return_exceptions=True,
                )
            if terminal_fallback_status and self.session_id:
                try:
                    if not await self._has_terminal_run_status():
                        await append_run_status_event(
                            session_id=self.session_id,
                            author="system",
                            status=terminal_fallback_status,
                            invocation_id=self.invocation_id or "",
                            detail=(
                                f"background_{terminal_fallback_status}:{self.invocation_id or ''}"
                            ),
                            session_service_provider=get_state().resolve_session_service,
                            run_mode=self._run_mode,
                            run_trigger=self._run_trigger,
                        )
                except Exception:
                    logger.exception("failed to write background terminal status fallback")

    def subscribe(self) -> asyncio.Queue[str | None]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        for chunk in self._backlog:
            queue.put_nowait(chunk)
        if self._done:
            queue.put_nowait(None)
        else:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        self._subscribers.discard(queue)

    def cancel(self) -> bool:
        if self._task.done():
            return False
        return self._task.cancel()

    async def iter_for_client(self) -> AsyncIterator[str]:
        queue = self.subscribe()
        try:
            while True:
                # 对 queue.get() 计时而不取消上游：cancel queue.get() 只影响本订阅者的等待，
                # 不影响 _consume 后台 task；心跳直接发给当前客户端，不进 _backlog，
                # 因此断线重连(SubscribeRunEvents)不会回放心跳帧。
                try:
                    chunk = await asyncio.wait_for(
                        queue.get(),
                        timeout=SSE_HEARTBEAT_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if chunk is None:
                    break
                yield chunk
        finally:
            self.unsubscribe(queue)


async def _cancel_detached_streams_for_session(session_id: str) -> None:
    target_session_id = str(session_id or "").strip()
    if not target_session_id:
        return
    registry = get_state().stream_registry
    detached_streams = [
        detached
        for detached in list(registry.streams_by_invocation.values())
        if detached.session_id == target_session_id
    ]
    for detached in detached_streams:
        detached.cancel()
    if detached_streams:
        await asyncio.gather(
            *(detached._task for detached in detached_streams),
            return_exceptions=True,
        )


def _clear_detached_resume_key(registry, invocation_id: str, resume_key: tuple[str, str]) -> None:
    registry.resume_keys_by_invocation.pop(invocation_id, None)
    if registry.active_resume_invocation_by_key.get(resume_key) == invocation_id:
        registry.active_resume_invocation_by_key.pop(resume_key, None)


def _detached_resume_key_from_input(
    session_id: str | None,
    resume_input: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    if not isinstance(resume_input, Mapping):
        return None
    if str(resume_input.get("type") or "").strip() != "agentengine.resume_checkpoint":
        return None
    normalized_session_id = str(session_id or "").strip()
    run_id = str(resume_input.get("run_id") or "").strip()
    if not normalized_session_id or not run_id:
        return None
    return normalized_session_id, run_id


def _reject_if_detached_resume_active(resume_key: tuple[str, str] | None) -> None:
    if resume_key is None:
        return
    active_resume_invocation_id = get_state().stream_registry.active_resume_invocation_by_key.get(
        resume_key
    )
    if not active_resume_invocation_id:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "resume_already_running",
            "message": "A checkpoint resume is already running for this session and run.",
            "invocation_id": active_resume_invocation_id,
            "session_id": resume_key[0],
            "run_id": resume_key[1],
        },
    )


def detached_streaming_response(
    source: AsyncIterator[str],
    *,
    invocation_id: str | None = None,
    session_id: str | None = None,
    resume_key: tuple[str, str] | None = None,
    run_mode: str = "unknown",
    run_trigger: str = "unknown",
) -> StreamingResponse:
    """Create an app-owned detached SSE response with reconnectable backlog."""

    detached = _DetachedSSEStream(
        source,
        invocation_id=invocation_id,
        session_id=session_id,
        run_mode=run_mode,
        run_trigger=run_trigger,
    )
    registry = detached._registry
    if invocation_id and resume_key:
        registry.resume_keys_by_invocation[invocation_id] = resume_key
        registry.active_resume_invocation_by_key[resume_key] = invocation_id
        detached._task.add_done_callback(
            lambda _task: _clear_detached_resume_key(registry, invocation_id, resume_key)
        )
    return StreamingResponse(detached.iter_for_client(), media_type="text/event-stream")
