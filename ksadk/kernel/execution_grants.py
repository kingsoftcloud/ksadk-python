"""Revocable execution admission, independent of any scheduling business domain.

The host authorizes grant management. A grant is scoped to an exact tenant,
AgentInstance and session; its owner is a trusted host reference. Grant state
and Inbox claim MUST be serialized by the same control-store transaction.
Claim is the start-qualification boundary, not evidence of a running model.
Revocation never revives a command and does not cancel already qualified work.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from ksadk.kernel.contracts import AgentControlCommand
from ksadk.kernel.errors import InvalidCommandError

GrantState = Literal["active", "suspended", "revoked"]


class ExecutionGrantSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    grant_id: str = Field(min_length=1, max_length=512)
    tenant_id: str = Field(min_length=1)
    agent_instance_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    owner_ref: str = Field(min_length=1)


class ExecutionGrantRecord(ExecutionGrantSpec):
    state: GrantState = "active"
    revision: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str


class ExecutionGrantCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    command_id: str
    idempotency_key: str
    inbox_state: str
    run_id: str | None = None
    run_state: str | None = None


class ExecutionGrantBarrier(BaseModel):
    model_config = ConfigDict(frozen=True)

    grant: ExecutionGrantRecord
    queued_message_ids: tuple[str, ...] = ()
    in_flight_message_ids: tuple[str, ...] = ()
    discarded_message_ids: tuple[str, ...] = ()
    settled_message_ids: tuple[str, ...] = ()
    commands: tuple[ExecutionGrantCommand, ...] = ()


class ExecutionGrantBlocked(InvalidCommandError):
    """A listed command lost eligibility before the transactional claim."""

    def __init__(self, state: str, *, message_id: str = "") -> None:
        self.grant_state = state
        self.message_id = message_id
        super().__init__(
            f"execution grant is {state}",
            details={"reason": f"execution_grant_{state}", "message_id": message_id},
        )


def execution_grant_id(command: AgentControlCommand) -> str | None:
    value = command.payload.get("execution_grant_id")
    if value is None:
        return None
    if command.command_type != "enqueue" or not isinstance(value, str) or not value.strip():
        raise InvalidCommandError("execution_grant_id requires enqueue and a nonempty string")
    if value != value.strip() or len(value) > 512:
        raise InvalidCommandError("invalid execution_grant_id")
    return value


def execution_grant_run_id(command: AgentControlCommand) -> str:
    """Stable lookup identity; never use a new Run to replay uncertain starts."""
    return str(
        uuid5(
            NAMESPACE_URL,
            "ksadk:execution-grant:"
            + json.dumps(
                [
                    command.tenant_id,
                    command.agent_instance_id,
                    command.session_id,
                    str(command.command_id),
                ],
                separators=(",", ":"),
            ),
        )
    )


def require_grant_scope(record: ExecutionGrantRecord, spec: ExecutionGrantSpec) -> None:
    if any(getattr(record, key) != value for key, value in spec.model_dump().items()):
        raise InvalidCommandError("execution grant scope or owner mismatch")


def command_grant_error(
    command: AgentControlCommand,
    record: ExecutionGrantRecord | None,
) -> str | None:
    if record is None:
        return "execution_grant_not_found"
    if any(
        getattr(record, key) != getattr(command, key)
        for key in (
            "tenant_id",
            "agent_instance_id",
            "session_id",
        )
    ):
        return "execution_grant_scope_mismatch"
    if record.state == "revoked":
        return "execution_grant_revoked"
    return None


def grant_operation_digest(
    spec: ExecutionGrantSpec, state: GrantState, expected_revision: int
) -> str:
    if state not in {"active", "suspended", "revoked"}:
        raise InvalidCommandError("unknown execution grant state")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise InvalidCommandError("expected_revision must be a positive integer")
    return hashlib.sha256(
        json.dumps(
            {
                "spec": spec.model_dump(),
                "state": state,
                "expected_revision": expected_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def transition_grant(
    record: ExecutionGrantRecord,
    state: GrantState,
    expected_revision: int,
    now: str,
) -> ExecutionGrantRecord:
    if record.revision != expected_revision:
        raise InvalidCommandError(
            "execution grant revision mismatch",
            details={
                "expected_revision": expected_revision,
                "actual_revision": record.revision,
            },
        )
    if record.state == "revoked" and state != "revoked":
        raise InvalidCommandError("revoked execution grant cannot be reactivated")
    return record.model_copy(
        update={
            "state": state,
            "revision": record.revision + 1,
            "updated_at": now,
        }
    )


def make_grant_barrier(
    record: ExecutionGrantRecord,
    messages: list[Any],
    runs: list[Any],
) -> ExecutionGrantBarrier:
    from ksadk.kernel.state import TERMINAL_RUN_STATES

    runs_by_command = {str(run.metadata.get("command_id")): run for run in runs}
    queued, inflight, discarded, settled, commands = [], [], [], [], []
    for message in messages:
        command = message.command
        if command is None or execution_grant_id(command) != record.grant_id:
            continue
        run = runs_by_command.get(str(command.command_id))
        status = str(message.status)
        commands.append(
            ExecutionGrantCommand(
                message_id=message.message_id,
                command_id=str(command.command_id),
                idempotency_key=command.idempotency_key,
                inbox_state=status,
                run_id=run.run_id if run else None,
                run_state=str(run.state) if run else None,
            )
        )
        if status == "accepted":
            queued.append(message.message_id)
        elif status == "discarded":
            discarded.append(message.message_id)
        elif run is not None and run.state in TERMINAL_RUN_STATES:
            settled.append(message.message_id)
        else:
            # Completed Inbox means adapter.start returned, not model completion.
            inflight.append(message.message_id)
    return ExecutionGrantBarrier(
        grant=record,
        queued_message_ids=tuple(queued),
        in_flight_message_ids=tuple(inflight),
        discarded_message_ids=tuple(discarded),
        settled_message_ids=tuple(settled),
        commands=tuple(commands),
    )
