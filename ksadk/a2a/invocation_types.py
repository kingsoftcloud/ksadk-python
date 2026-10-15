"""Credential-free declarations and stable local handles for A2A invocations.

These contracts identify a call but never authorize or route it. The control
plane remains the sole owner of membership, route, permit, and remote Task
resolution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    field_validator,
    model_validator,
    model_serializer,
)
from typing_extensions import Self

from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.a2a.message_digest import (
    MAX_A2A_MESSAGE_BYTES,
    MAX_A2A_MESSAGE_ID_LENGTH,
    MAX_A2A_MESSAGE_PARTS,
    canonical_message_bytes,
    canonical_message_sha256,
)
from ksadk.events.identity import stable_scope_id

if TYPE_CHECKING:
    from a2a.types import Message

_NonBlank = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")]
_UnixDeadline = Annotated[
    float,
    Field(gt=0, lt=253_402_300_800, strict=True, allow_inf_nan=False),
]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class RemoteAgentSpec(_FrozenContract):
    """A fixed Space binding without an endpoint, permit, or credential."""

    local_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    binding_id: _NonBlank
    space_id: str
    target_agent_id: str
    expected_version_id: str | None = None
    description: str | None = Field(default=None, max_length=4096)
    exposure: Literal["native", "tool", "auto"] = "auto"
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False, strict=True)
    policy_ref: str | None = Field(default=None, max_length=256)

    @field_validator("policy_ref")
    @classmethod
    def _policy_ref(cls, value):
        from ksadk.a2a.policy_refs import resolve_policy_ref
        resolve_policy_ref(value)
        return value

    @model_serializer(mode="wrap")
    def _serialize_compatible(self, handler):
        result = handler(self)
        # Existing checkpoints and approval digests predate this optional field.
        if self.policy_ref is None:
            result.pop("policy_ref", None)
        return result

    @field_validator("space_id", "target_agent_id", "expected_version_id")
    @classmethod
    def _resource_id(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        assert info.field_name is not None
        prefix = {
            "space_id": "a2a-space-",
            "target_agent_id": "a2a-agent-",
            "expected_version_id": "a2a-version-",
        }[info.field_name]
        return cast(str, require_a2a_resource_id(value, prefix, field_name=info.field_name))

    def resolve_version(self, version_id: str, *, production: bool = True) -> Self:
        """Freeze development discovery, or reject an unpin/drift in production."""

        if not isinstance(version_id, str):
            raise ValueError("A2A_VERSION_REQUIRED")
        values = self.model_dump()
        values["expected_version_id"] = version_id
        resolved = cast(Self, type(self).model_validate(values))
        if self.expected_version_id is None and production:
            raise ValueError("A2A_VERSION_PIN_REQUIRED")
        if self.expected_version_id is not None and self.expected_version_id != version_id:
            raise ValueError("A2A_VERSION_DRIFT")
        return resolved


class A2AInvocationHandle(_FrozenContract):
    """Immutable local identity allocated before the remote Task exists."""

    run_id: _NonBlank
    session_id: _NonBlank
    parent_scope_id: _NonBlank
    call_id: _NonBlank
    binding_id: _NonBlank
    target_agent_id: str
    attempt_id: _NonBlank
    platform_task_id: str | None = None
    remote_task_id: _NonBlank | None = None
    remote_context_id: _NonBlank | None = None

    @field_validator("target_agent_id", "platform_task_id")
    @classmethod
    def _resource_id(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        assert info.field_name is not None
        prefix = "a2a-agent-" if info.field_name == "target_agent_id" else "a2a-task-"
        return cast(str, require_a2a_resource_id(value, prefix, field_name=info.field_name))

    @model_validator(mode="after")
    def _binding_invariants(self) -> Self:
        if self.remote_task_id is not None and self.platform_task_id is None:
            raise ValueError("A2A_PLATFORM_TASK_REQUIRED")
        if self.remote_context_id is not None and self.remote_task_id is None:
            raise ValueError("A2A_REMOTE_TASK_REQUIRED")
        return self

    @property
    def scope_id(self) -> str:
        return cast(
            str,
            stable_scope_id(
                "a2a",
                self.run_id,
                self.call_id,
                self.target_agent_id,
                self.attempt_id,
            ),
        )

    def with_platform_task(self, platform_task_id: str) -> Self:
        if not isinstance(platform_task_id, str):
            raise ValueError("A2A_PLATFORM_TASK_REQUIRED")
        if self.platform_task_id is not None and self.platform_task_id != platform_task_id:
            raise ValueError("A2A_BINDING_CONFLICT")
        return cast(
            Self,
            type(self).model_validate({**self.model_dump(), "platform_task_id": platform_task_id}),
        )

    def bind_remote_task(
        self,
        remote_task_id: str,
        remote_context_id: str | None = None,
    ) -> Self:
        if self.platform_task_id is None:
            raise ValueError("A2A_PLATFORM_TASK_REQUIRED")
        if not isinstance(remote_task_id, str):
            raise ValueError("A2A_REMOTE_TASK_REQUIRED")
        if self.remote_task_id is not None and (self.remote_task_id, self.remote_context_id) != (
            remote_task_id,
            remote_context_id,
        ):
            raise ValueError("A2A_BINDING_CONFLICT")
        return cast(
            Self,
            type(self).model_validate(
                {
                    **self.model_dump(),
                    "remote_task_id": remote_task_id,
                    "remote_context_id": remote_context_id,
                }
            ),
        )


class A2AInvocationMessage(_FrozenContract):
    """Initial or continuation user content represented by an A2A Message."""

    message_id: str = Field(
        min_length=1,
        max_length=MAX_A2A_MESSAGE_ID_LENGTH,
        pattern=r"^\S(?:.*\S)?$",
    )
    text_parts: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_A2A_MESSAGE_PARTS,
        strict=False,
        repr=False,
    )
    data_parts: tuple[JsonValue, ...] = Field(
        default=(),
        max_length=MAX_A2A_MESSAGE_PARTS,
        strict=False,
        repr=False,
    )

    @field_validator("text_parts")
    @classmethod
    def _nonempty_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return value

    @model_validator(mode="after")
    def _wire_size(self) -> Self:
        if not self.data_parts and not any(part.strip() for part in self.text_parts):
            raise ValueError("A2A_MESSAGE_CONTENT_REQUIRED")
        if len(self.text_parts) + len(self.data_parts) > MAX_A2A_MESSAGE_PARTS:
            raise ValueError("A2A_MESSAGE_TOO_MANY_PARTS")
        if len(canonical_message_bytes(self.to_wire())) > MAX_A2A_MESSAGE_BYTES:
            raise ValueError("A2A_MESSAGE_TOO_LARGE")
        return self

    def to_wire(self) -> Message:
        from a2a.types import Message, Part, Role
        from google.protobuf.json_format import ParseDict
        from google.protobuf.struct_pb2 import Value

        return Message(
            message_id=self.message_id,
            role=Role.ROLE_USER,
            parts=[
                *[Part(text=text) for text in self.text_parts],
                *[Part(data=ParseDict(value, Value())) for value in self.data_parts],
            ],
        )

    @property
    def sha256(self) -> str:
        return canonical_message_sha256(self.to_wire())


class A2AInvocationContext(_FrozenContract):
    """Correlation fields; never proof of identity or approval."""

    caller_agent_id: _NonBlank
    audit_id: _NonBlank
    approval_request_id: _NonBlank | None = None


class A2AContentPolicy(_FrozenContract):
    include_reasoning_summary: bool = Field(default=False, strict=True)


class A2AInvocationRequest(_FrozenContract):
    spec: RemoteAgentSpec
    handle: A2AInvocationHandle
    message: A2AInvocationMessage
    context: A2AInvocationContext
    deadline_unix_seconds: _UnixDeadline
    idempotency_token: _NonBlank = Field(repr=False)
    content_policy: A2AContentPolicy = Field(default_factory=A2AContentPolicy)

    @model_validator(mode="after")
    def _initial_identity(self) -> Self:
        if self.spec.target_agent_id != self.handle.target_agent_id:
            raise ValueError("A2A_TARGET_MISMATCH")
        if self.spec.binding_id != self.handle.binding_id:
            raise ValueError("A2A_BINDING_MISMATCH")
        if self.handle.platform_task_id is not None:
            raise ValueError("A2A_UNBOUND_HANDLE_REQUIRED")
        return self

    @property
    def scope_id(self) -> str:
        return self.handle.scope_id

    @property
    def message_sha256(self) -> str:
        return self.message.sha256


A2AApprovalDecision = Literal["approved", "denied"]
A2AInvocationStatus = Literal[
    "submitted",
    "working",
    "input_required",
    "auth_required",
    "completed",
    "failed",
    "cancelled",
    "rejected",
    "unknown",
]
A2ASnapshotSource = Literal["local", "stream", "get_task"]
A2ACancelOutcome = Literal[
    "local_cancelled_before_send",
    "remote_cancelled",
    "cancel_requested_but_remote_unknown",
    "cancel_requested_but_remote_unsupported",
    "already_terminal",
    "not_found",
    "failed",
]


class A2AContinuationRequest(_FrozenContract):
    spec: RemoteAgentSpec
    handle: A2AInvocationHandle
    message: A2AInvocationMessage
    context: A2AInvocationContext
    deadline_unix_seconds: _UnixDeadline
    idempotency_token: _NonBlank = Field(repr=False)
    approval_decision: A2AApprovalDecision | None = None

    @model_validator(mode="after")
    def _continuation_identity(self) -> Self:
        if self.spec.target_agent_id != self.handle.target_agent_id:
            raise ValueError("A2A_TARGET_MISMATCH")
        if self.spec.binding_id != self.handle.binding_id:
            raise ValueError("A2A_BINDING_MISMATCH")
        if self.handle.platform_task_id is None:
            raise ValueError("A2A_PLATFORM_TASK_REQUIRED")
        if self.handle.remote_task_id is None:
            raise ValueError("A2A_REMOTE_TASK_REQUIRED")
        if self.approval_decision is not None and self.context.approval_request_id is None:
            raise ValueError("A2A_APPROVAL_CONTEXT_REQUIRED")
        return self

    @property
    def scope_id(self) -> str:
        return self.handle.scope_id

    @property
    def message_sha256(self) -> str:
        return self.message.sha256

    def to_wire_message(self) -> Message:
        message = self.message.to_wire()
        message.task_id = self.handle.remote_task_id
        if self.handle.remote_context_id is not None:
            message.context_id = self.handle.remote_context_id
        return message


class A2AInvocationError(_FrozenContract):
    """Sanitized failure safe to persist; stack/details are deliberately absent."""

    code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1, max_length=4096)
    retryable: bool = Field(default=False, strict=True)


class A2AInvocationOutputRef(_FrozenContract):
    scope_id: _NonBlank
    item_id: _NonBlank
    part_id: _NonBlank | None = None


class A2AInvocationSnapshot(_FrozenContract):
    handle: A2AInvocationHandle
    status: A2AInvocationStatus
    source: A2ASnapshotSource
    observed_at_unix_seconds: float = Field(gt=0, strict=True, allow_inf_nan=False)
    event_cursor: int = Field(default=0, ge=0, strict=True)
    last_event_id: _NonBlank | None = None
    output_refs: tuple[A2AInvocationOutputRef, ...] = ()
    continuation_id: _NonBlank | None = None
    error: A2AInvocationError | None = None

    @model_validator(mode="after")
    def _snapshot_invariants(self) -> Self:
        if self.handle.platform_task_id is None and not (
            self.status == "cancelled" and self.source == "local"
            and not self.output_refs and self.continuation_id is None
        ):
            raise ValueError("A2A_PLATFORM_TASK_REQUIRED")
        if self.source in {"stream", "get_task"} and self.handle.remote_task_id is None:
            raise ValueError("A2A_REMOTE_TASK_REQUIRED")
        if self.status in {"input_required", "auth_required"} and self.continuation_id is None:
            raise ValueError("A2A_CONTINUATION_REQUIRED")
        if self.status == "failed" and self.error is None:
            raise ValueError("A2A_ERROR_REQUIRED")
        if self.error is not None and self.status not in {"failed", "unknown"}:
            raise ValueError("A2A_ERROR_NOT_ALLOWED")
        if any(output.scope_id != self.handle.scope_id for output in self.output_refs):
            raise ValueError("A2A_OUTPUT_SCOPE_MISMATCH")
        return self

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled", "rejected"}


class A2ACancelResult(_FrozenContract):
    handle: A2AInvocationHandle
    outcome: A2ACancelOutcome
    requested_at_unix_seconds: float = Field(gt=0, strict=True, allow_inf_nan=False)
    resolved_at_unix_seconds: float = Field(gt=0, strict=True, allow_inf_nan=False)
    snapshot: A2AInvocationSnapshot | None = None
    error: A2AInvocationError | None = None

    @model_validator(mode="after")
    def _cancel_invariants(self) -> Self:
        # Before task allocation, allow queued intent or a confirmed local
        # cancellation snapshot. Neither permits claiming remote cancellation.
        if self.handle.platform_task_id is None and not (
            self.handle.remote_task_id is None and (
                (self.outcome == "cancel_requested_but_remote_unknown" and self.snapshot is None)
                or (self.outcome in {"local_cancelled_before_send", "already_terminal"}
                    and self.snapshot is not None and self.snapshot.status == "cancelled"
                    and self.snapshot.source == "local")
            )
        ):
            raise ValueError("A2A_PLATFORM_TASK_REQUIRED")
        if self.resolved_at_unix_seconds < self.requested_at_unix_seconds:
            raise ValueError("A2A_RESOLUTION_PRECEDES_REQUEST")
        if self.outcome == "remote_cancelled" and self.handle.remote_task_id is None:
            raise ValueError("A2A_REMOTE_TASK_REQUIRED")
        if self.outcome == "local_cancelled_before_send" and self.handle.remote_task_id is not None:
            raise ValueError("A2A_LOCAL_CANCEL_REQUIRES_UNBOUND_REMOTE")
        if self.snapshot is not None and self.snapshot.handle != self.handle:
            raise ValueError("A2A_SNAPSHOT_HANDLE_MISMATCH")
        if self.outcome in {"local_cancelled_before_send", "remote_cancelled"} and (
            self.snapshot is None or self.snapshot.status != "cancelled"
        ):
            raise ValueError("A2A_CANCELLED_SNAPSHOT_REQUIRED")
        if self.outcome == "remote_cancelled" and (
            self.snapshot is None or self.snapshot.source not in {"stream", "get_task"}
        ):
            raise ValueError("A2A_REMOTE_CANCEL_EVIDENCE_REQUIRED")
        if self.outcome == "already_terminal" and (
            self.snapshot is None or not self.snapshot.terminal
        ):
            raise ValueError("A2A_TERMINAL_SNAPSHOT_REQUIRED")
        if self.outcome == "failed" and self.error is None:
            raise ValueError("A2A_ERROR_REQUIRED")
        if self.error is not None and self.outcome != "failed":
            raise ValueError("A2A_ERROR_NOT_ALLOWED")
        return self

    @property
    def confirmed_cancelled(self) -> bool:
        return self.outcome in {"local_cancelled_before_send", "remote_cancelled"}


__all__ = [
    "A2AApprovalDecision",
    "A2ACancelOutcome",
    "A2ACancelResult",
    "A2AContentPolicy",
    "A2AContinuationRequest",
    "A2AInvocationContext",
    "A2AInvocationError",
    "A2AInvocationHandle",
    "A2AInvocationMessage",
    "A2AInvocationOutputRef",
    "A2AInvocationRequest",
    "A2AInvocationSnapshot",
    "A2AInvocationStatus",
    "A2ASnapshotSource",
    "RemoteAgentSpec",
]
