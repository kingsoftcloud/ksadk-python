"""Safe OpenAI-compatible model client for local Studio runs."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from ksadk.studio.contracts import NetworkPolicy, ResolvedModel, Usage
from ksadk.studio.errors import StudioError

_DENIED_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.azure.internal",
    "fd00:ec2::254",
}

#: 表示输出被 token 上限截断的 finishReason 集合（chat 与 Responses 两种 wire）。
_LENGTH_FINISH_REASONS = {"length", "incomplete", "max_output_tokens"}
#: length 截断重试时把 max_tokens 提到的目标值；未配置 max_tokens 的 profile
#: 首次截断重试也用该值兜底（authoring 输出完整 JSON 需要宽松上限）。
_LENGTH_RETRY_TARGET_TOKENS = 16384
_REPETITION_MIN_UNIT_CHARS = 24
_REPETITION_MAX_UNIT_CHARS = 512
_REPETITION_COUNT = 4

_LOGGER = logging.getLogger(__name__)

_DSML_INVOKE_RE = re.compile(
    r"<\s*(?:\|\s*){1,2}DSML\s*(?:\|\s*){1,2}invoke\b(?P<attrs>[^>]*)>"
    r"(?P<body>.*?)"
    r"<\s*(?:\|\s*){1,2}DSML\s*(?:\|\s*){1,2}/invoke\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    r"<\s*(?:\|\s*){1,2}DSML\s*(?:\|\s*){1,2}parameter\b(?P<attrs>[^>]*)>"
    r"(?P<value>.*?)"
    r"<\s*(?:\|\s*){1,2}DSML\s*(?:\|\s*){1,2}/parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_CALLS_WRAPPER_RE = re.compile(
    r"<\s*(?:\|\s*){1,2}DSML\s*(?:\|\s*){1,2}/?calls\s*>",
    re.IGNORECASE,
)
_DSML_ATTRIBUTE_RE = re.compile(
    r"(?P<name>[A-Za-z_][\w.-]*)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_DSML_SIMPLE_ARGUMENT_RE = re.compile(
    r"<(?P<name>[A-Za-z_][\w.-]*)>(?P<value>.*?)</(?P=name)>",
    re.DOTALL,
)
_DSML_STREAM_LOOKBEHIND = 64
_GLM_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?P<name>[A-Za-z_][\w.-]*)\s*(?P<body>.*?)</tool_call>",
    re.IGNORECASE | re.DOTALL,
)
_GLM_ARGUMENT_TAG_RE = re.compile(
    r"<(?P<tag>arg_key|arg_value)>(?P<value>.*?)</(?:arg_key|arg_value)>",
    re.IGNORECASE | re.DOTALL,
)
_GLM_ASSIGNMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z_][\w.-]*)\s*=\s*(?P<quote>['\"])(?P<value>.*)(?P=quote)$",
    re.DOTALL,
)
_GLM_ARGUMENT_KEY_RESIDUE_RE = re.compile(
    r"(?:</?\s*arg_(?:key|value)\s*>)+$",
    re.IGNORECASE,
)
_GLM_MALFORMED_TOOL_START_RE = re.compile(
    r"<\s*tool_call\s*>\s*(?P<name>[A-Za-z_][\w.-]*)",
    re.IGNORECASE,
)
_GLM_MALFORMED_KEY_RE = re.compile(
    r"(?:<\s*arg_value\s*>\s*)?(?P<name>[A-Za-z_][\w.-]*)\s*"
    r"</\s*arg_key\s*>",
    re.IGNORECASE,
)
_GLM_MALFORMED_ASSIGNMENT_RE = re.compile(
    r"(?P<name>[A-Za-z_][\w.-]*)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)"
    r"\s*</\s*arg_value\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SAFE_TOOL_NAME_ALIASES = {
    # GLM 5.2 has emitted this shorter DSML name for the explicitly
    # advertised workspace writer. The alias is accepted only when the target
    # tool is present in the current request's declared schema.
    "writefile": "writeworkspacefile",
}
_SAFE_TOOL_ARGUMENT_ALIASES = {
    "write_workspace_file": {
        "filename": "path",
        "filepath": "path",
    },
}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    raw: dict[str, Any] = field(default_factory=dict)


def _tool_call_arguments_text(value: Any) -> str:
    """Return provider tool arguments as valid JSON text when possible."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _accumulate_chat_tool_calls(
    fragments: dict[int, dict[str, str]],
    calls: Any,
    *,
    snapshot: bool,
) -> None:
    """Merge incremental or final-snapshot chat-completion tool calls.

    Most OpenAI-compatible providers stream arguments through
    ``choice.delta.tool_calls``. Some gateways only include the complete
    function call on the finishing choice (under ``message.tool_calls`` or
    ``choice.tool_calls``). Supporting both forms prevents a valid long call
    from degrading to an empty ``{}`` invocation at stream completion.
    """

    if not isinstance(calls, list):
        return
    for fallback_index, raw_call in enumerate(calls):
        if not isinstance(raw_call, dict):
            continue
        raw_index = raw_call.get("index")
        try:
            index = int(raw_index if raw_index is not None else fallback_index)
        except (TypeError, ValueError):
            index = fallback_index
        function = raw_call.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        current = fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})
        incoming_id = str(raw_call.get("id") or "")
        incoming_name = str(function.get("name") or raw_call.get("name") or "")
        if "arguments" in function:
            raw_arguments = function.get("arguments")
        elif "arguments" in raw_call:
            raw_arguments = raw_call.get("arguments")
        elif "input" in function:
            raw_arguments = function.get("input")
        else:
            raw_arguments = raw_call.get("input")
        incoming_arguments = _tool_call_arguments_text(raw_arguments)
        if snapshot:
            if incoming_id:
                current["id"] = incoming_id
            if incoming_name:
                current["name"] = incoming_name
            # Do not let an empty/default snapshot overwrite arguments that
            # were already reconstructed from deltas.
            if incoming_arguments and (incoming_arguments != "{}" or not current["arguments"]):
                current["arguments"] = incoming_arguments
            continue
        current["id"] += incoming_id
        current["name"] += incoming_name
        current["arguments"] += incoming_arguments


def _reasoning_from_responses_output(output: Any) -> str:
    """从 Responses wire 的 output 数组提取 reasoning 项文本（无则空串）。"""

    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        for block in item.get("summary") or []:
            if isinstance(block, dict) and block.get("type") in {"summary_text", "text"}:
                parts.append(str(block.get("text") or ""))
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in {"reasoning_text", "text"}:
                parts.append(str(part.get("text") or ""))
    return "".join(parts)


@dataclass(frozen=True)
class ModelResponse:
    content: str
    finish_reason: str
    usage: Usage
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]
    #: 推理文本（chat wire 的 reasoning_content / Responses wire 的 reasoning 项）。
    reasoning: str = ""


