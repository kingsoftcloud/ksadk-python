"""Total ASGI deadline for bounded metadata/history actions.

The response is buffered until completion so a timeout during serialization
cannot produce a partial HTTP 200. This middleware never wraps Agent or SSE
requests, which have their own lifetimes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

from starlette.datastructures import Headers

from ksadk.server.invocation_identity import trusted_invocation_identity_from_headers
from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget

logger = logging.getLogger(__name__)
INVALID_REMAINING_HEADER: Counter[str] = Counter()
_ACTIONS = frozenset({
    "GetSession", "ListSessions", "ListSessionEvents", "ListSessionMessages",
    "ListSessionCheckpoints", "ListToolReceipts", "GetAgentUiBootstrap",
    "GetCheckpointResumePreview",
})
_MAX_BUFFER_BYTES = 32 * 1024 * 1024
_ERROR_SEND_SECONDS = 0.5
_HISTORY_ACTIONS = frozenset({
    "ListSessionEvents", "ListSessionMessages", "ListSessionCheckpoints",
    "ListToolReceipts", "GetCheckpointResumePreview",
})


class _ResponseBufferLimitExceeded(RuntimeError):
    pass


def _remaining_ms(raw: str) -> int | None:
    if raw.startswith("-") and raw[1:] and raw[1:].isascii() and raw[1:].isdecimal():
        return 0
    if not raw or not raw.isascii() or not raw.isdecimal():
        return None
    # This carrier only shortens our 2.6-second cap. Avoid Python's maximum
    # integer-string conversion limit and safely saturate oversized values.
    significant = raw.lstrip("0")
    if not significant:
        return 0
    if len(significant) > 18:
        return 2600
    try:
        return int(significant)
    except ValueError:  # pragma: no cover - bounded ASCII digits above
        return None


class HistoryDeadlineMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if scope.get("method") != "POST" or path.rsplit("/", 1)[-1] not in _ACTIONS:
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        budget = 2.6
        headers = Headers(scope=scope)
        try:
            trusted = not trusted_invocation_identity_from_headers(headers).is_empty
        except Exception:
            trusted = False
        if trusted:
            raw = headers.get("x-agentengine-remaining-ms")
            if raw is not None:
                parsed = _remaining_ms(raw)
                if parsed is None:
                    INVALID_REMAINING_HEADER["malformed"] += 1
                else:
                    budget = min(budget, parsed / 1000)
        budget -= time.monotonic() - started
        if budget <= 0:
            await self._timeout(send)
            return

        messages: list[dict[str, Any]] = []
        buffered_bytes = 0

        async def buffered_send(message: dict[str, Any]) -> None:
            nonlocal buffered_bytes
            buffered_bytes += len(message.get("body", b""))
            if buffered_bytes > _MAX_BUFFER_BYTES:
                raise _ResponseBufferLimitExceeded()
            messages.append(message)

        token = (CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
                 if path.rsplit("/", 1)[-1] in _HISTORY_ACTIONS else None)
        try:
            async with asyncio.timeout(budget):
                await self.app(scope, receive, buffered_send)
        except _ResponseBufferLimitExceeded:
            await self._response_too_large(send)
            return
        except TimeoutError:
            await self._timeout(send)
            return
        finally:
            if token is not None:
                CURRENT_HISTORY_READ_BUDGET.reset(token)
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            await self._timeout(send)
            return
        emitted = False
        try:
            async with asyncio.timeout(remaining):
                for message in messages:
                    await send(message)
                    emitted = True
        except TimeoutError:
            if emitted:
                raise  # close a started response rather than append a second status
            await self._timeout(send)

    @staticmethod
    async def _timeout(send: Any) -> None:
        body = json.dumps({
            "Code": 504, "Message": "History request deadline exceeded",
            "Data": None, "ReasonCode": "RUNTIME_HISTORY_TIMEOUT",
        }).encode()
        await HistoryDeadlineMiddleware._send_error(send, 504, body)

    @staticmethod
    async def _response_too_large(send: Any) -> None:
        body = json.dumps({
            "Code": 422, "Message": "History response exceeds the bounded response size",
            "Data": None, "ReasonCode": "HISTORY_PROJECTION_BUDGET_EXCEEDED",
        }).encode()
        await HistoryDeadlineMiddleware._send_error(send, 422, body)

    @staticmethod
    async def _send_error(send: Any, status: int, body: bytes) -> None:
        # A slow/broken ASGI transport cannot extend the error path forever.
        # Both messages share one cap; once the header was sent, a blocked
        # body closes this response rather than trying another status.
        async with asyncio.timeout(_ERROR_SEND_SECONDS):
            await send({
                "type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
