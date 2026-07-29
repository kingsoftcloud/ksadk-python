from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from ksadk.conversations.context import (
    canonical_event_type,
)
from ksadk.conversations.model_options import normalize_model_options
from ksadk.conversations.run_kinds import (
    RUN_MODE_UNKNOWN,
    validate_run_mode,
)
from ksadk.conversations.runtime_persistence import append_conversation_event
from ksadk.sessions import SessionEvent
from ksadk.tools.gateway import (
    build_tool_receipt_idempotency_key,
)


def _has_pending_approval(events: Sequence[SessionEvent]) -> bool:
    pending = 0
    for event in events:
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type == "approval_request":
            pending += 1
        elif event_type == "approval_response" and pending > 0:
            pending -= 1
    return pending > 0


def _approval_request_id_from_event(event: SessionEvent) -> str:
    metadata = event.metadata or {}
    interrupt_info = metadata.get("interrupt_info")
    if isinstance(interrupt_info, Mapping):
        value = interrupt_info.get("approval_request_id") or interrupt_info.get("id")
        if value:
            return str(value)
    return str(event.id or "")


def _pending_approval_events(events: Sequence[SessionEvent]) -> list[SessionEvent]:
    pending: list[SessionEvent] = []
    for event in events:
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type == "approval_request":
            pending.append(event)
            continue
        if event_type != "approval_response" or not pending:
            continue
        resume_input = (event.metadata or {}).get("resume_input")
        response_id = ""
        if isinstance(resume_input, Mapping):
            response_id = str(
                resume_input.get("approval_request_id")
                or resume_input.get("interrupt_id")
                or resume_input.get("id")
                or ""
            )
        if response_id:
            pending = [
                item for item in pending if _approval_request_id_from_event(item) != response_id
            ]
        else:
            pending.pop()
    return pending


def _approval_request_events(events: Sequence[SessionEvent]) -> list[SessionEvent]:
    return [
        event
        for event in events
        if canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        == "approval_request"
    ]


def _approval_resume_run_mode(
    events: Sequence[SessionEvent],
    resume_input: Mapping[str, Any],
    *,
    fallback: str,
) -> str:
    target_ids = {
        str(value)
        for value in (
            resume_input.get("approval_request_id"),
            resume_input.get("interrupt_id"),
            resume_input.get("id"),
        )
        if value
    }
    approval_events = _pending_approval_events(events) or _approval_request_events(events)
    for approval_event in reversed(approval_events):
        approval_id = _approval_request_id_from_event(approval_event)
        if target_ids and approval_id not in target_ids:
            continue
        approval_invocation_id = str(approval_event.invocation_id or "")
        if not approval_invocation_id:
            continue
        for event in reversed(events):
            if event.event_type != "run_status" or event.invocation_id != approval_invocation_id:
                continue
            metadata = event.metadata or {}
            state_delta = event.state_delta or {}
            active_run = state_delta.get("active_run") if isinstance(state_delta, Mapping) else None
            state_mode = active_run.get("run_mode") if isinstance(active_run, Mapping) else None
            mode = validate_run_mode(str(metadata.get("run_mode") or state_mode or ""))
            if mode != RUN_MODE_UNKNOWN:
                return str(mode)
            break
    return str(validate_run_mode(fallback))


def _parse_approval_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _approval_decision_from_resume(resume_input: Mapping[str, Any]) -> dict[str, Any]:
    if "approve" in resume_input:
        approved = bool(resume_input.get("approve"))
    elif "approved" in resume_input:
        approved = bool(resume_input.get("approved"))
    else:
        approved = bool(
            resume_input.get("value", {}).get("approved")
            if isinstance(resume_input.get("value"), Mapping)
            else True
        )
    decision = {
        "approved": approved,
        "approval_request_id": str(
            resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id")
            or resume_input.get("id")
            or ""
        ),
    }
    if resume_input.get("reason") is not None:
        decision["reason"] = str(resume_input.get("reason") or "")
    return decision