@dataclass(frozen=True)
class ModelStreamChunk:
    """One OpenAI-compatible chat stream delta."""

    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    done: bool = False


def _dsml_attributes(value: str) -> dict[str, str]:
    return {
        match.group("name"): html.unescape(match.group("value"))
        for match in _DSML_ATTRIBUTE_RE.finditer(value)
    }


def _declared_tool_names(tools: list[dict[str, Any]] | None) -> dict[str, str]:
    declared: dict[str, str] = {}
    for tool in tools or []:
        function = tool.get("function") or tool
        name = str(function.get("name") or "").strip()
        if name:
            declared[re.sub(r"[^a-z0-9]", "", name.lower())] = name
    return declared


def _canonical_declared_tool_name(
    requested_name: str,
    declared: dict[str, str],
) -> str | None:
    key = re.sub(r"[^a-z0-9]", "", requested_name.lower())
    canonical = declared.get(key)
    if canonical:
        return canonical
    alias_target = _SAFE_TOOL_NAME_ALIASES.get(key)
    canonical = declared.get(alias_target or "")
    if canonical:
        return canonical
    # GLM 5.2 occasionally appends a leaked textual call id to a declared
    # name (for example ``write_file_ide49a``). Only strip this narrowly
    # shaped suffix and only accept the prefix when it resolves to an
    # explicitly advertised tool/alias.
    leaked_id = re.fullmatch(r"(?P<name>.+?)_id[a-z0-9]+", requested_name, re.IGNORECASE)
    if leaked_id:
        prefix = re.sub(r"[^a-z0-9]", "", leaked_id.group("name").lower())
        return declared.get(prefix) or declared.get(_SAFE_TOOL_NAME_ALIASES.get(prefix, ""))
    return None


def _canonical_declared_argument_name(
    requested_name: str,
    canonical_tool_name: str,
    declared_properties: frozenset[str],
) -> str | None:
    if requested_name in declared_properties:
        return requested_name
    key = re.sub(r"[^a-z0-9]", "", requested_name.lower())
    for declared_name in declared_properties:
        if re.sub(r"[^a-z0-9]", "", declared_name.lower()) == key:
            return declared_name
    alias = _SAFE_TOOL_ARGUMENT_ALIASES.get(canonical_tool_name, {}).get(key)
    return alias if alias in declared_properties else None


def _declared_tool_properties(
    tools: list[dict[str, Any]] | None,
) -> dict[str, frozenset[str]]:
    """Return declared JSON argument names keyed by canonical tool name.

    A few OpenAI-compatible GLM gateways have emitted a native function-call
    JSON key such as ``task</arg_key>``. We only repair that wire residue when
    the cleaned name is an explicitly declared property; arbitrary model keys
    are never guessed or renamed.
    """

    declared: dict[str, frozenset[str]] = {}
    for tool in tools or []:
        function = tool.get("function") or tool
        name = str(function.get("name") or "").strip()
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") if isinstance(parameters, dict) else {}
        if name:
            declared[name] = (
                frozenset(str(key) for key in properties if isinstance(key, str))
                if isinstance(properties, dict)
                else frozenset()
            )
    return declared


def _normalize_tool_call_arguments(
    calls: tuple[ToolCall, ...] | list[ToolCall],
    tools: list[dict[str, Any]] | None,
) -> tuple[ToolCall, ...]:
    """Normalize known provider wire residue in native function arguments."""

    properties_by_tool = _declared_tool_properties(tools)
    declared_names = _declared_tool_names(tools)
    normalized_calls: list[ToolCall] = []
    for call in calls:
        canonical_name = _canonical_declared_tool_name(call.name, declared_names) or call.name
        properties = properties_by_tool.get(canonical_name, frozenset())
        if not properties:
            normalized_calls.append(
                ToolCall(
                    id=call.id,
                    name=canonical_name,
                    arguments=call.arguments,
                    raw=call.raw,
                )
            )
            continue
        try:
            arguments = json.loads(call.arguments or "{}")
        except (TypeError, ValueError):
            normalized_calls.append(call)
            continue
        if not isinstance(arguments, dict):
            normalized_calls.append(call)
            continue
        repaired: dict[str, Any] = {}
        changed = False
        for raw_key, value in arguments.items():
            key = str(raw_key)
            candidate = _GLM_ARGUMENT_KEY_RESIDUE_RE.sub("", html.unescape(key).strip()).strip()
            if key not in properties and candidate in properties:
                key = candidate
                changed = True
            if key in repaired:
                raise StudioError(
                    "MODEL_TOOL_PROTOCOL_INVALID",
                    "模型返回了重复的工具参数",
                    status_code=502,
                    details={"toolName": call.name, "argument": key},
                )
            repaired[key] = value
        normalized_calls.append(
            ToolCall(
                id=call.id,
                name=canonical_name,
                arguments=(json.dumps(repaired, ensure_ascii=False) if changed else call.arguments),
                raw=call.raw,
            )
        )
    return tuple(normalized_calls)


def _dsml_argument_value(raw: str, attributes: dict[str, str]) -> Any:
    value = html.unescape(raw.strip())
    if attributes.get("string", "").lower() == "true":
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _recover_dsml_tool_calls(
    content: str,
    tools: list[dict[str, Any]] | None,
) -> tuple[str, list[ToolCall], bool]:
    """Recover DeepSeek's textual DSML tool protocol without exposing it as prose."""

    normalized = content.replace("｜", "|")
    if "DSML" not in normalized.upper():
        return content, [], False
    declared = _declared_tool_names(tools)
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    for index, match in enumerate(_DSML_INVOKE_RE.finditer(normalized)):
        attrs = _dsml_attributes(match.group("attrs"))
        requested_name = str(attrs.get("name") or "").strip()
        canonical_name = _canonical_declared_tool_name(requested_name, declared)
        if not canonical_name:
            raise StudioError(
                "MODEL_TOOL_PROTOCOL_INVALID",
                f"模型返回了未声明的工具调用：{requested_name or 'unknown'}",
                status_code=502,
                details={"toolName": requested_name or "unknown"},
            )
        body = match.group("body")
        arguments: dict[str, Any] = {}
        for parameter in _DSML_PARAMETER_RE.finditer(body):
            parameter_attrs = _dsml_attributes(parameter.group("attrs"))
            parameter_name = str(parameter_attrs.get("name") or "").strip()
            if parameter_name:
                arguments[parameter_name] = _dsml_argument_value(
                    parameter.group("value"), parameter_attrs
                )
        if not arguments:
            for parameter in _DSML_SIMPLE_ARGUMENT_RE.finditer(body):
                parameter_name = parameter.group("name")
                arguments[parameter_name] = _dsml_argument_value(parameter.group("value"), {})
        digest = hashlib.sha256(f"{canonical_name}:{index}:{match.group(0)}".encode()).hexdigest()[
            :20
        ]
        calls.append(
            ToolCall(
                id=f"call_dsml_{digest}",
                name=canonical_name,
                arguments=json.dumps(arguments, ensure_ascii=False),
                raw={"protocol": "dsml"},
            )
        )
        spans.append(match.span())
    if not calls:
        raise StudioError(
            "MODEL_TOOL_PROTOCOL_INVALID",
            "模型返回了无法解析的工具调用",
            status_code=502,
        )
    cleaned = normalized
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = _DSML_CALLS_WRAPPER_RE.sub("", cleaned).strip()
    if "DSML" in cleaned.upper():
        raise StudioError(
            "MODEL_TOOL_PROTOCOL_INVALID",
            "模型返回了不完整的工具调用",
            status_code=502,
        )
    return cleaned, calls, True


