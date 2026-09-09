"""Ephemeral broker handles minted only by the trusted runtime host.

This is not an upstream authorization database. The host must first verify
admission, connection identity and Build permissions. Restart loses all handles.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import model_validator

from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.contracts import Digest, Identifier, InvocationIdentity
from ksadk.resource_runtime.ipc import ResourceOperation


class ResourceScope(PluginContractModel):
    profile_digest: Digest
    generation_id: Identifier
    activation_id: Identifier
    build_digest: Digest
    binding_snapshot_digest: Digest
    identity: InvocationIdentity
    binding_id: Identifier
    allowed_operations: tuple[ResourceOperation, ...]

    @model_validator(mode="after")
    def unique_operations(self) -> ResourceScope:
        if not self.allowed_operations or len(self.allowed_operations) != len(
            set(self.allowed_operations)
        ):
            raise ValueError("Scope requires unique explicit operations")
        return self


@dataclass(frozen=True)
class ResourceLease:
    handle: str = field(repr=False)
    scope: ResourceScope
    expires_at: float


class LeaseRejected(PermissionError):
    def __init__(self):
        super().__init__("RESOURCE_SCOPE_INVALID")


class ResourceLeaseRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, capacity: int = 4096):
        if capacity < 1:
            raise ValueError("Lease capacity must be positive")
        self._clock = clock
        self._capacity = capacity
        self._leases: dict[str, ResourceLease] = {}
        self._lock = threading.RLock()

    def issue(self, scope: ResourceScope, *, lifetime: int = 600) -> ResourceLease:
        """Host-only entry point; never expose scope issuance as an IPC operation."""
        if type(lifetime) is not int or not 1 <= lifetime <= 600:
            raise ValueError("Resource lease lifetime must be 1..600 seconds")
        with self._lock:
            now = self._clock()
            self._leases = {
                key: lease for key, lease in self._leases.items() if lease.expires_at > now
            }
            if len(self._leases) >= self._capacity:
                raise RuntimeError("RESOURCE_LEASE_CAPACITY")
            # Round-trip validates even models constructed with unchecked model_copy.
            validated = ResourceScope.model_validate(scope.model_dump())
            for existing in self._leases.values():
                other = existing.scope
                if other.activation_id == validated.activation_id and (
                    other.identity != validated.identity
                    or other.generation_id != validated.generation_id
                    or other.build_digest != validated.build_digest
                    or other.profile_digest != validated.profile_digest
                    or other.binding_snapshot_digest != validated.binding_snapshot_digest
                ):
                    raise ValueError("An activation cannot change identity or frozen Build")
            lease = ResourceLease(secrets.token_urlsafe(32), validated, now + lifetime)
            self._leases[lease.handle] = lease
            return lease

    def authorize(
        self, handle: str, *, operation: ResourceOperation, generation_id: str, activation_id: str
    ) -> ResourceScope:
        with self._lock:
            lease = self._current(handle)
            if (
                lease.scope.generation_id != generation_id
                or lease.scope.activation_id != activation_id
                or operation not in lease.scope.allowed_operations
            ):
                raise LeaseRejected()
            return lease.scope

    def renew(self, handle: str, *, lifetime: int = 600) -> ResourceLease:
        """Host revalidates upstream authorization first; renewal cannot widen scope.

        Renewal rotates the opaque handle and invalidates the previous handle.
        Both actions are atomic relative to dispatch authorization and revocation.
        """
        return self.renew_many((handle,), lifetime=lifetime)[0]

    def renew_many(
        self, handles: tuple[str, ...], *, lifetime: int = 600
    ) -> tuple[ResourceLease, ...]:
        """Rotate one activation's handles atomically, after host revalidation."""
        with self._lock:
            if not handles or len(handles) != len(set(handles)):
                raise ValueError("Renewal requires unique existing handles")
            if type(lifetime) is not int or not 1 <= lifetime <= 600:
                raise ValueError("Resource lease lifetime must be 1..600 seconds")
            leases = tuple(self._current(handle) for handle in handles)
            if len({lease.scope.activation_id for lease in leases}) != 1:
                raise ValueError("Renewal cannot mix activations")
            now = self._clock()
            if any(lease.expires_at <= now for lease in leases):
                raise LeaseRejected()
            renewed = tuple(
                ResourceLease(secrets.token_urlsafe(32), lease.scope, now + lifetime)
                for lease in leases
            )
            for handle in handles:
                del self._leases[handle]
            self._leases.update({lease.handle: lease for lease in renewed})
            return renewed

    def resolve(
        self, handle: str, *, operation: ResourceOperation, generation_id: str
    ) -> ResourceScope:
        """Resolve caller identity from the opaque handle, not request arguments."""
        with self._lock:
            scope = self._current(handle).scope
            if scope.generation_id != generation_id or operation not in scope.allowed_operations:
                raise LeaseRejected()
            return scope

    def revoke_activation(self, activation_id: str) -> None:
        with self._lock:
            self._leases = {
                key: lease
                for key, lease in self._leases.items()
                if lease.scope.activation_id != activation_id
            }

    def remaining_lifetime(self, handles: tuple[str, ...]) -> float:
        """Observe the longest current lease for lifecycle cleanup, without renewal.

        Missing/revoked/expired handles contribute zero. Uses the issuance clock
        so a supervisor does not compare monotonic deadlines with wall time.
        This is not an authorization check for an operation.
        """
        with self._lock:
            now = self._clock()
            return max(0.0, max((
                self._leases[handle].expires_at - now
                for handle in handles if handle in self._leases
            ), default=0.0))

    def revoke_binding(self, activation_id: str, binding_id: str) -> None:
        """Revoke one upstream resource without disabling unrelated bindings."""
        with self._lock:
            self._leases = {
                key: lease
                for key, lease in self._leases.items()
                if (lease.scope.activation_id, lease.scope.binding_id)
                != (activation_id, binding_id)
            }

    def revoke_generation(self, generation_id: str) -> None:
        with self._lock:
            self._leases = {
                key: lease
                for key, lease in self._leases.items()
                if lease.scope.generation_id != generation_id
            }

    def _current(self, handle: str) -> ResourceLease:
        lease = self._leases.get(handle)
        if lease is None:
            raise LeaseRejected()
        if lease.expires_at <= self._clock():
            del self._leases[handle]
            raise LeaseRejected()
        return lease
