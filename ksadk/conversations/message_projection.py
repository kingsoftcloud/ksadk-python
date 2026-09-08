from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from ksadk.agui.a2ui_projection import project_a2ui_operations


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
    """Project persisted runtime events into the chat history contract.

    公开承诺字段（契约声明见 ``ksadk/events/projections.py``，执行形态为
    ``tests/protocol/test_cross_projection_golden.py``）：
    - 每条消息含 ``Role``/``Content.text``/``SeqId``/``StartSeqId``；
    - 开启 include_reasoning 时含 ``Reasoning``；开启 include_tool_events 时
      含 ``ToolEvents``（approval 项含 ``ApprovalRequestId``）；
    - A2UI 项以 ``Activities`` 携带（内含 ``surfaceId``）。

    内部不保证字段：分组实现细节、事件归并产生的中间键、未经开关开启的
    可选区块。
    """

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
    # Split tool events: approval-only events before first assistant_message
    # vs all tool events. When approval events precede a text completion, they
    # get their own assistant message placeholder.
    all_tool_events = (
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

    # Check if there are approval events before the first assistant_message.
    # When approval events precede a text completion, they get their own
    # assistant message placeholder with empty text.
    has_assistant_message = any(
        str(e.get("EventType") or "") == "assistant_message" for e in events
    )
    pre_assistant_tool_events: list[dict[str, Any]] = []
    post_assistant_tool_events: list[dict[str, Any]] = []
    if has_assistant_message and all_tool_events:
        # Split: approval events go to pre_assistant, tool_call/tool_result
        # go to post_assistant.
        for te in all_tool_events:
            if te.get("Type") == "approval":
                pre_assistant_tool_events.append(te)
            else:
                post_assistant_tool_events.append(te)
    else:
        post_assistant_tool_events = list(all_tool_events)

    has_pre_assistant_approvals = bool(pre_assistant_tool_events)

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
            # If there are approval events that preceded this assistant_message,
            # emit a placeholder assistant message for them first.
            if has_pre_assistant_approvals and not assistant_seen:
                anchor = next(
                    (e for e in events
                     if str(e.get("EventType") or "") == "approval_request"),
                    events[0],
                )
                placeholder = _base_message(anchor, "assistant", content="")
                if include_tool_events and pre_assistant_tool_events:
                    placeholder["ToolEvents"] = pre_assistant_tool_events
                projected.append(placeholder)
                assistant_seen = True
                # Don't add tool_events/reasoning to the next message — they
                # belong to the placeholder.
            message = _base_message(event, "assistant")
            # Attach reasoning to the first non-placeholder assistant message.
            # When has_pre_assistant_approvals is True, the placeholder was
            # already emitted, so this is the real assistant message.
            if include_reasoning and reasoning:
                message["Reasoning"] = reasoning
            if include_tool_events and post_assistant_tool_events:
                message["ToolEvents"] = post_assistant_tool_events
            if include_reasoning:
                blocks = _project_interleaved_blocks(events, tool_events=post_assistant_tool_events)
                if blocks:
                    message["Blocks"] = blocks
            projected.append(message)
            assistant_seen = True
        elif event_type == "assistant_stream_snapshot":
            latest_snapshot = event
        elif event_type == "assistant_stream_delta":
            streamed_text += _event_text(event)

    if not assistant_seen and (
        latest_snapshot is not None or streamed_text or reasoning or all_tool_events or activities
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
                    "a2ui.surface.begin",
                    "a2ui.surface.update",
                    "a2ui.surface.end",
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
        if all_tool_events:
            message["ToolEvents"] = all_tool_events
        if include_reasoning:
            blocks = _project_interleaved_blocks(events, tool_events=all_tool_events)
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


def _approval_status_from_resume(response: Any, resume_input: Mapping[str, Any]) -> str:
    """从 approval_response 事件的 resume_input 判断审批结果状态。

    支持两种协议：
    - mcp_approval_response: resume_input 含 approve/approved bool
    - ksadk_resume + decisions: resume_input.value.decisions[0].type 为
      approve/edit/reject（HumanInTheLoopMiddleware 协议）
    """
    if response is None:
        return "paused"
    # ksadk_resume + decisions 协议
    value = resume_input.get("value")
    if isinstance(value, Mapping):
        decisions = value.get("decisions")
        if isinstance(decisions, list) and decisions:
            first = decisions[0]
            if isinstance(first, Mapping):
                decision_type = str(first.get("type") or "").strip().lower()
                if decision_type in {"approve", "edit"}:
                    return "approved"
                if decision_type in {"reject", "respond"}:
                    return "denied"
    # mcp_approval_response 协议
    if resume_input.get("approve") or resume_input.get("approved"):
        return "approved"
    return "denied"


def _project_approval_events(
    events: Sequence[Mapping[str, Any]],
    *,
    responses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    approvals: list[dict[str, Any]] = []
    for request in events:
        event_type = str(request.get("EventType") or "")
        # 识别归一化后的 approval_request，以及未归一化的 canonical interaction.requested。
        if event_type not in {"approval_request", "interaction.requested"}:
            continue
        metadata = _event_metadata(request)
        # 归一化路径：interrupt_info 在 metadata；canonical 路径：在 Content.runtime_event.request.detail。
        interrupt_info = metadata.get("interrupt_info")
        if not isinstance(interrupt_info, Mapping):
            # 尝试从 canonical event 的 Content.runtime_event 提取
            raw_content = request.get("Content")
            content = raw_content if isinstance(raw_content, Mapping) else {}
            runtime_event = content.get("runtime_event")
            if isinstance(runtime_event, Mapping):
                req = runtime_event.get("request") or {}
                if isinstance(req, Mapping):
                    detail = req.get("detail")
                    if isinstance(detail, Mapping):
                        # 从 detail.action_requests[0] 提取（HumanInTheLoopMiddleware 结构）
                        action_requests = detail.get("action_requests")
                        first_action = (
                            action_requests[0]
                            if isinstance(action_requests, list) and action_requests
                            else {}
                        )
                        if not isinstance(first_action, Mapping):
                            first_action = {}
                        interrupt_info = {
                            "approval_request_id": runtime_event.get("interaction_id") or req.get("call_id"),
                            "id": runtime_event.get("interaction_id") or req.get("call_id"),
                            "tool_name": detail.get("tool_name") or first_action.get("name") or req.get("kind"),
                            "arguments": detail.get("arguments") or detail.get("args") or first_action.get("args") or first_action.get("arguments"),
                            "description": detail.get("description") or first_action.get("description"),
                            "review_configs": detail.get("review_configs"),
                            "approval_message": detail.get("message") or first_action.get("description"),
                        }
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
        status = _approval_status_from_resume(response, resume_input)
        # HumanInTheLoopMiddleware 的 interrupt_info 把 tool_name 嵌在
        # action_requests[0].name 里，runtime path 存的事件可能没有顶层 tool_name。
        # 这里统一提取，避免 Name fallback 成 "approval" 导致前端显示不对。
        action_requests = interrupt_info.get("action_requests")
        first_action = (
            action_requests[0]
            if isinstance(action_requests, list) and action_requests
            else {}
        )
        if not isinstance(first_action, Mapping):
            first_action = {}
        tool_name = (
            interrupt_info.get("tool_name")
            or first_action.get("name")
            or "approval"
        )
        entry: dict[str, Any] = {
            "SeqId": request.get("SeqId"),
            "Type": "approval",
            "Name": str(tool_name),
            "Status": status,
            "ApprovalRequestId": request_id or None,
        }
        if metadata.get("protocol") == "ag-ui":
            entry["Protocol"] = "ag-ui"
        # arguments/description 优先从顶层取（_get_interrupt_info/归一化已提取），
        # fallback 到 action_requests[0]（runtime path 存的原始 interrupt_info）。
        arguments = (
            interrupt_info.get("arguments")
            or interrupt_info.get("tool_args")
            or interrupt_info.get("args")
            or first_action.get("args")
            or first_action.get("arguments")
        )
        if arguments is not None:
            entry["Args"] = arguments
        description = (
            interrupt_info.get("description")
            or first_action.get("description")
        )
        if description:
            entry["Description"] = str(description)
        review_configs = interrupt_info.get("review_configs")
        if isinstance(review_configs, list) and review_configs:
            first_review = review_configs[0]
            if isinstance(first_review, Mapping):
                allowed = first_review.get("allowed_decisions")
                if isinstance(allowed, list) and allowed:
                    entry["AllowedDecisions"] = [str(d) for d in allowed]
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
            "a2ui.surface.begin",
            "a2ui.surface.update",
            "a2ui.surface.end",
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
        if str(event.get("EventType") or "") != "run.started":
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


_CANONICAL_EVENT_TYPE_MAP = {
    "run.started": "run.started",
    "run.completed": "run.completed",
    "run.failed": "run.failed",
    "run.canceled": "run.canceled",
    "run.interrupted": "run.interrupted",
    "item.started": "item.started",
    "item.updated": "item.updated",
    "item.completed": "item.completed",
    "interaction.requested": "approval.requested",
}


def _normalize_canonical_event(
    event: Mapping[str, Any],
    content: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Project a canonical v2 SessionEvent into the legacy v1 wire shape."""
    runtime_event = content.get("runtime_event")
    if not isinstance(runtime_event, Mapping):
        return event
    event_type = str(runtime_event.get("event_type") or "")
    item_kind = str(runtime_event.get("item_kind") or "")
    raw_source = runtime_event.get("source") or {}
    source = raw_source if isinstance(raw_source, Mapping) else {}
    normalized = dict(event)
    normalized_metadata = dict(metadata)

    if event_type == "run.started":
        source_metadata = source.get("metadata") if isinstance(source, Mapping) else None
        if isinstance(source_metadata, Mapping) and source_metadata.get("source") == "ag-ui":
            normalized["EventType"] = "user_message"
            normalized["Content"] = {"text": _input_text(source_metadata.get("input"))}
            normalized["Author"] = "user"
            normalized["Metadata"] = normalized_metadata
            return normalized
        normalized["EventType"] = "run.started"
        normalized["Content"] = {"status": runtime_event.get("status", "running")}
    elif event_type == "run.completed":
        normalized["EventType"] = "run.completed"
        normalized["Content"] = {"status": "completed"}
    elif event_type == "run.failed":
        normalized["EventType"] = "run.failed"
        error = runtime_event.get("error") or {}
        normalized["Content"] = {"status": "failed", "error": error.get("message", "")}
    elif event_type == "run.canceled":
        normalized["EventType"] = "run.canceled"
        normalized["Content"] = {"status": "canceled"}
    elif event_type == "run.interrupted":
        normalized["EventType"] = "run.interrupted"
        normalized["Content"] = {"status": "interrupted"}
    elif event_type == "item.started":
        if item_kind == "tool_call":
            initial = runtime_event.get("initial") or {}
            parts = initial.get("parts") if isinstance(initial, Mapping) else None
            part = parts[0] if isinstance(parts, list) and parts else {}
            call_id = part.get("call_id", "")
            name = part.get("name", "")
            args = part.get("arguments", {})
            normalized["EventType"] = "tool_call"
            normalized["Content"] = {"call_id": call_id, "name": name, "args": args}
            normalized_metadata.update({"call_id": call_id, "tool_name": name, "tool_args": args})
        elif item_kind == "data" and source.get("protocol") == "a2ui":
            surface_id = str(source.get("metadata", {}).get("surface_id") or "")
            source_metadata = source.get("metadata")
            initial = runtime_event.get("initial") or {}
            parts = initial.get("parts") if isinstance(initial, Mapping) else []
            data: Any = {}
            for part in (parts or []):
                if isinstance(part, Mapping) and part.get("content_type") == "data":
                    data = part.get("data")
                    break
            if (
                isinstance(data, list)
                and isinstance(source_metadata, Mapping)
                and source_metadata.get("operation_batch") is True
            ):
                normalized["Content"] = {
                    "surface_id": surface_id,
                    "a2ui_operations": data,
                }
            elif isinstance(data, list):
                normalized["Content"] = {"surface_id": surface_id, "components": data}
            elif isinstance(data, Mapping):
                normalized["Content"] = data
            else:
                normalized["Content"] = {"surface_id": surface_id}
            normalized["EventType"] = "a2ui.surface.begin"
        else:
            return event
    elif event_type == "item.completed":
        if item_kind == "message":
            snapshot = runtime_event.get("snapshot") or {}
            parts = snapshot.get("parts") if isinstance(snapshot, Mapping) else None
            text = ""
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, Mapping) and part.get("content_type") == "text":
                        text = part.get("text", "")
                        break
            if not text:
                return event
            normalized["EventType"] = "assistant_message"
            normalized["Content"] = {"role": "model", "parts": [{"text": text}]}
            normalized["Author"] = "assistant"
        elif item_kind == "tool_call":
            return event
        elif item_kind == "tool_result":
            snapshot = runtime_event.get("snapshot") or {}
            parts = snapshot.get("parts") if isinstance(snapshot, Mapping) else None
            part = parts[0] if isinstance(parts, list) and parts else {}
            call_id = part.get("call_id", "")
            result = part.get("result", "")
            normalized["EventType"] = "tool_result"
            normalized["Content"] = {"call_id": call_id, "name": "", "result": result}
            normalized_metadata.update({"call_id": call_id, "tool_output": result})
        elif item_kind == "data" and source.get("protocol") == "a2ui":
            source_metadata = source.get("metadata")
            if (
                isinstance(source_metadata, Mapping)
                and source_metadata.get("operation_batch") is True
            ):
                return event
            surface_id = str(source.get("metadata", {}).get("surface_id") or "")
            normalized["EventType"] = "a2ui.surface.end"
            normalized["Content"] = {"surface_id": surface_id}
        else:
            return event
    elif event_type == "item.updated":
        if item_kind == "message":
            update = runtime_event.get("update") or {}
            text = update.get("text", "") if isinstance(update, Mapping) else ""
            normalized["EventType"] = "assistant_stream_delta"
            normalized["Content"] = {"role": "model", "parts": [{"text": text}]}
        elif item_kind == "reasoning":
            update = runtime_event.get("update") or {}
            text = update.get("text", "") if isinstance(update, Mapping) else ""
            normalized["EventType"] = "reasoning"
            normalized["Content"] = {"role": "model", "parts": [{"text": text}]}
        elif item_kind == "data" and source.get("protocol") == "a2ui":
            surface_id = str(source.get("metadata", {}).get("surface_id") or "")
            update = runtime_event.get("update") or {}
            update_data = update.get("data") if isinstance(update, Mapping) else None
            if isinstance(update_data, list):
                normalized["Content"] = {"surface_id": surface_id, "components": update_data}
            elif isinstance(update_data, Mapping):
                normalized["Content"] = update_data
            else:
                normalized["Content"] = {"surface_id": surface_id}
            normalized["EventType"] = "a2ui.surface.update"
        else:
            return event
    elif event_type == "interaction.requested":
        interaction_kind = str(runtime_event.get("interaction_kind") or "approval")
        request = runtime_event.get("request") or {}
        if not isinstance(request, Mapping):
            request = {}
        if interaction_kind == "approval":
            interaction_id = str(runtime_event.get("interaction_id") or "")
            call_id = str(request.get("call_id") or "")
            kind = str(request.get("kind") or "approval")
            detail = request.get("detail")
            if not isinstance(detail, Mapping):
                detail = {}
            # HumanInTheLoopMiddleware 的 detail 把 tool_name/arguments/description
            # 嵌在 action_requests[0] 里，detail 顶层没有，需要提取。
            action_requests = detail.get("action_requests")
            first_action = (
                action_requests[0]
                if isinstance(action_requests, list) and action_requests
                else {}
            )
            if not isinstance(first_action, Mapping):
                first_action = {}
            review_configs = detail.get("review_configs")
            normalized["EventType"] = "approval_request"
            normalized["Content"] = {"detail": detail}
            normalized_metadata["interrupt_info"] = {
                "approval_request_id": interaction_id or call_id,
                "id": interaction_id or call_id,
                "tool_name": detail.get("tool_name")
                or first_action.get("name")
                or kind,
                "arguments": detail.get("arguments")
                or detail.get("args")
                or first_action.get("args")
                or first_action.get("arguments"),
                "description": detail.get("description")
                or first_action.get("description"),
                "review_configs": review_configs if isinstance(review_configs, list) else None,
                "approval_level": detail.get("approval_level")
                or first_action.get("approval_level"),
                "approval_message": detail.get("message")
                or first_action.get("description"),
            }
            # Preserve ag-ui protocol tag from source metadata.
            source = runtime_event.get("source") or {}
            source_metadata = source.get("metadata") if isinstance(source, Mapping) else None
            if isinstance(source_metadata, Mapping) and source_metadata.get("protocol") == "ag-ui":
                normalized_metadata["protocol"] = "ag-ui"
        else:
            # structured_input: project as approval_request but with
            # structured input schema in detail.
            interaction_id = str(runtime_event.get("interaction_id") or "")
            normalized["EventType"] = "approval_request"
            normalized["Content"] = {"detail": request}
            normalized_metadata["interrupt_info"] = {
                "approval_request_id": interaction_id,
                "id": interaction_id,
                "tool_name": "structured_input",
                "arguments": None,
            }
    elif event_type == "interaction.resolved":
        interaction_kind = str(runtime_event.get("interaction_kind") or "")
        response = runtime_event.get("response") or {}
        if not isinstance(response, Mapping):
            response = {}
        response_type = str(response.get("response_type") or "")
        interaction_id = str(runtime_event.get("interaction_id") or "")
        if response_type == "approval":
            decision = str(response.get("decision") or "")
            normalized["EventType"] = "approval_response"
            normalized["Content"] = {"detail": response}
            normalized_metadata["resume_input"] = {
                "approval_request_id": interaction_id,
                "approve": decision in ("approved", "approve", True),
                "decision": decision,
            }
            source = runtime_event.get("source") or {}
            source_metadata = source.get("metadata") if isinstance(source, Mapping) else None
            if isinstance(source_metadata, Mapping) and source_metadata.get("protocol") == "ag-ui":
                normalized_metadata["protocol"] = "ag-ui"
        else:
            normalized["EventType"] = "approval_response"
            normalized["Content"] = {"detail": response}
            normalized_metadata["resume_input"] = {
                "approval_request_id": interaction_id,
                "approve": True,
                "decision": "approved",
            }
    else:
        return event

    normalized["Metadata"] = normalized_metadata
    return normalized


def _normalize_runtime_event(
    event: Mapping[str, Any],
    *,
    agui_invocations: set[str],
) -> Mapping[str, Any]:
    metadata = _event_metadata(event)
    is_canonical = metadata.get("ksadk_canonical_runtime_event")
    if not metadata.get("ksadk_runtime_event") and not is_canonical:
        return event
    raw_content = event.get("Content")
    content = raw_content if isinstance(raw_content, Mapping) else {}
    if is_canonical:
        return _normalize_canonical_event(event, content, metadata)
    payload = content.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    event_type = str(event.get("EventType") or "")
    normalized = dict(event)
    normalized["Content"] = dict(payload)
    normalized_metadata = dict(metadata)

    if event_type == "run.started" and payload.get("source") == "ag-ui":
        normalized["EventType"] = "user_message"
        normalized["Content"] = {"text": _input_text(payload.get("input"))}
        normalized["Author"] = "user"
    elif event_type == "text.completed":
        normalized["EventType"] = "assistant_message"
    elif event_type == "text.delta":
        normalized["EventType"] = "assistant_stream_delta"
    elif event_type in {"reasoning.delta", "reasoning.completed"}:
        normalized["EventType"] = "reasoning"
    elif event_type == "tool.call.begin":
        normalized["EventType"] = "tool_call"
        normalized_metadata.update(
            {
                "call_id": payload.get("call_id"),
                "tool_name": payload.get("name"),
                "tool_args": payload.get("args"),
            }
        )
    elif event_type == "tool.call.end":
        normalized["EventType"] = "tool_result"
        normalized_metadata.update(
            {
                "call_id": payload.get("call_id"),
                "tool_name": payload.get("name"),
                "tool_output": payload.get("result", payload.get("error")),
            }
        )
    elif event_type == "approval.requested":
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
            "description": action.get("description")
            or approval_request.get("description")
            or detail.get("description"),
            "review_configs": approval_request.get("review_configs")
            or detail.get("review_configs"),
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
    elif event_type == "approval.resolved":
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
