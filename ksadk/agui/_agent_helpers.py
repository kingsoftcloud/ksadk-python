"""KsadkAGUIAgent 的静态辅助函数（纯移动自 ``ksadk.agui.agent``，行为不变）。

类内保留同名 staticmethod 委托，对外 ``KsadkAGUIAgent._x`` 调用面不变。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

from ag_ui.core import Interrupt, RunAgentInput

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.events.canonical import (
    ContentSnapshot,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
)
from ksadk.events.content import DataContent, TextContent
from ksadk.runtime.adapter import RunHandle

if TYPE_CHECKING:
    from ksadk.agui.agent import _ThreadRun


def approval_decision_for_audit(status: str, payload: Any) -> str:
    """Persist a stable decision value while accepting official AG-UI envelopes."""

    if status != "resolved":
        return "rejected"
    if payload is True:
        return "approved"
    if isinstance(payload, Mapping):
        for key in ("approve", "approved"):
            if key in payload:
                return "approved" if bool(payload[key]) else "rejected"
        for key in ("decision", "type"):
            if key in payload:
                return approval_decision_for_audit(status, payload[key])
        return "rejected"
    if isinstance(payload, str) and payload.strip().lower() in {"approve", "approved"}:
        return "approved"
    return "rejected"


def durable_handle(handle: RunHandle) -> dict[str, Any]:
    allowed = {
        "agent_id",
        "user_id",
        "checkpoint_id",
        "known_checkpoint_ids",
        "pending_approval_ids",
        "framework_ref",
        "thread_id",
        "checkpoint_ns",
        "resume_thread_id",
    }
    native_ref = {key: value for key, value in handle.native_ref.items() if key in allowed}
    return {
        "run_id": handle.run_id,
        "session_id": handle.session_id,
        "runtime_type": handle.runtime_type,
        "native_ref": json_safe(native_ref),
    }


def interrupt_from_payload(payload: Mapping[str, Any]) -> Interrupt:
    interrupt_id = str(payload.get("approval_id") or payload.get("call_id") or "")
    raw_detail = payload.get("detail")
    detail = raw_detail if isinstance(raw_detail, dict) else {}
    return Interrupt(
        id=interrupt_id,
        reason=str(detail.get("reason") or payload.get("kind") or "approval"),
        message=approval_message(detail, payload),
        tool_call_id=str(payload.get("call_id") or "") or None,
        response_schema=detail.get("response_schema"),
        metadata=approval_metadata(detail, payload),
    )


def interrupt_from_interaction(event: InteractionRequested) -> Interrupt:
    detail = event.request.detail if isinstance(event.request.detail, dict) else {}
    payload = {
        "approval_id": event.interaction_id,
        "call_id": event.request.call_id or "",
        "kind": event.request.kind,
        "detail": detail,
    }
    return interrupt_from_payload(payload)


def extract_text(snapshot: ContentSnapshot | None) -> str:
    if snapshot is None:
        return ""
    for part in snapshot.parts:
        if isinstance(part, TextContent):
            return part.text
    return ""


def first_part(snapshot: ContentSnapshot | None) -> Any:
    if snapshot is None or not snapshot.parts:
        return None
    return snapshot.parts[0]


def a2ui_operations(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    surface_id: str,
) -> list[dict[str, Any]]:
    if isinstance(event, ItemStarted):
        part = first_part(event.initial)
        if isinstance(part, DataContent):
            if isinstance(part.data, list):
                return [dict(op) for op in part.data if isinstance(op, Mapping)]
            if isinstance(part.data, Mapping):
                # Surface data dict (surface_id, catalog_id, components, etc.)
                # Use project_a2ui_operations to extract canonical operations.
                return project_a2ui_operations("a2ui.surface.begin", dict(part.data))
        return []
    if isinstance(event, ItemUpdated):
        if isinstance(event.update, DataContent):
            if isinstance(event.update.data, list):
                return [dict(op) for op in event.update.data if isinstance(op, Mapping)]
            if isinstance(event.update.data, Mapping):
                return project_a2ui_operations("a2ui.surface.update", dict(event.update.data))
        return []
    # ItemCompleted (end): produce deleteSurface to preserve AG-UI wire
    if surface_id:
        return [{"version": "v0.9", "deleteSurface": {"surfaceId": surface_id}}]
    return []


def duplicate_run(input: RunAgentInput) -> "_ThreadRun":
    from ksadk.agui.agent import _ThreadRun

    return _ThreadRun(
        handle=RunHandle(
            run_id=input.run_id,
            session_id=input.thread_id,
            runtime_type="ag-ui-duplicate",
        )
    )


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def latest_user_input(input: RunAgentInput) -> Any:
    for message in reversed(input.messages):
        if getattr(message, "role", None) == "user":
            return getattr(message, "content", "")
    return ""


def input_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return str(value.get("text") or value.get("content") or "").strip()
    return str(value or "").strip()


def approval_action(detail: Mapping[str, Any]) -> Mapping[str, Any]:
    nested_request = detail.get("approval_requests")
    actions = (
        nested_request.get("action_requests")
        if isinstance(nested_request, Mapping)
        else detail.get("action_requests")
    )
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, Mapping):
                return action
    return {}


def approval_metadata(
    detail: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    action = approval_action(detail)
    arguments = (
        action.get("args")
        or action.get("arguments")
        or detail.get("arguments")
        or detail.get("args")
        or payload.get("args")
    )
    metadata: dict[str, Any] = {
        "tool_name": str(
            action.get("name")
            or detail.get("tool_name")
            or payload.get("name")
            or payload.get("kind")
            or "approval"
        ),
        "arguments": arguments if arguments is not None else {},
    }
    approval_level = (
        action.get("approval_level")
        or detail.get("approval_level")
        or payload.get("approval_level")
    )
    if approval_level:
        metadata["approval_level"] = str(approval_level)
    return metadata


def approval_message(detail: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    action = approval_action(detail)
    return str(
        action.get("description")
        or detail.get("message")
        or payload.get("message")
        or "Approval required"
    )


def resume_fingerprint(status: str, payload: Any) -> Any:
    return json.dumps([status, payload], sort_keys=True, ensure_ascii=False, default=str)


__all__ = [
    "a2ui_operations",
    "approval_action",
    "approval_decision_for_audit",
    "approval_message",
    "approval_metadata",
    "durable_handle",
    "duplicate_run",
    "extract_text",
    "first_part",
    "input_text",
    "interrupt_from_interaction",
    "interrupt_from_payload",
    "json_safe",
    "latest_user_input",
    "resume_fingerprint",
]
