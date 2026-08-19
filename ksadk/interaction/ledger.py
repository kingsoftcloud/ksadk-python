# -*- coding: utf-8 -*-
"""``InteractionLedger`` port 与跨后端共享的台账语义（Phase 1 Task 5）。

所有 mutation 都接受 :class:`~ksadk.kernel.contracts.ActivationWriteGuard`
并在 store 事务内与 activation lease 做 fence CAS；request 写 pending 行 +
``interaction.requested``；terminal（resolve/cancel/expire）做 revision CAS、
first-wins，并在**同一个 store 事务**内追加恰好一个 terminal SessionEvent
（family=interaction, family_version=1）。

公共事件 payload 是 interactionEvent 投影，省略
``provider_id`` / ``native_target`` / ``continuation_metadata``。
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable
from uuid import uuid4

from ksadk.interaction.contracts import (
    InteractionRecord,
    InteractionReceipt,
    InteractionSubmission,
    RESOLVE_OUTCOMES,
)
from ksadk.kernel.contracts import (
    ActivationWriteGuard,
    SessionEventEnvelope,
)

INTERACTION_FAMILY = "interaction"
INTERACTION_FAMILY_VERSION = 1

ALREADY_RESOLVED = "interaction_already_resolved"
REVISION_MISMATCH = "interaction_revision_mismatch"
REQUEST_CONFLICT = "interaction_request_conflict"


def request_digest(record: InteractionRecord) -> str:
    """幂等域摘要：排除 revision/status/created_at 等可变或时钟字段。"""

    canonical = json.dumps(
        record.model_dump(
            mode="json",
            exclude={"revision", "status", "created_at"},
        ),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def submission_digest(submission: InteractionSubmission) -> str:
    canonical = json.dumps(
        submission.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def interaction_event(
    record: InteractionRecord,
    *,
    event_type: str,
    timestamp: str,
    request: dict | None = None,
    outcome: str | None = None,
    response=None,
    actor_ref: str | None = None,
    reason: str | None = None,
) -> SessionEventEnvelope:
    """构造 family=interaction/v1 的公共 interactionEvent。

    payload 是 schema 的 interactionEvent 投影：不含 provider_id /
    native_target / continuation_metadata 等内部字段。
    """

    payload: dict = {
        "schema_version": 1,
        "event_type": event_type,
        "interaction_id": record.interaction_id,
        "tenant_id": record.tenant_id,
        "agent_instance_id": record.agent_instance_id,
        "session_id": record.session_id,
        "run_id": record.run_id,
        "kind": record.kind,
        "revision": record.revision,
        "timestamp": timestamp,
    }
    if request is not None:
        payload["request"] = request
    if outcome is not None:
        payload["outcome"] = outcome
    if response is not None:
        payload["response"] = response
    if actor_ref is not None:
        payload["actor_ref"] = actor_ref
    if reason is not None:
        payload["reason"] = reason
    return SessionEventEnvelope(
        event_id=uuid4(),
        session_id=record.session_id,
        seq=0,  # 由 SessionEventStore 在持久化后分配
        timestamp=timestamp,
        family=INTERACTION_FAMILY,
        family_version=INTERACTION_FAMILY_VERSION,
        event_type=event_type,
        payload=payload,
        run_id=record.run_id,
        actor_ref=actor_ref or "agent-kernel",
    )


def requested_event_payload(record: InteractionRecord, timestamp: str) -> SessionEventEnvelope:
    request = {
        "kind": record.kind,
        "request_schema": record.request_schema,
    }
    if record.expires_at is not None:
        request["expires_at"] = record.expires_at
    if record.presentation is not None:
        request["presentation"] = record.presentation.model_dump(mode="json")
    return interaction_event(
        record,
        event_type="interaction.requested",
        timestamp=timestamp,
        request=request,
    )


def resolve_outcome(action: str) -> str:
    try:
        return RESOLVE_OUTCOMES[action]
    except KeyError:  # pragma: no cover - schema 已约束 action 枚举
        raise ValueError(f"non-resolve action {action!r}") from None


@runtime_checkable
class InteractionLedger(Protocol):
    """Durable first-wins interaction 台账 port（AgentKernelStore 一致性域）。"""

    async def request(
        self, record: InteractionRecord, *, guard: ActivationWriteGuard
    ) -> InteractionRecord: ...

    async def resolve(
        self, submission: InteractionSubmission, *, guard: ActivationWriteGuard
    ) -> InteractionReceipt: ...

    async def cancel(
        self, interaction_id: str, expected_revision: int, *, guard: ActivationWriteGuard
    ) -> InteractionReceipt: ...

    async def expire(
        self, interaction_id: str, expected_revision: int, *, guard: ActivationWriteGuard
    ) -> InteractionReceipt: ...

    async def get(
        self,
        interaction_id: str,
        *,
        tenant_id: str | None = None,
        agent_instance_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> InteractionRecord | None: ...

    async def list_pending_interactions(
        self, tenant_id: str, session_id: str
    ) -> list[InteractionRecord]: ...


__all__ = [
    "ALREADY_RESOLVED",
    "INTERACTION_FAMILY",
    "INTERACTION_FAMILY_VERSION",
    "InteractionLedger",
    "REVISION_MISMATCH",
    "REQUEST_CONFLICT",
    "interaction_event",
    "request_digest",
    "requested_event_payload",
    "resolve_outcome",
    "submission_digest",
]
