"""Project terminal facts without inventing tool execution or approval results."""

from collections.abc import Mapping, Sequence
from typing import Any


def settle_unfinished_tool_history(
    tools: list[dict[str, Any]], events: Sequence[Mapping[str, Any]],
) -> None:
    """Each caller group belongs to one original invocation, including its end."""
    terminal_types = {"run.completed", "run.failed", "run.canceled", "run.cancelled"}
    terminal_statuses = {"completed", "failed", "error", "canceled", "cancelled", "aborted"}
    ended = False
    for event in events:
        metadata = event.get("Metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if metadata.get("runtime_parent_scope_id"):
            continue
        content = event.get("Content")
        content = content if isinstance(content, Mapping) else {}
        if event.get("EventType") in terminal_types or (
            event.get("EventType") == "run_status"
            and str(content.get("status") or "").lower() in terminal_statuses
        ):
            ended = True
            break
    if ended:
        for tool in tools:
            if tool.get("Status") == "running":
                tool["Status"] = "unknown"


def normalize_interaction_history_envelope(
    event: Mapping[str, Any], content: Mapping[str, Any], metadata: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Keep Interaction/v1 identity in the existing legacy history read model."""
    envelope = content.get("session_event")
    if not (
        metadata.get("ksadk_session_event_envelope") is True
        and metadata.get("family") == "interaction"
        and metadata.get("family_version") == 1
        and isinstance(envelope, Mapping)
        and envelope.get("family") == "interaction"
        and envelope.get("family_version") == 1
    ):
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return None
    event_type = str(event.get("EventType") or "")
    identity = payload.get("interaction_id")
    if not (
        isinstance(identity, str) and identity
        and payload.get("event_type") == envelope.get("event_type") == event_type
        and payload.get("session_id") == envelope.get("session_id") == event.get("SessionId")
        and payload.get("run_id") == envelope.get("run_id") == event.get("InvocationId")
    ):
        return None
    normalized = dict(event)
    projected_metadata = dict(metadata)
    if event_type == "interaction.requested":
        request = payload.get("request")
        request = request if isinstance(request, Mapping) else {}
        normalized["EventType"] = "approval_request"
        normalized["Content"] = {"detail": request}
        projected_metadata["interrupt_info"] = {
            **request, "approval_request_id": identity, "id": identity,
            "tool_name": request.get("tool_name") or payload.get("kind") or "approval",
        }
    elif event_type == "interaction.resolved":
        outcome = str(payload.get("outcome") or "")
        normalized["EventType"] = "approval_response"
        normalized["Content"] = {"detail": {"outcome": outcome}}
        projected_metadata["resume_input"] = {
            "approval_request_id": identity, "decision": outcome,
            "approve": outcome == "approved",
        }
    else:
        return None
    normalized["Metadata"] = projected_metadata
    return normalized
