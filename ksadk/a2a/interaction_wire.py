"""Public A2A interaction hints, never permits or native resume references."""

import json

from ksadk.events.canonical import ApprovalRequest
from ksadk.kernel.approval_presentation import approval_presentation


def approval_from_presentation(*, call_id: str, presentation: dict) -> ApprovalRequest:
    """Project the Kernel's public preview, without copying its private record."""
    title = presentation.get("title") or "Remote approval"
    description = presentation.get("description") or "Approval required"
    try:
        payload = json.loads(description)
    except (ValueError, TypeError):
        payload = None
    arguments = payload.get("arguments") if isinstance(payload, dict) else None
    detail = {
        "name": title,
        "args": arguments if isinstance(arguments, dict) else {"description": description},
        "prompt": description,
    }
    risk = payload.get("risk") if isinstance(payload, dict) else None
    if isinstance(risk, str) and risk in {"low", "medium", "high", "critical"}:
        detail["risk"] = risk
    return ApprovalRequest(call_id=call_id, kind="tool", detail=detail)


def approval_metadata(request: ApprovalRequest) -> dict:
    """Preserve review semantics while keeping continuation authority private.

    Legacy consumers can render the enum as structured input. New consumers
    must still commit the user's decision through their own interaction ledger;
    this remote declaration cannot authorize a call or select a provider.
    """
    presentation = approval_presentation(request)
    return {
        "input_schema": {"type": "string", "enum": ["approve", "reject"]},
        "ksadk_interaction": {
            "version": 1,
            "kind": "approval",
            "allowed_decisions": ["approve", "reject"],
            "presentation": {
                "title": presentation.title,
                "description": presentation.description,
            },
        },
    }


def parse_approval_metadata(metadata: dict, *, call_id: str) -> ApprovalRequest | None:
    """Reject malformed typed hints; never read provider or resume data from wire."""
    value = metadata.get("ksadk_interaction")
    if value is None:
        return None
    if (not isinstance(value, dict)
            or set(value) != {"version", "kind", "allowed_decisions", "presentation"}
            or isinstance(value.get("version"), bool) or value.get("version") != 1
            or value.get("kind") != "approval"
            or value.get("allowed_decisions") != ["approve", "reject"]):
        raise ValueError("A2A_INTERACTION_DECLARATION_INVALID")
    presentation = value["presentation"]
    if (not isinstance(presentation, dict)
            or set(presentation) != {"title", "description"}
            or not isinstance(presentation["title"], str)
            or len(presentation["title"]) > 128
            or not isinstance(presentation["description"], str)
            or len(presentation["description"]) > 96000
            or metadata.get("input_schema") != {"type": "string", "enum": ["approve", "reject"]}):
        raise ValueError("A2A_INTERACTION_DECLARATION_INVALID")
    projected = approval_from_presentation(call_id=call_id, presentation=presentation)
    # Reapply redaction to untrusted remote preview fields.
    safe = approval_presentation(projected)
    return approval_from_presentation(
        call_id=call_id, presentation={"title": safe.title, "description": safe.description},
    )
