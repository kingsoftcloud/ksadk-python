"""EvalSet format detection and loss-aware file loading."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml

from .contracts import AssertionSpec, EvalSetVersion


class EvalSetFormat(str, Enum):
    NATIVE = "native"
    STUDIO_SUITE = "studio_suite/v0"
    ADK = "adk_evalset/v1"


class EvalSetParseError(ValueError):
    """A user-facing, safe parse error with a stable code."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        self.code = code
        self.path = path
        super().__init__(message if path is None else f"{message}: {path}")


_LEGACY_ASSERTIONS = {
    "contains": "response.contains",
    "equals": "response.equals",
    "notContains": "response.notContains",
    "jsonSchema": "response.jsonSchema",
    "maxLatencyMs": "runtime.maxLatencyMs",
    "maxInputTokens": "runtime.maxInputTokens",
    "maxOutputTokens": "runtime.maxOutputTokens",
    "toolCalled": "tool.called",
    "toolNotCalled": "tool.notCalled",
}

_STUDIO_ROOT_FIELDS = {"apiVersion", "api_version", "kind", "metadata", "cases"}
_STUDIO_CASE_FIELDS = {"id", "input", "assertions"}
_ADK_ROOT_FIELDS = {
    "eval_set_id",
    "evalSetId",
    "name",
    "description",
    "eval_cases",
    "evalCases",
    "creation_timestamp",
    "creationTimestamp",
}
_ADK_CASE_FIELDS = {
    "eval_id",
    "evalId",
    "id",
    "conversation",
    "session_input",
    "sessionInput",
    "creation_timestamp",
    "creationTimestamp",
    "assertions",
}
_ADK_TURN_FIELDS = {
    "invocation_id",
    "invocationId",
    "user_content",
    "userContent",
    "final_response",
    "finalResponse",
    "intermediate_data",
    "intermediateData",
    "creation_timestamp",
    "creationTimestamp",
}


def identify_evalset_format(value: Mapping[str, Any]) -> EvalSetFormat:
    """Identify a supported EvalSet without attempting to execute it."""

    if value.get("schemaVersion") == "ksadk.eval/v1" or value.get("schema_version") == (
        "ksadk.eval/v1"
    ):
        return EvalSetFormat.NATIVE
    if value.get("kind") == "EvaluationSuite":
        return EvalSetFormat.STUDIO_SUITE
    if isinstance(value.get("eval_cases", value.get("evalCases")), list):
        return EvalSetFormat.ADK
    raise EvalSetParseError(
        "UNSUPPORTED_EVALSET_FORMAT",
        "无法识别评测集格式，期望 ksadk.eval/v1、EvaluationSuite 或 ADK eval_cases",
    )


def parse_evalset(value: Mapping[str, Any]) -> EvalSetVersion:
    """Parse and normalize an in-memory EvalSet mapping."""

    if not isinstance(value, Mapping):
        raise EvalSetParseError("EVALSET_ROOT_INVALID", "评测集根节点必须是对象")
    detected = identify_evalset_format(value)
    try:
        if detected is EvalSetFormat.NATIVE:
            data = dict(value)
            data.setdefault("sourceFormat", detected.value)
            return EvalSetVersion.model_validate(data)
        if detected is EvalSetFormat.STUDIO_SUITE:
            return _parse_studio_suite(value)
        return _parse_adk_evalset(value)
    except EvalSetParseError:
        raise
    except (TypeError, ValueError) as exc:
        raise EvalSetParseError("EVALSET_INVALID", "评测集内容无效") from exc


