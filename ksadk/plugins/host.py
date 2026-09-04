"""Transactional, side-effect-owning PluginHost foundation (P2-03A).

The host has no knowledge of AgentControl, SessionEvent sequencing, or cloud
deployment.  It only stages a fully resolved profile, atomically swaps it once
healthy, and disposes owned effects in reverse dependency order.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import (
    CompositionProfile,
    PluginInventory,
    PluginInventoryItem,
    PluginLockEntry,
    PluginManifest,
)
from ksadk.plugins.resolver import (
    PluginRegistry,
    PluginResolutionError,
    ResolvedComposition,
)


class PluginHostError(RuntimeError):
    """Stable reject/failure from PluginHost profile transactions."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class ManagedPlugin(Protocol):
    """Effects owned by one staged plugin instance."""

    async def start(self) -> None: ...

    async def health(self) -> bool: ...

    async def drain(self) -> None: ...

    async def dispose(self) -> None: ...


@runtime_checkable
class PluginFactory(Protocol):
    """Factory seam; `stage` must not expose partially started effects."""

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> ManagedPlugin: ...


@dataclass(frozen=True)
class PluginCapabilityBinding:
    """One locked capability and the active runtime that owns it."""

    plugin_id: str
    plugin_version: str
    definition: str
    slot: str
    runtime: ManagedPlugin


@dataclass(frozen=True)
class PluginExecutionContext:
    """Read-only capability view passed to the active AgentProvider."""

    profile_digest: str
    plugin_lock_digest: str
    bindings: tuple[PluginCapabilityBinding, ...]

    def all(
        self,
        definition: str,
        *,
        slot: str | None = None,
    ) -> tuple[PluginCapabilityBinding, ...]:
        return tuple(
            binding
            for binding in self.bindings
            if binding.definition == definition
            and (slot is None or binding.slot == slot)
        )

    def require(
        self,
        definition: str,
        *,
        slot: str | None = None,
    ) -> PluginCapabilityBinding:
        matches = self.all(definition, slot=slot)
        if not matches:
            suffix = f" in slot {slot!r}" if slot is not None else ""
            raise PluginHostError(
                "plugin_capability_unavailable",
                f"active profile does not provide {definition!r}{suffix}",
            )
        if len(matches) != 1:
            raise PluginHostError(
                "plugin_capability_ambiguous",
                f"active profile provides more than one {definition!r}; select a slot",
            )
        return matches[0]


@runtime_checkable
class PreparedAgent(ManagedPlugin, Protocol):
    """One provider-owned activation prepared from an immutable Bundle."""

    async def execute(self, request: Any) -> Any: ...


@runtime_checkable
class ExecutableAgentProvider(ManagedPlugin, Protocol):
    """AgentProvider execution seam used by the first PluginHost vertical.

    ``prepare`` must either return an unstarted activation or clean up all of
    its own partial effects before raising.  PluginHost owns start, health,
    drain, and dispose for every returned activation.
    """

    async def prepare(
        self,
        bundle: ResolvedPluginBundle,
        *,
        capabilities: PluginExecutionContext,
    ) -> PreparedAgent: ...


@dataclass(frozen=True)
class _ActivePlugin:
    entry: PluginLockEntry
    runtime: ManagedPlugin


@dataclass(frozen=True)
class _ActiveGraph:
    resolved: ResolvedComposition
    plugins: tuple[_ActivePlugin, ...]


@dataclass
class _ActiveActivation:
    key: str
    graph: _ActiveGraph
    bundle_digest: str
    runtime: PreparedAgent
    operation_lock: asyncio.Lock
    operation_task: asyncio.Task[Any] | None = None
    closed: bool = False


