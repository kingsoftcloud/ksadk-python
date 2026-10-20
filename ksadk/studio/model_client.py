"""Safe OpenAI-compatible model client for local Studio runs."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlparse

import httpx

from ksadk._async_iterators import owning_async_context, owning_async_iterator
from ksadk._model_input import ProviderInputObservation, input_observation
from ksadk._model_json import ModelJSONPolicyError, load_model_json
from ksadk._model_sse import ModelSSEIdleTimeout, SSEFrame, SSEFramingError, model_sse_frames
from ksadk.model_proxy import compat as provider_compat
from ksadk.studio.contracts import ANTHROPIC_VERSION, NetworkPolicy, ResolvedModel, Usage
from ksadk.studio.errors import StudioError
from ksadk.studio.model_buffered_wire import (
    buffered_projection,
    validate_buffered_identity,
    wire_refusal,
)
from ksadk.studio.model_chat_tools import ChatToolCalls
from ksadk.studio.model_content_stream import AnthropicContent, ChatContent
from ksadk.studio.model_request_projection import anthropic_messages, responses_messages
from ksadk.studio.model_responses_content import ResponsesContent
from ksadk.studio.model_usage import AnthropicUsageAccumulator, provider_usage
from ksadk.studio.model_wire_failure import ModelResponseError, provider_error
from ksadk.studio.model_wire_identity import (
    IncompleteToolCallFrame,
    ModelWireIdentity,
    chat_choices,
    decode_model_frame,
    invalid_model_frame,
)
from ksadk.studio.model_wire_snapshot import reconcile_responses_snapshot
from ksadk.studio.model_wire_tools import AnthropicToolBlocks, ResponsesToolCalls

from .model_credentials import CredentialResolver as CredentialResolver

#: Anthropic Messages wire 把 max_tokens 设为必填；profile 未配置时的安全默认。
_ANTHROPIC_DEFAULT_MAX_TOKENS = 8192

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
# These explicit terminal reasons cannot authorize any partially assembled tool.
# Preserve them even when the provider ended before emitting its arguments.
_INCOMPLETE_CALL_NON_SUCCESS_FINISH_REASONS = {
    "length", "incomplete", "max_tokens", "max_output_tokens", "output_limit",
    "context_overflow", "context_length_exceeded", "policy_abort", "content_filter",
    "user_abort", "canceled", "cancelled",
}
_REPETITION_MIN_UNIT_CHARS = 24
_REPETITION_MAX_UNIT_CHARS = 512
_REPETITION_COUNT = 4

_LOGGER = logging.getLogger(__name__)


async def _http_model_error(response: httpx.Response, *, wire_api: str) -> StudioError:
    if response.is_redirect:
        return StudioError("NETWORK_TARGET_DENIED", "模型 endpoint 不允许重定向", status_code=403,
                           details={"statusCode": response.status_code})
    payload = {}
    usage = None
    try:
        raw = await response.aread()
        if len(raw) <= 16 * 1024 * 1024:
            payload = decode_model_frame(raw)
            validate_buffered_identity(payload, wire_api=wire_api)
            usage = provider_usage(payload.get("usage"), wire_api=wire_api)
    except (StudioError, httpx.HTTPError):
        # An unreadable error body cannot erase the actual HTTP failure fact.
        payload, usage = {}, None
    failure = provider_error(
        payload, wire_api=wire_api, status=response.status_code, usage=usage,
        headers=response.headers, provider_request_id=_provider_request_id(response.headers),
    )
    assert failure is not None
    return failure


@asynccontextmanager
async def _model_frames(
    response: httpx.Response, *, idle_timeout_seconds: float,
    known_policy_abort: Callable[[], bool] | None = None,
) -> AsyncIterator[AsyncIterator[SSEFrame]]:
    try:
        async with model_sse_frames(
            response.aiter_bytes(), max_bytes=16 * 1024 * 1024,
            idle_timeout_seconds=idle_timeout_seconds,
        ) as frames:
            yield frames
    except ModelSSEIdleTimeout:
        if known_policy_abort is None or not known_policy_abort():
            raise httpx.ReadTimeout("model stream idle timeout") from None
    except (SSEFramingError, UnicodeError):
        raise invalid_model_frame() from None
    except httpx.TransportError:
        # A typed refusal already observed in this attempt cannot become a
        # retryable request just because the remaining transport tail is lost.
        if known_policy_abort is None or not known_policy_abort():
            raise

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


@dataclass(frozen=True)
class ModelResponse:
    content: str
    finish_reason: str
    usage: Usage
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]
    #: 推理文本（chat wire 的 reasoning_content / Responses wire 的 reasoning 项）。
    reasoning: str = ""
    #: 工具回合只返回 reasoning_content、修复后仍无内容与工具调用（可见降级信号）。
    empty_reasoning_only: bool = False
    #: Upstream correlation id only; never contains authorization material.
    provider_request_id: str = ""
    provider_input: ProviderInputObservation | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ModelStreamChunk:
    """One OpenAI-compatible chat stream delta."""

    text: str = ""
    reasoning: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    done: bool = False
    # Preserve the provider terminal reason across the canonical gateway
    # boundary.  Dropping ``length`` made a truncated plan indistinguishable
    # from a complete answer to Agent runtimes.
    finish_reason: str = ""
    provider_request_id: str = ""
    # Complete provider-only replay at the terminal, never a public text delta.
    provider_content: tuple[dict[str, Any], ...] | None = field(default=None, repr=False)
    provider_input: ProviderInputObservation | None = field(default=None, repr=False)
    public_summary: str = ""


def _provider_request_id(headers: Any) -> str:
    """Extract a bounded upstream correlation id from common Gateway headers."""

    for name in (
        "x-request-id",
        "request-id",
        "x-amzn-requestid",
        "x-trace-id",
        "trace-id",
    ):
        try:
            value = str(headers.get(name) or "").strip()
        except Exception:  # noqa: BLE001 - third-party header mappings vary
            value = ""
        if value:
            return value[:256]
    return ""


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


def _delegation_reasoning_effort(
    model: ResolvedModel,
    tools: list[dict[str, Any]] | None,
    compatibility_profile: provider_compat.CompatProfile | None = None,
) -> str | None:
    """Return the provider-declared effort for a delegation decision.

    Provider-specific values live in ``model_proxy.compat``.  Studio only
    identifies the semantic turn (the controller exposes ``delegate_task``),
    avoiding transport branches keyed directly on model names.
    """

    if "delegate_task" not in _declared_tool_names(tools).values():
        return None
    profile = compatibility_profile or provider_compat.compat_profile(
        base_url=model.endpoint_url,
        model=model.model,
    )
    return provider_compat.default_delegation_reasoning_effort(
        profile,
        model=model.model,
    )


def _effective_reasoning_effort(
    model: ResolvedModel,
    tools: list[dict[str, Any]] | None,
    explicit: str | None,
    compatibility_profile: provider_compat.CompatProfile | None = None,
) -> str | None:
    """Resolve effort with the same precedence as mature agent runtimes.

    Explicit run/session selection wins, followed by the Agent's pinned
    default and then the endpoint's narrow controller-turn default.
    """

    profile = compatibility_profile or provider_compat.compat_profile(
        base_url=model.endpoint_url, model=model.model,
    )
    if not profile.supports_reasoning_effort:
        return None
    requested = (explicit or model.parameters.reasoning_effort
                 or _delegation_reasoning_effort(model, tools, profile))
    if not requested:
        return None
    from ksadk.model_proxy.transform import clamp_reasoning_effort

    return clamp_reasoning_effort(model.model, requested, policy=profile.reasoning_effort_policy)


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
            arguments = load_model_json(call.arguments or "{}")
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


class _AmbiguousToolInput(StudioError):
    """A failed identity/value check cannot enter malformed-text recovery."""


def _textual_argument_error() -> _AmbiguousToolInput:
    return _AmbiguousToolInput(
        "MODEL_TOOL_PROTOCOL_INVALID", "模型返回了重复或非法的工具参数", status_code=502,
    )


def _store_textual_argument(arguments: dict[str, Any], name: str, value: Any) -> None:
    if name in arguments:
        raise _textual_argument_error()
    arguments[name] = value


def _dsml_argument_value(raw: str, attributes: dict[str, str]) -> Any:
    value = html.unescape(raw.strip())
    if attributes.get("string", "").lower() == "true":
        return value
    try:
        return load_model_json(value)
    except ModelJSONPolicyError:
        raise _textual_argument_error() from None
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
                _store_textual_argument(
                    arguments, parameter_name,
                    _dsml_argument_value(parameter.group("value"), parameter_attrs),
                )
        if not arguments:
            for parameter in _DSML_SIMPLE_ARGUMENT_RE.finditer(body):
                parameter_name = parameter.group("name")
                _store_textual_argument(arguments, parameter_name,
                                        _dsml_argument_value(parameter.group("value"), {}))
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
                _store_textual_argument(
                    arguments, assignment.group("name"),
                    _dsml_argument_value(assignment.group("value"), {"string": "true"}),
                )
                pending_key = ""
            elif tag == "arg_key":
                pending_key = raw_value
            elif pending_key:
                _store_textual_argument(arguments, pending_key, _dsml_argument_value(raw_value, {}))
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
        if isinstance(exc, _AmbiguousToolInput):
            raise
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
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        allow_empty: bool = False,
        response_format: dict[str, Any] | None = None,
        retry_on_length: bool = False,
        retry_reasoning_only: bool = True,
        max_output_tokens: int | None = None,
        output_budget_mode: Literal["adaptive", "fixed"] = "adaptive",
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None,
        compatibility_profile: provider_compat.CompatProfile | None = None,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
    ) -> ModelResponse:
        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        wire_api = (model.wire_api or "chat").strip().lower()
        # 端点差异只允许来自声明式兼容档位（不按模型名写传输/解析分支）。
        profile = compatibility_profile or provider_compat.compat_profile(
            base_url=model.endpoint_url, model=model.model
        )
        payload: dict[str, Any]
        if wire_api == "responses":
            payload = self._responses_payload(model, messages, tools)
        elif wire_api == "anthropic":
            payload = self._anthropic_payload(model, messages, tools, compatibility_profile=profile)
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
                payload["tool_choice"] = tool_choice or "auto"
                # OpenAI-compatible gateways do not all share the same default.
                # Declare this explicitly so one model turn may return several
                # independent calls; the stream parser and Harness dispatcher
                # already preserve and execute such batches.
                payload["parallel_tool_calls"] = True
            if response_format and model.parameters.allow_json_response_format:
                payload["response_format"] = response_format
            payload = provider_compat.apply_chat_compat(payload, profile, model=model.model)
        self._apply_tool_choice(payload, wire_api, tool_choice)
        self._apply_parallel_tool_policy(payload, wire_api, parallel_tool_calls)
        if max_output_tokens is not None:
            payload["max_output_tokens" if wire_api == "responses" else "max_tokens"] = (
                max_output_tokens
            )
        # A Native call owns a hard output limit. Endpoint recommendations
        # and legacy length recovery must not enlarge that allocation.
        if (
            output_budget_mode == "adaptive"
            and wire_api != "responses" and max_output_tokens is not None
        ):
            # 不变量：max_output_tokens > thinking_budget。只在**引擎显式压预算**
            # （委派决策回合）时抬：这是实测零产出事故的成因；agent 作者自己配置
            # 的 maxTokens 不静默改写，否则等于替作者改配置。
            effective_budget = provider_compat.required_output_budget(
                profile, payload.get("max_tokens")
            )
            if effective_budget is not None:
                payload["max_tokens"] = effective_budget
        effective_effort = _effective_reasoning_effort(model, tools, reasoning_effort, profile)
        self._apply_reasoning_effort(payload, wire_api, effective_effort)
        # 思考决策必须在"有效输出预算"确定之后做，且**与是否带工具无关**：
        # 预算不足以容纳思考预算时（如委派决策回合的 2048），必须关思考，
        # 否则始终思考的模型会把预算全花在 reasoning 上、零产出。
        self._apply_thinking_policy(payload, wire_api, profile, tools, effective_effort,
                                    thinking_mode=thinking_mode or model.parameters.thinking_mode)
        if wire_api not in {"responses", "anthropic"}:
            # Apply the complete endpoint/model policy after all host options,
            # so a late effort or thinking switch cannot reintroduce a field
            # rejected by this model. No adaptive retry or budget increase.
            payload = provider_compat.apply_chat_compat(payload, profile, model=model.model)
        self._apply_output_token_parameter(payload, wire_api, output_token_parameter)
        if wire_api == "anthropic":
            headers = self._anthropic_headers(credential, payload)
        else:
            headers = {
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            }
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        dropped_response_format = False
        length_retried = False
        reasoning_only_retried = False
        last_length_error: StudioError | None = None
        client = self._http_client()
        observation = None
        observed_usage = None
        try:
            # 额外 1 次迭代仅用于 response_format 400 降级重发，
            # 其余失败路径仍受 max_attempts 约束（会在原上限处 raise）。
            for attempt in range(1, max_attempts + 2):
                observation = None
                observed_usage = None
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
                if (
                    response.status_code == 400
                    and "response_format" in payload
                    and not dropped_response_format
                ):
                    dropped_response_format = True
                    payload.pop("response_format")
                    continue
                if response.status_code >= 400:
                    failure = await _http_model_error(response, wire_api=wire_api)
                    from ksadk.harness.model_provider import classify_model_failure

                    if (classify_model_failure(failure).kind.value in {"rate_limit", "unavailable"}
                            and attempt < max_attempts):
                        await self.sleep(backoff_seconds * attempt)
                        continue
                    raise failure
                observation = None
                if wire_api == "anthropic" and len(response.content) <= 16 * 1024 * 1024:
                    try:
                        observation = input_observation(decode_model_frame(response.content),
                                                        payload, "buffered")
                    except (ValueError, TypeError):
                        raise invalid_model_frame() from None
                try:
                    if wire_api == "responses":
                        parsed = self._parse_responses_response(response, allow_empty=allow_empty)
                    elif wire_api == "anthropic":
                        parsed = self._parse_anthropic_response(response, allow_empty=allow_empty)
                    else:
                        parsed = self._parse_response(response, allow_empty=allow_empty)
                except StudioError as exc:
                    if type(exc) is ModelResponseError:
                        exc.provider_input = observation
                    # finishReason=length 的空响应（大 JSON 被 max_tokens 截断）：
                    # 一次性扩容 max_tokens 重发；再次截断则按原错误上抛。
                    if (
                        retry_on_length and output_budget_mode == "adaptive"
                        and not length_retried and self._is_length_truncation(exc)
                    ):
                        length_retried = True
                        last_length_error = exc
                        payload = self._expand_length_budget(payload)
                        continue
                    raise
                parsed = replace(parsed, provider_input=observation)
                observed_usage = parsed.usage
                if (
                    retry_on_length
                    and output_budget_mode == "adaptive"
                    and not length_retried
                    and parsed.finish_reason in _LENGTH_FINISH_REASONS
                ):
                    # 有内容但被 length 截断（结构化输出必然残缺），同样扩容重发一次。
                    length_retried = True
                    payload = self._expand_length_budget(payload)
                    continue
                if parsed.finish_reason == "content_filter":
                    return parsed
                if not (
                    "DSML" in parsed.content.replace("｜", "|").upper()
                    or "<tool_call" in parsed.content.lower()
                ):
                    _raise_for_repetitive_output(parsed.content)
                if wire_api != "responses" and not parsed.tool_calls:
                    try:
                        cleaned, recovered_calls, recovered = _recover_textual_tool_calls(
                            parsed.content, tools
                        )
                    except StudioError as error:
                        raise ModelResponseError(error, parsed.usage) from None
                    if recovered:
                        parsed = ModelResponse(
                            content=cleaned,
                            finish_reason="tool_calls",
                            usage=parsed.usage,
                            tool_calls=recovered_calls,
                            raw_message=parsed.raw_message,
                            reasoning=parsed.reasoning,
                            provider_request_id=parsed.provider_request_id,
                            provider_input=parsed.provider_input,
                        )
                if (
                    wire_api != "responses"
                    and tools
                    and not parsed.content.strip()
                    and not parsed.tool_calls
                    and parsed.reasoning.strip()
                ):
                    # 思考型 OpenAI 兼容端点（GLM / DeepSeek / MiMo 等）在工具
                    # 回合可能只返回 reasoning_content。这是**已知的正常形态**，
                    # 不是传输故障：按档位关掉思考修一次（而不是强推
                    # tool_choice=required），仍为空则作为"可见空回合"返回，
                    # 交给上层循环决定——绝不升级成致命错误，否则会被 provider
                    # 重试链放大成整轮 ABORT（2026-09-18 实测事故）。
                    if retry_reasoning_only and not reasoning_only_retried:
                        reasoning_only_retried = True
                        repair_payload = {
                            **payload,
                            "messages": [
                                *list(payload.get("messages") or []),
                                {
                                    "role": "system",
                                    "content": (
                                        "The previous response contained reasoning only. "
                                        "Return a concrete final answer, or call one of the "
                                        "declared tools using the native tool-call protocol; "
                                        "do not return reasoning alone."
                                    ),
                                },
                            ],
                        }
                        payload = provider_compat.with_thinking_disabled(
                            repair_payload, profile, reasoning_effort=effective_effort,
                        )
                        continue
                    return ModelResponse(
                        content="",
                        finish_reason=parsed.finish_reason,
                        usage=parsed.usage,
                        tool_calls=[],
                        raw_message=parsed.raw_message,
                        reasoning=parsed.reasoning,
                        empty_reasoning_only=True,
                        provider_request_id=parsed.provider_request_id,
                        provider_input=parsed.provider_input,
                    )
                if parsed.tool_calls:
                    try:
                        normalized_calls = list(_normalize_tool_call_arguments(
                            parsed.tool_calls, tools,
                        ))
                    except StudioError as error:
                        raise ModelResponseError(error, parsed.usage) from None
                    parsed = ModelResponse(
                        content=parsed.content,
                        finish_reason=parsed.finish_reason,
                        usage=parsed.usage,
                        tool_calls=normalized_calls,
                        raw_message=parsed.raw_message,
                        reasoning=parsed.reasoning,
                        provider_request_id=parsed.provider_request_id,
                        provider_input=parsed.provider_input,
                    )
                return parsed
        except asyncio.CancelledError:
            raise
        except StudioError as error:
            if observation is not None:
                failure = (error if type(error) is ModelResponseError else
                           ModelResponseError(error, observed_usage))
                failure.provider_input = observation
                raise failure from None
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
        timeout_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        output_budget_mode: Literal["adaptive", "fixed"] = "adaptive",
        reasoning_effort: str | None = None,
        retry_empty_stream: bool = True,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None,
        compatibility_profile: provider_compat.CompatProfile | None = None,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        """Stream chat-completions deltas without buffering the response.

        Studio's Harness path uses the same credential and network policy as
        ``complete``. Wire-specific decoders share byte framing and lifecycle
        ownership; provider events remain outside the Native decision core.
        """
        if (model.wire_api or "chat").strip().lower() == "responses":
            iterator = self._stream_responses(
                model,
                messages=messages,
                network_policy=network_policy,
                timeout_seconds=timeout_seconds,
                tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                thinking_mode=thinking_mode,
                compatibility_profile=compatibility_profile,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                output_token_parameter=output_token_parameter,
            )
            async with owning_async_iterator(iterator, owner="studio.responses"):
                async for chunk in iterator:
                    yield chunk
            return
        if (model.wire_api or "chat").strip().lower() == "anthropic":
            iterator = self._stream_anthropic(
                model,
                messages=messages,
                network_policy=network_policy,
                timeout_seconds=timeout_seconds,
                tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                thinking_mode=thinking_mode,
                compatibility_profile=compatibility_profile,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                output_token_parameter=output_token_parameter,
            )
            async with owning_async_iterator(iterator, owner="studio.anthropic"):
                async for chunk in iterator:
                    yield chunk
            return
        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        profile = compatibility_profile or provider_compat.compat_profile(
            base_url=model.endpoint_url, model=model.model
        )
        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages,
            "stream": True,
            # OpenAI-compatible chat streams do not have to include usage in
            # ordinary delta chunks.  Request the terminal usage chunk when
            # the endpoint profile supports it; ``apply_chat_compat`` removes
            # this option for strict providers that reject the extension.
            "stream_options": {"include_usage": True},
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
            payload["parallel_tool_calls"] = True
        payload = provider_compat.apply_chat_compat(payload, profile, model=model.model)
        self._apply_tool_choice(payload, "chat", tool_choice)
        self._apply_parallel_tool_policy(payload, "chat", parallel_tool_calls)
        # Fixed allocations are owned by the caller, including empty-stream
        # fallback. Only the legacy adaptive path may apply recommendations.
        if max_output_tokens is not None and output_budget_mode == "adaptive":
            effective_budget = provider_compat.required_output_budget(
                profile, payload.get("max_tokens")
            )
            if effective_budget is not None:
                payload["max_tokens"] = effective_budget
        effective_effort = _effective_reasoning_effort(model, tools, reasoning_effort, profile)
        self._apply_reasoning_effort(payload, "chat", effective_effort)
        self._apply_thinking_policy(payload, "chat", profile, tools, effective_effort,
                                    thinking_mode=thinking_mode or model.parameters.thinking_mode)
        payload = provider_compat.apply_chat_compat(payload, profile, model=model.model)
        self._apply_output_token_parameter(payload, "chat", output_token_parameter)
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        client = self._http_client()
        request_started = time.monotonic()

        async def recover_empty_response() -> ModelResponse:
            """Ask once for a usable answer after a reasoning-only stream.

            Some OpenAI-compatible reasoning models occasionally finish a
            stream after emitting only ``reasoning_content``. Replaying the
            exact same request through the outer ten-attempt provider policy
            is both slow and usually deterministic. Give the provider one
            explicit, non-streaming repair turn instead; if that is empty as
            well, preserve ``MODEL_EMPTY_RESPONSE`` so the Harness can stop
            immediately rather than presenting it as transient congestion.
            """

            return await self.complete(
                model,
                messages=[
                    *messages,
                    {
                        "role": "system",
                        "content": (
                            "The previous response contained no usable final answer or tool "
                            "call. Return either a concrete final answer or one valid native "
                            "tool call now. Do not return reasoning alone."
                        ),
                    },
                ],
                network_policy=network_policy,
                timeout_seconds=timeout_seconds,
                max_attempts=1,
                backoff_seconds=0,
                tools=tools,
                retry_on_length=True,
                max_output_tokens=max_output_tokens,
                output_budget_mode=output_budget_mode,
                reasoning_effort=reasoning_effort,
                thinking_mode=thinking_mode,
                compatibility_profile=profile,
                **({"tool_choice": tool_choice} if tool_choice is not None else {}),
                **({"parallel_tool_calls": parallel_tool_calls}
                   if parallel_tool_calls is not None else {}),
                **({"output_token_parameter": output_token_parameter}
                   if output_token_parameter is not None else {}),
            )

        try:
            response_context = client.stream(
                "POST", model.endpoint_url, headers=headers, json=payload, timeout=timeout
            )
            async with owning_async_context(response_context, owner="studio.chat_http") as response:
                if response.is_redirect or response.status_code >= 400:
                    raise await _http_model_error(response, wire_api="chat")
                provider_request_id = _provider_request_id(response.headers)
                tool_stream = ChatToolCalls()
                first_chunk_logged = False
                raw_text = ""
                content_stream = ChatContent()
                emitted_text = ""
                pending_visible_text = ""
                textual_tool_detected = False
                latest_usage: Usage | None = None
                pending_done_finish_reason = ""
                refused = False
                wire_identity = ModelWireIdentity()
                last_repetition_check_length = 0
                async with _model_frames(
                    response, idle_timeout_seconds=timeout_seconds,
                    known_policy_abort=lambda: refused,
                ) as frames:
                    async for frame in frames:
                        raw = frame.data.strip()
                        if raw == "[DONE]":
                            break
                        if not raw:
                            continue
                        chunk = decode_model_frame(raw)
                        wire_identity.accept(chunk, wire_api="chat")
                        failure = provider_error(
                            chunk, wire_api="chat", headers=response.headers,
                            provider_request_id=provider_request_id,
                            usage=provider_usage(chunk.get("usage"), wire_api="chat"),
                        )
                        if failure is not None:
                            raise failure
                        choices = chat_choices(chunk)
                        finish_reason = next(
                            (
                                choice["finish_reason"]
                                for choice in choices
                                if choice.get("finish_reason")
                            ),
                            "",
                        )
                        if (
                            finish_reason
                            and pending_done_finish_reason
                            and finish_reason != pending_done_finish_reason
                        ):
                            raise invalid_model_frame()
                        usage_value = provider_usage(chunk.get("usage"), wire_api="chat")
                        if usage_value is not None:
                            latest_usage = usage_value
                        if usage_value is not None and not choices:
                            # A usage-only frame can precede or follow the finish.
                            # Report it now, so later absent/invalid data cannot
                            # erase known counters. Do not synthesize a terminal.
                            yield ModelStreamChunk(
                                usage=usage_value, provider_request_id=provider_request_id,
                            )
                        if not choices and chunk.get("choices"):
                            # Explicit alternative-only packets have no selected
                            # choice. Their root snapshots cannot be attributed to
                            # choice zero; keep only this attempt's known usage.
                            continue
                        root_message = chunk.get("message") or {}
                        try:
                            # Deltas precede same-frame complete snapshots. A
                            # snapshot may already include that last fragment.
                            for choice in choices:
                                tool_stream.accept(choice.get("delta"), snapshot=False)
                                tool_stream.accept(choice.get("message"), snapshot=True)
                                tool_stream.accept(choice, snapshot=True)
                            tool_stream.accept(root_message, snapshot=True)
                            tool_stream.accept(chunk, snapshot=True)
                        except StudioError as error:
                            raise ModelResponseError(error, latest_usage) from None
                        refused = refused or wire_refusal(
                            root_message.get("refusal"), wire_api="chat",
                        )
                        content_choices = choices or ([{}] if any(
                            root_message.get(key) is not None
                            for key in ("content", "reasoning_content", "reasoning")
                        ) else [])
                        for choice in content_choices:
                            delta = choice.get("delta") or {}
                            refused = wire_refusal(delta.get("refusal"), wire_api="chat") or refused
                            try:
                                text, reasoning = content_stream.accept(
                                    delta, choice.get("message") or {}, root_message,
                                )
                            except StudioError as error:
                                raise ModelResponseError(error, latest_usage) from None
                            raw_text += text
                            pending_visible_text += text
                            message = choice.get("message") or {}
                            refused = wire_refusal(message.get("refusal"), wire_api="chat") \
                                or refused
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
                                    _raise_for_repetitive_output(
                                        emitted_text + pending_visible_text
                                    )
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
                                    provider_request_id=provider_request_id,
                                )
                        if finish_reason:
                            pending_done_finish_reason = finish_reason
                            content_stream.finished = True
                            tool_stream.finished = True
                # A finish notification does not seal the HTTP response. Keep
                # consuming its usage and snapshots until [DONE] or natural
                # EOF, then publish exactly one final tool batch. A read error
                # before that boundary cannot authorize tool effects.
                if refused:
                    yield ModelStreamChunk(
                        usage=latest_usage, done=True, finish_reason="content_filter",
                        provider_request_id=provider_request_id,
                    )
                    return
                empty_output_limit_retry = (
                    retry_empty_stream and not tool_stream.calls and not raw_text.strip()
                    and pending_done_finish_reason in {
                        "length", "incomplete", "max_tokens", "max_output_tokens", "output_limit",
                    }
                )
                if (pending_done_finish_reason in _INCOMPLETE_CALL_NON_SUCCESS_FINISH_REASONS
                        and not empty_output_limit_retry):
                    # An explicit non-success termination cannot authorize any
                    # partially assembled tool. Preserve its reason; do not
                    # retry. An unfilled call under a success/missing reason
                    # still falls through to tool_stream.finish() and surfaces
                    # as a provider_retryable_error.
                    yield ModelStreamChunk(
                        tool_calls=(), usage=latest_usage, done=True,
                        finish_reason=pending_done_finish_reason,
                        provider_request_id=provider_request_id,
                    )
                    return
                try:
                    sealed_tools = tool_stream.finish()
                except IncompleteToolCallFrame:
                    # An unfilled tool call under a success/missing finish is
                    # incomplete safe inference, not a hostile frame: zero
                    # tool effects authorize one bounded retry. No argument is
                    # fabricated to {} and no tool executes; reported usage is
                    # preserved, never synthesized. This mirrors the
                    # non-success short-circuit above but classifies the
                    # attempt as provider_retryable instead of terminal, so a
                    # genuine MODEL_RESPONSE_INVALID (identity/shape/late
                    # mutation) stays non-retryable via the clause below.
                    yield ModelStreamChunk(
                        tool_calls=(), usage=latest_usage, done=True,
                        finish_reason="provider_retryable_error",
                        provider_request_id=provider_request_id,
                    )
                    return
                except StudioError as error:
                    raise ModelResponseError(error, latest_usage) from None
                calls = tuple(ToolCall(id=value.call_id, name=value.name, arguments=value.arguments)
                              for value in sealed_tools)
                final_text = raw_text
                if not calls and (
                    textual_tool_detected
                    or "DSML" in raw_text.replace("｜", "|").upper()
                    or "<tool_call" in raw_text.lower()
                ):
                    final_text, recovered_calls, _ = _recover_textual_tool_calls(raw_text, tools)
                    calls = tuple(recovered_calls)
                calls = _normalize_tool_call_arguments(calls, tools)
                if not final_text.strip() and not calls and retry_empty_stream:
                    repair = await recover_empty_response()
                    final_text = repair.content
                    calls = tuple(repair.tool_calls)
                    latest_usage = repair.usage
                    # A repaired explicit output-cap stream retains its original
                    # terminal reason. Ordinary empty streams use the repair
                    # reason so a truncated repair cannot become success.
                    if not empty_output_limit_retry:
                        pending_done_finish_reason = repair.finish_reason
                if not calls and not textual_tool_detected:
                    _raise_for_repetitive_output(final_text)
                remaining_text = final_text
                if emitted_text and final_text.startswith(emitted_text):
                    remaining_text = final_text[len(emitted_text) :]
                if remaining_text:
                    yield ModelStreamChunk(
                        text=remaining_text,
                        provider_request_id=provider_request_id,
                    )
                yield ModelStreamChunk(
                    tool_calls=calls,
                    usage=latest_usage,
                    done=True,
                    finish_reason=pending_done_finish_reason,
                    provider_request_id=provider_request_id,
                )
        except asyncio.CancelledError:
            raise

    async def _stream_responses(
        self,
        model: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        network_policy: NetworkPolicy,
        timeout_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        compatibility_profile: provider_compat.CompatProfile | None = None,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        """Adapt the native Responses SSE event stream to Harness chunks."""

        if (thinking_mode or model.parameters.thinking_mode) == "enabled":
            raise ValueError("model_thinking_mode_unsupported")

        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        payload = self._responses_payload(model, messages, tools)
        self._apply_tool_choice(payload, "responses", tool_choice)
        self._apply_parallel_tool_policy(payload, "responses", parallel_tool_calls)
        self._apply_output_token_parameter(payload, "responses", output_token_parameter)
        payload["stream"] = True
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        if reasoning_effort:
            profile = compatibility_profile or provider_compat.compat_profile(
                base_url=model.endpoint_url, model=model.model,
            )
            self._apply_reasoning_effort(
                payload, "responses", _effective_reasoning_effort(
                    model, None, reasoning_effort, profile,
                ),
            )
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        client = self._http_client()
        request_started = time.monotonic()
        first_delta_logged = False
        tool_items = ResponsesToolCalls()
        content_items = ResponsesContent(tool_items.items)
        from ksadk.harness.public_summary import PublicSummaryBuffer

        summary_buffer = PublicSummaryBuffer()
        raw_text = ""
        raw_reasoning = ""
        refused = False
        wire_identity = ModelWireIdentity()
        response_context = client.stream(
            "POST", model.endpoint_url, headers=headers, json=payload, timeout=timeout
        )
        async with owning_async_context(
            response_context, owner="studio.responses_http"
        ) as response:
            if response.is_redirect or response.status_code >= 400:
                raise await _http_model_error(response, wire_api="responses")
            provider_request_id = _provider_request_id(response.headers)
            async with _model_frames(
                response, idle_timeout_seconds=timeout_seconds,
                known_policy_abort=lambda: refused,
            ) as frames:
                async for frame in frames:
                    raw = frame.data.strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = decode_model_frame(raw)
                    wire_identity.accept(event, wire_api="responses")
                    if event.get("type") in {"error", "response.failed"}:
                        failed_body = event.get("response")
                        if failed_body is not None and not isinstance(failed_body, dict):
                            raise invalid_model_frame()
                        if event["type"] == "response.failed":
                            failed_body = dict(failed_body or {})
                            if failed_body.get("status") not in (None, "failed"):
                                raise invalid_model_frame()
                            failed_body["status"] = "failed"
                        else:
                            failed_body = {"error": event.get("error", event),
                                           "usage": event.get("usage")}
                        failure = provider_error(
                            failed_body, wire_api="responses", headers=response.headers,
                            provider_request_id=provider_request_id,
                            usage=provider_usage(failed_body.get("usage"), wire_api="responses"),
                        )
                        assert failure is not None
                        raise failure
                    completed_tool = tool_items.accept(event)
                    text, reasoning = content_items.accept(event)
                    event_type = str(event.get("type") or "")
                    public_summary = summary_buffer.drain(content_items.public_summary(), final=(
                        event_type in {"response.completed", "response.incomplete"}
                    ))
                    if public_summary and model.parameters.public_thinking_summary:
                        yield ModelStreamChunk(public_summary=public_summary,
                                               provider_request_id=provider_request_id)
                    refused = refused or content_items.refused
                    usage_value: Usage | None = None
                    if event_type in {"response.completed", "response.incomplete"}:
                        response_payload = event.get("response") or {}
                        usage_value = provider_usage(
                            response_payload.get("usage"), wire_api="responses",
                        )
                    elif event_type in {
                        "error", "response.failed", "response.cancelled", "response.canceled",
                    }:
                        raise StudioError(
                            "MODEL_REQUEST_FAILED",
                            "模型 Responses 流式响应未完成",
                            status_code=502,
                            details={"eventType": event_type},
                        )
                    if text or reasoning:
                        raw_text += text
                        raw_reasoning += reasoning
                        if not first_delta_logged:
                            first_delta_logged = True
                            _LOGGER.info(
                                "model Responses stream first delta: model=%s ttfb_ms=%d",
                                model.model,
                                int((time.monotonic() - request_started) * 1000),
                            )
                        yield ModelStreamChunk(
                            text=text,
                            reasoning=reasoning,
                            provider_request_id=provider_request_id,
                        )
                    if completed_tool is not None and not refused:
                        yield ModelStreamChunk(
                            tool_calls=(ToolCall(
                                id=completed_tool.call_id, name=completed_tool.name,
                                arguments=completed_tool.arguments or "{}",
                            ),),
                            provider_request_id=provider_request_id,
                        )
                    if event_type in {"response.completed", "response.incomplete"}:
                        try:
                            snapshot = reconcile_responses_snapshot(
                                response_payload,
                                status="completed" if event_type == "response.completed"
                                else "incomplete",
                                tools=tool_items, text=raw_text, reasoning=raw_reasoning,
                                content=content_items,
                            )
                        except StudioError as error:
                            raise ModelResponseError(error, usage_value) from None
                        if snapshot is not None:
                            refused = refused or snapshot.projection.finish == "content_filter"
                            if not refused:
                                if snapshot.text_tail or snapshot.reasoning_tail:
                                    yield ModelStreamChunk(
                                        text=snapshot.text_tail, reasoning=snapshot.reasoning_tail,
                                        provider_request_id=provider_request_id,
                                    )
                                for tool in snapshot.completed_calls:
                                    yield ModelStreamChunk(tool_calls=(ToolCall(
                                        id=tool.call_id, name=tool.name,
                                        arguments=tool.arguments or "{}",
                                    ),), provider_request_id=provider_request_id)
                        if refused:
                            yield ModelStreamChunk(
                                usage=usage_value, done=True, finish_reason="content_filter",
                                provider_request_id=provider_request_id,
                            )
                            return
                        if event_type == "response.completed":
                            try:
                                tool_items.finish()
                                content_items.finish()
                            except StudioError as error:
                                raise ModelResponseError(error, usage_value) from None
                            finish_reason = "tool_calls" if tool_items.calls else "stop"
                        else:
                            details = response_payload.get("incomplete_details")
                            if details is not None and not isinstance(details, dict):
                                raise invalid_model_frame()
                            reason = (details or {}).get("reason")
                            if reason is not None and type(reason) is not str:
                                raise invalid_model_frame()
                            if reason == "max_output_tokens":
                                try:
                                    content_items.finish(require_closed=False)
                                except StudioError as error:
                                    raise ModelResponseError(error, usage_value) from None
                            # An unclosed input must not grant bounded-text commit
                            # authority even when output exhaustion is confirmed.
                            open_input = any(not tool.closed for tool in tool_items.calls.values())
                            finish_reason = (
                                "content_filter" if reason == "content_filter"
                                else "length" if reason == "max_output_tokens" and not open_input
                                else "incomplete"
                            )
                            if snapshot is not None and snapshot.projection.calls:
                                finish_reason = snapshot.projection.finish
                        yield ModelStreamChunk(
                            usage=usage_value, done=True, finish_reason=finish_reason,
                            provider_request_id=provider_request_id,
                        )
                        return

            if refused:
                yield ModelStreamChunk(
                    done=True, finish_reason="content_filter",
                    provider_request_id=provider_request_id,
                )

    async def _stream_anthropic(
        self,
        model: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        network_policy: NetworkPolicy,
        timeout_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        compatibility_profile: provider_compat.CompatProfile | None = None,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        """Adapt the native Anthropic Messages SSE event stream to Harness chunks."""

        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        profile = compatibility_profile or provider_compat.compat_profile(
            base_url=model.endpoint_url, model=model.model,
        )
        payload = self._anthropic_payload(model, messages, tools, compatibility_profile=profile)
        self._apply_tool_choice(payload, "anthropic", tool_choice)
        self._apply_parallel_tool_policy(payload, "anthropic", parallel_tool_calls)
        self._apply_output_token_parameter(payload, "anthropic", output_token_parameter)
        payload["stream"] = True
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        effective_effort = _effective_reasoning_effort(model, tools, reasoning_effort, profile)
        self._apply_reasoning_effort(
            payload, "anthropic", effective_effort,
        )
        self._apply_thinking_policy(payload, "anthropic", profile, tools, effective_effort,
                                    thinking_mode=thinking_mode or model.parameters.thinking_mode)
        headers = self._anthropic_headers(credential, payload)
        headers["Accept"] = "text/event-stream"
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        client = self._http_client()
        request_started = time.monotonic()
        first_delta_logged = False
        # content_block index → tool_use block 累积（id/name 来自 block_start，参数来自分片）。
        tool_blocks = AnthropicToolBlocks()
        content_stream = AnthropicContent(tool_blocks)
        raw_text = ""
        stop_reason = ""
        refused = False
        usage_accumulator = AnthropicUsageAccumulator()
        wire_identity = ModelWireIdentity()
        response_context = client.stream(
            "POST", model.endpoint_url, headers=headers, json=payload, timeout=timeout
        )
        async with owning_async_context(
            response_context, owner="studio.anthropic_http"
        ) as response:
            if response.is_redirect or response.status_code >= 400:
                raise await _http_model_error(response, wire_api="anthropic")
            async with _model_frames(
                response, idle_timeout_seconds=timeout_seconds,
                known_policy_abort=lambda: refused or stop_reason == "refusal",
            ) as frames:
                async for frame in frames:
                    raw = frame.data.strip()
                    if not raw or raw == "[DONE]":
                        continue
                    event = decode_model_frame(raw)
                    wire_identity.accept(event, wire_api="anthropic")
                    if event.get("type") == "error":
                        failure = provider_error(
                            event, wire_api="anthropic", headers=response.headers,
                            provider_request_id=_provider_request_id(response.headers),
                            usage=provider_usage(event.get("usage"), wire_api="anthropic"),
                        )
                        if failure is None:
                            raise invalid_model_frame()
                        raise failure
                    tool_blocks.accept(event)
                    text, reasoning = content_stream.accept(event)
                    event_type = str(event.get("type") or "")
                    usage_update: Usage | None = None
                    if event_type == "message_start":
                        message_usage = (event.get("message") or {}).get("usage")
                        usage_update = usage_accumulator.update(message_usage)
                    elif event_type == "message_delta":
                        delta = event.get("delta") or {}
                        incoming_stop = delta.get("stop_reason")
                        if incoming_stop:
                            if stop_reason and incoming_stop != stop_reason:
                                raise invalid_model_frame()
                            stop_reason = incoming_stop
                            content_stream.finished = True
                        usage_update = usage_accumulator.update(event.get("usage"))
                        try:
                            refused = wire_refusal(
                                delta.get("stop_details"), wire_api="anthropic",
                            ) or refused
                        except StudioError as error:
                            raise ModelResponseError(error, usage_accumulator.report()) from None
                    elif event_type == "error":
                        raise StudioError(
                            "MODEL_REQUEST_FAILED",
                            "模型 Anthropic 流式响应未完成",
                            status_code=502,
                            details={
                                "eventType": str(
                                    (event.get("error") or {}).get("type") or event_type
                                )
                            },
                        )
                    if event_type in {"message_start", "message_delta"}:
                        fields = event["message"] if event_type == "message_start" else event
                        try:
                            observed = input_observation(fields, payload, event_type)
                        except (ValueError, TypeError):
                            raise ModelResponseError(
                                invalid_model_frame(), usage_accumulator.report(),
                            ) from None
                        if observed is not None:
                            # Final fallback metadata replaces the start array, including [].
                            yield ModelStreamChunk(provider_input=observed,
                                                   provider_request_id=_provider_request_id(
                                                       response.headers,
                                                   ))
                    raw_text += text
                    if text or reasoning or usage_update is not None:
                        if not first_delta_logged and (text or reasoning):
                            first_delta_logged = True
                            _LOGGER.info(
                                "model Anthropic stream first delta: model=%s ttfb_ms=%d",
                                model.model,
                                int((time.monotonic() - request_started) * 1000),
                            )
                        yield ModelStreamChunk(text=text, reasoning=reasoning, usage=usage_update)
                    if event_type == "message_stop":
                        if refused or stop_reason == "refusal":
                            yield ModelStreamChunk(
                                usage=usage_accumulator.report(), done=True,
                                finish_reason="content_filter",
                            )
                            return
                        try:
                            text_tail, reasoning_tail = content_stream.finish()
                        except StudioError as error:
                            raise ModelResponseError(error, usage_accumulator.report()) from None
                        if text_tail or reasoning_tail:
                            raw_text += text_tail
                            yield ModelStreamChunk(text=text_tail, reasoning=reasoning_tail)
                        calls = tuple(
                            ToolCall(
                                id=value.call_id, name=value.name,
                                arguments=value.arguments or "{}",
                            )
                            for value in tool_blocks.finish()
                        )
                        calls = _normalize_tool_call_arguments(calls, tools)
                        finish_reason = {
                            "end_turn": "stop",
                            "stop_sequence": "stop",
                            "max_tokens": "length",
                            "tool_use": "tool_calls",
                            "refusal": "content_filter",
                        }.get(stop_reason, stop_reason)
                        if refused:
                            finish_reason, calls = "content_filter", ()
                        if not raw_text.strip() and not calls and finish_reason != "content_filter":
                            raise StudioError(
                                "MODEL_EMPTY_RESPONSE",
                                "模型未返回可用内容",
                                status_code=502,
                                details={"finishReason": finish_reason},
                            )
                        if not calls and raw_text.strip():
                            _raise_for_repetitive_output(raw_text)
                        usage_value = usage_accumulator.report()
                        yield ModelStreamChunk(
                            tool_calls=calls, usage=usage_value, done=True,
                            finish_reason=finish_reason,
                            provider_content=content_stream.replay_content(),
                        )
                        return

            if refused or stop_reason == "refusal":
                yield ModelStreamChunk(
                    usage=usage_accumulator.report(), done=True, finish_reason="content_filter",
                )

    @staticmethod
    def _is_length_truncation(exc: StudioError) -> bool:
        if exc.code != "MODEL_EMPTY_RESPONSE":
            return False
        return str(exc.details.get("finishReason") or "") in _LENGTH_FINISH_REASONS

    @staticmethod
    def _expand_length_budget(payload: dict[str, Any]) -> dict[str, Any]:
        if "max_output_tokens" in payload:
            key = "max_output_tokens"
        elif "max_completion_tokens" in payload:
            key = "max_completion_tokens"
        else:
            key = "max_tokens"
        current = int(payload.get(key) or 0)
        payload[key] = max(current, _LENGTH_RETRY_TARGET_TOKENS)
        return payload

    @staticmethod
    def _apply_tool_choice(
        payload: dict[str, Any], wire_api: str, choice: str | dict[str, Any] | None,
    ) -> None:
        """Project explicit host admission without silently downgrading it.

        Omitted choice preserves each wire's existing default. Unsupported
        provider policies are reported by the ordinary error path; this layer
        never retries with auto or changes a reasoning profile to force tools.
        """
        if choice is None:
            return
        if isinstance(choice, dict):
            function = choice.get("function")
            if (set(choice) != {"type", "function"} or choice.get("type") != "function"
                    or not isinstance(function, dict) or set(function) != {"name"}
                    or not isinstance(function["name"], str) or not function["name"].strip()):
                raise ValueError("invalid named tool choice")
            name = function["name"]
            available = {
                tool.get("name") or (tool.get("function") or {}).get("name")
                for tool in payload.get("tools", ())
            }
            if name not in available:
                raise ValueError("named tool choice needs an advertised tool")
            payload["tool_choice"] = (
                {"type": "tool", "name": name} if wire_api == "anthropic"
                else {"type": "function", "name": name} if wire_api == "responses"
                else {"type": "function", "function": {"name": name}}
            )
            return
        if choice == "required" and not payload.get("tools"):
            raise ValueError("required tool choice needs an admitted tool")
        if wire_api == "anthropic":
            kinds = {"auto": "auto", "required": "any", "none": "none"}
            if choice not in kinds:
                raise ValueError("unsupported Anthropic tool choice")
            payload["tool_choice"] = {"type": kinds[choice]}
        else:
            payload["tool_choice"] = choice

    @staticmethod
    def _apply_parallel_tool_policy(
        payload: dict[str, Any], wire_api: str, parallel: bool | None,
    ) -> None:
        """Preserve explicit generation policy independently of tool execution.

        Omission keeps the existing wire defaults. This controls model output;
        it grants no permission to execute effects concurrently.
        """
        if parallel is None or not payload.get("tools"):
            return
        if wire_api == "anthropic":
            choice = dict(payload.get("tool_choice") or {"type": "auto"})
            if choice.get("type") != "none":
                choice["disable_parallel_tool_use"] = not parallel
            payload["tool_choice"] = choice
        else:
            payload["parallel_tool_calls"] = parallel

    @staticmethod
    def _apply_output_token_parameter(
        payload: dict[str, Any], wire_api: str,
        parameter: Literal["max_tokens", "max_completion_tokens"] | None,
    ) -> None:
        """Rename the Chat limit after budget projection; preserve its value.

        Responses and Anthropic keep their native output-limit fields. No
        model-name inference, second cap, or adaptive request is introduced.
        """
        if wire_api not in {"responses", "anthropic"} and parameter == "max_completion_tokens":
            if "max_tokens" in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")

    @staticmethod
    def _apply_thinking_policy(
        payload: dict[str, Any],
        wire_api: str,
        profile: provider_compat.CompatProfile,
        tools: list[dict[str, Any]] | None,
        effort: str | None,
        *, thinking_mode: Literal["enabled", "disabled"] | None = None,
    ) -> None:
        if thinking_mode == "enabled":
            # Chat's explicit thinking switch is not Anthropic's adaptive/
            # budgeted thinking or Responses' reasoning object. Do not guess a
            # cross-protocol mapping or silently discard the user's selection.
            if wire_api != "chat":
                raise ValueError("model_thinking_mode_unsupported")
            payload.update(provider_compat.with_thinking_enabled(payload, profile))
            return
        if wire_api not in {"chat", "anthropic"} or not profile.supports_thinking_field:
            return
        if thinking_mode == "disabled":
            payload.update(provider_compat.with_thinking_disabled(
                payload, profile, reasoning_effort=effort,
            ))
        elif provider_compat.should_disable_thinking(
            profile, tools_present=bool(tools) and payload.get("tool_choice") not in (
                "none", {"type": "none"},
            ), max_output_tokens=payload.get("max_tokens"),
            reasoning_effort=effort,
        ):
            payload["thinking"] = {"type": "disabled"}
        elif wire_api == "anthropic" and profile.anthropic_block_binding == "drop_block":
            thinking = dict(payload.get("thinking") or {"type": "adaptive"})
            if thinking.get("type") in {"adaptive", "enabled"}:
                thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
                payload["thinking"] = thinking

    @staticmethod
    def _anthropic_headers(credential: str, payload: dict[str, Any]) -> dict[str, str]:
        headers = {
            "x-api-key": credential,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        if "block_binding" in (payload.get("thinking") or {}):
            headers["anthropic-beta"] = "thinking-binding-controls-2026-08-01"
        return headers

    @staticmethod
    def _apply_reasoning_effort(
        payload: dict[str, Any], wire_api: str, effort: str | None,
    ) -> None:
        """Project an already resolved effort without changing thinking mode or caps."""
        if effort is None:
            return
        if wire_api in {"responses", "anthropic"}:
            key = "reasoning" if wire_api == "responses" else "output_config"
            config = payload.get(key)
            config = dict(config) if isinstance(config, dict) else {}
            config["effort"] = effort
            payload[key] = config
        else:
            payload["reasoning_effort"] = effort

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
        instructions, items = responses_messages(messages)
        payload: dict[str, Any] = {
            "model": model.model,
            "input": items,
        }
        if responses_tools:
            payload["tools"] = responses_tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
        if model.parameters.max_tokens is not None:
            payload["max_output_tokens"] = model.parameters.max_tokens
        if model.parameters.temperature is not None:
            payload["temperature"] = model.parameters.temperature
        if model.parameters.top_p is not None:
            payload["top_p"] = model.parameters.top_p
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        return payload

    @staticmethod
    def _parse_buffered_response(
        response: httpx.Response, *, wire_api: str, allow_empty: bool = False,
    ) -> ModelResponse:
        if len(response.content) > 16 * 1024 * 1024:
            raise StudioError(
                "MODEL_RESPONSE_TOO_LARGE", "模型响应超过 16 MiB 限制", status_code=502,
            )
        payload = decode_model_frame(response.content)
        validate_buffered_identity(payload, wire_api=wire_api)
        usage = provider_usage(payload.get("usage"), wire_api=wire_api)
        try:
            failure = provider_error(
                payload, wire_api=wire_api, usage=usage, headers=response.headers,
                provider_request_id=_provider_request_id(response.headers),
            )
            if failure is not None:
                raise failure
            parsed = buffered_projection(payload, wire_api=wire_api)
        except StudioError as error:
            raise ModelResponseError(error, usage) from None
        calls = [ToolCall(id=call.call_id, name=call.name, arguments=call.arguments, raw=call.raw)
                 for call in parsed.calls]
        if (not parsed.content and not calls and not allow_empty
                and parsed.finish not in {"content_filter", "canceled", "cancelled"}):
            raise ModelResponseError(StudioError(
                "MODEL_EMPTY_RESPONSE", "模型未返回可用内容", status_code=502,
                details={"finishReason": parsed.finish},
            ), usage)
        return ModelResponse(
            content=parsed.content, finish_reason=parsed.finish, usage=usage or Usage(),
            tool_calls=calls,
            raw_message=parsed.raw_message, reasoning=parsed.reasoning,
            provider_request_id=_provider_request_id(response.headers),
        )

    @staticmethod
    def _parse_responses_response(
        response: httpx.Response, allow_empty: bool = False,
    ) -> ModelResponse:
        return OpenAICompatibleModelClient._parse_buffered_response(
            response, wire_api="responses", allow_empty=allow_empty,
        )

    @staticmethod
    def _anthropic_payload(
        model: ResolvedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        compatibility_profile: provider_compat.CompatProfile | None = None,
    ) -> dict[str, Any]:
        """把 chat 语义消息映射到 Anthropic Messages API 最小可用载荷。"""
        profile = compatibility_profile or provider_compat.compat_profile(
            base_url=model.endpoint_url, model=model.model,
        )
        system_parts, merged = anthropic_messages(
            messages, mid_system=profile.anthropic_mid_system is True,
        )
        payload: dict[str, Any] = {
            "model": model.model,
            "messages": merged,
            # Anthropic Messages wire 必填 max_tokens；未配置 profile 参数时给安全默认。
            "max_tokens": model.parameters.max_tokens or _ANTHROPIC_DEFAULT_MAX_TOKENS,
        }
        if model.parameters.temperature is not None:
            payload["temperature"] = model.parameters.temperature
        if model.parameters.top_p is not None:
            payload["top_p"] = model.parameters.top_p
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        anthropic_tools = []
        for tool in tools or []:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict):
                continue
            anthropic_tools.append({
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            })
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        return payload

    @staticmethod
    def _parse_anthropic_response(
        response: httpx.Response, allow_empty: bool = False,
    ) -> ModelResponse:
        return OpenAICompatibleModelClient._parse_buffered_response(
            response, wire_api="anthropic", allow_empty=allow_empty,
        )

    @staticmethod
    def _parse_response(response: httpx.Response, allow_empty: bool = False) -> ModelResponse:
        return OpenAICompatibleModelClient._parse_buffered_response(
            response, wire_api="chat", allow_empty=allow_empty,
        )
