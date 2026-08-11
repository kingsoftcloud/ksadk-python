from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional, Sequence

from ksadk.conversations.run_kinds import (
    RUN_MODE_FOREGROUND,
)
from ksadk.conversations.runtime_invocation import _response_sse
from ksadk.conversations.runtime_payloads import (
    build_compaction_sse_event,
    build_responses_payload,
)
from ksadk.conversations.runtime_stream_events import _iter_conversation_turn_events
from ksadk.runtime.conversation_execution import iter_runtime_conversation_semantic_events
from ksadk.runtime.executor import RuntimeExecutor, RuntimeStartPreparation
from ksadk.runtime.launch import RuntimeLaunchContext


async def stream_conversation_turn(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> AsyncIterator[str]:
    """Legacy ksadk response SSE stream used by hosted chat and chat-completions."""
    events = _iter_conversation_turn_events(
        runner=runner,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        prepare_runner=prepare_runner,
        model_metadata=model_metadata,
        model_options=model_options,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        custom_metadata=custom_metadata,
        resume_input=resume_input,
        account_id=account_id,
        invocation_id=invocation_id,
        session_service_provider=session_service_provider,
        run_mode=run_mode,
    )
    async for chunk in _stream_conversation_semantic_events(
        events=events,
        model=model,
        session_id=session_id,
    ):
        yield chunk


async def stream_runtime_conversation_turn(
    *,
    executor: RuntimeExecutor,
    launch_context: RuntimeLaunchContext,
    agent_id: str,
    user_id: str,
    messages: Sequence[Dict[str, Any]],
    session_id: Optional[str],
    model: Optional[str],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
    runtime_preparation: RuntimeStartPreparation | None = None,
) -> AsyncIterator[str]:
    """Legacy hosted-chat SSE driven by the canonical RuntimeExecutor."""

    events = iter_runtime_conversation_semantic_events(
        executor=executor,
        launch_context=launch_context,
        agent_id=agent_id,
        user_id=user_id,
        messages=messages,
        session_id=session_id,
        model=model,
        model_metadata=model_metadata,
        model_options=model_options,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        custom_metadata=custom_metadata,
        resume_input=resume_input,
        account_id=account_id,
        invocation_id=invocation_id,
        session_service_provider=session_service_provider,
        run_mode=run_mode,
        runtime_preparation=runtime_preparation,
    )
    async for chunk in _stream_conversation_semantic_events(
        events=events,
        model=model,
        session_id=session_id,
    ):
        yield chunk


async def _stream_conversation_semantic_events(
    *,
    events: AsyncIterator[dict[str, Any]],
    model: Optional[str],
    session_id: Optional[str],
) -> AsyncIterator[str]:
    async for event in events:
        event_type = event.get("type")
        if event_type == "compaction":
            yield build_compaction_sse_event(
                phase=str(event.get("phase") or "start"),
                trigger=str(event.get("trigger") or "auto"),
                compacted_until_seq_id=event.get("compacted_until_seq_id"),
                total_chars=event.get("total_chars"),
                total_estimated_tokens=event.get("total_estimated_tokens"),
                group_count=event.get("group_count"),
                threshold_percentage=event.get("threshold_percentage"),
            )
        elif event_type == "thinking":
            yield _response_sse("response.reasoning.delta", {"delta": event.get("delta", "")})
        elif event_type == "text":
            text_payload: dict[str, Any] = {"delta": event.get("delta", "")}
            if event.get("replace"):
                text_payload["replace"] = True
            yield _response_sse("response.output_text.delta", text_payload)
        elif event_type == "tool_call":
            yield _response_sse(
                "response.tool_call",
                {
                    "name": event.get("name"),
                    "args": event.get("args", {}),
                    "run_id": event.get("run_id"),
                    "stage": event.get("stage"),
                    "event_kind": event.get("event_kind"),
                    "display_title": event.get("display_title"),
                    "display_summary": event.get("display_summary"),
                },
            )
        elif event_type == "tool_result":
            yield _response_sse(
                "response.tool_result",
                {
                    "name": event.get("name"),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                },
            )
        elif event_type in {"stage_tool_call", "stage_tool_result"}:
            yield _response_sse(
                f"response.ksadk.{event_type}",
                {
                    "type": event_type,
                    "name": event.get("name"),
                    "args": event.get("args", {}),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                    "stage": event.get("stage"),
                    "event_kind": event.get("event_kind"),
                    "display_title": event.get("display_title"),
                    "display_summary": event.get("display_summary"),
                },
            )
        elif event_type == "interrupt":
            yield _response_sse(
                "response.approval_request", {"interrupt_info": event.get("interrupt_info")}
            )
        elif event_type == "error":
            yield _response_sse(
                "response.error", {"message": event.get("message") or "Agent 运行失败"}
            )
        elif event_type == "cancelled":
            yield _response_sse("response.cancelled", {"status": "cancelled"})
        elif event_type == "completed":
            final_payload = build_responses_payload(
                output_text=str(event.get("output_text") or ""),
                model=event.get("model") or model,
                session_id=str(event.get("session_id") or session_id or ""),
                metadata=(
                    event.get("metadata") if isinstance(event.get("metadata"), Mapping) else None
                ),
                usage=event.get("usage") if isinstance(event.get("usage"), Mapping) else None,
            )
            yield _response_sse("response.completed", final_payload)


async def stream_responses_conversation_turn(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    include_agentengine_metadata: bool = False,
    resume_input: Mapping[str, Any] | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> AsyncIterator[str]:
    """OpenAI Responses-style SSE stream."""
    response_id = f"resp_{uuid.uuid4().hex}"
    events = _iter_conversation_turn_events(
        runner=runner,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        prepare_runner=prepare_runner,
        model_metadata=model_metadata,
        model_options=model_options,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        custom_metadata=custom_metadata,
        resume_input=resume_input,
        response_id=response_id,
        account_id=account_id,
        invocation_id=invocation_id,
        session_service_provider=session_service_provider,
        run_mode=run_mode,
    )
    async for chunk in _stream_responses_semantic_events(
        events=events,
        model=model,
        session_id=session_id,
        custom_metadata=custom_metadata,
        include_agentengine_metadata=include_agentengine_metadata,
        response_id=response_id,
    ):
        yield chunk


async def stream_runtime_responses_conversation_turn(
    *,
    executor: RuntimeExecutor,
    launch_context: RuntimeLaunchContext,
    agent_id: str,
    user_id: str,
    messages: Sequence[Dict[str, Any]],
    session_id: Optional[str],
    model: Optional[str],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    include_agentengine_metadata: bool = False,
    resume_input: Mapping[str, Any] | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
    runtime_preparation: RuntimeStartPreparation | None = None,
) -> AsyncIterator[str]:
    """OpenAI Responses SSE driven by the canonical RuntimeExecutor."""

    response_id = f"resp_{uuid.uuid4().hex}"
    events = iter_runtime_conversation_semantic_events(
        executor=executor,
        launch_context=launch_context,
        agent_id=agent_id,
        user_id=user_id,
        messages=messages,
        session_id=session_id,
        model=model,
        model_metadata=model_metadata,
        model_options=model_options,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        custom_metadata=custom_metadata,
        resume_input=resume_input,
        response_id=response_id,
        account_id=account_id,
        invocation_id=invocation_id,
        session_service_provider=session_service_provider,
        run_mode=run_mode,
        runtime_preparation=runtime_preparation,
    )
    async for chunk in _stream_responses_semantic_events(
        events=events,
        model=model,
        session_id=session_id,
        custom_metadata=custom_metadata,
        include_agentengine_metadata=include_agentengine_metadata,
        response_id=response_id,
    ):
        yield chunk


async def _stream_responses_semantic_events(
    *,
    events: AsyncIterator[dict[str, Any]],
    model: Optional[str],
    session_id: Optional[str],
    custom_metadata: Mapping[str, Any] | None,
    include_agentengine_metadata: bool,
    response_id: str,
) -> AsyncIterator[str]:
    """Serialize one semantic event stream into the Responses wire format."""

    created_at = int(time.time())
    response_metadata = dict(custom_metadata or {})

    message_item_id = f"msg_{uuid.uuid4().hex[:12]}"
    reasoning_item_id = f"rs_{uuid.uuid4().hex[:12]}"
    next_output_index = 0
    text_output_index: int | None = None
    reasoning_output_index: int | None = None
    message_started = False
    content_started = False
    reasoning_started = False
    completed_text = ""
    lifecycle_started = False

    def _start_response_lifecycle(current_session_id: str | None = None) -> list[str]:
        nonlocal lifecycle_started, response_metadata
        if lifecycle_started:
            return []
        lifecycle_started = True
        initial_payload = build_responses_payload(
            output_text="",
            model=model,
            session_id=current_session_id or session_id or "",
            response_id=response_id,
            created_at=created_at,
            status="in_progress",
            metadata=response_metadata,
        )
        return [
            _response_sse("response.created", initial_payload),
            _response_sse("response.in_progress", initial_payload),
        ]

    def _message_item(status: str, text: str = "") -> dict[str, Any]:
        content = [{"type": "output_text", "text": text}] if text or status == "completed" else []
        return {
            "id": message_item_id,
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": content,
        }

    def _reasoning_item(status: str) -> dict[str, Any]:
        return {
            "id": reasoning_item_id,
            "type": "reasoning",
            "status": status,
            "summary": [],
        }

    def _next_output_index() -> int:
        nonlocal next_output_index
        output_index = next_output_index
        next_output_index += 1
        return output_index

    async for event in events:
        if include_agentengine_metadata:
            event_metadata = event.get("metadata")
            agentengine_metadata = (
                event_metadata.get("agentengine") if isinstance(event_metadata, Mapping) else None
            )
            if isinstance(agentengine_metadata, Mapping):
                response_metadata["agentengine"] = dict(agentengine_metadata)
        if not lifecycle_started:
            for lifecycle_chunk in _start_response_lifecycle(str(event.get("session_id") or "")):
                yield lifecycle_chunk
        event_type = event.get("type")
        if event_type == "compaction":
            yield build_compaction_sse_event(
                phase=str(event.get("phase") or "start"),
                trigger=str(event.get("trigger") or "auto"),
                compacted_until_seq_id=event.get("compacted_until_seq_id"),
                total_chars=event.get("total_chars"),
                total_estimated_tokens=event.get("total_estimated_tokens"),
                group_count=event.get("group_count"),
                threshold_percentage=event.get("threshold_percentage"),
            )
            continue

        if event_type == "thinking":
            if not reasoning_started:
                reasoning_started = True
                reasoning_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {
                        "output_index": reasoning_output_index,
                        "item": _reasoning_item("in_progress"),
                    },
                )
            yield _response_sse(
                "response.reasoning.delta",
                {
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "delta": event.get("delta", ""),
                },
            )
            continue

        if event_type == "text":
            if not message_started:
                message_started = True
                text_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {"output_index": text_output_index, "item": _message_item("in_progress")},
                )
            if not content_started:
                content_started = True
                yield _response_sse(
                    "response.content_part.added",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    },
                )
            delta = str(event.get("delta") or "")
            replace = bool(event.get("replace"))
            completed_text = delta if replace else completed_text + delta
            text_delta_payload: dict[str, Any] = {
                "item_id": message_item_id,
                "output_index": text_output_index,
                "content_index": 0,
                "delta": delta,
            }
            if replace:
                text_delta_payload["replace"] = True
            yield _response_sse(
                "response.output_text.delta",
                text_delta_payload,
            )
            continue

        if event_type == "tool_call":
            args_json = json.dumps(event.get("args", {}) or {}, ensure_ascii=False)
            call_id = str(event.get("run_id") or f"call_{uuid.uuid4().hex[:12]}")
            item_id = f"fc_{uuid.uuid4().hex[:12]}"
            call_output_index = _next_output_index()
            item = {
                "id": item_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": event.get("name") or "unknown",
                "arguments": "",
            }
            yield _response_sse(
                "response.output_item.added", {"output_index": call_output_index, "item": item}
            )
            yield _response_sse(
                "response.function_call_arguments.delta",
                {"item_id": item_id, "output_index": call_output_index, "delta": args_json},
            )
            item["arguments"] = args_json
            item["status"] = "completed"
            yield _response_sse(
                "response.function_call_arguments.done",
                {"item_id": item_id, "output_index": call_output_index, "arguments": args_json},
            )
            yield _response_sse(
                "response.output_item.done", {"output_index": call_output_index, "item": item}
            )
            if event.get("display_title") or event.get("display_summary"):
                yield _response_sse(
                    "response.ksadk.tool_call",
                    {
                        "type": "tool_call",
                        "name": event.get("name"),
                        "args": event.get("args", {}),
                        "run_id": event.get("run_id"),
                        "stage": event.get("stage"),
                        "event_kind": event.get("event_kind"),
                        "display_title": event.get("display_title"),
                        "display_summary": event.get("display_summary"),
                    },
                )
            continue

        if event_type == "tool_result":
            yield _response_sse(
                "response.ksadk.tool_result",
                {
                    "name": event.get("name"),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                },
            )
            continue

        if event_type in {"stage_tool_call", "stage_tool_result"}:
            yield _response_sse(
                f"response.ksadk.{event_type}",
                {
                    "type": event_type,
                    "name": event.get("name"),
                    "args": event.get("args", {}),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                    "stage": event.get("stage"),
                    "event_kind": event.get("event_kind"),
                    "display_title": event.get("display_title"),
                    "display_summary": event.get("display_summary"),
                },
            )
            continue

        if event_type == "interrupt":
            interrupt_info = event.get("interrupt_info")
            if isinstance(interrupt_info, Mapping) and interrupt_info.get("tool_name"):
                raw_arguments = (
                    interrupt_info.get("arguments")
                    or interrupt_info.get("tool_args")
                    or interrupt_info.get("args")
                    or {}
                )
                arguments = (
                    raw_arguments
                    if isinstance(raw_arguments, str)
                    else json.dumps(raw_arguments, ensure_ascii=False)
                )
                approval_item = {
                    "id": str(
                        interrupt_info.get("approval_request_id")
                        or interrupt_info.get("id")
                        or f"appr_{uuid.uuid4().hex[:12]}"
                    ),
                    "type": "mcp_approval_request",
                    "name": str(interrupt_info.get("tool_name")),
                    "arguments": arguments,
                    "server_label": str(interrupt_info.get("server_label") or "ksadk"),
                }
                approval_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {"output_index": approval_output_index, "item": approval_item},
                )
                yield _response_sse(
                    "response.output_item.done",
                    {"output_index": approval_output_index, "item": approval_item},
                )
            else:
                yield _response_sse(
                    "response.ksadk.approval_request", {"interrupt_info": interrupt_info}
                )
            incomplete_payload = build_responses_payload(
                output_text=completed_text,
                model=model,
                session_id=str(event.get("session_id") or session_id or ""),
                response_id=response_id,
                created_at=created_at,
                status="incomplete",
                metadata=response_metadata,
                incomplete_details={
                    "reason": "approval_required",
                    "ksadk_interrupt": interrupt_info,
                },
            )
            yield _response_sse("response.incomplete", incomplete_payload)
            return

        if event_type == "error":
            failed_payload = build_responses_payload(
                output_text=completed_text,
                model=model,
                session_id=session_id or "",
                response_id=response_id,
                created_at=created_at,
                status="failed",
                metadata=response_metadata,
                usage=event.get("usage") if isinstance(event.get("usage"), Mapping) else None,
                error={"message": event.get("message") or "Agent 运行失败"},
            )
            yield _response_sse("response.failed", failed_payload)
            return

        if event_type == "cancelled":
            cancelled_payload = build_responses_payload(
                output_text=completed_text,
                model=model,
                session_id=str(event.get("session_id") or session_id or ""),
                response_id=response_id,
                created_at=created_at,
                status="cancelled",
                metadata=response_metadata,
                usage=event.get("usage") if isinstance(event.get("usage"), Mapping) else None,
            )
            yield _response_sse("response.cancelled", cancelled_payload)
            return

        if event_type == "completed":
            completed_text = str(event.get("output_text") or completed_text)
            if completed_text and not message_started:
                message_started = True
                text_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {"output_index": text_output_index, "item": _message_item("in_progress")},
                )
                content_started = True
                yield _response_sse(
                    "response.content_part.added",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    },
                )
            if message_started:
                yield _response_sse(
                    "response.output_text.done",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "text": completed_text,
                    },
                )
                yield _response_sse(
                    "response.content_part.done",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": completed_text},
                    },
                )
                yield _response_sse(
                    "response.output_item.done",
                    {
                        "output_index": text_output_index,
                        "item": _message_item("completed", completed_text),
                    },
                )
            if reasoning_started:
                yield _response_sse(
                    "response.output_item.done",
                    {"output_index": reasoning_output_index, "item": _reasoning_item("completed")},
                )
            final_payload = build_responses_payload(
                output_text=completed_text,
                model=event.get("model") or model,
                session_id=str(event.get("session_id") or session_id or ""),
                response_id=str(event.get("response_id") or response_id),
                created_at=created_at,
                status="completed",
                metadata=response_metadata,
                usage=event.get("usage") if isinstance(event.get("usage"), Mapping) else None,
                output_items=(
                    event.get("responses_output")
                    if isinstance(event.get("responses_output"), Sequence)
                    else None
                ),
            )
            yield _response_sse("response.completed", final_payload)
            return

    if not lifecycle_started:
        for lifecycle_chunk in _start_response_lifecycle():
            yield lifecycle_chunk