class PluginActivationSession:
    """Provider-owned activation retained across turns for one session.

    The handle is deliberately small: PluginHost still owns lifecycle and
    profile fencing, while the provider owns any native thread/checkpoint state
    inside ``PreparedAgent``.  Calls are serialized per activation because a
    conversational session is an ordered command stream.
    """

    def __init__(self, host: "PluginHost", active: _ActiveActivation) -> None:
        self._host = host
        self._active = active

    @property
    def key(self) -> str:
        return self._active.key

    @property
    def bundle_digest(self) -> str:
        return self._active.bundle_digest

    @property
    def closed(self) -> bool:
        return self._active.closed

    async def execute(self, request: Any) -> Any:
        return await self._host._execute_activation(self._active, request)

    async def runtime_adapter(self) -> Any:
        """Return an optional provider-owned RuntimeAdapter for AgentKernel.

        ``execute`` remains the minimum cross-provider ABI.  Providers that
        need durable AgentControl/SessionEvent ownership (for example local
        Scheduler Lite) may expose this additive seam.  PluginHost retains the
        activation/profile fence; unsupported providers fail explicitly.
        """

        return await self._host._runtime_adapter_activation(self._active)

    async def close(self) -> None:
        await self._host.close_activation(self._active.key, expected=self._active)