def _consecutive_approval_denials_from_events(events: Sequence[SessionEvent]) -> int:
    denials = 0
    for event in reversed(events):
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type != "approval_response":
            continue
        resume_input = (event.metadata or {}).get("resume_input")
        if not isinstance(resume_input, Mapping):
            break
        decision = resume_input.get("approval")
        if not isinstance(decision, Mapping):
            decision = _approval_decision_from_resume(resume_input)
        if bool(decision.get("approved") or decision.get("approve")):
            break
        denials += 1
    return denials


def _normalize_approval_resume_input(
    resume_input: Mapping[str, Any],
    events: Sequence[SessionEvent],
    *,
    include_resolved: bool = False,
) -> dict[str, Any]:
    normalized = dict(resume_input)
    if not _is_approval_resume_input(normalized):
        return normalized

    approval_request_id = str(
        normalized.get("approval_request_id") or normalized.get("interrupt_id") or ""
    )
    pending_events = (
        _approval_request_events(events) if include_resolved else _pending_approval_events(events)
    )
    matched_event = None
    for event in reversed(pending_events):
        if not approval_request_id or _approval_request_id_from_event(event) == approval_request_id:
            matched_event = event
            break
    if matched_event is None:
        return normalized

    interrupt_info = (matched_event.metadata or {}).get("interrupt_info")
    if not isinstance(interrupt_info, Mapping):
        return normalized
    if not (interrupt_info.get("tool_name") or normalized.get("tool_name")):
        return normalized

    decision = _approval_decision_from_resume(normalized)
    tool_args = _parse_approval_arguments(
        interrupt_info.get("arguments")
        or interrupt_info.get("tool_args")
        or interrupt_info.get("args")
        or {}
    )
    tool_args["approval"] = decision

    if not normalized.get("approval_request_id"):
        request_id = _approval_request_id_from_event(matched_event)
        if request_id:
            normalized["approval_request_id"] = request_id
            decision["approval_request_id"] = request_id
    if interrupt_info.get("tool_name") and not normalized.get("tool_name"):
        normalized["tool_name"] = str(interrupt_info.get("tool_name"))
    if interrupt_info.get("run_id") and not normalized.get("run_id"):
        normalized["run_id"] = str(interrupt_info.get("run_id"))
    normalized["approval"] = decision
    normalized["tool_args"] = tool_args
    return normalized


def _builtin_tool_callable(tool_name: str):
    name = str(tool_name or "").strip()
    if not name:
        return None
    if name in {
        "write_workspace_file",
        "write_workspace_files",
        "delete_workspace_file",
    }:
        from ksadk.toolsets import workspace

        return getattr(workspace, name, None)
    if name == "execute_skills":
        from ksadk.toolsets.skills import execute_skills

        return execute_skills
    if name in {"run_command", "run_code"}:
        from ksadk.toolsets import sandbox

        return getattr(sandbox, name, None)
    return None