def load_evalset(path: str | Path) -> EvalSetVersion:
    """Read YAML/JSON and return the normalized, digest-bearing EvalSet."""

    file_path = Path(path)
    if not file_path.is_file():
        raise EvalSetParseError(
            "EVALSET_FILE_NOT_FOUND", "评测集文件不存在", path=str(path)
        )
    try:
        raw = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EvalSetParseError(
            "EVALSET_ENCODING_INVALID", "评测集必须是 UTF-8 文件", path=str(path)
        ) from exc
    try:
        value = json.loads(raw) if file_path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise EvalSetParseError(
            "EVALSET_FILE_INVALID",
            "评测集不是有效的 JSON/YAML",
            path=str(path),
        ) from exc
    try:
        return parse_evalset(value)
    except EvalSetParseError as exc:
        if exc.path is not None:
            raise
        raise EvalSetParseError(exc.code, str(exc), path=str(path)) from exc


def _parse_studio_suite(value: Mapping[str, Any]) -> EvalSetVersion:
    _reject_unknown_fields(value, _STUDIO_ROOT_FIELDS, "Studio EvaluationSuite")
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise EvalSetParseError(
            "EVALSET_METADATA_INVALID", "Studio EvaluationSuite 缺少 metadata"
        )
    cases = value.get("cases")
    if not isinstance(cases, list):
        raise EvalSetParseError(
            "EVALSET_CASES_INVALID", "Studio EvaluationSuite 的 cases 必须是数组"
        )
    normalized = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise EvalSetParseError(
                "EVALSET_CASE_INVALID", f"第 {index + 1} 个 case 必须是对象"
            )
        _reject_unknown_fields(case, _STUDIO_CASE_FIELDS, f"Studio case {index + 1}")
        assertions = case.get("assertions", [])
        if not isinstance(assertions, list):
            raise EvalSetParseError(
                "EVALSET_ASSERTIONS_INVALID",
                f"case {case.get('id', index)} 的 assertions 必须是数组",
            )
        normalized.append(
            {
                "id": case.get("id", f"case-{index + 1}"),
                "input": case.get("input"),
                "assertions": [_normalize_assertion(item) for item in assertions],
            }
        )
    return EvalSetVersion(
        name=str(metadata.get("name") or value.get("name") or "studio-suite"),
        cases=normalized,
        metadata=dict(metadata),
        source_format=EvalSetFormat.STUDIO_SUITE.value,
    )


def _parse_adk_evalset(value: Mapping[str, Any]) -> EvalSetVersion:
    _reject_unknown_fields(value, _ADK_ROOT_FIELDS, "ADK EvalSet")
    raw_cases = value.get("eval_cases", value.get("evalCases"))
    if not isinstance(raw_cases, list):
        raise EvalSetParseError("EVALSET_CASES_INVALID", "ADK eval_cases 必须是数组")
    normalized = []
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise EvalSetParseError(
                "EVALSET_CASE_INVALID", f"第 {index + 1} 个 ADK case 必须是对象"
            )
        _reject_unknown_fields(raw_case, _ADK_CASE_FIELDS, f"ADK case {index + 1}")
        conversations = raw_case.get("conversation", [])
        if not isinstance(conversations, list) or not conversations:
            raise EvalSetParseError(
                "EVALSET_CONVERSATION_INVALID",
                f"ADK case {index + 1} 缺少 conversation",
            )
        turns = [_adk_turn(turn, index) for turn in conversations]
        assertions = raw_case.get("assertions", [])
        if not isinstance(assertions, list):
            raise EvalSetParseError(
                "EVALSET_ASSERTIONS_INVALID",
                f"ADK case {index + 1} 的 assertions 必须是数组",
            )
        normalized.append(
            {
                "id": str(
                    raw_case.get("eval_id")
                    or raw_case.get("evalId")
                    or raw_case.get("id")
                    or f"case-{index + 1}"
                ),
                "turns": turns,
                "assertions": [_normalize_assertion(item) for item in assertions],
                "metadata": {
                    "sessionInput": raw_case.get(
                        "session_input", raw_case.get("sessionInput")
                    ),
                    "creationTimestamp": raw_case.get(
                        "creation_timestamp", raw_case.get("creationTimestamp")
                    ),
                },
            }
        )
    return EvalSetVersion(
        name=str(
            value.get("name")
            or value.get("eval_set_id")
            or value.get("evalSetId")
            or "adk-evalset"
        ),
        cases=normalized,
        metadata={
            "description": value.get("description"),
            "creationTimestamp": value.get(
                "creation_timestamp", value.get("creationTimestamp")
            ),
        },
        source_format=EvalSetFormat.ADK.value,
    )


