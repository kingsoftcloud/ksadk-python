# -*- coding: utf-8 -*-
"""RunAgent / Responses 入口的 kernel 路径（Phase 1 Task 8 Step 3-5）。

只在 ``kernel_route_active()`` 时启用（灰度 opt-in）；旧 HTTP 行为保持兼容：
- accepted -> 202、duplicate -> 200、rejected -> 400、unsupported -> 409、
  queue_full -> 429、persistence_uncertain -> 503（RECEIPT_HTTP_STATUS）。
- 旧响应 shape 不变；非流式在 receipt accepted 后才开始消费 stream。
- SSE 的 reconnect cursor 源自同一 Session seq（SessionEventSubscription）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from ksadk.kernel import ingress
from ksadk.kernel.contracts import AgentControlReceipt

logger = logging.getLogger(__name__)


def _envelope_text(payload: dict[str, Any]) -> str:
    return str(payload.get("delta") or payload.get("text") or "")


async def _kernel_submit(
    *,
    mapper: str,
    session_id: str,
    idempotency_key: str,
    content: Any,
    correlation_ref: str | None,
    source_kind: str,
) -> tuple[AgentControlReceipt, ingress.TrustedRuntimeContext]:
    trusted = ingress.trusted_context(
        source_kind=source_kind,
        source_ref=idempotency_key,
        session_id=session_id,
        operations=("enqueue",),
    )
    correlation_kwarg = {
        "map_run_request": "invocation_id",
        "map_responses_request": "response_id",
        "map_agui_request": "run_id",
        "map_a2a_task": "task_id",
        "map_studio_request": "run_id",
    }[mapper]
    command = getattr(ingress, mapper)(
        trusted=trusted,
        session_id=session_id,
        idempotency_key=idempotency_key,
        content=content,
        **({correlation_kwarg: correlation_ref} if correlation_ref else {}),
    )
    receipt = await ingress.submit_command(command, permit=trusted.permit)
    return receipt, trusted


def _kernel_error_response(receipt: AgentControlReceipt) -> JSONResponse:
    return JSONResponse(
        status_code=ingress.receipt_http_status(receipt),
        content={
            "error": ingress.receipt_error_payload(receipt),
        },
        headers=ingress.receipt_response_headers(receipt),
    )


def _sse_chunk(payload: dict[str, Any], *, event: str | None, seq: int) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"id: {seq}\n{prefix}data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def kernel_stream_response(
    *,
    receipt: AgentControlReceipt,
    trusted: ingress.TrustedRuntimeContext,
    session_id: str,
) -> StreamingResponse:
    """统一 cursor：从 receipt.accepted_seq 之后读 Session 事件。"""

    after_seq = int(receipt.accepted_seq or 0)

    async def generator():
        async for seq, projected in ingress.subscribe_projected(
            session_id,
            trusted=trusted,
            after_seq=after_seq,
            projector=_responses_projector,
        ):
            if projected is None:
                continue
            kind, payload = projected
            yield _sse_chunk(payload, event=kind, seq=seq)

    return StreamingResponse(generator(), media_type="text/event-stream")


def _responses_projector(envelope: Any) -> tuple[str, dict[str, Any]] | None:
    """Session envelope -> 旧 Responses SSE shape（cursor 仍用 envelope.seq）。"""

    payload = envelope.payload or {}
    event_type = envelope.event_type
    if event_type == "run.completed":
        text = str(payload.get("output_text") or "")
        return "response.completed", {
            "type": "response.completed",
            "output_text": text,
            "delta": text,
        }
    text = _envelope_text(payload)
    if text:
        return "response.output_text.delta", {
            "type": "response.output_text.delta",
            "delta": text,
        }
    return None


async def kernel_conversation_turn(
    *,
    receipt: AgentControlReceipt,
    trusted: ingress.TrustedRuntimeContext,
    session_id: str,
    build_payload,
):
    """非流式 kernel 路径：receipt accepted 后订阅聚合 output_text。"""

    if receipt.status not in ("accepted", "duplicate"):
        return _kernel_error_response(receipt)
    output_text = ""
    async for _seq, projected in ingress.subscribe_projected(
        session_id,
        trusted=trusted,
        after_seq=int(receipt.accepted_seq or 0),
        projector=_responses_projector,
    ):
        if projected and projected[0] == "response.completed":
            output_text = str(projected[1].get("output_text") or output_text)
        elif projected:
            output_text += str(projected[1].get("delta") or "")
    payload = build_payload(output_text)
    return JSONResponse(
        status_code=ingress.receipt_http_status(receipt),
        content=payload,
        headers=ingress.receipt_response_headers(receipt),
    )


__all__ = [
    "kernel_conversation_turn",
    "kernel_stream_response",
]
