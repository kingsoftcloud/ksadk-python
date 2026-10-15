"""Native Hook contracts. A Hook can propose data, never grant runtime authority.

This is a new native protocol, not an adapter for the legacy ProviderHook API.
Dispatch and durable lifecycle admission belong to the Kernel and host Ports.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal, Protocol

from pydantic import Field, JsonValue, model_validator

from .model import ModelToolCall
from .state import Contract, RunScope

HookEvent = Literal["PreToolUse", "PostToolUse", "Stop", "PreCompact"]
HookFailure = Literal[
    "producer_failed", "producer_invalid", "input_budget_exceeded", "output_budget_exceeded",
    "producer_timeout", "binding_unavailable", "policy_denied", "policy_error",
]


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def hook_execution_policy_digest(*, cleanup_timeout_seconds: float) -> str:
    if (type(cleanup_timeout_seconds) not in (int, float)
            or not math.isfinite(cleanup_timeout_seconds) or cleanup_timeout_seconds <= 0):
        raise ValueError("Hook cleanup wait must be explicit, finite and positive")
    policy = {
        "version": "ksadk.native-hook-callback-lifetime/v1",
        "cleanup_timeout_seconds": float(cleanup_timeout_seconds),
    }
    return "sha256:" + hashlib.sha256(_json_bytes(policy)).hexdigest()


class HookDefinition(Contract):
    hook_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9._-]+$")
    source_ref: str = Field(min_length=1, max_length=512)
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    execution_policy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # Host authority for interpreting unknown callback outcomes. Never inferred
    # from the Hook's event/name or from model-produced data.
    read_only: bool = Field(strict=True)
    events: tuple[HookEvent, ...] = Field(min_length=1, max_length=4)
    tool_names: tuple[str, ...] | None = None
    # All I/O limits are explicit host inputs. No invisible timeout or retry.
    timeout_seconds: float = Field(strict=True, gt=0, allow_inf_nan=False)
    max_input_bytes: int = Field(strict=True, ge=1)
    max_output_bytes: int = Field(strict=True, ge=1)
    failure_mode: Literal["warn", "block"]
    compaction_source_projection: Literal["canonical_user"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def valid_selection(self):
        if len(set(self.events)) != len(self.events):
            raise ValueError("duplicate Hook events")
        if self.tool_names is not None and (
            not self.tool_names or any(not name.strip() for name in self.tool_names)
            or len(set(self.tool_names)) != len(self.tool_names)
        ):
            raise ValueError("invalid Hook tool selection")
        if set(self.events) <= {"Stop", "PreCompact"} and self.tool_names is not None:
            raise ValueError("lifecycle Hook cannot select tool names")
        if self.compaction_source_projection is not None and "PreCompact" not in self.events:
            raise ValueError("compaction projection requires PreCompact")
        return self

    def matches(self, event: HookEvent, tool_name: str | None = None) -> bool:
        return event in self.events and (
            event in {"Stop", "PreCompact"} or self.tool_names is None
            or tool_name in self.tool_names
        )


class PreCompactSubject(Contract):
    """The admitted original prefix, before any summary or callback projection."""

    source_revision: int = Field(strict=True, ge=0)
    source_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_message_count: int = Field(strict=True, ge=0)
    message_refs: tuple[str, ...]
    messages: tuple[dict[str, JsonValue], ...]
    retained_message_refs: tuple[str, ...]
    source_projection: Literal["canonical_user"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def source_alignment(self):
        if (len(self.message_refs) != len(self.messages)
                or len(set(self.message_refs)) != len(self.message_refs)
                or set(self.message_refs) & set(self.retained_message_refs)):
            raise ValueError("PreCompact source references do not match its removed messages")
        return self


class HookInvocationRequest(Contract):
    schema_version: Literal["ksadk.native-hook-request/v1"] = "ksadk.native-hook-request/v1"
    scope: RunScope
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    goal_revision: int = Field(strict=True, ge=0)
    goal: str = Field(min_length=1)
    tool_call_count: int = Field(strict=True, ge=0)
    stop_continuation_count: int = Field(strict=True, ge=0)
    event: HookEvent
    definition: HookDefinition
    call: ModelToolCall | None = None
    tool_result: dict[str, JsonValue] | None = None
    candidate_id: str | None = Field(default=None, min_length=1)
    candidate_text: str | None = None
    compaction: PreCompactSubject | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def valid_subject(self):
        if not self.definition.matches(self.event, self.call.name if self.call else None):
            raise ValueError("Hook event or tool is outside its definition")
        if self.event == "PreCompact":
            if (self.compaction is None or self.call is not None or self.tool_result is not None
                    or self.candidate_id is not None or self.candidate_text is not None):
                raise ValueError("PreCompact requires only an admitted compaction source")
            if self.compaction.source_projection != self.definition.compaction_source_projection:
                raise ValueError("PreCompact projection differs from its locked definition")
        elif self.compaction is not None:
            raise ValueError("only PreCompact may contain a compaction source")
        elif self.event == "Stop":
            if (self.call is not None or self.tool_result is not None
                    or self.candidate_id is None or self.candidate_text is None):
                raise ValueError("Stop Hook requires only a completion candidate")
        elif self.call is None or self.candidate_id is not None or self.candidate_text is not None:
            raise ValueError("tool Hook requires only a tool subject")
        elif self.event == "PreToolUse" and self.tool_result is not None:
            raise ValueError("PreToolUse cannot contain an executed result")
        elif self.event == "PostToolUse":
            if self.tool_result is None:
                raise ValueError("PostToolUse requires an executed result")
            # Lazy import keeps HookDefinition usable by the Port contracts.
            from .ports import ToolResult

            ToolResult.model_validate(self.tool_result)
        _json_bytes(self.input_payload())
        return self

    def input_payload(self) -> dict:
        payload = {
            "goal": self.goal,
            "tool_call_count": self.tool_call_count,
            "stop_continuation_count": self.stop_continuation_count,
            "call": self.call.model_dump(mode="json") if self.call else None,
            "tool_result": self.tool_result,
            "candidate_id": self.candidate_id,
            "candidate_text": self.candidate_text,
        }
        if self.compaction is not None:
            payload["compaction"] = self.compaction.model_dump(mode="json")
        return payload

    @property
    def input_digest(self) -> str:
        payload = {
            "definition": self.definition.model_dump(mode="json"),
            "input": self.input_payload(),
        }
        return "sha256:" + hashlib.sha256(_json_bytes(payload)).hexdigest()

    @property
    def input_bytes(self) -> int:
        return len(_json_bytes(self.input_payload()))

    @property
    def stop_hook_active(self) -> bool:
        return self.event == "Stop" and self.stop_continuation_count > 0


class HookOutput(Contract):
    action: Literal["continue", "deny", "repair"] = "continue"
    additional_context: tuple[str, ...] = ()
    updated_arguments: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def valid_data(self):
        _json_bytes(self.model_dump(mode="json"))
        if self.action == "repair" and not any(text.strip() for text in self.additional_context):
            raise ValueError("Stop repair requires actionable context")
        return self


class HookInvocationResult(Contract):
    schema_version: Literal["ksadk.native-hook-result/v1"] = "ksadk.native-hook-result/v1"
    scope: RunScope
    run_id: str
    session_id: str
    turn_id: str
    operation_key: str
    goal_revision: int = Field(strict=True, ge=0)
    event: HookEvent
    hook_id: str
    source_ref: str
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["ok", "failed", "uncertain"]
    output: HookOutput | None = None
    reason: HookFailure | None = None

    @model_validator(mode="after")
    def valid_outcome(self):
        if self.status == "ok":
            if self.output is None or self.reason is not None:
                raise ValueError("successful Hook requires only a valid output")
        elif self.output is not None or self.reason is None:
            raise ValueError("failed Hook cannot supply an output")
        if self.output is not None:
            if self.output.updated_arguments is not None and (
                self.event != "PreToolUse" or self.output.action != "continue"
            ):
                raise ValueError("only a continuing PreToolUse may propose new arguments")
            if self.output.action == "repair" and self.event != "Stop":
                raise ValueError("only Stop may request repair")
        return self

    @classmethod
    def for_request(
        cls, request: HookInvocationRequest, *, output: HookOutput | None = None,
        reason: HookFailure | None = None, uncertain: bool = False,
    ):
        return cls(
            **request.model_dump(include={
                "scope", "run_id", "session_id", "turn_id", "operation_key",
                "goal_revision", "event",
            }),
            hook_id=request.definition.hook_id, source_ref=request.definition.source_ref,
            binding_digest=request.definition.binding_digest, input_digest=request.input_digest,
            status="uncertain" if uncertain else "failed" if reason is not None else "ok",
            output=output, reason=reason,
        )


class HookPort(Protocol):
    async def invoke(self, request: HookInvocationRequest) -> HookInvocationResult: ...


def validate_hook_result(
    request: HookInvocationRequest, result: HookInvocationResult,
) -> HookInvocationResult:
    # Frozen Pydantic objects contain mutable JSON. Detach/revalidate both
    # sides, including objects created by model_copy/model_construct.
    request = HookInvocationRequest.model_validate_json(
        _json_bytes(request.model_dump(mode="json"))
    )
    result = HookInvocationResult.model_validate_json(_json_bytes(result.model_dump(mode="json")))
    identity = ("scope", "run_id", "session_id", "turn_id", "operation_key",
                "goal_revision", "event")
    if any(getattr(request, key) != getattr(result, key) for key in identity):
        raise ValueError("Hook receipt identity mismatch")
    if (result.hook_id, result.source_ref, result.binding_digest, result.input_digest) != (
        request.definition.hook_id, request.definition.source_ref,
        request.definition.binding_digest, request.input_digest,
    ):
        raise ValueError("Hook receipt binding or input mismatch")
    if request.input_bytes > request.definition.max_input_bytes and not (
        result.status == "failed" and result.reason == "input_budget_exceeded"
    ):
        raise ValueError("Hook input exceeded its declared budget")
    if result.output is not None and (
        len(_json_bytes(result.output.model_dump(mode="json")))
        > request.definition.max_output_bytes
    ):
        raise ValueError("Hook output exceeded its declared budget")
    return result
