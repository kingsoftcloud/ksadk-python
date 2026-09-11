"""Versioned, provider-neutral execution ports granted to trusted plugins.

The host resolves bindings and authorizes every operation. Implementations must
not accept a model/browser-supplied PluginExecutionScope as authorization.
No collaboration or scheduling policy belongs to this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

EXECUTION_HOST_PROTOCOL = "ksadk.execution-host/v1"


@dataclass(frozen=True)
class PluginExecutionScope:
    plugin_id: str
    plugin_digest: str
    authority_ref: str
    tenant_id: str
    owner_subject: str
    binding_ref: str
    session_id: str


@dataclass(frozen=True)
class ExecutionReceipt:
    status: Literal["missing", "accepted", "duplicate", "rejected", "uncertain"]
    command_id: str | None = None
    message_id: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    reason: str | None = None
    output: str = ""
    tokens: int = 0
    source_event_id: str | None = None


@dataclass(frozen=True)
class ExecutionBarrier:
    state: Literal["active", "suspended", "revoked"]
    revision: int
    in_flight: tuple[str, ...] = ()
    queued: tuple[str, ...] = ()
    discarded: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


class PluginExecutionHost(Protocol):
    async def describe(self, scope: PluginExecutionScope) -> dict[str, Any]: ...

    async def ensure_session(self, scope: PluginExecutionScope) -> None: ...

    async def submit(
        self,
        scope: PluginExecutionScope,
        *,
        content: str,
        idempotency_key: str,
        grant_id: str,
        causation: str,
        policy_context: dict[str, Any],
    ) -> ExecutionReceipt: ...

    async def lookup(
        self, scope: PluginExecutionScope, idempotency_key: str
    ) -> ExecutionReceipt: ...

    async def set_grant(
        self,
        scope: PluginExecutionScope,
        grant_id: str,
        state: Literal["active", "suspended", "revoked"],
        idempotency_key: str,
    ) -> ExecutionBarrier: ...

    async def cancel(
        self, scope: PluginExecutionScope, run_id: str, idempotency_key: str
    ) -> ExecutionReceipt: ...

    async def interactions(self, scope: PluginExecutionScope) -> list[dict[str, Any]]: ...

    async def get_interaction(
        self, scope: PluginExecutionScope, *, run_id: str, interaction_id: str
    ) -> dict[str, Any] | None: ...

    async def submit_interaction(
        self,
        scope: PluginExecutionScope,
        *,
        run_id: str,
        interaction_id: str,
        expected_revision: int,
        action: str,
        response: dict[str, Any],
        idempotency_key: str,
        actor_subject: str,
    ) -> dict[str, Any]: ...
