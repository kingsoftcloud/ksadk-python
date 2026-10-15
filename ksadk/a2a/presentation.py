"""Frozen public contracts for remote scope and tool-activity presentation."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from typing_extensions import Self

from ksadk.a2a.ids import require_a2a_resource_id

EXECUTION_SCOPE_SCHEMA = "execution.scope/v1"
A2A_TOOL_ACTIVITY_EXTENSION_URI = "urn:ksadk:a2a:tool-activity:v1"
A2A_TOOL_ACTIVITY_SCHEMA = "ksadk.a2a.tool-activity/v1"
A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES = 16 * 1024
A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES = 1024 * 1024
PRESENTATION_INPUT_CAPABILITY = "ksadk.presentation"
PRESENTATION_OUTPUT_CAPABILITY = "agent.block"

PresentationProfile = Literal["flat-v1", "agent-block-v1"]
ScopeStatus = Literal[
    "submitted",
    "working",
    "input_required",
    "completed",
    "failed",
    "cancelled",
]

_NONBLANK_PATTERN = r"^\S(?:.*\S)?$"
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(authorization|cookie|credential|password|permit|secret|token|api[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_STACK_KEY = re.compile(
    r"(?:stack(?:[_-]?trace)?|trace[_-]?back|exception)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s(){}<>\"']+", re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(
    r"(?:\bBearer\s+\S{4,}|\b(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S{4,})",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"
_REDACTED_INTERNAL_URL = "[REDACTED_INTERNAL_URL]"
_MAX_PUBLIC_STRING_LENGTH = 4096
_MAX_PUBLIC_COLLECTION_ITEMS = 128


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ScopeTriggerRef(_FrozenContract):
    scope_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    item_id: str = Field(min_length=1, max_length=512, pattern=_NONBLANK_PATTERN)
    call_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)


class PublicAgentRef(_FrozenContract):
    id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    name: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    version_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    protocol: Literal["a2a/1.0"] = "a2a/1.0"

    @field_validator("id", "version_id")
    @classmethod
    def _resource_id(cls, value: str, info: Any) -> str:
        prefix = "a2a-agent-" if info.field_name == "id" else "a2a-version-"
        return require_a2a_resource_id(value, prefix, field_name=info.field_name)


class ScopeOutputRef(_FrozenContract):
    scope_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    item_id: str = Field(min_length=1, max_length=512, pattern=_NONBLANK_PATTERN)
    part_id: str | None = Field(default=None, min_length=1, max_length=512)


class ScopeCancelState(_FrozenContract):
    capability: Literal["supported", "unsupported", "unknown"]
    request_state: Literal[
        "none",
        "cancel_requested",
        "unsupported",
        "unknown",
        "confirmed",
        "local_confirmed",
    ] = "none"


class ExecutionScopeDescriptor(_FrozenContract):
    """The only child-scope lifecycle record projected into RuntimeEvent v2."""

    schema_: Literal["execution.scope/v1"] = Field(
        default=EXECUTION_SCOPE_SCHEMA,
        alias="schema",
    )
    scope_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    parent_scope_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    trigger_ref: ScopeTriggerRef
    binding_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    agent: PublicAgentRef
    status: ScopeStatus
    started_at: float = Field(gt=0, strict=True, allow_inf_nan=False)
    ended_at: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
    output_refs: tuple[ScopeOutputRef, ...] = ()
    cancel: ScopeCancelState

    @model_validator(mode="after")
    def _state_invariants(self) -> Self:
        if self.scope_id == self.parent_scope_id:
            raise ValueError("A2A_SCOPE_PARENT_CYCLE")
        if self.trigger_ref.scope_id != self.parent_scope_id:
            raise ValueError("A2A_SCOPE_TRIGGER_PARENT_MISMATCH")
        terminal = self.status in {"completed", "failed", "cancelled"}
        if terminal != (self.ended_at is not None):
            raise ValueError("A2A_SCOPE_TERMINAL_TIME_MISMATCH")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("A2A_SCOPE_END_PRECEDES_START")
        if any(output.scope_id != self.scope_id for output in self.output_refs):
            raise ValueError("A2A_SCOPE_OUTPUT_REF_MISMATCH")
        if self.status == "cancelled" and self.cancel.request_state not in {
            "confirmed",
            "local_confirmed",
        }:
            raise ValueError("A2A_SCOPE_CANCEL_CONFIRMATION_REQUIRED")
        if self.cancel.request_state == "confirmed" and self.cancel.capability != "supported":
            raise ValueError("A2A_SCOPE_CANCEL_CAPABILITY_MISMATCH")
        if self.cancel.request_state == "unsupported" and self.cancel.capability != "unsupported":
            raise ValueError("A2A_SCOPE_CANCEL_CAPABILITY_MISMATCH")
        if (
            self.cancel.request_state in {"confirmed", "local_confirmed"}
            and self.status != "cancelled"
        ):
            raise ValueError("A2A_SCOPE_CANCEL_STATUS_MISMATCH")
        return self

    def public_locator(self) -> dict[str, Any]:
        """Return the immutable fields that every replacement must preserve."""

        return {
            "schema": self.schema_,
            "scope_id": self.scope_id,
            "parent_scope_id": self.parent_scope_id,
            "trigger_ref": self.trigger_ref.model_dump(mode="json"),
            "binding_id": self.binding_id,
            "agent": self.agent.model_dump(mode="json"),
            "started_at": self.started_at,
        }


_SCOPE_TRANSITIONS: dict[ScopeStatus, frozenset[ScopeStatus]] = {
    "submitted": frozenset({"submitted", "working", "cancelled"}),
    "working": frozenset({"working", "input_required", "completed", "failed", "cancelled"}),
    "input_required": frozenset({"input_required", "working", "completed", "failed", "cancelled"}),
    "completed": frozenset({"completed"}),
    "failed": frozenset({"failed"}),
    "cancelled": frozenset({"cancelled"}),
}


def validate_scope_transition(
    previous: ExecutionScopeDescriptor,
    current: ExecutionScopeDescriptor,
) -> None:
    """Validate immutable locator fields and the frozen child state machine."""

    if previous.public_locator() != current.public_locator():
        raise ValueError("A2A_SCOPE_LOCATOR_CONFLICT")
    if current.status not in _SCOPE_TRANSITIONS[previous.status]:
        raise ValueError("A2A_SCOPE_STATUS_TRANSITION_INVALID")
    if current.cancel.request_state == "local_confirmed" and previous.status not in {
        "submitted",
        "cancelled",
    }:
        raise ValueError("A2A_SCOPE_LOCAL_CANCEL_REQUIRES_PRE_SEND")
    if previous.status in {"completed", "failed", "cancelled"} and previous != current:
        raise ValueError("A2A_SCOPE_TERMINAL_CONFLICT")


class ToolActivity(_FrozenContract):
    """One already-sanitized public tool observation carried by A2A DataPart."""

    schema_: Literal["ksadk.a2a.tool-activity/v1"] = Field(
        default=A2A_TOOL_ACTIVITY_SCHEMA,
        alias="schema",
    )
    event_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    item_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    call_id: str = Field(min_length=1, max_length=256, pattern=_NONBLANK_PATTERN)
    item_kind: Literal["tool_call", "tool_result"]
    operation: Literal["started", "updated", "completed", "failed"]
    execution_status: Literal["running", "completed", "failed", "unknown"]
    name: str | None = None
    arguments: JsonValue = None
    result: JsonValue = None
    is_error: bool = Field(default=False, strict=True)
    visibility: Literal["public"] = "public"
    truncated: bool = Field(default=False, strict=True)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str | None) -> str | None:
        if value is not None and not _TOOL_NAME.fullmatch(value):
            raise ValueError("tool activity name is not a safe public label")
        return value

    @model_validator(mode="after")
    def _public_invariants(self) -> Self:
        payload = {"arguments": self.arguments, "result": self.result}
        if _unsafe_public_paths(payload):
            raise ValueError("unsafe public tool activity")
        if self.item_kind == "tool_call" and self.name is None:
            raise ValueError("A2A_TOOL_NAME_REQUIRED")
        if self.item_kind == "tool_call" and self.result is not None:
            raise ValueError("A2A_TOOL_CALL_RESULT_NOT_ALLOWED")
        if self.item_kind == "tool_result" and self.arguments is not None:
            raise ValueError("A2A_TOOL_RESULT_ARGUMENTS_NOT_ALLOWED")
        if self.is_error and self.execution_status != "failed":
            raise ValueError("A2A_TOOL_ERROR_STATUS_MISMATCH")
        if self.operation == "failed" and self.execution_status != "failed":
            raise ValueError("A2A_TOOL_OPERATION_STATUS_MISMATCH")
        if len(self.model_dump_json().encode("utf-8")) > A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES:
            raise ValueError("A2A_TOOL_ACTIVITY_EVENT_LIMIT")
        return self


def _sensitive_key_name(key: str) -> bool:
    # Split both lowerCamelCase and acronym boundaries before token matching.
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return bool(_SENSITIVE_KEY.search(normalized) or _STACK_KEY.search(normalized))


def _is_sensitive_key(key: str, value: object) -> bool:
    return _sensitive_key_name(key) and value != _REDACTED


def _contains_internal_url(value: str) -> bool:
    for raw_url in _URL.findall(value):
        try:
            host = (urlsplit(raw_url).hostname or "").lower().rstrip(".")
        except ValueError:
            return True  # Malformed authorities cannot be declared public.
        if host == "localhost" or host.endswith((".internal", ".local", ".localhost")):
            return True
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            continue
        if not address.is_global:
            return True
    return False


def _unsafe_public_paths(value: object) -> tuple[str, ...]:
    unsafe: list[str] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if _is_sensitive_key(str(key), item):
                    unsafe.append(child_path)
                visit(item, child_path)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            for index, item in enumerate(node):
                visit(item, f"{path}[{index}]")
        elif isinstance(node, str) and node not in {_REDACTED, _REDACTED_INTERNAL_URL}:
            if _contains_internal_url(node) or _SENSITIVE_VALUE.search(node):
                unsafe.append(path)

    visit(value, "")
    return tuple(unsafe)


def _sanitize_public_value(value: object) -> tuple[JsonValue, bool]:
    truncated = False
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _MAX_PUBLIC_COLLECTION_ITEMS:
                truncated = True
                break
            key = str(raw_key)[:256]
            if _sensitive_key_name(key):
                result[key] = _REDACTED
                truncated = True
                continue
            clean, child_truncated = _sanitize_public_value(item)
            result[key] = clean
            truncated = truncated or child_truncated
        return result, truncated
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values: list[JsonValue] = []
        for index, item in enumerate(value):
            if index >= _MAX_PUBLIC_COLLECTION_ITEMS:
                truncated = True
                break
            clean, child_truncated = _sanitize_public_value(item)
            values.append(clean)
            truncated = truncated or child_truncated
        return values, truncated
    if isinstance(value, str):
        if _contains_internal_url(value):
            return _REDACTED_INTERNAL_URL, True
        if _SENSITIVE_VALUE.search(value):
            return _REDACTED, True
        if len(value) > _MAX_PUBLIC_STRING_LENGTH:
            return value[:_MAX_PUBLIC_STRING_LENGTH] + "…[truncated]", True
        return value, False
    if value is None or isinstance(value, (bool, int, float)):
        return cast(JsonValue, value), False
    return str(value)[:_MAX_PUBLIC_STRING_LENGTH], True


def build_public_tool_activity(payload: Mapping[str, Any]) -> ToolActivity:
    """Sanitize untrusted native values into the strict public wire contract."""

    values = dict(payload)
    truncated = bool(values.get("truncated", False))
    for field in ("arguments", "result"):
        clean, field_truncated = _sanitize_public_value(values.get(field))
        values[field] = clean
        truncated = truncated or field_truncated
    values["truncated"] = truncated
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES:
        values["arguments"] = (
            {"summary": "public tool payload omitted by size policy"}
            if values.get("arguments") is not None
            else None
        )
        values["result"] = (
            {"summary": "public tool payload omitted by size policy"}
            if values.get("result") is not None
            else None
        )
        values["truncated"] = True
    return ToolActivity.model_validate(values)


def validate_tool_activity_batch(activities: Iterable[ToolActivity]) -> None:
    """Enforce the cumulative public tool-activity limit for one invocation."""

    total = sum(len(activity.model_dump_json().encode("utf-8")) for activity in activities)
    if total > A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES:
        raise ValueError("A2A_TOOL_ACTIVITY_INVOCATION_LIMIT")


def negotiate_presentation_profile(
    *,
    producer_declares_capability: bool,
    requested_profile: str | None,
    renderer_supports_agent_block: bool,
) -> PresentationProfile:
    """Choose one lane; any missing capability falls back to ``flat-v1``."""

    if requested_profile not in {None, "flat-v1", "agent-block-v1"}:
        raise ValueError(f"unsupported presentation profile: {requested_profile!r}")
    if (
        producer_declares_capability
        and requested_profile == "agent-block-v1"
        and renderer_supports_agent_block
    ):
        return "agent-block-v1"
    return "flat-v1"


__all__ = [
    "A2A_TOOL_ACTIVITY_EXTENSION_URI",
    "A2A_TOOL_ACTIVITY_MAX_EVENT_BYTES",
    "A2A_TOOL_ACTIVITY_MAX_INVOCATION_BYTES",
    "A2A_TOOL_ACTIVITY_SCHEMA",
    "EXECUTION_SCOPE_SCHEMA",
    "PRESENTATION_INPUT_CAPABILITY",
    "PRESENTATION_OUTPUT_CAPABILITY",
    "ExecutionScopeDescriptor",
    "PresentationProfile",
    "PublicAgentRef",
    "ScopeCancelState",
    "ScopeOutputRef",
    "ScopeStatus",
    "ScopeTriggerRef",
    "ToolActivity",
    "build_public_tool_activity",
    "negotiate_presentation_profile",
    "validate_scope_transition",
    "validate_tool_activity_batch",
]
