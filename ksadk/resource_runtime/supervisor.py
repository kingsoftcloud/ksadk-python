"""Generation-owned resource lifecycle for trusted Studio/runtime adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ksadk.resource_runtime.broker import ResourceBroker, ResourceWriteAuthorizer
from ksadk.resource_runtime.discovery_receipts import (
    DiscoverySelectionReceipts,
    discovery_scope_key,
)
from ksadk.resource_runtime.ipc import ResourceRequest
from ksadk.resource_runtime.leases import (
    LeaseRejected,
    ResourceLease,
    ResourceLeaseRegistry,
    ResourceScope,
)
from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.process import ResourceWorkerProcess
from ksadk.resource_runtime.socket_server import ResourceSocketServer
from ksadk.resource_runtime.worker import DiscoverySkillWorkerBinding, WorkerInitialization

ResourceRevalidator = Callable[[tuple[ResourceScope, ...]], Awaitable[bool]]


@dataclass(frozen=True)
class ActiveResources:
    activation_id: str
    socket_path: Path
    leases: tuple[ResourceLease, ...] = field(repr=False)
    worker_pid: int


@dataclass
class _Running:
    worker: ResourceWorkerProcess
    resources: ActiveResources
    lease_changed: asyncio.Event = field(default_factory=asyncio.Event)


class _ActivationApprovals:
    """Dispatch only to the approval owner admitted for this exact activation."""

    def __init__(self):
        self.owners: dict[str, tuple[tuple[ResourceScope, ...], ResourceWriteAuthorizer]] = {}

    def register(self, scopes: tuple[ResourceScope, ...], owner: ResourceWriteAuthorizer) -> None:
        activation_id = scopes[0].activation_id
        if activation_id in self.owners:
            raise ValueError("RESOURCE_APPROVAL_OWNER_IMMUTABLE")
        self.owners[activation_id] = (scopes, owner)

    def revoke(self, activation_id: str) -> None:
        self.owners.pop(activation_id, None)

    async def authorize(self, request: ResourceRequest, scope: ResourceScope) -> str | None:
        admitted = self.owners.get(scope.activation_id)
        if admitted is None or scope not in admitted[0]:
            return None
        event_id = await admitted[1].authorize(request, scope)
        # An approval may finish after the run was stopped. Do not revive it.
        return event_id if self.owners.get(scope.activation_id) is admitted else None


class ResourceSupervisor:
    """One owner per Core generation, with one worker per activation.

    Admission and real credential/principal verification precede activate().
    This class never derives user identity from tool arguments, reads credential
    environment variables, restarts failed activations or replays requests.
    """

    def __init__(
        self,
        generation_id: str,
        *,
        max_workers: int = 32,
        write_authorizer: ResourceWriteAuthorizer | None = None,
        operation_ledger: OperationLedger | None = None,
    ):
        if type(max_workers) is not int or not 1 <= max_workers <= 128:
            raise ValueError("Resource worker limit must be 1..128")
        self.generation_id = generation_id
        self._max_workers = max_workers
        if write_authorizer is not None and operation_ledger is None:
            raise ValueError("Write approval requires a durable operation ledger")
        self._default_write_authorizer = write_authorizer
        self._has_operation_ledger = operation_ledger is not None
        self._discovery_receipts = (
            DiscoverySelectionReceipts(operation_ledger) if operation_ledger is not None else None
        )
        self._approvals = _ActivationApprovals()
        self._registry = ResourceLeaseRegistry()
        self._broker = ResourceBroker(
            self._registry,
            generation_id,
            write_authorizer=self._approvals if operation_ledger is not None else None,
            operation_ledger=operation_ledger,
        )
        self._socket = ResourceSocketServer(self._broker)
        self._lock = asyncio.Lock()
        self._running: dict[str, _Running] = {}
        self._used: set[str] = set()
        self._monitors: set[asyncio.Task] = set()
        self._closed = False

    async def start_broker(self) -> Path:
        """Prepare private ingress before Core startup, without workers or grants."""
        async with self._lock:
            return await self._start_broker_locked()

    async def _start_broker_locked(self) -> Path:
        if self._closed:
            raise RuntimeError("RESOURCE_SUPERVISOR_CLOSED")
        if self._socket.path is None:
            try:
                await self._socket.start()
            except BaseException:
                self._closed = True
                raise
        assert self._socket.path is not None
        return self._socket.path

    async def activate(
        self,
        initialization: WorkerInitialization,
        *,
        write_authorizer: ResourceWriteAuthorizer | None = None,
    ) -> ActiveResources:
        # Revalidate even models assembled via unchecked model_copy/model_construct.
        initialization = WorkerInitialization.model_validate(initialization.pipe_payload())
        discovery_keys = {}
        restored_bindings = list(initialization.bindings)
        for index, binding in enumerate(initialization.bindings):
            if isinstance(binding, DiscoverySkillWorkerBinding):
                if self._discovery_receipts is None:
                    raise ValueError("RESOURCE_DISCOVERY_RECEIPTS_REQUIRED")
                binding_scope = next(
                    item for item in initialization.scopes
                    if item.binding_id == binding.config.binding.id
                )
                key = discovery_scope_key(binding_scope, binding.selection_run_ref)
                discovery_keys[binding_scope.binding_id] = key
                restored = await asyncio.to_thread(self._discovery_receipts.read, key)
                # Only host ledger contents may restore selections. Ignore caller
                # supplied preloads at this public lifecycle entry.
                restored_bindings[index] = binding.model_copy(
                    update={"restored_selections": restored}
                )
        initialization = initialization.model_copy(update={"bindings": tuple(restored_bindings)})
        scope = initialization.scopes[0]
        owner = write_authorizer if write_authorizer is not None else self._default_write_authorizer
        if owner is not None and not self._has_operation_ledger:
            raise ValueError("Write approval requires a durable operation ledger")
        if scope.generation_id != self.generation_id:
            raise ValueError("Resource activation belongs to another generation")
        async with self._lock:
            if self._closed:
                raise RuntimeError("RESOURCE_SUPERVISOR_CLOSED")
            if scope.activation_id in self._used:
                raise ValueError("RESOURCE_ACTIVATION_ALREADY_USED")
            if len(self._used) >= 4096:
                raise RuntimeError("RESOURCE_GENERATION_CAPACITY")
            if len(self._running) >= self._max_workers:
                raise RuntimeError("RESOURCE_WORKER_CAPACITY")
            await self._start_broker_locked()
            self._used.add(scope.activation_id)
            worker = None
            try:
                if owner is not None:
                    self._approvals.register(initialization.scopes, owner)
                worker = await ResourceWorkerProcess.start(initialization)
                leases = []
                for binding_scope in initialization.scopes:
                    binding = next(
                        item
                        for item in initialization.bindings
                        if item.config.binding.id == binding_scope.binding_id
                    )
                    self._broker.register(
                        binding_scope,
                        worker,
                        artifact_root=getattr(binding, "artifact_directory", None),
                        discovery_key=discovery_keys.get(binding_scope.binding_id),
                    )
                    leases.append(self._registry.issue(binding_scope))
                assert self._socket.path is not None
                resources = ActiveResources(
                    scope.activation_id, self._socket.path, tuple(leases), worker.pid
                )
                self._running[scope.activation_id] = _Running(worker, resources)
                monitor = asyncio.create_task(self._monitor(scope.activation_id, worker))
                self._monitors.add(monitor)
                monitor.add_done_callback(self._monitors.discard)
                return resources
            except BaseException:
                self._approvals.revoke(scope.activation_id)
                self._broker.revoke_activation(scope.activation_id)
                if worker is not None:
                    await worker.aclose()
                await self._broker.retire_activation(scope.activation_id)
                raise

    async def deactivate(self, activation_id: str) -> None:
        async with self._lock:
            await self._deactivate_locked(activation_id)

    async def owns(self, current: ActiveResources) -> bool:
        """Confirm an opaque activation handle still names this live generation."""

        async with self._lock:
            running = self._running.get(current.activation_id)
            return (
                not self._closed
                and running is not None
                and running.resources is current
                and running.worker.is_running
            )

    async def renew(
        self,
        current: ActiveResources,
        *,
        revalidate: ResourceRevalidator,
        lifetime: int = 600,
    ) -> ActiveResources:
        """Host-only renewal after fresh upstream permission checks.

        The caller replaces its connector with the returned handles at a safe
        turn boundary. No model operation, automatic timer, or default grant is
        installed here. The verifier must check every supplied binding scope.
        """
        if type(lifetime) is not int or not 1 <= lifetime <= 600:
            raise ValueError("Resource lease lifetime must be 1..600 seconds")
        async with self._lock:
            running = self._running.get(current.activation_id)
            if (
                self._closed
                or running is None
                or running.resources is not current
                or not running.worker.is_running
            ):
                raise ValueError("RESOURCE_RENEWAL_STALE")
            scopes = tuple(
                ResourceScope.model_validate(lease.scope.model_dump()) for lease in current.leases
            )
        # Do not block immediate deactivation while checking the platform.
        try:
            if await asyncio.wait_for(revalidate(scopes), timeout=15) is not True:
                raise PermissionError("RESOURCE_RENEWAL_DENIED")
        except BaseException:
            async with self._lock:
                latest = self._running.get(current.activation_id)
                if latest is running and latest.resources is current:
                    await self._deactivate_locked(current.activation_id)
            raise
        async with self._lock:
            if (
                self._closed
                or self._running.get(current.activation_id) is not running
                or running.resources is not current
                or not running.worker.is_running
            ):
                raise ValueError("RESOURCE_RENEWAL_STALE")
            try:
                leases = self._registry.renew_many(
                    tuple(lease.handle for lease in current.leases),
                    lifetime=lifetime,
                )
            except BaseException:
                await self._deactivate_locked(current.activation_id)
                raise
            renewed = ActiveResources(
                current.activation_id, current.socket_path, leases, current.worker_pid
            )
            running.resources = renewed
            running.lease_changed.set()
            return renewed

    async def runtime_status(self, *, agent_id: str, activation_id: str) -> dict | None:
        """Observe this live activation without issuing leases or probing upstream.

        Unknown and differently owned activations are indistinguishable. This is
        transport availability, not proof that current upstream grants are valid.
        """
        async with self._lock:
            running = self._running.get(activation_id)
            if running is None:
                return None
            scopes = [lease.scope for lease in running.resources.leases]
            if any(scope.identity.agent_id != agent_id for scope in scopes):
                return None
            items = []
            for lease in running.resources.leases:
                available = running.worker.is_running and not self._closed
                try:
                    self._registry.authorize(
                        lease.handle,
                        operation=lease.scope.allowed_operations[0],
                        generation_id=self.generation_id,
                        activation_id=activation_id,
                    )
                except LeaseRejected:
                    available = False
                items.append(
                    {
                        "bindingId": lease.scope.binding_id,
                        "runtimeState": "available" if available else "unavailable",
                        "allowedOperations": list(lease.scope.allowed_operations)
                        if available
                        else [],
                    }
                )
            first = scopes[0]
            return {
                "agentId": agent_id,
                "activationId": activation_id,
                "generationId": self.generation_id,
                "buildDigest": first.build_digest,
                "bindingSnapshotDigest": first.binding_snapshot_digest,
                "observedAt": datetime.now(timezone.utc).isoformat(),
                "upstreamVerification": "not-observed",
                "items": items,
            }

    async def _deactivate_locked(self, activation_id: str) -> None:
        self._approvals.revoke(activation_id)
        self._broker.revoke_activation(activation_id)
        running = self._running.pop(activation_id, None)
        if running is not None:
            await running.worker.aclose()
        await self._broker.retire_activation(activation_id)

    async def _monitor(self, activation_id: str, worker: ResourceWorkerProcess) -> None:
        stopped = asyncio.create_task(worker.wait_stopped())
        try:
            while True:
                async with self._lock:
                    running = self._running.get(activation_id)
                    if running is None or running.worker is not worker:
                        return
                    remaining = self._registry.remaining_lifetime(
                        tuple(lease.handle for lease in running.resources.leases)
                    )
                    if stopped.done() or remaining <= 0:
                        await self._deactivate_locked(activation_id)
                        return
                    # Read current leases and clear under the same lock as renew.
                    # An old expiry wakeup never revokes a successfully renewed run.
                    running.lease_changed.clear()
                changed = asyncio.create_task(running.lease_changed.wait())
                try:
                    # Also notice complete binding revocation without another model
                    # request. This timer cannot refresh credentials or issue leases.
                    await asyncio.wait(
                        (stopped, changed), timeout=min(remaining, 30.0),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
        finally:
            if not stopped.done():
                stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._socket.aclose()
            for activation_id in tuple(self._running):
                await self._deactivate_locked(activation_id)
        # Monitors may be waiting on the supervisor lock, so join after releasing it.
        if self._monitors:
            await asyncio.gather(*tuple(self._monitors), return_exceptions=True)