def _recover_glm_tool_calls(
    content: str,
    tools: list[dict[str, Any]] | None,
) -> tuple[str, list[ToolCall], bool]:
    """Recover GLM's textual ``<tool_call>`` fallback protocol.

    Some OpenAI-compatible GLM endpoints put tool calls in ``content`` rather
    than ``delta.tool_calls``.  Real responses also occasionally encode an
    argument as ``<arg_value>name=\"value\"</arg_value>`` (or close an
    ``arg_key`` with ``</arg_value>``), so parsing is intentionally tolerant
    about those two wire quirks while remaining fail-closed on tool names.
    """

    if "<tool_call" not in content.lower():
        return content, [], False
    declared = _declared_tool_names(tools)
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    for index, match in enumerate(_GLM_TOOL_CALL_RE.finditer(content)):
        requested_name = match.group("name").strip()
        canonical_name = _canonical_declared_tool_name(requested_name, declared)
        if not canonical_name:
            raise StudioError(
                "MODEL_TOOL_PROTOCOL_INVALID",
                f"模型返回了未声明的工具调用：{requested_name or 'unknown'}",
                status_code=502,
                details={"toolName": requested_name or "unknown"},
            )
        arguments: dict[str, Any] = {}
        pending_key = ""
        for token in _GLM_ARGUMENT_TAG_RE.finditer(match.group("body")):
            tag = token.group("tag").lower()
            raw_value = html.unescape(token.group("value").strip())
            assignment = _GLM_ASSIGNMENT_RE.match(raw_value)
            if assignment:
                arguments[assignment.group("name")] = _dsml_argument_value(
                    assignment.group("value"), {"string": "true"}
                )
                pending_key = ""
            elif tag == "arg_key":
                pending_key = raw_value
            elif pending_key:
                arguments[pending_key] = _dsml_argument_value(raw_value, {})
                pending_key = ""
            else:
                raise StudioError(
                    "MODEL_TOOL_PROTOCOL_INVALID",
                    "模型返回了无法解析的工具参数",
                    status_code=502,
                )
        if pending_key or not arguments:
            raise StudioError(
                "MODEL_TOOL_PROTOCOL_INVALID",
                "模型返回了不完整的工具调用",
                status_code=502,
            )
        digest = hashlib.sha256(f"{canonical_name}:{index}:{match.group(0)}".encode()).hexdigest()[
            :20
        ]
        calls.append(
            ToolCall(
                id=f"call_glm_{digest}",
                name=canonical_name,
                arguments=json.dumps(arguments, ensure_ascii=False),
                raw={"protocol": "glm-text"},
            )
        )
        spans.append(match.span())
    if not calls:
        raise StudioError(
            "MODEL_TOOL_PROTOCOL_INVALID",
            "模型返回了无法解析的工具调用",
            status_code=502,
        )
    cleaned = content
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    if "<tool_call" in cleaned.lower():
        raise StudioError(
            "MODEL_TOOL_PROTOCOL_INVALID",
            "模型返回了不完整的工具调用",
            status_code=502,
        )
    return cleaned.strip(), calls, True


def _recover_malformed_glm_tool_call(
    content: str,
    tools: list[dict[str, Any]] | None,
) -> tuple[str, list[ToolCall], bool]:
    """Recover the bounded malformed fallback emitted by GLM 5.2.

    Some GLM 5.2 compatible gateways emit a textual ``<tool_call>`` marker
    but lose the closing marker and mix up ``arg_key``/``arg_value`` tags.
    Recovery remains fail-closed: the tool must resolve to a declared schema,
    every recovered argument must resolve to a declared property, and at
    least one argument must be recovered. Text after the marker is discarded
    so a hallucinated "file created" sentence cannot escape as visible output
    before the tool actually executes.
    """

    start = _GLM_MALFORMED_TOOL_START_RE.search(content)
    if start is None:
        return content, [], False
    declared_names = _declared_tool_names(tools)
    requested_name = start.group("name").strip()
    canonical_name = _canonical_declared_tool_name(requested_name, declared_names)
    if not canonical_name:
        raise StudioError(
            "MODEL_TOOL_PROTOCOL_INVALID",
            f"模型返回了未声明的工具调用：{requested_name or 'unknown'}",
            status_code=502,
            details={"toolName": requested_name or "unknown"},
        )
    properties = _declared_tool_properties(tools).get(canonical_name, frozenset())
    body = content[start.end() :]
    raw_arguments: list[tuple[str, str]] = []

    key_matches = list(_GLM_MALFORMED_KEY_RE.finditer(body))
    for index, key_match in enumerate(key_matches):
        next_start = key_matches[index + 1].start() if index + 1 < len(key_matches) else len(body)
        value = body[key_match.end() : next_start]
        value = re.sub(r"^\s*<\s*arg_value\s*>\s*", "", value, flags=re.IGNORECASE)
        value = re.split(r"</\s*arg_value\s*>", value, maxsplit=1, flags=re.IGNORECASE)[0]
        raw_arguments.append((key_match.group("name"), html.unescape(value.strip())))

    if not raw_arguments:
        raw_arguments.extend(
            (match.group("name"), html.unescape(match.group("value")))
            for match in _GLM_MALFORMED_ASSIGNMENT_RE.finditer(body)
        )

    arguments: dict[str, Any] = {}
    for raw_name, raw_value in raw_arguments:
        argument_name = _canonical_declared_argument_name(
            raw_name,
            canonical_name,
            properties,
        )
        if not argument_name:
            raise StudioError(
                "MODEL_TOOL_PROTOCOL_INVALID",
                "模型返回了未声明的工具参数",
                status_code=502,
                details={"toolName": canonical_name, "argument": raw_name},
            )
        if argument_name in arguments:
            raise StudioError(
                "MODEL_TOOL_PROTOCOL_INVALID",
                "模型返回了重复的工具参数",
                status_code=502,
                details={"toolName": canonical_name, "argument": argument_name},
            )
        arguments[argument_name] = _dsml_argument_value(raw_value, {"string": "true"})

    if not arguments:
        raise StudioError(
            "MODEL_TOOL_PROTOCOL_INVALID",
            "模型返回了无法解析的工具调用",
            status_code=502,
        )
    digest = hashlib.sha256(
        f"{canonical_name}:malformed:{content[start.start() :]}".encode()
    ).hexdigest()[:20]
    return (
        content[: start.start()].strip(),
        [
            ToolCall(
                id=f"call_glm_{digest}",
                name=canonical_name,
                arguments=json.dumps(arguments, ensure_ascii=False),
                raw={"protocol": "glm-text-recovered"},
            )
        ],
        True,
    )