def _adk_turn(value: Any, case_index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvalSetParseError(
            "EVALSET_TURN_INVALID",
            f"ADK case {case_index + 1} 的 turn 必须是对象",
        )
    _reject_unknown_fields(value, _ADK_TURN_FIELDS, f"ADK case {case_index + 1} turn")
    user_content = value.get("user_content", value.get("userContent"))
    final_response = value.get("final_response", value.get("finalResponse"))
    input_text = _part_text(user_content)
    output_text = _part_text(final_response)
    if not input_text:
        input_text = str(value.get("input") or "")
    if not input_text:
        raise EvalSetParseError(
            "EVALSET_INPUT_INVALID",
            f"ADK case {case_index + 1} 的 turn 缺少用户输入",
        )
    intermediate = value.get("intermediate_data", value.get("intermediateData")) or {}
    if not isinstance(intermediate, Mapping):
        raise EvalSetParseError(
            "EVALSET_INTERMEDIATE_INVALID",
            f"ADK case {case_index + 1} 的 intermediate_data 必须是对象",
        )
    _reject_unknown_fields(
        intermediate,
        {"tool_uses", "toolUses", "intermediate_responses", "intermediateResponses"},
        f"ADK case {case_index + 1} intermediate_data",
    )
    intermediate_responses = intermediate.get(
        "intermediate_responses", intermediate.get("intermediateResponses", [])
    )
    if intermediate_responses:
        raise EvalSetParseError(
            "EVALSET_UNSUPPORTED_INTERMEDIATE_RESPONSE",
            f"ADK case {case_index + 1} 含暂不支持的 intermediate_responses",
        )
    tools = intermediate.get("tool_uses", intermediate.get("toolUses", []))
    if not isinstance(tools, list):
        raise EvalSetParseError(
            "EVALSET_TOOLS_INVALID",
            f"ADK case {case_index + 1} 的工具轨迹必须是数组",
        )
    return {
        "input": input_text,
        "expected_output": output_text or None,
        "expected_tools": tools,
        "metadata": {
            "invocationId": value.get("invocation_id", value.get("invocationId")),
            "creationTimestamp": value.get(
                "creation_timestamp", value.get("creationTimestamp")
            ),
        },
    }


def _part_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    parts = value.get("parts", [])
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if not isinstance(part, Mapping) or set(part) - {"text"}:
            raise EvalSetParseError(
                "EVALSET_UNSUPPORTED_CONTENT_PART",
                "ADK EvalSet 暂只支持 text Part",
            )
        if part.get("text"):
            texts.append(str(part["text"]))
    return "\n".join(texts)


def _normalize_assertion(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvalSetParseError("EVALSET_ASSERTION_INVALID", "断言必须是对象")
    data = dict(value)
    assertion_type = data.pop("type", None)
    if assertion_type in _LEGACY_ASSERTIONS:
        assertion_type = _LEGACY_ASSERTIONS[assertion_type]
    if not isinstance(assertion_type, str):
        raise EvalSetParseError("EVALSET_ASSERTION_TYPE_INVALID", "断言缺少 type")
    data["type"] = assertion_type
    try:
        return AssertionSpec.model_validate(data).model_dump(mode="python", by_alias=False)
    except ValueError as exc:
        raise EvalSetParseError(
            "EVALSET_ASSERTION_INVALID", f"不支持或参数无效的断言: {assertion_type}"
        ) from exc


def _reject_unknown_fields(
    value: Mapping[str, Any], allowed: set[str], location: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvalSetParseError(
            "EVALSET_UNSUPPORTED_FIELD",
            f"{location} 含暂不支持的字段: {', '.join(unknown)}",
        )
