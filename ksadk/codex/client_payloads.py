"""Small, dependency-light payload mappings for the Codex client."""

from __future__ import annotations

from typing import Any, Optional


def _coerce_enum(enum_type: Any, value: Any) -> Any:
    if value is None or isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        supported = ", ".join(member.value for member in enum_type)
        raise ValueError(
            f"unsupported {enum_type.__name__} {value!r}; expected {supported}"
        ) from exc


def thread_kwargs(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Map runtime config to the official AsyncThread.start surface."""

    from openai_codex import ApprovalMode, Sandbox  # type: ignore[import-not-found]

    config = config or {}
    known = {
        "approval_mode", "base_instructions", "cwd", "developer_instructions", "ephemeral", "model",
        # ``summary`` is a turn-level reasoning setting.  It is accepted by
        # ``AsyncThread.turn`` but not by the thread start/resume methods in
        # current openai-codex releases.  Keeping it out here prevents a
        # follow-up/resume from failing before the turn can even start.
        "personality", "sandbox", "service_tier",
    }
    result = {key: value for key, value in config.items() if key in known}
    result.setdefault("ephemeral", True)
    if config.get("sandbox_read_only", False):
        result["sandbox"] = Sandbox.read_only
        result.setdefault("approval_mode", ApprovalMode.deny_all)
    else:
        result["sandbox"] = _coerce_enum(Sandbox, result.get("sandbox"))
    result["approval_mode"] = _coerce_enum(ApprovalMode, result.get("approval_mode"))
    return {key: value for key, value in result.items() if value is not None}


def turn_kwargs(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Map runtime config to the official AsyncThread.turn surface."""

    from openai_codex import ApprovalMode, Sandbox  # type: ignore[import-not-found]

    config = config or {}
    known = {
        "approval_mode", "cwd", "effort", "model", "output_schema",
        "personality", "sandbox", "service_tier", "summary",
    }
    result = {key: value for key, value in config.items() if key in known}
    if config.get("sandbox_read_only", False):
        result["sandbox"] = Sandbox.read_only
        result.setdefault("approval_mode", ApprovalMode.deny_all)
    else:
        result["sandbox"] = _coerce_enum(Sandbox, result.get("sandbox"))
    result["approval_mode"] = _coerce_enum(ApprovalMode, result.get("approval_mode"))
    return {key: value for key, value in result.items() if value is not None}


def coerce_input(prompt: Any) -> Any:
    return prompt


def notification_to_event_dict(notification: Any) -> Optional[dict[str, Any]]:
    payload = notification.payload
    if hasattr(payload, "model_dump"):
        params = payload.model_dump(mode="json", by_alias=True)
    else:
        params = getattr(payload, "params", None)
        if not isinstance(params, dict):
            params = {}
    method = str(getattr(notification, "method", ""))
    supported_methods = {
        "item/started", "item/completed", "item/agentMessage/delta",
        "item/autoApprovalReview/started", "item/autoApprovalReview/completed",
        "thread/tokenUsage/updated", "turn/started", "turn/completed", "error",
        "item/reasoning/summaryTextDelta", "item/reasoning/textDelta",
        "item/reasoning/summaryPartAdded", "thread/goal/updated", "thread/goal/cleared",
    }
    return {"method": method, "params": params} if method in supported_methods else None