def _recover_textual_tool_calls(
    content: str,
    tools: list[dict[str, Any]] | None,
) -> tuple[str, list[ToolCall], bool]:
    if "DSML" in content.replace("｜", "|").upper():
        return _recover_dsml_tool_calls(content, tools)
    try:
        return _recover_glm_tool_calls(content, tools)
    except StudioError as exc:
        if exc.code != "MODEL_TOOL_PROTOCOL_INVALID":
            raise
        return _recover_malformed_glm_tool_call(content, tools)


def _partition_tool_stream_text(value: str) -> tuple[str, str, bool]:
    """Split a streamed text buffer before a possible textual tool marker.

    Ordinary model prose must remain truly streaming even when tools are
    available.  Only the short suffix beginning at ``<`` is held long enough
    to decide whether a provider is starting a textual DSML tool call.
    """

    normalized = value.replace("｜", "|")
    marker = re.search(
        r"<\s*(?:\|\s*){1,2}DSML|<\s*tool_call\s*>",
        normalized,
        re.IGNORECASE,
    )
    if marker:
        start = marker.start()
        return value[:start], value[start:], True
    candidate_start = value.rfind("<")
    if candidate_start >= 0 and len(value) - candidate_start <= _DSML_STREAM_LOOKBEHIND:
        return value[:candidate_start], value[candidate_start:], False
    return value, "", False


