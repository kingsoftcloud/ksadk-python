"""Private completed-turn handoff, distinct from interrupted invocation recovery."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk.a2a.invocation_types import RemoteAgentSpec
from ksadk.codex.rollout_checkpoint import NativeRolloutCheckpoint
from ksadk.events.canonical import RunCompleted
from ksadk.kernel.state import RunState
from ksadk.runtime.adapter import RunHandle, StartRequest
from ksadk.runtime.executor import handle_digest


class CodexSessionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["codex.a2a.session/v1"] = Field(alias="schema")
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    agent_instance_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    start_request: StartRequest
    bindings: dict[str, RemoteAgentSpec]
    rollout: NativeRolloutCheckpoint
    terminal_event: RunCompleted

    @model_validator(mode="after")
    def validate_completed_scope(self):
        if (
            self.rollout.thread_id != self.thread_id
            or self.start_request.session_id != self.session_id
        ):
            raise ValueError("A2A_CODEX_SESSION_SCOPE_CONFLICT")
        if not self.bindings or any(
            name != spec.local_name for name, spec in self.bindings.items()
        ):
            raise ValueError("A2A_CODEX_SESSION_BINDING_CONFLICT")
        self.rollout.require_completed_turn(self.turn_id)
        event = self.terminal_event
        if (
            event.run_id != self.run_id
            or event.source.framework != "codex"
            or event.source.native_run_id != self.turn_id
            or event.source.metadata.get("thread_id") != self.thread_id
        ):
            raise ValueError("A2A_CODEX_SESSION_TERMINAL_CONFLICT")
        return self


def validated_terminal_recovery_checkpoint(run, handle: RunHandle, *, experts):
    """Authorize only replay of an already captured terminal fact for this Run."""
    try:
        checkpoint = CodexSessionCheckpoint.model_validate(run.metadata.get("runtime_checkpoint"))
        saved = RunHandle.model_validate(run.metadata.get("handle"))
        request = checkpoint.start_request
        if (
            run.state != RunState.RUNNING
            or run.run_id != checkpoint.run_id
            or run.session_id != checkpoint.session_id
            or run.agent_instance_id != checkpoint.agent_instance_id
            or run.metadata.get("tenant_id") != checkpoint.tenant_id
            or saved != handle
            or handle_digest(saved) != run.metadata.get("handle_digest")
            or handle.runtime_type != "codex"
            or handle.session_id != run.session_id
            or handle.run_id != checkpoint.thread_id
            or handle.native_ref.get("thread_id") != checkpoint.thread_id
            or handle.native_ref.get("user_id") != request.user_id
            or request.metadata.get("run_id") != run.run_id
            or not request.metadata.get("command_id")
            or request.metadata.get("command_id") != run.metadata.get("command_id")
            or (
                request.metadata.get("invocation_id")
                and request.metadata["invocation_id"] != run.run_id
            )
            or checkpoint.bindings != {name: expert.spec for name, expert in experts.items()}
        ):
            raise ValueError("scope mismatch")
        return checkpoint
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A2A_CODEX_TERMINAL_CHECKPOINT_INVALID") from None


async def terminal_recovery_event(checkpoint):
    from ksadk.events.canonical import ItemCompleted
    from ksadk.events.run_authority import current_run_event_authority

    authority = current_run_event_authority()
    if authority is None:
        raise RuntimeError("A2A_KERNEL_AUTHORITY_REQUIRED")
    identity = authority.runtime_checkpoint_identity()
    if (
        authority.run_id != checkpoint.run_id
        or authority.session_id != checkpoint.session_id
        or identity["tenant_id"] != checkpoint.tenant_id
        or identity["agent_instance_id"] != checkpoint.agent_instance_id
    ):
        raise ValueError("A2A_CODEX_TERMINAL_AUTHORITY_CONFLICT")
    prior = await authority.list(checkpoint.session_id)
    items = {(event.scope_id, event.item_id) for event in prior if isinstance(event, ItemCompleted)}
    if any(
        (ref.scope_id, ref.item_id) not in items for ref in checkpoint.terminal_event.output_refs
    ):
        raise ValueError("A2A_CODEX_TERMINAL_OUTPUT_NOT_DURABLE")
    return checkpoint.terminal_event


def validated_session_checkpoint(
    run,
    handle: RunHandle,
    request: StartRequest,
    *,
    agent_instance_id: str,
    tenant_id: str,
    experts,
):
    """Validate a Kernel-loaded predecessor against the current trusted owner.

    The caller supplies the canonical predecessor, never a caller-uploaded run.
    This performs no I/O and never replays an outbound invocation.
    """
    try:
        checkpoint = CodexSessionCheckpoint.model_validate(run.metadata.get("runtime_checkpoint"))
        saved = RunHandle.model_validate(run.metadata.get("handle"))
        previous = checkpoint.start_request
        if (
            run.state != RunState.COMPLETED
            or run.run_id != checkpoint.run_id
            or run.agent_instance_id != checkpoint.agent_instance_id
            or run.agent_instance_id != agent_instance_id
            or run.metadata.get("tenant_id") != checkpoint.tenant_id
            or checkpoint.tenant_id != tenant_id
            or run.session_id != checkpoint.session_id
            or request.session_id != checkpoint.session_id
            or not previous.user_id
            or request.user_id != previous.user_id
            or saved != handle
            or handle_digest(saved) != run.metadata.get("handle_digest")
            or handle.runtime_type != "codex"
            or handle.session_id != run.session_id
            or handle.run_id != checkpoint.thread_id
            or handle.native_ref.get("thread_id") != checkpoint.thread_id
            or handle.native_ref.get("user_id") != previous.user_id
            or request.metadata.get("thread_id") != checkpoint.thread_id
            or previous.metadata.get("run_id") != run.run_id
            or not previous.metadata.get("command_id")
            or previous.metadata.get("command_id") != run.metadata.get("command_id")
            or (
                previous.metadata.get("invocation_id")
                and previous.metadata["invocation_id"] != run.run_id
            )
            or not request.metadata.get("run_id")
            or request.metadata["run_id"] == run.run_id
            or checkpoint.bindings != {name: expert.spec for name, expert in experts.items()}
        ):
            raise ValueError("scope mismatch")
        return checkpoint
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A2A_CODEX_SESSION_CHECKPOINT_INVALID") from None
