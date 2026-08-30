# -*- coding: utf-8 -*-
"""``AgentKernelStore`` port：durable Inbox / Run / ActivationLease 状态（Phase 1 Task 3）。

所有 mutation 都接受 ``expected_fence: int`` 并与当前 activation lease 的
fencing token 做事务内 CAS 比较；不匹配抛 :class:`StaleFenceError`。
accept/claim/complete/run-transition 会同步向 SessionEventStore 追加对应的
``ControlEvent/v1``（family=control, family_version=1）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from uuid import uuid4

from ksadk.kernel.contracts import (
    ActivationLease,
    AgentControlCommand,
    AgentControlReceipt,
    SessionEventEnvelope,
)
from ksadk.kernel.state import InboxState, RunState


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def command_digest(command: AgentControlCommand) -> str:
    """Stable digest of the caller's idempotency domain.

    Server admission deliberately issues a fresh permit for every network
    attempt.  ``authorization_ref`` and the interaction ``token_ref`` therefore
    authenticate an attempt, but are not part of the business mutation.  They
    must be verified before this digest is consulted, then excluded here so a
    legitimate retry can resolve to the original receipt.
    """

    canonical = command.model_dump(mode="json")
    canonical.pop("command_id", None)
    canonical.pop("submitted_at", None)
    canonical.pop("authorization_ref", None)
    # ``source.kind`` identifies the ingress semantics; ``source.ref`` is the
    # Server HTTP request id and therefore changes on every transport retry.
    source = dict(canonical.get("source") or {})
    source.pop("ref", None)
    canonical["source"] = source

    payload = dict(canonical.get("payload") or {})
    if command.command_type == "submit_interaction":
        payload.pop("token_ref", None)
    canonical["payload"] = payload

    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ActivationLeaseRequest:
    agent_instance_id: str
    session_id: str
    activation_id: str
    runtime_type: str = "ksadk"
    bundle_digest: str = ""
    capability_digest: str = ""
    lease_ttl_seconds: float = 30.0


class InboxMessage:
    """一条已进入 durable Inbox 的 control command。"""

    def __init__(
        self,
        *,
        message_id: str,
        agent_instance_id: str,
        session_id: str,
        idempotency_key: str,
        request_digest: str,
        accepted_seq: int,
        status: InboxState,
        claimed_fence: int | None = None,
        command: AgentControlCommand | None = None,
    ) -> None:
        self.message_id = message_id
        self.agent_instance_id = agent_instance_id
        self.session_id = session_id
        self.idempotency_key = idempotency_key
        self.request_digest = request_digest
        self.accepted_seq = accepted_seq
        self.status = status
        self.claimed_fence = claimed_fence
        self.command = command


class RunRecord:
    """一个 Run 的 durable 状态行。"""

    def __init__(
        self,
        *,
        run_id: str,
        agent_instance_id: str,
        session_id: str,
        state: RunState,
        activation_fence: int = 0,
        created_at: str | None = None,
        updated_at: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent_instance_id = agent_instance_id
        self.session_id = session_id
        self.state = RunState(state)
        self.activation_fence = int(activation_fence)
        self.created_at = created_at
        self.updated_at = updated_at
        self.metadata = dict(metadata or {})

    def model_copy(self, *, update: dict) -> "RunRecord":
        clone = RunRecord(
            run_id=self.run_id,
            agent_instance_id=self.agent_instance_id,
            session_id=self.session_id,
            state=self.state,
            activation_fence=self.activation_fence,
            created_at=self.created_at,
            updated_at=self.updated_at,
            metadata=self.metadata,
        )
        for key, value in update.items():
            if key == "state":
                clone.state = RunState(value)
            elif hasattr(clone, key):
                setattr(clone, key, value)
            else:
                clone.metadata[key] = value
        return clone


def new_message_id() -> str:
    return str(uuid4())


def control_event(
    *,
    session_id: str,
    event_type: str,
    payload: dict,
    run_id: str | None = None,
    actor_ref: str = "agent-kernel",
    causation_id: str | None = None,
) -> SessionEventEnvelope:
    """构造 family=control / family_version=1 的 kernel fact。"""

    return SessionEventEnvelope(
        event_id=uuid4(),
        session_id=session_id,
        seq=0,  # 由 SessionEventStore 在持久化后分配
        timestamp=now_iso(),
        family="control",
        family_version=1,
        event_type=event_type,
        payload=payload,
        run_id=run_id,
        causation_id=causation_id,
        actor_ref=actor_ref,
    )


@runtime_checkable
class AgentKernelStore(Protocol):
    """Durable Inbox / Run / Lease port。PostgreSQL 实现在 Task 4。"""

    async def accept_command(
        self, command: AgentControlCommand, *, queue_limit: int
    ) -> AgentControlReceipt: ...

    async def claim_next(
        self, agent_instance_id: str, session_id: str, fencing_token: int
    ) -> InboxMessage | None: ...

    async def complete_claim(self, message_id: str, *, expected_fence: int) -> None: ...

    async def acquire_activation(self, request: ActivationLeaseRequest) -> ActivationLease: ...

    async def renew_activation(
        self, activation_id: str, *, expected_fence: int, lease_ttl_seconds: float
    ) -> ActivationLease: ...

    async def release_activation(self, activation_id: str, *, expected_fence: int) -> None: ...

    async def append_event(
        self,
        envelope: SessionEventEnvelope,
        *,
        expected_fence: int,
        agent_instance_id: str | None = None,
    ) -> SessionEventEnvelope: ...

    async def load_run(self, run_id: str) -> RunRecord | None: ...

    async def save_run_transition(
        self, run: RunRecord, *, expected_fence: int
    ) -> RunRecord: ...

    async def load_message(self, message_id: str) -> InboxMessage | None: ...


__all__ = [
    "AgentKernelStore",
    "ActivationLeaseRequest",
    "InboxMessage",
    "RunRecord",
    "command_digest",
    "control_event",
    "new_message_id",
    "now_iso",
    "now_utc",
]
