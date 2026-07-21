from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote


def project_session_messages(
    events: Sequence[Mapping[str, Any]],
    *,
    include_reasoning: bool = False,
    include_tool_events: bool = False,
    include_attachments: bool = True,
) -> list[dict[str, Any]]:
    """Project persisted runtime events into the chat history contract."""

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for event in sorted(events, key=lambda item: int(item.get("SeqId") or 0)):
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
            )
        )
    return sorted(messages, key=lambda item: int(item.get("SeqId") or 0))


def _project_event_group(
    events: Sequence[Mapping[str, Any]],
    *,
    include_reasoning: bool,
    include_tool_events: bool,
    include_attachments: bool,
) -> list[dict[str, Any]]:
    reasoning = [
        {"text": _event_text(event), "SeqId": event.get("SeqId")}
        for event in events
        if event.get("EventType") == "reasoning" and _event_text(event)
    ]
    tool_events = _project_tool_events(events) if include_tool_events else []
    projected: list[dict[str, Any]] = []
    assistant_seen = False
    latest_snapshot: Mapping[str, Any] | None = None
    seen_assistant: set[tuple[str, str]] = set()
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
            metadata = event.get("Metadata") if isinstance(event.get("Metadata"), Mapping) else {}
            dedup_key = (str(metadata.get("response_id") or ""), _event_text(event))
            if dedup_key in seen_assistant:
                continue
            seen_assistant.add(dedup_key)
            message = _base_message(event, "assistant")
            if include_reasoning and reasoning:
                message["Reasoning"] = reasoning
            if tool_events:
                message["ToolEvents"] = tool_events
            projected.append(message)
            assistant_seen = True
        elif event_type == "assistant_stream_snapshot":
            latest_snapshot = event

    if not assistant_seen and (latest_snapshot is not None or reasoning or tool_events):
        anchor = next(
            (
                event
                for event in reversed(events)
                if event.get("EventType")
                in {"assistant_stream_snapshot", "approval_request", "tool_call", "reasoning"}
            ),
            events[-1],
        )
        message = _base_message(
            anchor,
            "assistant",
            content=_event_text(latest_snapshot) if latest_snapshot is not None else "",
        )
        if include_reasoning and reasoning:
            message["Reasoning"] = reasoning
        if tool_events:
            message["ToolEvents"] = tool_events
        projected.append(message)

    for message in projected:
        message["StartSeqId"] = start_seq_id
    return projected


def _base_message(
    event: Mapping[str, Any],
    role: str,
    *,
    content: str | None = None,
) -> dict[str, Any]:
    metadata = event.get("Metadata") if isinstance(event.get("Metadata"), Mapping) else {}
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


def _project_tool_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []
    pending_by_key: dict[str, dict[str, Any]] = {}
    seen_result_receipts: set[str] = set()

    for event in events:
        event_type = str(event.get("EventType") or "")
        metadata = event.get("Metadata") if isinstance(event.get("Metadata"), Mapping) else {}
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
            str(receipt.get("idempotency_key") or "")
            if isinstance(receipt, Mapping)
            else ""
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

    projected.extend(_project_approval_events(events))
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


def _tool_event_from_call(
    event: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, Any]:
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


def _project_approval_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    responses: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("EventType") != "approval_response":
            continue
        metadata = event.get("Metadata") if isinstance(event.get("Metadata"), Mapping) else {}
        resume_input = metadata.get("resume_input")
        if not isinstance(resume_input, Mapping):
            continue
        request_id = str(
            resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id")
            or resume_input.get("id")
            or ""
        )
        responses[request_id] = event

    approvals: list[dict[str, Any]] = []
    for request in events:
        if request.get("EventType") != "approval_request":
            continue
        metadata = request.get("Metadata") if isinstance(request.get("Metadata"), Mapping) else {}
        interrupt_info = metadata.get("interrupt_info")
        if not isinstance(interrupt_info, Mapping):
            interrupt_info = {}
        request_id = str(
            interrupt_info.get("approval_request_id") or interrupt_info.get("id") or ""
        )
        response = responses.get(request_id)
        response_metadata = (
            response.get("Metadata")
            if response is not None and isinstance(response.get("Metadata"), Mapping)
            else {}
        )
        resume_input = response_metadata.get("resume_input")
        if not isinstance(resume_input, Mapping):
            resume_input = {}
        status = "paused" if response is None else (
            "approved"
            if resume_input.get("approve") or resume_input.get("approved")
            else "denied"
        )
        entry: dict[str, Any] = {
            "SeqId": request.get("SeqId"),
            "Type": "approval",
            "Name": str(interrupt_info.get("tool_name") or "approval"),
            "Status": status,
            "ApprovalRequestId": request_id or None,
        }
        arguments = (
            interrupt_info.get("arguments")
            or interrupt_info.get("tool_args")
            or interrupt_info.get("args")
        )
        if arguments is not None:
            entry["Args"] = arguments
        approvals.append(entry)
    return approvals


def _event_attachments(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = event.get("Metadata") if isinstance(event.get("Metadata"), Mapping) else {}
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
