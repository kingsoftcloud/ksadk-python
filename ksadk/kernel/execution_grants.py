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
import math
import time
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    attempt_epoch: int | None = Field(default=None, ge=1, strict=True)


class ExecutionGrantRecord(ExecutionGrantSpec):
    expires_at: str | None = None
    state: GrantState = "active"
    revision: int = Field(default=1, ge=1)
    # Claim admission has an independent revision: pausing a queue must not
    # invalidate the grant or permits of a command already executing.
    admission_allowed: bool = True
    admission_revision: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str

    @field_validator("expires_at")
    @classmethod
    def valid_expiry(cls, value: str | None) -> str | None:
        return normalize_grant_time(value) if value is not None else None

    @model_validator(mode="after")
    def expiry_requires_epoch(self):
        if (self.expires_at is None) != (self.attempt_epoch is None):
            raise ValueError("execution grant expiry and attempt_epoch must be specified together")
        return self


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
        if command.payload.get("execution_grant_attempt_epoch") is not None:
            raise InvalidCommandError("execution_grant_attempt_epoch requires execution_grant_id")
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
    *,
    now: str | None = None,
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
    epoch = command.payload.get("execution_grant_attempt_epoch")
    if epoch != record.attempt_epoch or (epoch is not None and type(epoch) is not int):
        return "execution_grant_attempt_epoch_mismatch"
    if grant_expired(record, now=now):
        return "execution_grant_expired"
    return None


def normalize_grant_time(value: str) -> str:
    """Require an absolute timezone-aware deadline, never a restart-relative TTL."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("grant time must be an ISO 8601 timestamp with timezone") from error
    if parsed.tzinfo is None:
        raise ValueError("grant time must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def grant_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def grant_expired(record: ExecutionGrantRecord, *, now: str | None = None) -> bool:
    return (
        record.expires_at is not None
        and normalize_grant_time(now or grant_now()) >= record.expires_at
    )


def require_active_grant(record: ExecutionGrantRecord, *, now: str | None = None) -> None:
    if record.state != "active":
        raise ExecutionGrantBlocked(record.state)
    if grant_expired(record, now=now):
        raise ExecutionGrantBlocked("expired")


def grant_renewal_digest(
    spec: ExecutionGrantSpec,
    *,
    expected_revision: int,
    expires_at: str,
    renewal_id: str,
) -> str:
    if not isinstance(renewal_id, str) or not renewal_id.strip():
        raise InvalidCommandError("grant renewal requires renewal_id")
    # Reuse strict revision validation without conflating renewal and state mutations.
    grant_operation_digest(spec, "active", expected_revision)
    return hashlib.sha256(
        json.dumps(
            {
                "operation": "renew",
                "spec": spec.model_dump(),
                "expected_revision": expected_revision,
                "expires_at": normalize_grant_time(expires_at),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def admission_operation_digest(
    spec: ExecutionGrantSpec, allowed: bool, expected_revision: int
) -> str:
    if type(allowed) is not bool:
        raise InvalidCommandError("execution admission requires a boolean")
    grant_operation_digest(spec, "active", expected_revision)
    return hashlib.sha256(
        json.dumps(
            {
                "operation": "admission",
                "spec": spec.model_dump(),
                "allowed": allowed,
                "expected_revision": expected_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def transition_admission(
    record: ExecutionGrantRecord, allowed: bool, expected_revision: int, now: str
) -> ExecutionGrantRecord:
    if record.admission_revision != expected_revision:
        raise InvalidCommandError("execution admission revision conflict")
    require_active_grant(record, now=now)
    return record.model_copy(
        update={
            "admission_allowed": allowed,
            "admission_revision": record.admission_revision + 1,
            "updated_at": now,
        }
    )


def renew_grant(
    record: ExecutionGrantRecord,
    *,
    expected_revision: int,
    expires_at: str,
    now: str,
) -> ExecutionGrantRecord:
    require_active_grant(record, now=now)
    if record.expires_at is None:
        raise InvalidCommandError("legacy execution grant cannot be converted by renewal")
    expires_at = normalize_grant_time(expires_at)
    if expires_at <= record.expires_at or expires_at <= normalize_grant_time(now):
        raise InvalidCommandError("grant renewal must extend the existing deadline")
    return transition_grant(record, "active", expected_revision, now).model_copy(
        update={"expires_at": expires_at}
    )


class LocalExecutionGrantClock:
    """Conservative local deadline; persisted expiry alone cannot survive clock rollback.

    A process restart deliberately loses the monotonic anchor. A fresh trusted
    host renewal must restore it before claim/tools; reading/ensuring a durable
    record and replaying an old renewal receipt must never restore authority.
    """

    def _anchor_grant(
        self,
        record: ExecutionGrantRecord,
        *,
        observed_deadline: float | None = None,
    ) -> None:
        if record.expires_at is None:
            return
        if not hasattr(self, "_grant_deadlines"):
            self._grant_deadlines = {}
        remaining = (
            datetime.fromisoformat(record.expires_at) - datetime.fromisoformat(grant_now())
        ).total_seconds()
        deadline = grant_monotonic() + max(0, remaining)
        if observed_deadline is not None:
            deadline = min(deadline, observed_deadline)
        self._grant_deadlines[record.grant_id] = (record.expires_at, deadline)

    def _local_grant_error(self, record: ExecutionGrantRecord | None) -> str | None:
        if record is None or record.expires_at is None:
            return None
        anchor = getattr(self, "_grant_deadlines", {}).get(record.grant_id)
        if anchor is None or anchor[0] != record.expires_at:
            return "execution_grant_clock_unverified"
        if grant_monotonic() >= anchor[1]:
            return "execution_grant_expired"
        return None

    def _require_local_grant(self, record: ExecutionGrantRecord) -> None:
        require_active_grant(record)
        error = self._local_grant_error(record)
        if error:
            raise ExecutionGrantBlocked(error.removeprefix("execution_grant_"))

    def _require_local_renewal(self, record: ExecutionGrantRecord) -> None:
        # A newly authorized renewal may establish a missing restart anchor;
        # a known elapsed monotonic deadline must never revive on clock rollback.
        if self._local_grant_error(record) == "execution_grant_expired":
            raise ExecutionGrantBlocked("expired")


def grant_monotonic() -> float:
    return time.monotonic()


def grant_deadline_budget(remaining_ttl_seconds: float | None) -> float | None:
    """Anchor a trusted host's conservative remaining lifetime before awaiting IO.

    Hosts derive this from signed server time, subtracting request elapsed time
    and a safety margin. It is transport context, not part of mutation identity.
    The local anchor also caps it by absolute expiry; it can only expire earlier.
    """
    if remaining_ttl_seconds is None:
        return None
    if (
        type(remaining_ttl_seconds) not in (int, float)
        or not math.isfinite(remaining_ttl_seconds)
        or remaining_ttl_seconds < 0
    ):
        raise InvalidCommandError("remaining_ttl_seconds must be finite and nonnegative")
    return grant_monotonic() + remaining_ttl_seconds


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
                "spec": spec.model_dump(exclude_none=True),
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