class PluginHost:
    """Apply resolved profiles without tearing down a healthy old graph first."""

    def __init__(
        self,
        registry: PluginRegistry,
        factories: Mapping[str, PluginFactory],
        *,
        allowed_permissions: frozenset[str] = frozenset(),
        services: Mapping[str, Any] | None = None,
    ) -> None:
        self._registry = registry
        self._factories = dict(factories)
        self._allowed_permissions = frozenset(allowed_permissions)
        self._services = dict(services or {})
        self._transaction_lock = asyncio.Lock()
        self._active: _ActiveGraph | None = None
        # A profile switch moves default admission atomically, while already
        # prepared conversations may hold provider-native thread/checkpoint
        # state. Retired graphs are reclaimed only after their last session
        # activation closes.
        self._retired: list[_ActiveGraph] = []
        self._activations: dict[str, _ActiveActivation] = {}
        self._last_failure: PluginHostError | None = None

    async def apply(self, profile: CompositionProfile) -> PluginInventory:
        """Resolve, admit, stage, health-check, then atomically switch profile.

        Any resolve/admission/stage/health error leaves the current graph
        untouched.  Staged effects from the failed candidate are drained and
        disposed before the error is returned.
        """

        async with self._transaction_lock:
            resolved = self.preflight(profile)
            if self._active and (
                self._active.resolved.profile_digest == resolved.profile_digest
                and self._active.resolved.plugin_lock_digest == resolved.plugin_lock_digest
            ):
                return self._inventory_for(self._active)

            ordered_entries = _dependency_order(resolved.plugin_lock.plugins)
            staged: list[_ActivePlugin] = []
            try:
                for entry in ordered_entries:
                    manifest = self._registry.manifest_for(entry.id, entry.version)
                    factory = self._factories[entry.id]
                    runtime = await factory.stage(
                        manifest,
                        profile=resolved.profile,
                        services=self._services,
                    )
                    staged.append(_ActivePlugin(entry=entry, runtime=runtime))
                for plugin in staged:
                    await plugin.runtime.start()
                    if not await plugin.runtime.health():
                        raise PluginHostError(
                            "plugin_health_failed",
                            f"plugin {plugin.entry.id}@{plugin.entry.version} failed health check",
                        )
            except asyncio.CancelledError:
                await self._finish_cleanup(self._dispose_staged(staged))
                raise
            except PluginHostError as error:
                await self._finish_cleanup(self._dispose_staged(staged))
                self._last_failure = error
                raise
            except Exception as error:  # noqa: BLE001 - boundary adapts plugins
                await self._finish_cleanup(self._dispose_staged(staged))
                failure = PluginHostError("plugin_stage_failed", str(error))
                self._last_failure = failure
                raise failure from error

            candidate = _ActiveGraph(resolved=resolved, plugins=tuple(staged))
            previous = self._active
            # This single assignment is the profile switch.  It only happens
            # after every staged effect has passed health, preserving the old
            # graph on all earlier failures.
            self._active = candidate
            self._last_failure = None
            if previous is not None:
                self._retired.append(previous)
                await self._finish_cleanup(self._dispose_unpinned_retired_graphs())
            return self._inventory_for(candidate)

    def preflight(self, profile: CompositionProfile) -> ResolvedComposition:
        """Resolve and admit a profile without importing or starting a plugin."""

        try:
            resolved = self._registry.resolve(profile)
        except PluginResolutionError as error:
            failure = PluginHostError(error.code, str(error))
            self._last_failure = failure
            raise failure from error
        entries = _dependency_order(resolved.plugin_lock.plugins)
        self._admit(entries)
        for entry in entries:
            if entry.id not in self._factories:
                failure = PluginHostError(
                    "plugin_factory_unavailable",
                    f"no factory is registered for {entry.id}@{entry.version}",
                )
                self._last_failure = failure
                raise failure
        return resolved

    def inventory(self) -> PluginInventory | None:
        """Return only the currently active profile inventory, if any."""

        if self._active is None:
            return None
        return self._inventory_for(self._active)

    @property
    def activation_count(self) -> int:
        """Number of live provider-owned activations (diagnostic only)."""

        return sum(not active.closed for active in self._activations.values())

    async def execute(self, bundle: ResolvedPluginBundle, request: Any) -> Any:
        """Execute one disposable activation (compatibility convenience API)."""

        session = await self.open_activation(
            bundle,
            activation_key=f"one-shot:{uuid4().hex}",
        )
        try:
            return await session.execute(request)
        finally:
            await session.close()

    async def open_activation(
        self,
        bundle: ResolvedPluginBundle,
        *,
        activation_key: str,
    ) -> PluginActivationSession:
        """Open or reuse one profile-fenced, provider-owned activation.

        Reuse is permitted for the exact graph and immutable Bundle digest. A
        profile switch retires its old graph for new sessions, but an existing
        session key remains pinned to that graph until it closes.
        """

        key = str(activation_key).strip()
        if not key or len(key) > 512:
            raise PluginHostError(
                "agent_activation_key_invalid",
                "activation_key must be a non-empty value of at most 512 characters",
            )
        async with self._transaction_lock:
            existing = self._activations.get(key)
            if existing is not None:
                if (
                    not existing.closed
                    and existing.bundle_digest == bundle.bundle_digest
                    and self._bundle_matches_graph(bundle, existing.graph)
                ):
                    # This activation may belong to a retired graph: it was
                    # pinned before a newer profile became active.
                    return PluginActivationSession(self, existing)
                await self._close_activation_record(existing)
                if self._activations.get(key) is existing:
                    self._activations.pop(key, None)
                await self._finish_cleanup(self._dispose_unpinned_retired_graphs())

            graph = self._require_bundle_graph(bundle)
            runtime = await self._prepare_activation(graph, bundle)
            active = _ActiveActivation(
                key=key,
                graph=graph,
                bundle_digest=bundle.bundle_digest,
                runtime=runtime,
                operation_lock=asyncio.Lock(),
            )
            self._activations[key] = active
            return PluginActivationSession(self, active)

    async def close_activation(
        self,
        activation_key: str,
        *,
        expected: _ActiveActivation | None = None,
    ) -> None:
        """Drain and dispose a retained activation if it is still current."""

        async with self._transaction_lock:
            active = self._activations.get(activation_key)
            if active is None or (expected is not None and active is not expected):
                return
            await self._close_activation_record(active)
            if self._activations.get(activation_key) is active:
                self._activations.pop(activation_key, None)
            await self._finish_cleanup(self._dispose_unpinned_retired_graphs())

    @property
    def last_failure(self) -> PluginHostError | None:
        return self._last_failure

    async def dispose(self) -> None:
        """Drain and dispose active plus retired graphs; there is no implicit restart."""

        async with self._transaction_lock:
            graphs = [
                graph
                for graph in [self._active, *self._retired]
                if graph is not None
            ]
            self._active = None
            self._retired = []
            if graphs:
                await self._finish_cleanup(self._dispose_all_graphs(graphs))

    @staticmethod
    async def _finish_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
        """Defer caller cancellation until an owned-effect cleanup finishes."""

        cleanup_task = asyncio.create_task(cleanup)
        interrupted = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        cleanup_task.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _dispose_graph(self, graph: _ActiveGraph) -> None:
        await self._close_graph_activations(graph)
        await self._dispose_staged(list(graph.plugins))

    async def _dispose_all_graphs(self, graphs: list[_ActiveGraph]) -> None:
        await self._close_all_activations()
        for graph in graphs:
            await self._dispose_staged(list(graph.plugins))

    async def _dispose_unpinned_retired_graphs(self) -> None:
        for graph in tuple(self._retired):
            if any(
                not activation.closed and activation.graph is graph
                for activation in self._activations.values()
            ):
                continue
            await self._dispose_staged(list(graph.plugins))
            self._retired.remove(graph)

    def _admit(self, entries: list[PluginLockEntry]) -> None:
        for entry in entries:
            manifest = self._registry.manifest_for(entry.id, entry.version)
            missing = sorted(set(manifest.spec.permissions) - self._allowed_permissions)
            if missing:
                raise PluginHostError(
                    "plugin_permission_denied",
                    f"plugin {entry.id}@{entry.version} requests unapproved permissions: "
                    + ", ".join(missing),
                )

    @staticmethod
    async def _dispose_staged(staged: list[_ActivePlugin]) -> None:
        """Best-effort cleanup preserves the primary failure while draining all effects."""

        for plugin in reversed(staged):
            try:
                await plugin.runtime.drain()
            except Exception:  # noqa: BLE001 - disposal must continue
                pass
            try:
                await plugin.runtime.dispose()
            except Exception:  # noqa: BLE001 - disposal must continue
                pass

    @staticmethod
    async def _dispose_activation(activation: PreparedAgent) -> None:
        """Best-effort cleanup for a prepared one-shot activation."""

        try:
            await activation.drain()
        except Exception:  # noqa: BLE001 - disposal must continue
            pass
        try:
            await activation.dispose()
        except Exception:  # noqa: BLE001 - disposal must continue
            pass

    def _require_bundle_graph(self, bundle: ResolvedPluginBundle) -> _ActiveGraph:
        graph = self._active
        if graph is None:
            raise PluginHostError(
                "plugin_profile_inactive", "no plugin profile is active"
            )
        if not self._bundle_matches_graph(bundle, graph):
            raise PluginHostError(
                "plugin_bundle_profile_mismatch",
                "Bundle composition does not match the active plugin graph",
            )
        return graph

    @staticmethod
    def _bundle_matches_graph(bundle: ResolvedPluginBundle, graph: _ActiveGraph) -> bool:
        return (
            bundle.composition.profile_digest == graph.resolved.profile_digest
            and bundle.composition.plugin_lock_digest == graph.resolved.plugin_lock_digest
        )

    async def _prepare_activation(
        self,
        graph: _ActiveGraph,
        bundle: ResolvedPluginBundle,
    ) -> PreparedAgent:
        capabilities = self._execution_context(graph)
        provider_binding = capabilities.require(
            "agent.provider/v1", slot="agent.execution"
        )
        provider = provider_binding.runtime
        if not isinstance(provider, ExecutableAgentProvider):
            raise PluginHostError(
                "agent_provider_not_executable",
                f"plugin {provider_binding.plugin_id}@"
                f"{provider_binding.plugin_version} does not implement prepare",
            )

        activation: PreparedAgent | None = None
        try:
            try:
                activation = await provider.prepare(bundle, capabilities=capabilities)
            except PluginHostError:
                raise
            except Exception as error:  # noqa: BLE001 - provider boundary
                raise PluginHostError("agent_prepare_failed", str(error)) from error
            if not isinstance(activation, PreparedAgent):
                raise PluginHostError(
                    "agent_activation_invalid",
                    "AgentProvider.prepare returned an invalid activation",
                )
            try:
                await activation.start()
            except Exception as error:  # noqa: BLE001 - activation boundary
                raise PluginHostError(
                    "agent_activation_start_failed", str(error)
                ) from error
            try:
                healthy = await activation.health()
            except Exception as error:  # noqa: BLE001 - activation boundary
                raise PluginHostError(
                    "agent_activation_health_failed", str(error)
                ) from error
            if not healthy:
                raise PluginHostError(
                    "agent_activation_health_failed",
                    "prepared Agent activation failed health check",
                )
            return activation
        except asyncio.CancelledError:
            if activation is not None:
                await self._dispose_activation(activation)
            raise
        except PluginHostError as error:
            self._last_failure = error
            if activation is not None:
                await self._dispose_activation(activation)
            raise

    async def _execute_activation(
        self,
        active: _ActiveActivation,
        request: Any,
    ) -> Any:
        async with active.operation_lock:
            task = asyncio.current_task()
            active.operation_task = task
            try:
                if active.closed:
                    raise PluginHostError(
                        "agent_activation_closed",
                        f"Agent activation {active.key!r} is closed",
                    )
                result = await active.runtime.execute(request)
            except asyncio.CancelledError:
                if not active.closed:
                    await self._dispose_activation(active.runtime)
                active.closed = True
                raise
            except PluginHostError as error:
                self._last_failure = error
                await self._dispose_activation(active.runtime)
                active.closed = True
                raise
            except Exception as error:  # noqa: BLE001 - provider boundary
                failure = PluginHostError("agent_execution_failed", str(error))
                self._last_failure = failure
                await self._dispose_activation(active.runtime)
                active.closed = True
                raise failure from error
            finally:
                if active.operation_task is task:
                    active.operation_task = None
            self._last_failure = None
            return result

    async def _runtime_adapter_activation(self, active: _ActiveActivation) -> Any:
        async with active.operation_lock:
            if active.closed:
                raise PluginHostError(
                    "agent_activation_closed",
                    f"Agent activation {active.key!r} is closed",
                )
            provide = getattr(active.runtime, "runtime_adapter", None)
            if not callable(provide):
                raise PluginHostError(
                    "agent_provider_runtime_adapter_unavailable",
                    "AgentProvider does not expose a Kernel RuntimeAdapter",
                )
            try:
                adapter = provide()
                if asyncio.iscoroutine(adapter):
                    adapter = await adapter
            except PluginHostError:
                raise
            except Exception as error:  # noqa: BLE001 - provider boundary
                raise PluginHostError(
                    "agent_provider_runtime_adapter_failed", str(error)
                ) from error
            return adapter

    async def _close_activation_record(self, active: _ActiveActivation) -> None:
        await self._finish_cleanup(self._close_activation_record_owned(active))

    async def _close_activation_record_owned(self, active: _ActiveActivation) -> None:
        """Finish the entire terminal close before propagating caller cancellation."""

        if active.closed and active.operation_task is None:
            return
        active.closed = True
        operation = active.operation_task
        await self._abort_activation(active.runtime)
        if operation is not None and operation is not asyncio.current_task():
            operation.cancel()
        try:
            await asyncio.wait_for(active.operation_lock.acquire(), timeout=2.0)
        except TimeoutError:
            await self._bounded_dispose_activation(active.runtime)
            return
        try:
            await self._bounded_dispose_activation(active.runtime)
        finally:
            active.operation_lock.release()

    @staticmethod
    async def _abort_activation(activation: PreparedAgent) -> None:
        """Finish provider revocation before a close can report success.

        ``abort`` is currently implemented only by the Harness activation and
        revokes activation-scoped MCP credentials.  A host-side timeout would
        let close return while those credentials still authorize requests.
        The underlying DSH lifecycle operation owns its network timeout and
        kills the whole generation when revocation cannot be proven.
        """

        abort = getattr(activation, "abort", None)
        if not callable(abort):
            return
        try:
            result = abort()
            if asyncio.iscoroutine(result):
                await result
        except BaseException:  # abort is best-effort; disposal still follows
            pass

    @classmethod
    async def _bounded_dispose_activation(cls, activation: PreparedAgent) -> None:
        try:
            await asyncio.wait_for(cls._dispose_activation(activation), timeout=2.0)
        except BaseException:  # terminal close cannot wait forever on provider code
            pass

    async def _close_graph_activations(self, graph: _ActiveGraph) -> None:
        selected = [
            (key, active)
            for key, active in tuple(self._activations.items())
            if active.graph is graph
        ]
        if selected:
            await asyncio.gather(
                *(self._close_activation_record(active) for _key, active in selected)
            )
        for key, active in selected:
            if self._activations.get(key) is active:
                self._activations.pop(key, None)

    async def _close_all_activations(self) -> None:
        selected = tuple(self._activations.items())
        if selected:
            await asyncio.gather(
                *(self._close_activation_record(active) for _key, active in selected)
            )
        for key, active in selected:
            if self._activations.get(key) is active:
                self._activations.pop(key, None)

    @staticmethod
    def _execution_context(graph: _ActiveGraph) -> PluginExecutionContext:
        bindings = tuple(
            PluginCapabilityBinding(
                plugin_id=plugin.entry.id,
                plugin_version=plugin.entry.version,
                definition=capability.definition,
                slot=capability.slot,
                runtime=plugin.runtime,
            )
            for plugin in graph.plugins
            for capability in plugin.entry.provides
        )
        return PluginExecutionContext(
            profile_digest=graph.resolved.profile_digest,
            plugin_lock_digest=graph.resolved.plugin_lock_digest,
            bindings=bindings,
        )

    @staticmethod
    def _inventory_for(graph: _ActiveGraph) -> PluginInventory:
        return PluginInventory(
            profile_digest=graph.resolved.profile_digest,
            plugin_lock_digest=graph.resolved.plugin_lock_digest,
            plugins=[
                PluginInventoryItem(
                    id=plugin.entry.id,
                    version=plugin.entry.version,
                    digest=plugin.entry.digest,
                    state="ready",
                    health="healthy",
                )
                for plugin in graph.plugins
            ],
        )


def _dependency_order(entries: list[PluginLockEntry]) -> list[PluginLockEntry]:
    """Dependency-first deterministic topological order from the locked graph."""

    by_id = {entry.id: entry for entry in entries}
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[PluginLockEntry] = []

    def visit(plugin_id: str) -> None:
        if plugin_id in visiting:
            raise PluginHostError("plugin_dependency_cycle", "plugin lock dependency cycle")
        if plugin_id in visited:
            return
        entry = by_id.get(plugin_id)
        if entry is None:
            raise PluginHostError(
                "plugin_dependency_unresolved",
                f"plugin dependency {plugin_id!r} is missing from the lock",
            )
        visiting.add(plugin_id)
        for dependency in sorted(entry.dependencies, key=lambda item: item.id):
            visit(dependency.id)
        visiting.remove(plugin_id)
        visited.add(plugin_id)
        ordered.append(entry)

    for entry in sorted(entries, key=lambda item: item.id):
        visit(entry.id)
    return ordered


__all__ = [
    "ExecutableAgentProvider",
    "ManagedPlugin",
    "PluginCapabilityBinding",
    "PluginActivationSession",
    "PluginExecutionContext",
    "PluginFactory",
    "PluginHost",
    "PluginHostError",
    "PreparedAgent",
]