def _tool_receipt_metadata(
    *,
    session_id: str,
    run_id: str,
    tool_name: str,
    tool_args: Mapping[str, Any],
    tool_call_id: str | None = None,
    checkpoint_id: str | None = None,
    framework: str | None = None,
    framework_ref: Mapping[str, Any] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    idempotency_key = build_tool_receipt_idempotency_key(
        session_id=session_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_args=tool_args,
    )
    return {
        "receipt_id": f"tr_{idempotency_key.removeprefix('tool_receipt:')[:24]}",
        "idempotency_key": idempotency_key,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id or run_id,
        "run_id": run_id,
        "checkpoint_id": checkpoint_id or "",
        "framework": framework or "",
        "framework_ref": dict(framework_ref or {}),
        "status": status,
        "created_at": time.time(),
    }


def _tool_receipt_status_from_output(output: Any) -> str:
    if not isinstance(output, Mapping):
        return "failed"
    status = str(output.get("status") or "").strip().lower()
    if status == "accepted_not_extracted":
        return "completed"
    return "completed" if output.get("ok") is not False else "failed"


def _tool_resume_run_id(resume_input: Mapping[str, Any]) -> str:
    return str(
        resume_input.get("run_id")
        or resume_input.get("call_id")
        or resume_input.get("approval_request_id")
        or resume_input.get("interrupt_id")
        or ""
    )


def _tool_receipt_idempotency_key_for_resume(
    *,
    session_id: str,
    resume_input: Mapping[str, Any],
) -> str | None:
    tool_name = str(resume_input.get("tool_name") or "").strip()
    if not tool_name:
        return None
    tool_args = resume_input.get("tool_args")
    if not isinstance(tool_args, Mapping):
        return None
    run_id = _tool_resume_run_id(resume_input)
    if not run_id:
        return None
    idempotency_key = build_tool_receipt_idempotency_key(
        session_id=session_id,
        run_id=run_id,
        tool_call_id=run_id,
        tool_name=tool_name,
        tool_args=dict(tool_args),
    )
    return str(idempotency_key) if idempotency_key else None


def _find_tool_receipt_event_by_key(
    events: Sequence[SessionEvent],
    idempotency_key: str,
) -> SessionEvent | None:
    for event in reversed(events):
        if event.event_type != "tool_result":
            continue
        metadata = event.metadata or {}
        receipt = metadata.get("tool_receipt")
        if not isinstance(receipt, Mapping):
            continue
        if str(receipt.get("idempotency_key") or "") == idempotency_key:
            return event
    return None


def _latest_checkpoint_metadata_for_run(
    events: Sequence[SessionEvent],
    run_id: str,
) -> dict[str, Any]:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return {}
    for event in reversed(events):
        if event.event_type != "run_checkpoint":
            continue
        metadata = event.metadata or {}
        if str(metadata.get("run_id") or "").strip() != normalized_run_id:
            continue
        checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            continue
        framework_ref = metadata.get("framework_ref")
        return {
            "checkpoint_id": checkpoint_id,
            "framework": str(metadata.get("framework") or ""),
            "framework_ref": dict(framework_ref) if isinstance(framework_ref, Mapping) else {},
        }
    return {}


async def _execute_approved_builtin_tool_resume(
    *,
    session_id: str,
    invocation_id: str,
    resume_input: Mapping[str, Any],
    session_service_provider: Callable[[], Any],
    existing_events: Sequence[SessionEvent] | None = None,
) -> dict[str, Any] | None:
    approval = resume_input.get("approval")
    if not isinstance(approval, Mapping) or not bool(approval.get("approved")):
        return None
    tool_name = str(resume_input.get("tool_name") or "").strip()
    tool_func = _builtin_tool_callable(tool_name)
    if tool_func is None:
        return None

    tool_args = resume_input.get("tool_args")
    if not isinstance(tool_args, Mapping):
        return None
    call_args = dict(tool_args)
    run_id = _tool_resume_run_id(resume_input)
    service = session_service_provider()
    if existing_events is None:
        existing_events = await service.get_events(session_id)
    checkpoint_metadata = _latest_checkpoint_metadata_for_run(existing_events, run_id)
    receipt = _tool_receipt_metadata(
        session_id=session_id,
        run_id=run_id,
        tool_call_id=run_id,
        tool_name=tool_name,
        tool_args=call_args,
        checkpoint_id=checkpoint_metadata.get("checkpoint_id"),
        framework=checkpoint_metadata.get("framework"),
        framework_ref=checkpoint_metadata.get("framework_ref"),
    )
    existing_event = _find_tool_receipt_event_by_key(
        existing_events,
        receipt["idempotency_key"],
    )
    if existing_event is not None:
        existing_metadata = existing_event.metadata or {}
        output = existing_metadata.get("tool_output", "")
        if isinstance(output, Mapping):
            output = {**dict(output), "replayed": True}
        replayed_receipt = {
            **dict((existing_metadata.get("tool_receipt") or receipt)),
            "replayed": True,
            "replayed_from_event_id": existing_event.id,
        }
        await append_conversation_event(
            session_id=session_id,
            author="tool",
            role="user",
            text=str(output),
            invocation_id=invocation_id,
            event_type="tool_result",
            session_service_provider=session_service_provider,
            metadata={
                "tool_name": tool_name,
                "tool_args": call_args,
                "tool_output": output,
                "run_id": run_id,
                "approval_request_id": resume_input.get("approval_request_id")
                or resume_input.get("interrupt_id"),
                "tool_receipt": replayed_receipt,
                "replayed": True,
            },
        )
        return {
            "type": "function_call_output",
            "call_id": run_id,
            "output": output,
        }

    try:
        if tool_name in {"run_command", "run_code"}:
            output = await asyncio.to_thread(tool_func, **call_args)
        else:
            output = tool_func(**call_args)
    except Exception as exc:
        output = {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}

    receipt["status"] = _tool_receipt_status_from_output(output)
    await append_conversation_event(
        session_id=session_id,
        author="tool",
        role="user",
        text=str(output),
        invocation_id=invocation_id,
        event_type="tool_result",
        session_service_provider=session_service_provider,
        metadata={
            "tool_name": tool_name,
            "tool_args": call_args,
            "tool_output": output,
            "run_id": run_id,
            "approval_request_id": resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id"),
            "tool_receipt": receipt,
        },
    )
    return {
        "type": "function_call_output",
        "call_id": run_id,
        "output": output,
    }


def _is_approval_resume_input(resume_input: Mapping[str, Any]) -> bool:
    return str(resume_input.get("type") or "").strip() in {
        "mcp_approval_response",
        "ksadk_resume",
        "ksadk.approval_response",
    }


def _is_checkpoint_resume_input(resume_input: Mapping[str, Any]) -> bool:
    return str(resume_input.get("type") or "").strip() == "agentengine.resume_checkpoint"


def _failed_status_for_resume(resume_input: Mapping[str, Any] | None) -> str:
    """checkpoint resume 失败时返回 resume_failed，否则返回 failed。

    仅 checkpoint resume 的失败才写 resume_failed（独立终态，触发 SSE [DONE]
    并让前端展示"恢复失败"）；approval/ksadk_resume 等其他 resume 失败仍写 failed。
    """
    if resume_input is not None and _is_checkpoint_resume_input(resume_input):
        return "resume_failed"
    return "failed"


def _normalize_checkpoint_resume_input(resume_input: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(resume_input.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("Checkpoint resume requires run_id")

    checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        raise ValueError("Checkpoint resume requires checkpoint_id")

    framework = str(resume_input.get("framework") or "langgraph").strip() or "langgraph"
    raw_framework_ref = resume_input.get("framework_ref")
    framework_ref = dict(raw_framework_ref) if isinstance(raw_framework_ref, Mapping) else {}
    raw_framework_detail = framework_ref.get(framework)
    framework_detail = (
        dict(raw_framework_detail) if isinstance(raw_framework_detail, Mapping) else {}
    )
    framework_detail.setdefault("checkpoint_id", checkpoint_id)
    if resume_input.get("thread_id") and not framework_detail.get("thread_id"):
        framework_detail["thread_id"] = str(resume_input.get("thread_id"))
    framework_ref[framework] = framework_detail

    resume_attempt_id = str(resume_input.get("resume_attempt_id") or "").strip()
    if not resume_attempt_id:
        resume_attempt_id = f"resume_{uuid.uuid4().hex}"

    return {
        "type": "agentengine.resume_checkpoint",
        "run_id": run_id,
        "checkpoint_id": checkpoint_id,
        "resume_attempt_id": resume_attempt_id,
        "framework": framework,
        "framework_ref": framework_ref,
        "metadata": dict(resume_input.get("metadata") or resume_input.get("Metadata") or {}),
        "checkpoint_metadata": dict(
            resume_input.get("checkpoint_metadata")
            or resume_input.get("CheckpointMetadata")
            or resume_input.get("metadata")
            or resume_input.get("Metadata")
            or {}
        ),
        "resume_instruction_enabled": bool(
            resume_input.get("resume_instruction_enabled")
            or resume_input.get("ResumeInstructionEnabled")
        ),
        "resume_instruction": str(
            resume_input.get("resume_instruction") or resume_input.get("ResumeInstruction") or ""
        ).strip(),
    }


def _agentengine_resume_metadata(resume_input: Mapping[str, Any] | None) -> dict[str, Any]:
    if not resume_input or not _is_checkpoint_resume_input(resume_input):
        return {}
    return {
        "agentengine": {
            "action": "resume_checkpoint",
            "run_id": str(resume_input.get("run_id") or ""),
            "checkpoint_id": str(resume_input.get("checkpoint_id") or ""),
            "resume_attempt_id": str(resume_input.get("resume_attempt_id") or ""),
            "framework": str(resume_input.get("framework") or ""),
            "framework_ref": dict(resume_input.get("framework_ref") or {}),
        }
    }


def _extract_agentengine_metadata(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    agentengine = metadata.get("agentengine")
    if not isinstance(agentengine, Mapping):
        return {}
    return {"agentengine": dict(agentengine)}


def _checkpoint_event_args_from_agentengine_metadata(
    agentengine_metadata: Mapping[str, Any] | None,
    *,
    fallback_run_id: str,
) -> dict[str, Any] | None:
    if not isinstance(agentengine_metadata, Mapping):
        return None
    framework = str(agentengine_metadata.get("framework") or "langgraph").strip() or "langgraph"
    raw_framework_ref = agentengine_metadata.get("framework_ref")
    if not isinstance(raw_framework_ref, Mapping):
        return None
    framework_ref = dict(raw_framework_ref)
    raw_framework_detail = framework_ref.get(framework)
    if not isinstance(raw_framework_detail, Mapping):
        return None
    framework_detail = dict(raw_framework_detail)
    checkpoint_id = str(framework_detail.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        return None
    run_id = str(agentengine_metadata.get("run_id") or fallback_run_id or "").strip()
    if not run_id:
        return None
    phase = str(agentengine_metadata.get("phase") or "").strip()
    display_metadata = {
        key: value
        for key, value in agentengine_metadata.items()
        if key
        not in {
            "run_id",
            "checkpoint_id",
            "framework",
            "framework_ref",
            "phase",
        }
    }
    return {
        "run_id": run_id,
        "checkpoint_id": checkpoint_id,
        "framework": framework,
        "framework_ref": framework_ref,
        "phase": phase,
        "metadata": display_metadata,
    }


def _merge_agentengine_metadata(
    *metadata_items: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for metadata in metadata_items:
        if not isinstance(metadata, Mapping):
            continue
        agentengine = metadata.get("agentengine")
        if not isinstance(agentengine, Mapping):
            continue
        next_agentengine = dict(agentengine)
        merged.update(next_agentengine)
        framework_ref = agentengine.get("framework_ref")
        if isinstance(framework_ref, Mapping):
            merged_framework_ref = merged.get("framework_ref")
            existing_framework_ref = (
                merged_framework_ref if isinstance(merged_framework_ref, Mapping) else {}
            )
            merged["framework_ref"] = {
                **dict(existing_framework_ref),
                **dict(framework_ref),
            }
    return {"agentengine": merged} if merged else {}


def _format_resume_response_text(resume_input: Mapping[str, Any]) -> str:
    item_type = str(resume_input.get("type") or "resume")
    if item_type == "mcp_approval_response":
        approval_request_id = str(resume_input.get("approval_request_id") or "")
        approve = resume_input.get("approve")
        reason = str(resume_input.get("reason") or "").strip()
        parts = [f"mcp_approval_response approval_request_id={approval_request_id}"]
        if approve is not None:
            parts.append(f"approve={bool(approve)}")
        if reason:
            parts.append(f"reason={reason}")
        return " ".join(parts)

    if item_type == "function_call_output":
        output = resume_input.get("output", "")
        if isinstance(output, (dict, list)):
            output_text = json.dumps(output, ensure_ascii=False, sort_keys=True)
        else:
            output_text = str(output)
        return (
            f"function_call_output call_id={resume_input.get('call_id') or ''} output={output_text}"
        )

    return f"{item_type} {json.dumps(dict(resume_input), ensure_ascii=False, sort_keys=True)}"


def _stringify_responses_item_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _responses_output_item_text(item: Mapping[str, Any]) -> str:
    for text_field in ("output_text", "text", "summary_text", "delta"):
        value = item.get(text_field)
        if isinstance(value, str) and value:
            return value
    summary = item.get("summary")
    if isinstance(summary, Sequence) and not isinstance(summary, (str, bytes, bytearray)):
        parts: list[str] = []
        for part in summary:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text") or part.get("summary_text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)
    content = item.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _semantic_events_from_responses_output(output: Sequence[Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for raw_item in output:
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item_type = str(item.get("type") or "").strip()
        item_id = str(item.get("id") or item.get("item_id") or "")
        call_id = str(item.get("call_id") or "")
        if item_type == "function_call":
            name = str(item.get("name") or item.get("tool_name") or "tool")
            args = _stringify_responses_item_value(
                item.get("arguments")
                if "arguments" in item
                else item.get("args", item.get("input"))
            )
            if item_id:
                tool_names[item_id] = name
            if call_id:
                tool_names[call_id] = name
            events.append(
                {
                    "type": "tool_call",
                    "name": name,
                    "args": args,
                    "run_id": call_id or item_id or None,
                }
            )
            continue
        if item_type == "function_call_output":
            name = (
                tool_names.get(call_id)
                or tool_names.get(item_id)
                or str(item.get("name") or "tool")
            )
            output_text = _stringify_responses_item_value(
                item.get("output") if "output" in item else item.get("result", item.get("content"))
            )
            events.append(
                {
                    "type": "tool_result",
                    "name": name,
                    "output": output_text,
                    "run_id": call_id or item_id or None,
                }
            )
            continue
        if item_type in {"reasoning", "reasoning_summary", "reasoning_summary_text"}:
            text = _responses_output_item_text(item)
            if text:
                events.append({"type": "thinking", "delta": text})
    return events


def _model_options_disable_reasoning(model_options: Mapping[str, Any] | None) -> bool:
    normalized = normalize_model_options(model_options)
    reasoning = normalized.get("reasoning")
    if isinstance(reasoning, Mapping):
        effort = str(reasoning.get("effort") or "").strip().lower()
        if effort in {"none", "off", "disabled", "disable", "false", "0"}:
            return True
    max_reasoning_tokens = normalized.get("max_reasoning_tokens")
    if max_reasoning_tokens is not None:
        try:
            return int(max_reasoning_tokens) <= 0
        except (TypeError, ValueError):
            pass
    thinking = normalized.get("thinking")
    if isinstance(thinking, Mapping):
        raw_type = str(thinking.get("type") or thinking.get("status") or "").strip().lower()
        return raw_type in {"disabled", "disable", "off", "none", "false", "0"}
    if isinstance(thinking, bool):
        return not thinking
    raw_thinking = str(thinking or "").strip().lower()
    return raw_thinking in {"disabled", "disable", "off", "none", "false", "0"}


def _filter_responses_reasoning_output(output: Sequence[Any]) -> list[Any]:
    filtered: list[Any] = []
    for raw_item in output:
        if isinstance(raw_item, Mapping):
            item_type = str(raw_item.get("type") or "").strip()
            if item_type in {"reasoning", "reasoning_summary", "reasoning_summary_text"}:
                continue
        filtered.append(raw_item)
    return filtered
