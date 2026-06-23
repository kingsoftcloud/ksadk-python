from __future__ import annotations

from typing import Any, Mapping


_VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_CHAT_COMPLETIONS_REASONING_EFFORTS = {"low", "medium", "high"}
_PASSTHROUGH_OPTION_KEYS = {"temperature", "top_p", "max_tokens", "max_completion_tokens"}


def _normalized_effort(value: Any) -> str | None:
    effort = str(value or "").strip().lower()
    return effort if effort in _VALID_REASONING_EFFORTS else None


def _is_disabled_thinking(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, Mapping):
        raw_type = str(value.get("type") or value.get("status") or "").strip().lower()
        return raw_type in {"disabled", "disable", "off", "none", "false", "0"}
    raw = str(value or "").strip().lower()
    return raw in {"disabled", "disable", "off", "none", "false", "0"}


def _is_enabled_thinking(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        raw_type = str(value.get("type") or value.get("status") or "").strip().lower()
        return raw_type in {"enabled", "enable", "on", "true", "1"}
    raw = str(value or "").strip().lower()
    return raw in {"enabled", "enable", "on", "true", "1"}


def normalize_model_options(model_options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize request model options while preserving provider-specific fallbacks.

    The runtime's canonical reasoning switch is `reasoning.effort`. Legacy
    `thinking` inputs are accepted for compatibility and kept in the normalized
    payload because some OpenAI-compatible providers still read them.
    """

    if not isinstance(model_options, Mapping):
        return {}

    normalized = dict(model_options)
    reasoning = normalized.get("reasoning")
    canonical_effort: str | None = None
    if isinstance(reasoning, Mapping):
        canonical_effort = _normalized_effort(reasoning.get("effort"))
    canonical_effort = canonical_effort or _normalized_effort(normalized.get("reasoning_effort"))

    thinking = normalized.get("thinking")
    if canonical_effort is None and "thinking_enabled" in normalized:
        thinking = bool(normalized.get("thinking_enabled"))
        normalized.setdefault(
            "thinking",
            {"type": "enabled"} if thinking else {"type": "disabled"},
        )
    if canonical_effort is None and "thinking" in normalized:
        if _is_disabled_thinking(thinking):
            canonical_effort = "none"
            if isinstance(thinking, Mapping):
                normalized["thinking"] = {**dict(thinking), "type": "disabled"}
            else:
                normalized["thinking"] = {"type": "disabled"}
            normalized.setdefault("max_reasoning_tokens", 0)
        elif _is_enabled_thinking(thinking):
            if isinstance(thinking, Mapping):
                explicit_effort = _normalized_effort(thinking.get("effort"))
                if explicit_effort:
                    canonical_effort = explicit_effort
                normalized["thinking"] = {**dict(thinking), "type": "enabled"}
            else:
                normalized["thinking"] = {"type": "enabled"}
            canonical_effort = canonical_effort or "medium"

    if canonical_effort is not None:
        existing_reasoning = dict(reasoning) if isinstance(reasoning, Mapping) else {}
        existing_reasoning["effort"] = canonical_effort
        normalized["reasoning"] = existing_reasoning

    return normalized


def model_options_for_chat_completions(model_options: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_model_options(model_options)
    payload: dict[str, Any] = {}
    for key in _PASSTHROUGH_OPTION_KEYS:
        if key in normalized:
            payload[key] = normalized[key]
    reasoning = normalized.get("reasoning")
    if isinstance(reasoning, Mapping):
        effort = _normalized_effort(reasoning.get("effort"))
        if effort in _CHAT_COMPLETIONS_REASONING_EFFORTS:
            payload["reasoning_effort"] = effort

    extra_body = dict(normalized.get("extra_body") or {})
    if "max_reasoning_tokens" in normalized:
        extra_body.setdefault("max_reasoning_tokens", normalized["max_reasoning_tokens"])
    if extra_body:
        payload["extra_body"] = extra_body
    return payload


def model_options_for_responses(model_options: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_model_options(model_options)
    payload: dict[str, Any] = {}
    for key in _PASSTHROUGH_OPTION_KEYS:
        if key in normalized:
            payload[key] = normalized[key]
    reasoning = normalized.get("reasoning")
    if isinstance(reasoning, Mapping):
        effort = _normalized_effort(reasoning.get("effort"))
        if effort:
            payload["reasoning"] = {"effort": effort}

    extra_body = dict(normalized.get("extra_body") or {})
    if "thinking" in normalized:
        extra_body.setdefault("thinking", normalized["thinking"])
    if "max_reasoning_tokens" in normalized:
        extra_body.setdefault("max_reasoning_tokens", normalized["max_reasoning_tokens"])
    if extra_body:
        payload["extra_body"] = extra_body
    return payload
