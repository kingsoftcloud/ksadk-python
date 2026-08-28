"""Platform-owned Sandbox lease contract for Harness fencing.

The SDK intentionally defines only the contract.  The authoritative lease
store belongs to the runtime control plane so ownership is shared across
hosts; an in-process or local SQLite lock must not be presented as a cloud
fencing guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SandboxLeaseConflict(RuntimeError):
    """The caller no longer owns the current Sandbox fencing token."""


@dataclass(frozen=True)
class SandboxLeaseScope:
    """Tenant/workspace namespace for one authoritative lease."""

    tenant_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.workspace_id.strip():
            raise ValueError("Sandbox Lease Scope 缺少 tenant/workspace")


@dataclass(frozen=True)
class SandboxLeaseGrant:
    """One time-bounded ownership grant returned by the control plane."""

    backend_id: str
    handle_id: str
    scope: SandboxLeaseScope
    owner_id: str
    fencing_token: int
    expires_at: float


class SandboxLeaseProvider(Protocol):
    """Authoritative, cross-process lease provider.

    ``acquire`` must isolate tenant/workspace and monotonically increase
    ``fencing_token`` whenever ownership changes within that scope. ``renew``
    and ``release`` must reject stale owner/token/scope tuples with
    :class:`SandboxLeaseConflict`.
    """

    async def acquire(
        self,
        *,
        backend_id: str,
        handle_id: str,
        scope: SandboxLeaseScope,
        owner_id: str,
        ttl_seconds: float,
    ) -> SandboxLeaseGrant: ...

    async def renew(
        self,
        grant: SandboxLeaseGrant,
        *,
        ttl_seconds: float,
    ) -> SandboxLeaseGrant: ...

    async def release(self, grant: SandboxLeaseGrant) -> None: ...


__all__ = [
    "SandboxLeaseConflict",
    "SandboxLeaseGrant",
    "SandboxLeaseProvider",
    "SandboxLeaseScope",
]
