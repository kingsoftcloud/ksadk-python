from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.events.runtime_event import EventType


def _event_metadata(event: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = event.get("Metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def project_session_messages(
    events: Sequence[Mapping[str, Any]],
    *,
    include_reasoning: bool = False,
    include_tool_events: bool = False,
    include_attachments: bool = True,
) -> list[dict[str, Any]]:
    """Project persisted runtime events into the chat history contract."""

    agui_invocations = _agui_invocation_ids(events)
    normalized = [
        _normalize_runtime_event(event, agui_invocations=agui_invocations) for event in events
    ]
    approval_responses = _approval_response_events(normalized)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for event in sorted(normalized, key=lambda item: int(item.get("SeqId") or 0)):
        invocation_id = str(event.get("InvocationId") or f"seq:{event.get('SeqId')}")
        groups.setdefault(invocation_id, []).append(event)

    messages: list[dict[str, Any]] = []
    for group in groups.values():
        messages.extend(
            _project_event_group(
                group,
                include_reasoning=include_reasoning,
                include_tool_events=include_tool_events,
                include_attachments=include_attachments,
                approval_responses=approval_responses,
            )
        )
    return sorted(messages, key=lambda item: int(item.get("SeqId") or 0))


def _project_event_group(
    events: Sequence[Mapping[str, Any]],
    *,
    include_reasoning: bool,
    include_tool_events: bool,
    include_attachments: bool,
    approval_responses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    reasoning = [
        {"text": _event_text(event), "SeqId": event.get("SeqId")}
        for event in events
        if event.get("EventType") == "reasoning" and _event_text(event)
    ]
    tool_events = (
        _project_tool_events(events, approval_responses=approval_responses)
        if include_tool_events
        else []
    )
    activities = _project_a2ui_activities(events)
    projected: list[dict[str, Any]] = []
    assistant_seen = False
    latest_snapshot: Mapping[str, Any] | None = None
    streamed_text = ""
    start_seq_id = min((int(event.get("SeqId") or 0) for event in events), default=0)

    for event in events:
        event_type = str(event.get("EventType") or "")
        if event_type == "user_message":
            message = _base_message(event, "user")
            if include_attachments:
                attachments = _event_attachments(event)
                if attachments:
                    message["Attachments"] = attachments
            projected.append(message)
        elif event_type == "assistant_message":
            message = _base_message(event, "assistant")
            if include_reasoning and reasoning:
                message["Reasoning"] = reasoning
            if tool_events:
                message["ToolEvents"] = tool_events
            if include_reasoning:
                blocks = _project_interleaved_blocks(events, tool_events=tool_events)
                if blocks:
                    message["Blocks"] = blocks
            projected.append(message)
            assistant_seen = True
        elif event_type == "assistant_stream_snapshot":
            latest_snapshot = event
        elif event_type == "assistant_stream_delta":
            streamed_text += _event_text(event)

    if not assistant_seen and (
        latest_snapshot is not None or streamed_text or reasoning or tool_events or activities
    ):
        anchor = next(
            (
                event
                for event in reversed(events)
                if event.get("EventType")
                in {
                    "assistant_stream_snapshot",
                    "assistant_stream_delta",
                    "approval_request",
                    "tool_call",
                    "reasoning",
                    EventType.A2UI_SURFACE_BEGIN,
                    EventType.A2UI_SURFACE_UPDATE,
                    EventType.A2UI_SURFACE_END,
                }
            ),
            events[-1],
        )
        message = _base_message(
            anchor,
            "assistant",
            content=(
                _event_text(latest_snapshot) if latest_snapshot is not None else streamed_text
            ),
        )
        if include_reasoning and reasoning:
            message["Reasoning"] = reasoning
        if tool_events:
            message["ToolEvents"] = tool_events
        if include_reasoning:
            blocks = _project_interleaved_blocks(events, tool_events=tool_events)
            if blocks:
                message["Blocks"] = blocks
        projected.append(message)

    if activities:
        assistant = next(
            (message for message in reversed(projected) if message.get("Role") == "assistant"),
            None,
        )
        if assistant is not None:
            assistant["Activities"] = activities

    for message in projected:
        message["StartSeqId"] = start_seq_id
    return projected


def _project_interleaved_blocks(
    events: Sequence[Mapping[str, Any]],
    *,
    tool_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct ordered chat blocks from persisted streaming events.

    ``assistant_stream_snapshot`` carries a cumulative text value.  The delta
    relative to the prior snapshot is the text emitted at that point in the
    event sequence.  Reasoning events written before that snapshot can
    therefore be replayed as ``thinking → text → thinking → text`` instead of
    being flattened into the legacy ``Reasoning`` and ``Content`` fields.

    Older transcripts wrote all reasoning only at terminal time.  Their first
    reasoning event appears after streamed text, so their original boundaries
    are unknowable.  Return no blocks for those transcripts and let clients
    use the existing best-effort compatibility projection.
    """

    blocks: list[dict[str, Any]] = []
    latest_snapshot_text = ""
    saw_stream_text = False
    saw_reasoning_after_stream_text = False
    tools_by_seq_id = {
        int(tool.get("SeqId") or 0): tool
        for tool in tool_events
        if int(tool.get("SeqId") or 0) > 0
    }

    def append_text(text: str, seq_id: Any) -> None:
        if not text:
            return
        if blocks and blocks[-1].get("Type") == "text":
            blocks[-1]["Content"] = f"{blocks[-1].get('Content') or ''}{text}"
            return
        blocks.append({"Type": "text", "Content": text, "SeqId": seq_id})

    def append_thinking(text: str, seq_id: Any) -> None:
        if not text:
            return
        if blocks and blocks[-1].get("Type") == "thinking":
            blocks[-1]["Content"] = f"{blocks[-1].get('Content') or ''}{text}"
            return
        blocks.append({"Type": "thinking", "Content": text, "SeqId": seq_id})

    def append_tool(event: Mapping[str, Any]) -> None:
        tool = tools_by_seq_id.get(int(event.get("SeqId") or 0))
        if not tool:
            return
        blocks.append(
            {
                "Type": "tool",
                "SeqId": tool.get("SeqId"),
                "Name": tool.get("Name") or "tool",
                "Args": tool.get("Args"),
                "Result": tool.get("Result"),
                "Status": tool.get("Status") or "completed",
                "ToolCallId": tool.get("ToolCallId"),
            }
        )

    for event in events:
        event_type = str(event.get("EventType") or "")
        text = _event_text(event)
        seq_id = event.get("SeqId")

        if event_type == "reasoning":
            metadata = _event_metadata(event)
            if saw_stream_text and metadata.get("stream_boundary") != "before_text":
                saw_reasoning_after_stream_text = True
            append_thinking(text, seq_id)
            continue
        if event_type == "tool_call":
            append_tool(event)
            continue
        if event_type == "assistant_stream_delta":
            append_text(text, seq_id)
            saw_stream_text = saw_stream_text or bool(text)
            continue
        if event_type == "assistant_stream_snapshot":
            if text.startswith(latest_snapshot_text):
                append_text(text[len(latest_snapshot_text) :], seq_id)
            elif text != latest_snapshot_text:
                # A replacement snapshot has no safe incremental boundary.
                # Keep the latest content as a standalone text block instead
                # of duplicating already-replayed output.
                append_text(text, seq_id)
            latest_snapshot_text = text
            saw_stream_text = saw_stream_text or bool(text)
            continue
        if event_type == "assistant_message":
            if latest_snapshot_text and text.startswith(latest_snapshot_text):
                append_text(text[len(latest_snapshot_text) :], seq_id)
            elif not latest_snapshot_text:
                append_text(text, seq_id)

    if saw_reasoning_after_stream_text:
        return []
    return blocks


def _base_message(
    event: Mapping[str, Any],
    role: str,
    *,
    content: str | None = None,
) -> dict[str, Any]:
    metadata = _event_metadata(event)
    message: dict[str, Any] = {
        "MessageId": event.get("EventId"),
        "Role": role,
        "Content": {"text": _event_text(event) if content is None else content},
        "Timestamp": event.get("Timestamp"),
        "SeqId": event.get("SeqId"),
        "InvocationId": event.get("InvocationId"),
    }
    for target, *sources in (
        ("ResponseId", "response_id", "ResponseId"),
        ("TraceId", "trace_id", "TraceId"),
        ("RootSpanId", "root_span_id", "rootSpanId", "RootSpanId"),
    ):
        value = next((metadata.get(source) for source in sources if metadata.get(source)), None)
        if value:
            message[target] = str(value)
    return message


def _event_text(event: Mapping[str, Any]) -> str:
    content = event.get("Content")
    if isinstance(content, str):
        return content
    if not isinstance(content, Mapping):
        return ""
    if content.get("text") is not None:
        return str(content.get("text") or "")
    return "".join(
        str(part.get("text") or "")
        for part in content.get("parts") or []
        if isinstance(part, Mapping)
    )


def _project_tool_events(
    events: Sequence[Mapping[str, Any]],
    *,
    approval_responses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []
    pending_by_key: dict[str, dict[str, Any]] = {}
    seen_result_receipts: set[str] = set()

    for event in events:
        event_type = str(event.get("EventType") or "")
        metadata = _event_metadata(event)
        if event_type == "tool_call":
            entry = _tool_event_from_call(event, metadata)
            projected.append(entry)
            pending_calls.append(entry)
            pair_key = _tool_pair_key(metadata)
            if pair_key:
                pending_by_key[pair_key] = entry
            continue
        if event_type != "tool_result":
            continue

        receipt = metadata.get("tool_receipt")
        receipt_key = (
            str(receipt.get("idempotency_key") or "") if isinstance(receipt, Mapping) else ""
        )
        if receipt_key and receipt_key in seen_result_receipts:
            continue
        if receipt_key:
            seen_result_receipts.add(receipt_key)

        name = str(metadata.get("tool_name") or "tool")
        pair_key = _tool_pair_key(metadata)
        match = pending_by_key.get(pair_key) if pair_key else None
        if match is None or match["Status"] != "running":
            match = next(
                (
                    item
                    for item in reversed(pending_calls)
                    if item["Name"] == name and item["Status"] == "running"
                ),
                None,
            )
        if match is None:
            entry = _tool_event_from_call(event, metadata)
            projected.append(entry)
        else:
            entry = match
        output = metadata.get("tool_output", _event_text(event))
        entry["Status"] = (
            "failed" if isinstance(output, Mapping) and output.get("ok") is False else "completed"
        )
        entry["Result"] = output
        entry["ResultSeqId"] = event.get("SeqId")

    projected.extend(_project_approval_events(events, responses=approval_responses))
    return projected


def _tool_pair_key(metadata: Mapping[str, Any]) -> str:
    call_id = metadata.get("call_id")
    if call_id:
        return f"call:{call_id}"
    tool_receipt = metadata.get("tool_receipt")
    if isinstance(tool_receipt, Mapping) and tool_receipt.get("tool_call_id"):
        return f"call:{tool_receipt['tool_call_id']}"
    run_id = metadata.get("run_id")
    if run_id:
        return f"run:{run_id}:{metadata.get('tool_name') or 'tool'}"
    return ""


def _tool_event_from_call(event: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    tool_receipt = metadata.get("tool_receipt")
    call_id = metadata.get("call_id") or metadata.get("run_id")
    if not call_id and isinstance(tool_receipt, Mapping):
        call_id = tool_receipt.get("tool_call_id")
    return {
        "SeqId": event.get("SeqId"),
        "Type": "tool_call",
        "Name": str(metadata.get("tool_name") or "tool"),
        "Args": metadata.get("tool_args"),
        "Status": "running",
        "ToolCallId": call_id,
    }


def _project_approval_events(
    events: Sequence[Mapping[str, Any]],
    *,
    responses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    approvals: list[dict[str, Any]] = []
    for request in events:
        if request.get("EventType") != "approval_request":
            continue
        metadata = _event_metadata(request)
        interrupt_info = metadata.get("interrupt_info")
        if not isinstance(interrupt_info, Mapping):
            interrupt_info = {}
        request_id = str(
            interrupt_info.get("approval_request_id") or interrupt_info.get("id") or ""
        )
        response = responses.get(request_id)
        response_metadata = _event_metadata(response) if response is not None else {}
        resume_input = response_metadata.get("resume_input")
        if not isinstance(resume_input, Mapping):
            resume_input = {}
        status = (
            "paused"
            if response is None
            else (
                "approved"
                if resume_input.get("approve") or resume_input.get("approved")
                else "denied"
            )
        )
        entry: dict[str, Any] = {
            "SeqId": request.get("SeqId"),
            "Type": "approval",
            "Name": str(interrupt_info.get("tool_name") or "approval"),
            "Status": status,
            "ApprovalRequestId": request_id or None,
        }
        if metadata.get("protocol") == "ag-ui":
            entry["Protocol"] = "ag-ui"
        arguments = (
            interrupt_info.get("arguments")
            or interrupt_info.get("tool_args")
            or interrupt_info.get("args")
        )
        if arguments is not None:
            entry["Args"] = arguments
        approval_level = interrupt_info.get("approval_level") or interrupt_info.get("risk_level")
        if approval_level:
            entry["ApprovalLevel"] = str(approval_level)
        approval_message = interrupt_info.get("approval_message") or interrupt_info.get("message")
        if approval_message:
            entry["ApprovalMessage"] = str(approval_message)
        approvals.append(entry)
    return approvals


def _approval_response_events(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    responses: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("EventType") != "approval_response":
            continue
        metadata = _event_metadata(event)
        resume_input = metadata.get("resume_input")
        if not isinstance(resume_input, Mapping):
            continue
        request_id = str(
            resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id")
            or resume_input.get("id")
            or ""
        )
        if request_id:
            responses[request_id] = event
    return responses


def _project_a2ui_activities(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("EventType") or "")
        if event_type not in {
            EventType.A2UI_SURFACE_BEGIN,
            EventType.A2UI_SURFACE_UPDATE,
            EventType.A2UI_SURFACE_END,
        }:
            continue
        content = event.get("Content")
        payload = content if isinstance(content, Mapping) else {}
        operations = project_a2ui_operations(event_type, payload)
        if not operations:
            continue
        surface_id = str(payload.get("surface_id") or payload.get("surfaceId") or "")
        activities.append(
            {
                "SeqId": event.get("SeqId"),
                "Type": "a2ui-surface",
                "MessageId": f"{event.get('InvocationId')}:a2ui:{surface_id}",
                "SurfaceId": surface_id,
                "Content": {
                    "surfaceId": surface_id,
                    "a2ui_operations": operations,
                },
            }
        )
    return activities


def _agui_invocation_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Locate AG-UI runs so history written before ``payload.protocol`` remains usable."""
    invocation_ids: set[str] = set()
    for event in events:
        if str(event.get("EventType") or "") != EventType.RUN_STARTED:
            continue
        if not _event_metadata(event).get("ksadk_runtime_event"):
            continue
        content = event.get("Content")
        payload = content.get("payload") if isinstance(content, Mapping) else None
        if isinstance(payload, Mapping) and payload.get("source") == "ag-ui":
            invocation_id = str(event.get("InvocationId") or "")
            if invocation_id:
                invocation_ids.add(invocation_id)
    return invocation_ids


def _normalize_runtime_event(
    event: Mapping[str, Any],
    *,
    agui_invocations: set[str],
) -> Mapping[str, Any]:
    metadata = _event_metadata(event)
    if not metadata.get("ksadk_runtime_event"):
        return event
    raw_content = event.get("Content")
    content = raw_content if isinstance(raw_content, Mapping) else {}
    payload = content.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    event_type = str(event.get("EventType") or "")
    normalized = dict(event)
    normalized["Content"] = dict(payload)
    normalized_metadata = dict(metadata)

    if event_type == EventType.RUN_STARTED and payload.get("source") == "ag-ui":
        normalized["EventType"] = "user_message"
        normalized["Content"] = {"text": _input_text(payload.get("input"))}
        normalized["Author"] = "user"
    elif event_type == EventType.TEXT_COMPLETED:
        normalized["EventType"] = "assistant_message"
    elif event_type == EventType.TEXT_DELTA:
        normalized["EventType"] = "assistant_stream_delta"
    elif event_type in {EventType.REASONING_DELTA, EventType.REASONING_COMPLETED}:
        normalized["EventType"] = "reasoning"
    elif event_type == EventType.TOOL_CALL_BEGIN:
        normalized["EventType"] = "tool_call"
        normalized_metadata.update(
            {
                "call_id": payload.get("call_id"),
                "tool_name": payload.get("name"),
                "tool_args": payload.get("args"),
            }
        )
    elif event_type == EventType.TOOL_CALL_END:
        normalized["EventType"] = "tool_result"
        normalized_metadata.update(
            {
                "call_id": payload.get("call_id"),
                "tool_name": payload.get("name"),
                "tool_output": payload.get("result", payload.get("error")),
            }
        )
    elif event_type == EventType.APPROVAL_REQUESTED:
        detail = payload.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        approval_request = detail.get("approval_requests")
        approval_request = approval_request if isinstance(approval_request, Mapping) else detail
        action_requests = approval_request.get("action_requests")
        action = (
            next(
                (item for item in action_requests if isinstance(item, Mapping)),
                {},
            )
            if isinstance(action_requests, list)
            else {}
        )
        normalized["EventType"] = "approval_request"
        normalized_metadata["interrupt_info"] = {
            "approval_request_id": payload.get("approval_id") or payload.get("call_id"),
            "id": payload.get("approval_id") or payload.get("call_id"),
            "tool_name": action.get("name")
            or approval_request.get("tool_name")
            or detail.get("tool_name")
            or payload.get("kind")
            or "approval",
            "arguments": action.get("args")
            or action.get("arguments")
            or approval_request.get("arguments")
            or approval_request.get("args")
            or detail.get("arguments")
            or detail.get("args"),
            "approval_level": action.get("approval_level")
            or approval_request.get("approval_level")
            or detail.get("approval_level")
            or payload.get("approval_level"),
            "approval_message": action.get("description")
            or approval_request.get("message")
            or detail.get("message")
            or payload.get("message"),
        }
        is_agui_approval = payload.get("protocol") == "ag-ui" or (
            str(event.get("InvocationId") or "") in agui_invocations
        )
        if is_agui_approval:
            normalized_metadata["protocol"] = "ag-ui"
    elif event_type == EventType.APPROVAL_RESOLVED:
        decision = payload.get("decision")
        normalized["EventType"] = "approval_response"
        normalized_metadata["resume_input"] = {
            "approval_request_id": payload.get("approval_id") or payload.get("call_id"),
            "approve": _approval_decision_is_approved(decision),
            "decision": decision,
        }
    normalized["Metadata"] = normalized_metadata
    return normalized


def _approval_decision_is_approved(decision: Any) -> bool:
    """Accept the historical AG-UI decision envelopes during transcript replay."""

    if decision is True:
        return True
    if isinstance(decision, Mapping):
        for key in ("approve", "approved"):
            if key in decision:
                return bool(decision[key])
        return _approval_decision_is_approved(decision.get("decision"))
    return isinstance(decision, str) and decision.strip().lower() in {"approve", "approved"}


def _input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if value.get("text") is not None:
            return str(value.get("text") or "")
        if value.get("content") is not None:
            return _input_text(value.get("content"))
    if isinstance(value, list):
        return "".join(_input_text(item) for item in value)
    return "" if value is None else str(value)


def _event_attachments(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = _event_metadata(event)
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in metadata.get("attachments") or []:
        if not isinstance(item, Mapping):
            continue
        file_uri = str(item.get("file_uri") or item.get("fileUri") or "")
        if not file_uri or file_uri in seen:
            continue
        mime = str(item.get("mime_type") or item.get("mimeType") or "")
        attachments.append(
            {
                "file_uri": file_uri,
                "name": str(item.get("display_name") or item.get("displayName") or ""),
                "mime": mime,
                "size": int(item.get("size_bytes") or item.get("sizeBytes") or 0),
                "is_image": mime.startswith("image/"),
                "url": "/agentengine/api/v1/AttachmentContent?FileUri=" + quote(file_uri, safe=""),
            }
        )
        seen.add(file_uri)
    return attachments
