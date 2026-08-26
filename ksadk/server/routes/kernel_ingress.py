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
    runtime_options: dict[str, Any] | None = None,
) -> tuple[AgentControlReceipt, ingress.TrustedRuntimeContext]:
    trusted = ingress.trusted_context(
        source_kind=source_kind,
        source_ref=idempotency_key,
        session_id=session_id,
        # A foreground compatibility request admits a mutation and then reads
        # the same session's canonical stream.  It remains session-bound, but
        # needs explicit authority for both operations.
        operations=("enqueue", "subscribe_events"),
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
        **({"runtime_options": runtime_options} if runtime_options else {}),
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
            projector=_new_responses_projector(),
        ):
            if projected is None:
                continue
            kind, payload = projected
            yield _sse_chunk(payload, event=kind, seq=seq)
            if kind == "response.output_item.done" and (
                (payload.get("item") or {}).get("type") == "mcp_approval_request"
            ):
                yield _sse_chunk(
                    {"type": "response.incomplete"},
                    event="response.incomplete",
                    seq=seq,
                )
                return
            # A foreground response stream is scoped to one admitted run.
            # SessionEventStore subscriptions are deliberately long-lived for
            # replay/SSE clients, so do not leave this HTTP response open after
            # the terminal fact has been projected.
            if kind in {"response.completed", "response.failed", "response.canceled"}:
                return

    return StreamingResponse(generator(), media_type="text/event-stream")


def _responses_projector(envelope: Any) -> tuple[str, dict[str, Any]] | None:
    """Session envelope -> 旧 Responses SSE shape（cursor 仍用 envelope.seq）。"""

    payload = envelope.payload or {}
    event_type = envelope.event_type
    if event_type == "interaction.requested":
        request = payload.get("request") or {}
        presentation = request.get("presentation") or {}
        description = presentation.get("description") or ""
        try:
            visible = json.loads(description) if description else {}
        except (TypeError, json.JSONDecodeError):
            visible = {}
        arguments = visible.get("arguments") if isinstance(visible, dict) else {}
        return "response.output_item.done", {
            "type": "response.output_item.done",
            "item": {
                "id": str(payload.get("interaction_id") or ""),
                "type": "mcp_approval_request",
                "name": str(presentation.get("title") or payload.get("kind") or "approval"),
                "arguments": json.dumps(arguments or {}, ensure_ascii=False),
            },
        }
    if event_type == "run.completed":
        text = str(payload.get("output_text") or "")
        return "response.completed", {
            "type": "response.completed",
            "output_text": text,
            "delta": text,
        }
    if event_type == "run.failed":
        return "response.failed", {
            "type": "response.failed",
            "error": payload.get("error") or {"code": "runtime_failed"},
        }
    if event_type in {"run.canceled", "run.interrupted"}:
        return "response.canceled", {
            "type": "response.canceled",
            "reason": payload.get("reason") or event_type,
        }
    text = _envelope_text(payload)
    if text:
        return "response.output_text.delta", {
            "type": "response.output_text.delta",
            "delta": text,
        }
    return None


def _new_responses_projector():
    """Create a session-scoped canonical RuntimeEvent -> Responses projector.

    ``run.completed.output_refs`` deliberately point at canonical items instead
    of duplicating answer text.  A projector therefore keeps only the small
    item snapshot needed for this one HTTP/SSE response and resolves those
    refs when the terminal fact arrives.
    """

    item_text: dict[str, str] = {}

    def project(envelope: Any) -> tuple[str, dict[str, Any]] | None:
        payload = envelope.payload or {}
        event_type = envelope.event_type
        if event_type == "item.updated":
            item_id = str(payload.get("item_id") or "")
            update = payload.get("update") or {}
            text = str(update.get("text") or "")
            if item_id and text:
                item_text[item_id] = (
                    text if payload.get("op") == "replace" else item_text.get(item_id, "") + text
                )
            return None
        if event_type == "item.completed":
            item_id = str(payload.get("item_id") or "")
            parts = (payload.get("snapshot") or {}).get("parts") or []
            if item_id and isinstance(parts, list):
                item_text[item_id] = "".join(
                    str(part.get("text") or "") for part in parts if isinstance(part, dict)
                )
            return None
        if event_type == "run.completed":
            refs = payload.get("output_refs") or []
            output = "".join(
                item_text.get(str(ref.get("item_id") or ""), "")
                for ref in refs
                if isinstance(ref, dict)
            )
            projected_payload = dict(payload)
            projected_payload["output_text"] = output or str(payload.get("output_text") or "")
            return _responses_projector(
                type("Envelope", (), {"event_type": event_type, "payload": projected_payload})()
            )
        return _responses_projector(envelope)

    return project


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
        projector=_new_responses_projector(),
    ):
        if projected and projected[0] == "response.completed":
            output_text = str(projected[1].get("output_text") or output_text)
            break
        if projected and projected[0] in {"response.failed", "response.canceled"}:
            return JSONResponse(
                status_code=502,
                content={"error": projected[1]},
            )
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
