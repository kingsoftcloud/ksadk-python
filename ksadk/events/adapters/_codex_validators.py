"""Codex adapter 的 payload 校验辅助（纯移动自 adapters.codex，行为不变）。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal, NoReturn, cast

from pydantic import JsonValue

_CODEX_ERROR_INFO_VALUES = frozenset("""
    contextWindowExceeded sessionBudgetExceeded usageLimitExceeded serverOverloaded
    cyberPolicy internalServerError unauthorized badRequest threadRollbackFailed
    sandboxError other
    """.split())
_CODEX_ERROR_INFO_VARIANTS = frozenset("""
    httpConnectionFailed responseStreamConnectionFailed responseStreamDisconnected
    responseTooManyFailedAttempts activeTurnNotSteerable
    """.split())


class CodexMappingError(ValueError):
    """A Codex app-server message violates the locked native contract."""

    def __init__(self, code: str, field_name: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field_name = field_name
        self.source = "codex"


def _fail(code: str, field_name: str, message: str) -> NoReturn:
    raise CodexMappingError(code, field_name, message)


def _request_id(value: Any, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _fail(
            "missing_native_identity",
            field_name,
            "Codex JSON-RPC id must be a string or integer",
        )
    normalized = str(value)
    if not normalized:
        raise CodexMappingError(
            "missing_native_identity", field_name, "Codex JSON-RPC id cannot be empty"
        )
    return normalized


def _safe_codex_error_info_kind(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value in _CODEX_ERROR_INFO_VALUES else "unknown"
    if isinstance(value, Mapping):
        for variant in _CODEX_ERROR_INFO_VARIANTS:
            if variant in value:
                return variant
    return "unknown"


def _validated_user_input_answers(
    result: Mapping[str, Any],
    question_ids: frozenset[str],
    secret_question_ids: frozenset[str],
) -> dict[str, JsonValue]:
    answers = _mapping(result.get("answers"), "result.answers")
    sanitized: dict[str, JsonValue] = {}
    for raw_question_id, raw_answer in answers.items():
        question_id = _required_string(raw_question_id, "result.answers question id")
        if question_id not in question_ids:
            _fail(
                "invalid_interaction_response",
                "result.answers",
                "Codex requestUserInput response contains an unknown question id",
            )
        answer = _mapping(raw_answer, f"result.answers.{question_id}")
        values = answer.get("answers")
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes))
            or any(not isinstance(value, str) for value in values)
        ):
            _fail(
                "invalid_interaction_response",
                f"result.answers.{question_id}.answers",
                "Codex requestUserInput answers must be a string array",
            )
        if question_id in secret_question_ids:
            sanitized[question_id] = {"answersPresent": True, "redacted": True}
        else:
            sanitized[question_id] = _json_value(answer)
    for question_id in sorted(secret_question_ids - sanitized.keys()):
        sanitized[question_id] = {"answersPresent": False, "redacted": True}
    return {"answers": sanitized}


def _question_schema(
    value: Any,
) -> tuple[str | None, dict[str, JsonValue], frozenset[str], frozenset[str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail(
            "invalid_interaction_request",
            "params.questions",
            "Codex requestUserInput questions must be an array",
        )
    properties: dict[str, JsonValue] = {}
    required: list[str] = []
    prompts: list[str] = []
    secret_question_ids: set[str] = set()
    for index, raw_question in enumerate(value):
        question = _mapping(raw_question, f"params.questions[{index}]")
        question_id = _required_string(question.get("id"), f"params.questions[{index}].id")
        if question_id in properties:
            _fail(
                "invalid_interaction_request",
                f"params.questions[{index}].id",
                f"Codex requestUserInput question id {question_id!r} is duplicated",
            )
        is_secret = question.get("isSecret", False)
        if not isinstance(is_secret, bool):
            _fail(
                "invalid_interaction_request",
                f"params.questions[{index}].isSecret",
                "Codex requestUserInput isSecret must be a boolean",
            )
        if is_secret:
            secret_question_ids.add(question_id)
        prompt = _required_text(question.get("question"), f"params.questions[{index}].question")
        header = _required_text(question.get("header"), f"params.questions[{index}].header")
        options = question.get("options")
        labels: list[str] = []
        option_details: list[JsonValue] = []
        if options is not None:
            if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
                _fail(
                    "invalid_interaction_request",
                    f"params.questions[{index}].options",
                    "Codex question options must be an array",
                )
            for option_index, raw_option in enumerate(options):
                option = _mapping(
                    raw_option,
                    f"params.questions[{index}].options[{option_index}]",
                )
                labels.append(
                    _required_text(
                        option.get("label"),
                        f"params.questions[{index}].options[{option_index}].label",
                    )
                )
                option_details.append(_json_value(option))
        property_schema: dict[str, JsonValue] = {
            "type": "string",
            "title": header,
            "description": prompt,
            "x-codex-options": option_details,
            "x-codex-is-secret": is_secret,
            "x-codex-is-other": bool(question.get("isOther", False)),
        }
        if labels:
            property_schema["enum"] = cast(JsonValue, labels)
        properties[question_id] = property_schema
        required.append(question_id)
        prompts.append(prompt)
    return (
        "\n".join(prompts) or None,
        {
            "type": "object",
            "properties": properties,
            "required": cast(JsonValue, required),
        },
        frozenset(properties),
        frozenset(secret_question_ids),
    )


def _approval_decision(value: Any) -> Literal["approved", "rejected", "canceled"]:
    if isinstance(value, str):
        if value in {"accept", "acceptForSession"}:
            return "approved"
        if value == "decline":
            return "rejected"
        if value == "cancel":
            return "canceled"
    elif isinstance(value, Mapping) and len(value) == 1:
        variant = next(iter(value))
        payload = _mapping(value[variant], f"result.decision.{variant}")
        if variant == "acceptWithExecpolicyAmendment":
            amendment = _mapping(
                payload.get("execpolicy_amendment"),
                "result.decision.acceptWithExecpolicyAmendment.execpolicy_amendment",
            )
            command = amendment.get("command")
            if (
                not isinstance(command, Sequence)
                or isinstance(command, (str, bytes))
                or not command
                or any(not isinstance(part, str) or not part for part in command)
            ):
                _fail(
                    "invalid_interaction_response",
                    "result.decision.acceptWithExecpolicyAmendment.execpolicy_amendment.command",
                    "Codex execpolicy amendment command must be a non-empty string array",
                )
            return "approved"
        if variant == "applyNetworkPolicyAmendment":
            amendment = _mapping(
                payload.get("network_policy_amendment"),
                "result.decision.applyNetworkPolicyAmendment.network_policy_amendment",
            )
            _required_string(
                amendment.get("host"),
                "result.decision.applyNetworkPolicyAmendment.network_policy_amendment.host",
            )
            action = _required_string(
                amendment.get("action"),
                "result.decision.applyNetworkPolicyAmendment.network_policy_amendment.action",
            )
            if action not in {"allow", "deny"}:
                _fail(
                    "invalid_interaction_response",
                    "result.decision.applyNetworkPolicyAmendment.network_policy_amendment.action",
                    f"Unsupported network policy amendment action: {action}",
                )
            return "approved"
    _fail(
        "invalid_interaction_response",
        "result.decision",
        f"Unsupported Codex approval decision: {value}",
    )


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        _fail("invalid_protocol_message", field_name, f"Codex {field_name} must be text")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(
            "invalid_protocol_message",
            field_name,
            f"Codex {field_name} must be a non-negative integer",
        )
    return value


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field_name)


def _string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail("invalid_item_snapshot", field_name, f"Codex {field_name} must be an array of text")
    if any(not isinstance(part, str) for part in value):
        _fail("invalid_item_snapshot", field_name, f"Codex {field_name} must contain only text")
    return tuple(cast(Sequence[str], value))


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return cast(JsonValue, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(
                "non_json_protocol_data",
                "protocol data",
                "Codex protocol data contains a non-finite float",
            )
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            _fail(
                "non_json_protocol_data",
                "protocol data",
                "Codex protocol object keys must be strings",
            )
        return cast(JsonValue, {key: _json_value(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return cast(JsonValue, [_json_value(item) for item in value])
    _fail(
        "non_json_protocol_data",
        "protocol data",
        f"Codex protocol value is not stably JSON serializable: {type(value).__name__}",
    )


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(
            "missing_native_identity",
            field_name,
            f"Codex {field_name} must be a non-empty string",
        )
    return value


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_protocol_message", field_name, f"Codex {field_name} must be an object")
    return value


__all__ = ["CodexMappingError"]