def _repeated_output_unit(value: str) -> str:
    """Return a repeated tail unit when a provider is stuck in a text loop."""

    if len(value) < _REPETITION_MIN_UNIT_CHARS * _REPETITION_COUNT:
        return ""
    window = value[-(_REPETITION_MAX_UNIT_CHARS * _REPETITION_COUNT) :]
    max_unit = min(_REPETITION_MAX_UNIT_CHARS, len(window) // _REPETITION_COUNT)
    for unit_length in range(_REPETITION_MIN_UNIT_CHARS, max_unit + 1):
        unit = window[-unit_length:]
        if window.endswith(unit * _REPETITION_COUNT) and len(set(unit.strip())) >= 6:
            return unit
    return ""


def _raise_for_repetitive_output(value: str) -> None:
    unit = _repeated_output_unit(value)
    if not unit:
        return
    raise StudioError(
        "MODEL_REPETITIVE_OUTPUT",
        "模型输出出现重复循环，已停止本次生成",
        status_code=502,
        details={"repeatedChars": len(unit)},
    )


class CredentialResolver:
    """Resolve Secret references through the shared workspace configuration.

    Workspace credentials use ``.agentkit/config.yaml`` with explicit imports
    and legacy migration. Standalone instances retain process/session behavior.
    """

    _FALLBACK_PAIRS: tuple[tuple[str, str], ...] = (
        ("AGENTKIT_MODEL_API_KEY", "OPENAI_API_KEY"),
        ("OPENAI_API_KEY", "AGENTKIT_MODEL_API_KEY"),
    )

    def __init__(self, workspace: Any = None, *, configuration: Any = None) -> None:
        self._session_values: dict[str, bytearray] = {}
        self._lock = RLock()
        self._workspace = workspace
        from ksadk.studio.configuration import WorkspaceConfiguration

        self.configuration = configuration or (
            WorkspaceConfiguration(workspace) if workspace is not None else None
        )
        self._persisted: dict[str, str] | None = None

    def _load_persisted(self) -> dict[str, str]:
        return self.configuration.secrets() if self.configuration is not None else {}

    @staticmethod
    def _validate_name(name: str) -> str:
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        if not name or any(char not in allowed for char in name):
            raise StudioError(
                "SECRET_REFERENCE_INVALID",
                "环境变量 Secret 引用格式无效",
                status_code=422,
                field="credentialName",
            )
        return name

    @classmethod
    def _environment_name(cls, reference: str) -> str:
        if not reference.startswith("env://"):
            raise StudioError(
                "SECRET_BACKEND_UNAVAILABLE",
                "当前本地 Runtime 仅支持 env:// Secret 引用",
                status_code=501,
                details={"scheme": reference.partition("://")[0]},
            )
        return cls._validate_name(reference.removeprefix("env://"))

    @staticmethod
    def _zero(value: bytearray | None) -> None:
        if value is not None:
            value[:] = b"\x00" * len(value)

    def put_session(self, name: str, value: str) -> dict[str, str | bool]:
        name = self._validate_name(name)
        if (
            not value
            or len(value) > 16_384
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise StudioError(
                "SECRET_VALUE_INVALID",
                "凭证不能为空、包含控制字符或超过 16 KiB",
                status_code=422,
                field="value",
            )
        encoded = bytearray(value.encode("utf-8"))
        with self._lock:
            previous = self._session_values.get(name)
            self._session_values[name] = encoded
            self._zero(previous)
            if self.configuration is not None:
                self.configuration.put_secret(name, value)
        return self.status(f"env://{name}")

    def delete_session(self, name: str) -> dict[str, str | bool]:
        name = self._validate_name(name)
        with self._lock:
            previous = self._session_values.pop(name, None)
            self._zero(previous)
            if self.configuration is not None:
                self.configuration.put_secret(name, None)
        return self.status(f"env://{name}")

    def clear_session(self) -> None:
        with self._lock:
            values = list(self._session_values.values())
            self._session_values.clear()
            for value in values:
                self._zero(value)

    def _fallback_name(self, name: str) -> str | None:
        for primary, alternate in self._FALLBACK_PAIRS:
            if name == primary and alternate != primary:
                return alternate
        return None

    def _resolve_source(self, name: str) -> tuple[bool, str]:
        """Return (configured, source) considering session, persisted file, env, and fallback."""
        fallback = self._fallback_name(name)
        if self.configuration is not None:
            value, source = self.configuration.resolve_candidates(
                [name, *([fallback] if fallback else [])]
            )
            with self._lock:
                # put_session() keeps the session overlay and the persisted copy in
                # sync; if the persisted copy vanished (deleted via another resolver
                # instance) the session entry is stale and must not shadow it.
                if value and name in self._session_values:
                    return True, "session"
                if value and fallback and fallback in self._session_values:
                    return True, "session-alias"
            return bool(value), source
        with self._lock:
            if name in self._session_values:
                return True, "session"
            if fallback and fallback in self._session_values:
                return True, "session-alias"
        persisted = self._load_persisted()
        if name in persisted:
            return True, "workspace"
        if fallback and fallback in persisted:
            return True, "workspace-alias"
        if os.environ.get(name):
            return True, "environment"
        if fallback and os.environ.get(fallback):
            return True, "fallback"
        return False, "missing"

    def _session_value(self, name: str) -> str | None:
        with self._lock:
            value = self._session_values.get(name)
            return value.decode("utf-8") if value is not None else None

    def status(self, reference: str) -> dict[str, str | bool]:
        name = self._environment_name(reference)
        configured, source = self._resolve_source(name)
        return {
            "reference": reference,
            "name": name,
            "configured": configured,
            "source": source,
            "persistence": source,
        }

    def resolve(self, reference: str, *, allow_aliases: bool = True) -> str:
        name = self._environment_name(reference)
        fallback = self._fallback_name(name) if allow_aliases else None
        aliases = [name, *([fallback] if fallback else [])]
        if self.configuration is not None:
            value, _source = self.configuration.resolve_candidates(aliases)
            if value:
                return value
            raise StudioError(
                "SECRET_NOT_FOUND",
                "凭证尚未配置，请在设置中填写",
                status_code=422,
                details={"reference": reference},
            )
        for candidate in aliases:
            value = self._session_value(candidate)
            if value is not None:
                return value
        persisted = self._load_persisted()
        for candidate in aliases:
            if candidate in persisted:
                return persisted[candidate]
        for candidate in aliases:
            value = os.environ.get(candidate)
            if value:
                return value
        raise StudioError(
            "SECRET_NOT_FOUND",
            "模型凭证尚未配置",
            status_code=422,
            details={"reference": reference},
        )

    def exists(self, reference: str) -> bool:
        try:
            return bool(self.status(reference)["configured"])
        except StudioError:
            return False


class NetworkGuard:
    async def check(self, endpoint_url: str, policy: NetworkPolicy) -> None:
        parsed = urlparse(endpoint_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            raise StudioError(
                "MODEL_ENDPOINT_INVALID",
                "模型地址必须是有效的 HTTP(S) URL",
                status_code=422,
            )
        allowed = {value.lower().rstrip(".") for value in policy.allowed_hosts}
        if policy.mode == "restricted" and host not in allowed:
            raise StudioError(
                "NETWORK_TARGET_DENIED",
                "目标 hostname 不在网络允许清单中",
                status_code=403,
                details={"host": host},
            )
        if host in _DENIED_METADATA_HOSTS:
            raise self._denied(host)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                0,
                socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise StudioError(
                "NETWORK_TARGET_UNRESOLVED",
                "模型 hostname 无法解析",
                status_code=422,
                details={"host": host},
            ) from exc
        for record in records:
            address = str(record[4][0]).split("%", 1)[0]
            ip = ipaddress.ip_address(address)
            if str(ip) in _DENIED_METADATA_HOSTS or ip.is_link_local or ip.is_multicast:
                raise self._denied(host)
            if (
                ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_unspecified
            ) and not policy.allow_private_network:
                raise self._denied(host)

    @staticmethod
    def _denied(host: str) -> StudioError:
        return StudioError(
            "NETWORK_TARGET_DENIED",
            "目标地址不满足本地 Runtime 网络策略",
            status_code=403,
            details={"host": host},
        )


class OpenAICompatibleModelClient:
    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver | None = None,
        network_guard: NetworkGuard | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.credential_resolver = credential_resolver or CredentialResolver()
        self.network_guard = network_guard or NetworkGuard()
        self.transport = transport
        self.sleep = sleep
        # Keep one AsyncClient for the lifetime of the Studio service. Creating
        # a client for every turn defeats HTTP keep-alive and forces a fresh
        # TCP/TLS handshake before the model can produce its first token.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = RLock()

    def _http_client(self) -> httpx.AsyncClient:
        with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    transport=self.transport,
                    follow_redirects=False,
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                        keepalive_expiry=30.0,
                    ),
                )
            return self._client

    async def aclose(self) -> None:
        """Close the shared connection pool during Studio shutdown."""

        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def complete(
        self,
        model: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        network_policy: NetworkPolicy,
        timeout_seconds: int,
        max_attempts: int,
        backoff_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        allow_empty: bool = False,
        response_format: dict[str, Any] | None = None,
        retry_on_length: bool = False,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        wire_api = (model.wire_api or "chat").strip().lower()
        payload: dict[str, Any]
        if wire_api == "responses":
            payload = self._responses_payload(model, messages, tools)
        else:
            payload = {
                "model": model.model,
                "messages": messages,
                "stream": False,
            }
            # 未显式配置的采样参数一律不携带字段，交给服务端默认，
            # 避免触碰各模型族的硬约束（kimi 温度、各家 max_tokens 上限等）。
            if model.parameters.temperature is not None:
                payload["temperature"] = model.parameters.temperature
            if model.parameters.max_tokens is not None:
                payload["max_tokens"] = model.parameters.max_tokens
            if model.parameters.top_p is not None:
                payload["top_p"] = model.parameters.top_p
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            if response_format and model.parameters.allow_json_response_format:
                payload["response_format"] = response_format
        if max_output_tokens is not None:
            payload["max_output_tokens" if wire_api == "responses" else "max_tokens"] = (
                max_output_tokens
            )
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        dropped_response_format = False
        length_retried = False
        last_length_error: StudioError | None = None
        client = self._http_client()
        try:
            # 额外 1 次迭代仅用于 response_format 400 降级重发，
            # 其余失败路径仍受 max_attempts 约束（会在原上限处 raise）。
            for attempt in range(1, max_attempts + 2):
                try:
                    response = await client.post(
                        model.endpoint_url,
                        headers=headers,
                        json=payload,
                        timeout=timeout,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= max_attempts:
                        raise StudioError(
                            "MODEL_REQUEST_FAILED",
                            "模型请求网络失败",
                            status_code=502,
                            details={"attempts": attempt, "errorType": type(exc).__name__},
                        ) from exc
                    await self.sleep(backoff_seconds * attempt)
                    continue
                if response.is_redirect:
                    raise StudioError(
                        "NETWORK_TARGET_DENIED",
                        "模型 endpoint 不允许重定向",
                        status_code=403,
                        details={"statusCode": response.status_code},
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < max_attempts:
                        await self.sleep(backoff_seconds * attempt)
                        continue
                if (
                    response.status_code == 400
                    and "response_format" in payload
                    and not dropped_response_format
                ):
                    # 网关不支持 response_format：去掉该字段重发一次。
                    dropped_response_format = True
                    payload.pop("response_format")
                    continue
                if response.status_code >= 400:
                    if response.status_code == 429:
                        # A selected authoring profile must not silently fall
                        # back to a different model.  Preserve the actual
                        # upstream condition so Studio can offer the user a
                        # useful retry/switch decision instead of reporting a
                        # misleading generic 502.
                        raise StudioError(
                            "MODEL_RATE_LIMITED",
                            "所选生成模型当前限流，请稍后重试或切换模型 Profile",
                            status_code=429,
                            details={"upstreamStatus": response.status_code},
                        )
                    upstream_detail = ""
                    try:
                        upstream_detail = response.text[:200]
                    except Exception:  # noqa: BLE001 - 诊断信息尽力而为
                        upstream_detail = ""
                    raise StudioError(
                        "MODEL_REQUEST_FAILED",
                        "模型服务返回错误",
                        status_code=502,
                        details={
                            "upstreamStatus": response.status_code,
                            "upstreamError": upstream_detail,
                        },
                    )
                try:
                    if wire_api == "responses":
                        parsed = self._parse_responses_response(response, allow_empty=allow_empty)
                    else:
                        parsed = self._parse_response(response, allow_empty=allow_empty)
                except StudioError as exc:
                    # finishReason=length 的空响应（大 JSON 被 max_tokens 截断）：
                    # 一次性扩容 max_tokens 重发；再次截断则按原错误上抛。
                    if retry_on_length and not length_retried and self._is_length_truncation(exc):
                        length_retried = True
                        last_length_error = exc
                        payload = self._expand_length_budget(payload)
                        continue
                    raise
                if (
                    retry_on_length
                    and not length_retried
                    and parsed.finish_reason in _LENGTH_FINISH_REASONS
                ):
                    # 有内容但被 length 截断（结构化输出必然残缺），同样扩容重发一次。
                    length_retried = True
                    payload = self._expand_length_budget(payload)
                    continue
                if not (
                    "DSML" in parsed.content.replace("｜", "|").upper()
                    or "<tool_call" in parsed.content.lower()
                ):
                    _raise_for_repetitive_output(parsed.content)
                if wire_api != "responses" and not parsed.tool_calls:
                    cleaned, recovered_calls, recovered = _recover_textual_tool_calls(
                        parsed.content, tools
                    )
                    if recovered:
                        parsed = ModelResponse(
                            content=cleaned,
                            finish_reason="tool_calls",
                            usage=parsed.usage,
                            tool_calls=recovered_calls,
                            raw_message=parsed.raw_message,
                            reasoning=parsed.reasoning,
                        )
                if parsed.tool_calls:
                    parsed = ModelResponse(
                        content=parsed.content,
                        finish_reason=parsed.finish_reason,
                        usage=parsed.usage,
                        tool_calls=list(_normalize_tool_call_arguments(parsed.tool_calls, tools)),
                        raw_message=parsed.raw_message,
                        reasoning=parsed.reasoning,
                    )
                return parsed
        except asyncio.CancelledError:
            raise
        if last_length_error is not None:
            raise last_length_error
        raise AssertionError("unreachable")

    async def stream(
        self,
        model: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        network_policy: NetworkPolicy,
        timeout_seconds: int,
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        """Stream chat-completions deltas without buffering the response.

        Studio's Harness path uses the same credential and network policy as
        ``complete``.  Responses-wire profiles currently remain on the
        buffered path because their event vocabulary is provider-specific.
        """
        if (model.wire_api or "chat").strip().lower() == "responses":
            async for chunk in self._stream_responses(
                model,
                messages=messages,
                network_policy=network_policy,
                timeout_seconds=timeout_seconds,
                tools=tools,
                max_output_tokens=max_output_tokens,
            ):
                yield chunk
            return
        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages,
            "stream": True,
        }
        if model.parameters.temperature is not None:
            payload["temperature"] = model.parameters.temperature
        if model.parameters.max_tokens is not None:
            payload["max_tokens"] = model.parameters.max_tokens
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        if model.parameters.top_p is not None:
            payload["top_p"] = model.parameters.top_p
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        client = self._http_client()
        request_started = time.monotonic()
        try:
            async with client.stream(
                "POST", model.endpoint_url, headers=headers, json=payload, timeout=timeout
            ) as response:
                if response.is_redirect or response.status_code >= 400:
                    raise StudioError(
                        "MODEL_REQUEST_FAILED",
                        "模型流式请求失败",
                        status_code=502,
                        details={"upstreamStatus": response.status_code},
                    )
                tool_fragments: dict[int, dict[str, str]] = {}
                first_chunk_logged = False
                raw_text = ""
                emitted_text = ""
                pending_visible_text = ""
                textual_tool_detected = False
                done_emitted = False
                last_repetition_check_length = 0
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    if not raw:
                        continue
                    try:
                        chunk = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    usage = chunk.get("usage") or {}
                    usage_value = (
                        Usage(
                            input_tokens=int(usage.get("prompt_tokens") or 0),
                            output_tokens=int(usage.get("completion_tokens") or 0),
                            total_tokens=int(usage.get("total_tokens") or 0),
                            cached_input_tokens=int(
                                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                            ),
                            reasoning_output_tokens=int(
                                (usage.get("completion_tokens_details") or {}).get(
                                    "reasoning_tokens"
                                )
                                or 0
                            ),
                            reported=True,
                            source="model-provider",
                        )
                        if usage
                        else None
                    )
                    _accumulate_chat_tool_calls(
                        tool_fragments,
                        chunk.get("tool_calls"),
                        snapshot=True,
                    )
                    root_message = chunk.get("message") or {}
                    if isinstance(root_message, dict):
                        _accumulate_chat_tool_calls(
                            tool_fragments,
                            root_message.get("tool_calls"),
                            snapshot=True,
                        )
                        if isinstance(root_message.get("function_call"), dict):
                            _accumulate_chat_tool_calls(
                                tool_fragments,
                                [{"index": 0, "function": root_message["function_call"]}],
                                snapshot=True,
                            )
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = str(delta.get("content") or "")
                        raw_text += text
                        pending_visible_text += text
                        reasoning = str(
                            delta.get("reasoning_content") or delta.get("reasoning") or ""
                        )
                        _accumulate_chat_tool_calls(
                            tool_fragments,
                            delta.get("tool_calls"),
                            snapshot=False,
                        )
                        if isinstance(delta.get("function_call"), dict):
                            _accumulate_chat_tool_calls(
                                tool_fragments,
                                [{"index": 0, "function": delta["function_call"]}],
                                snapshot=False,
                            )
                        message = choice.get("message") or {}
                        if isinstance(message, dict):
                            _accumulate_chat_tool_calls(
                                tool_fragments,
                                message.get("tool_calls"),
                                snapshot=True,
                            )
                            if isinstance(message.get("function_call"), dict):
                                _accumulate_chat_tool_calls(
                                    tool_fragments,
                                    [{"index": 0, "function": message["function_call"]}],
                                    snapshot=True,
                                )
                        _accumulate_chat_tool_calls(
                            tool_fragments,
                            choice.get("tool_calls"),
                            snapshot=True,
                        )
                        if isinstance(choice.get("function_call"), dict):
                            _accumulate_chat_tool_calls(
                                tool_fragments,
                                [{"index": 0, "function": choice["function_call"]}],
                                snapshot=True,
                            )
                        visible_text = ""
                        if text and not textual_tool_detected:
                            visible_text, pending_visible_text, marker_found = (
                                _partition_tool_stream_text(pending_visible_text)
                            )
                            textual_tool_detected = marker_found
                            emitted_text += visible_text
                            visible_length = len(emitted_text) + len(pending_visible_text)
                            if (
                                not textual_tool_detected
                                and visible_length - last_repetition_check_length >= 64
                            ):
                                last_repetition_check_length = visible_length
                                _raise_for_repetitive_output(emitted_text + pending_visible_text)
                        if visible_text or reasoning or usage_value is not None:
                            if not first_chunk_logged and (text or reasoning):
                                first_chunk_logged = True
                                _LOGGER.info(
                                    "model stream first delta: model=%s ttfb_ms=%d",
                                    model.model,
                                    int((time.monotonic() - request_started) * 1000),
                                )
                            yield ModelStreamChunk(
                                text=visible_text,
                                reasoning=reasoning,
                                usage=usage_value,
                            )
                    finish = any(
                        str(choice.get("finish_reason") or "")
                        for choice in chunk.get("choices") or []
                    )
                    incomplete_native_call = any(
                        value["name"] and not value["arguments"]
                        for value in tool_fragments.values()
                    )
                    if finish and not done_emitted and not incomplete_native_call:
                        calls = tuple(
                            ToolCall(
                                id=value["id"],
                                name=value["name"],
                                arguments=value["arguments"] or "{}",
                            )
                            for value in tool_fragments.values()
                            if value["name"]
                        )
                        final_text = raw_text
                        if not calls and textual_tool_detected:
                            final_text, recovered_calls, _ = _recover_textual_tool_calls(
                                raw_text, tools
                            )
                            calls = tuple(recovered_calls)
                        calls = _normalize_tool_call_arguments(calls, tools)
                        if not final_text.strip() and not calls:
                            raise StudioError(
                                "MODEL_EMPTY_RESPONSE",
                                "模型未返回可用内容",
                                status_code=502,
                                details={"finishReason": "stream-completed"},
                            )
                        if not calls and not textual_tool_detected:
                            _raise_for_repetitive_output(final_text)
                        remaining_text = final_text
                        if emitted_text and final_text.startswith(emitted_text):
                            remaining_text = final_text[len(emitted_text) :]
                        if remaining_text:
                            yield ModelStreamChunk(text=remaining_text)
                        yield ModelStreamChunk(tool_calls=calls, done=True)
                        done_emitted = True
                if not done_emitted:
                    calls = tuple(
                        ToolCall(
                            id=value["id"],
                            name=value["name"],
                            arguments=value["arguments"] or "{}",
                        )
                        for value in tool_fragments.values()
                        if value["name"]
                    )
                    final_text = raw_text
                    if not calls and (
                        textual_tool_detected
                        or "DSML" in raw_text.replace("｜", "|").upper()
                        or "<tool_call" in raw_text.lower()
                    ):
                        final_text, recovered_calls, _ = _recover_textual_tool_calls(
                            raw_text, tools
                        )
                        calls = tuple(recovered_calls)
                    calls = _normalize_tool_call_arguments(calls, tools)
                    if not final_text.strip() and not calls:
                        raise StudioError(
                            "MODEL_EMPTY_RESPONSE",
                            "模型未返回可用内容",
                            status_code=502,
                            details={"finishReason": "stream-ended"},
                        )
                    if not calls and not textual_tool_detected:
                        _raise_for_repetitive_output(final_text)
                    remaining_text = final_text
                    if emitted_text and final_text.startswith(emitted_text):
                        remaining_text = final_text[len(emitted_text) :]
                    if remaining_text:
                        yield ModelStreamChunk(text=remaining_text)
                    yield ModelStreamChunk(tool_calls=calls, done=True)
        except asyncio.CancelledError:
            raise

    async def _stream_responses(
        self,
        model: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        network_policy: NetworkPolicy,
        timeout_seconds: int,
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        """Adapt the native Responses SSE event stream to Harness chunks."""

        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        payload = self._responses_payload(model, messages, tools)
        payload["stream"] = True
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        client = self._http_client()
        request_started = time.monotonic()
        first_delta_logged = False
        function_calls: dict[str, dict[str, str]] = {}
        emitted_calls: set[str] = set()
        async with client.stream(
            "POST", model.endpoint_url, headers=headers, json=payload, timeout=timeout
        ) as response:
            if response.is_redirect or response.status_code >= 400:
                raise StudioError(
                    "MODEL_REQUEST_FAILED",
                    "模型 Responses 流式请求失败",
                    status_code=502,
                    details={"upstreamStatus": response.status_code},
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                event_type = str(event.get("type") or "")
                text = ""
                reasoning = ""
                usage_value: Usage | None = None
                if event_type == "response.output_text.delta":
                    text = str(event.get("delta") or "")
                elif event_type in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                }:
                    reasoning = str(event.get("delta") or "")
                elif event_type == "response.function_call_arguments.delta":
                    key = str(event.get("item_id") or event.get("output_index") or "0")
                    current = function_calls.setdefault(
                        key, {"id": "", "name": "", "arguments": ""}
                    )
                    current["arguments"] += str(event.get("delta") or "")
                elif event_type == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        key = str(
                            item.get("id")
                            or event.get("item_id")
                            or event.get("output_index")
                            or "0"
                        )
                        current = function_calls.setdefault(
                            key, {"id": "", "name": "", "arguments": ""}
                        )
                        current["id"] = str(item.get("call_id") or item.get("id") or "")
                        current["name"] = str(item.get("name") or "")
                        current["arguments"] = str(item.get("arguments") or "")
                elif event_type == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        key = str(
                            item.get("id")
                            or event.get("item_id")
                            or event.get("output_index")
                            or "0"
                        )
                        current = function_calls.setdefault(
                            key, {"id": "", "name": "", "arguments": ""}
                        )
                        current["id"] = str(item.get("call_id") or item.get("id") or current["id"])
                        current["name"] = str(item.get("name") or current["name"])
                        current["arguments"] = str(
                            item.get("arguments") or current["arguments"] or "{}"
                        )
                elif event_type in {"response.completed", "response.incomplete"}:
                    response_payload = event.get("response") or {}
                    raw_usage = response_payload.get("usage") or {}
                    if raw_usage:
                        usage_value = Usage(
                            input_tokens=int(raw_usage.get("input_tokens") or 0),
                            output_tokens=int(raw_usage.get("output_tokens") or 0),
                            total_tokens=int(raw_usage.get("total_tokens") or 0),
                            cached_input_tokens=int(
                                (raw_usage.get("input_tokens_details") or {}).get("cached_tokens")
                                or 0
                            ),
                            reasoning_output_tokens=int(
                                (raw_usage.get("output_tokens_details") or {}).get(
                                    "reasoning_tokens"
                                )
                                or 0
                            ),
                            reported=True,
                            source="model-provider",
                        )
                elif event_type in {"response.failed", "response.cancelled", "response.canceled"}:
                    raise StudioError(
                        "MODEL_REQUEST_FAILED",
                        "模型 Responses 流式响应未完成",
                        status_code=502,
                        details={"eventType": event_type},
                    )
                if text or reasoning:
                    if not first_delta_logged:
                        first_delta_logged = True
                        _LOGGER.info(
                            "model Responses stream first delta: model=%s ttfb_ms=%d",
                            model.model,
                            int((time.monotonic() - request_started) * 1000),
                        )
                    yield ModelStreamChunk(text=text, reasoning=reasoning)
                if event_type == "response.output_item.done":
                    completed_key = str(
                        (event.get("item") or {}).get("id")
                        or event.get("item_id")
                        or event.get("output_index")
                        or "0"
                    )
                    calls = tuple(
                        ToolCall(
                            id=value["id"],
                            name=value["name"],
                            arguments=value["arguments"] or "{}",
                        )
                        for key, value in function_calls.items()
                        if key == completed_key and key not in emitted_calls and value["name"]
                    )
                    if calls:
                        emitted_calls.add(completed_key)
                        yield ModelStreamChunk(tool_calls=calls)
                if event_type in {"response.completed", "response.incomplete"}:
                    yield ModelStreamChunk(usage=usage_value, done=True)

    @staticmethod
    def _is_length_truncation(exc: StudioError) -> bool:
        if exc.code != "MODEL_EMPTY_RESPONSE":
            return False
        return str(exc.details.get("finishReason") or "") in _LENGTH_FINISH_REASONS

    @staticmethod
    def _expand_length_budget(payload: dict[str, Any]) -> dict[str, Any]:
        key = "max_output_tokens" if "max_output_tokens" in payload else "max_tokens"
        current = int(payload.get(key) or 0)
        payload[key] = max(current, _LENGTH_RETRY_TARGET_TOKENS)
        return payload

    @staticmethod
    def _responses_payload(
        model: ResolvedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """把 chat 语义消息映射到 Responses API 最小可用载荷。"""
        # Responses tools are flat objects, unlike Chat Completions' nested
        # ``function`` envelope. Accept both shapes because Harness supplies
        # Chat-style schemas internally.
        responses_tools = []
        for tool in tools or []:
            function = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(function, dict):
                responses_tools.append({"type": "function", **function})
            elif isinstance(tool, dict):
                responses_tools.append(dict(tool))
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            text = str(message.get("content") or "")
            if role == "system":
                instructions.append(text)
                continue
            part_type = "output_text" if role == "assistant" else "input_text"
            items.append({"role": role, "content": [{"type": part_type, "text": text}]})
        payload: dict[str, Any] = {
            "model": model.model,
            "input": items,
        }
        if responses_tools:
            payload["tools"] = responses_tools
        if model.parameters.max_tokens is not None:
            payload["max_output_tokens"] = model.parameters.max_tokens
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        return payload

    @staticmethod
    def _parse_responses_response(
        response: httpx.Response, allow_empty: bool = False
    ) -> ModelResponse:
        if len(response.content) > 16 * 1024 * 1024:
            raise StudioError(
                "MODEL_RESPONSE_TOO_LARGE",
                "模型响应超过 16 MiB 限制",
                status_code=502,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise StudioError(
                "MODEL_RESPONSE_INVALID",
                "模型响应不符合 Responses 协议",
                status_code=502,
            ) from exc
        content = payload.get("output_text")
        if not isinstance(content, str) or not content:
            parts: list[str] = []
            for item in payload.get("output") or []:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        parts.append(str(part.get("text") or ""))
            content = "".join(parts)
        status = str(payload.get("status") or "")
        if not content and not allow_empty:
            raise StudioError(
                "MODEL_EMPTY_RESPONSE",
                "模型未返回可用内容",
                status_code=502,
                details={"status": status, "finishReason": status},
            )
        raw_usage = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens") or 0),
            output_tokens=int(raw_usage.get("output_tokens") or 0),
            total_tokens=int(raw_usage.get("total_tokens") or 0),
            cached_input_tokens=int(
                (raw_usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
            ),
            reasoning_output_tokens=int(
                (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
            ),
        )
        return ModelResponse(
            content=content,
            finish_reason="stop" if status == "completed" else status,
            usage=usage,
            tool_calls=[],
            raw_message={"output": payload.get("output")},
            reasoning=_reasoning_from_responses_output(payload.get("output")),
        )

    @staticmethod
    def _parse_response(response: httpx.Response, allow_empty: bool = False) -> ModelResponse:
        if len(response.content) > 16 * 1024 * 1024:
            raise StudioError(
                "MODEL_RESPONSE_TOO_LARGE",
                "模型响应超过 16 MiB 限制",
                status_code=502,
            )
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise StudioError(
                "MODEL_RESPONSE_INVALID",
                "模型响应不符合 OpenAI-compatible 协议",
                status_code=502,
            ) from exc
        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or "{}"),
                    raw=raw,
                )
            )
        content = str(message.get("content") or "")
        finish_reason = str(choice.get("finish_reason") or "")
        if not content and not calls and not allow_empty:
            raise StudioError(
                "MODEL_EMPTY_RESPONSE",
                "模型未返回可用内容",
                status_code=502,
                details={"finishReason": finish_reason},
            )
        raw_usage = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
            total_tokens=int(raw_usage.get("total_tokens") or 0),
            cached_input_tokens=int(
                (raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
            reasoning_output_tokens=int(
                (raw_usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            ),
            reported=bool(raw_usage),
            source="model-provider" if raw_usage else None,
        )
        return ModelResponse(
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=calls,
            raw_message=message,
            reasoning=str(message.get("reasoning_content") or message.get("reasoning") or ""),
        )
