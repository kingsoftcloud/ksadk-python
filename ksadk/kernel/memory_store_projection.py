"""Project in-memory kernel records into public storage contracts."""

from __future__ import annotations

from typing import Any

from ksadk.kernel.contracts import (
    ActivationLease,
    AgentControlCommand,
    AgentControlReceipt,
    ControlError,
)
from ksadk.kernel.state import InboxState
from ksadk.kernel.store import ActivationLeaseRequest, InboxMessage


class _MemoryStoreProjectionMixin:
    @staticmethod
    def _receipt(
        command: AgentControlCommand,
        status: str,
        *,
        message_id: str | None = None,
        accepted_seq: int | None = None,
        error: ControlError | None = None,
    ) -> AgentControlReceipt:
        return AgentControlReceipt(
            command_id=command.command_id,
            status=status,  # type: ignore[arg-type]
            message_id=message_id,
            accepted_seq=accepted_seq,
            error=error,
        )

    @staticmethod
    def _to_message(row: dict[str, Any]) -> InboxMessage:
        return InboxMessage(
            message_id=row["message_id"],
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            accepted_seq=row["accepted_seq"],
            status=InboxState(row["status"]),
            claimed_fence=row["claimed_fence"],
            command=row.get("command"),
        )

    @staticmethod
    def _lease(request: ActivationLeaseRequest, row: dict[str, Any]) -> ActivationLease:
        return ActivationLease(
            agent_instance_id=request.agent_instance_id,
            activation_id=row["activation_id"],
            fencing_token=row["fencing_token"],
            lease_expires_at=row["lease_expires_at_iso"],
            bundle_digest=row["bundle_digest"],
            runtime_type=row["runtime_type"],
            capability_digest=row["capability_digest"],
        )

    @staticmethod
    def _activation_key(row: dict[str, Any]) -> tuple[str, str]:
        return (row["agent_instance_id"], row["session_id"])
