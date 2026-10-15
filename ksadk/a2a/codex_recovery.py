"""Validate private recovery state before native restore or remote access."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk.a2a.codex import CodexToolCall
from ksadk.a2a.invocation_types import RemoteAgentSpec
from ksadk.codex.rollout_checkpoint import NativeRolloutCheckpoint
from ksadk.kernel.state import RunState
from ksadk.runtime.adapter import RunHandle, StartRequest
from ksadk.runtime.executor import handle_digest


class CodexRecoveryCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["codex.a2a.native/v1"] = Field(alias="schema")
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    agent_instance_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    start_request: StartRequest
    bindings: dict[str, RemoteAgentSpec]
    calls: tuple[CodexToolCall, ...] = Field(min_length=1)
    rollout: NativeRolloutCheckpoint
    continuation_started: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_native_scope(self):
        if (
            self.rollout.thread_id != self.thread_id
            or self.start_request.session_id != self.session_id
        ):
            raise ValueError("A2A_CODEX_RECOVERY_SCOPE_CONFLICT")
        identities = set()
        for name, spec in self.bindings.items():
            if name != spec.local_name:
                raise ValueError("A2A_CODEX_RECOVERY_BINDING_CONFLICT")
        for call in self.calls:
            key = (call.thread_id, call.turn_id, call.call_id)
            if key in identities:
                raise ValueError("A2A_CODEX_RECOVERY_DUPLICATE_CALL")
            identities.add(key)
            if (
                call.thread_id != self.thread_id
                or call.tool not in self.bindings
                or set(call.arguments) != {"message"}
                or not isinstance(call.arguments["message"], str)
                or not call.arguments["message"].strip()
            ):
                raise ValueError("A2A_CODEX_RECOVERY_CALL_CONFLICT")
            self.rollout.require_call(
                turn_id=call.turn_id, call_id=call.call_id, tool=call.tool, arguments=call.arguments
            )
        return self


def validated_recovery_checkpoint(run, handle: RunHandle, *, experts):
    """No I/O. A valid digest alone is not enough to authorize another scope."""
    try:
        saved_handle = RunHandle.model_validate(run.metadata.get("handle"))
        checkpoint = CodexRecoveryCheckpoint.model_validate(run.metadata.get("runtime_checkpoint"))
        if (
            saved_handle != handle
            or handle_digest(saved_handle) != run.metadata.get("handle_digest")
            or handle.runtime_type != "codex"
            or handle.run_id != checkpoint.thread_id
            or handle.native_ref.get("thread_id") != checkpoint.thread_id
            or handle.session_id != run.session_id
            or handle.native_ref.get("user_id") != checkpoint.start_request.user_id
            or run.run_id != checkpoint.run_id
            or run.session_id != checkpoint.session_id
            or run.agent_instance_id != checkpoint.agent_instance_id
            or run.metadata.get("tenant_id") != checkpoint.tenant_id
            or run.state not in {RunState.RUNNING, RunState.WAITING, RunState.PAUSED}
        ):
            raise ValueError("scope mismatch")
        metadata = checkpoint.start_request.metadata
        if (
            metadata.get("run_id") != run.run_id
            or not isinstance(metadata.get("command_id"), str)
            or not metadata["command_id"]
            or metadata.get("command_id") != run.metadata.get("command_id")
            or (metadata.get("invocation_id") and metadata["invocation_id"] != run.run_id)
        ):
            raise ValueError("start identity mismatch")
        expected = {name: expert.spec for name, expert in experts.items()}
        if checkpoint.bindings != expected:
            raise ValueError("frozen bindings changed")
        return checkpoint
    except (ValueError, TypeError, AttributeError):
        # Never expose serialized native instructions or private metadata through
        # a public run failure. Callers cannot restore on this error.
        raise ValueError("A2A_CODEX_RECOVERY_CHECKPOINT_INVALID") from None
