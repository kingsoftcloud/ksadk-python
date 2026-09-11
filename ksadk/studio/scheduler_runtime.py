"""Scheduler-specific target, title and event adapters for the Build registry."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ksadk.kernel.bootstrap import (
    AgentKernelRuntime,
    AgentKernelRuntimeConfig,
    build_agent_kernel_runtime,
)
from ksadk.kernel.contracts import (
    AgentControlCommand,
    AgentControlPermit,
    AgentControlReceipt,
    SessionEventSubscription,
)
from ksadk.kernel.ingress import trusted_context
from ksadk.scheduler.contracts import ScheduledTaskTarget, ScheduleOccurrence
from ksadk.studio.kernel_registry import (
    ResolveAdapterProvider,
    ResolveBuild,
    StudioBuildKernelError,
    StudioBuildKernelRegistry,
    StudioBuildRuntimeTarget,
)


class StudioSchedulerRuntimeError(StudioBuildKernelError):
    """Stable local scheduling failure safe to persist on an occurrence."""


StudioScheduledRuntimeTarget = StudioBuildRuntimeTarget


class StudioScheduledKernelRegistry(StudioBuildKernelRegistry):
    """Add Scheduler references and session presentation to shared Build Kernels."""

    build_id: str
    agent_id: str
    tenant_id: str
    agent_instance_id: str


@dataclass
class _RuntimeEntry:
    target: StudioScheduledRuntimeTarget
    spec: StudioRunSpec
    runtime: AgentKernelRuntime


class StudioScheduledKernelRegistry:
    """Own exact per-Build Kernel runtimes for one Studio process.

    The registry does not register anything in ``ksadk.kernel.ingress`` and
    therefore cannot redirect normal conversations or hosted HTTP traffic.
    Scheduler dispatch uses :meth:`submit` and :meth:`read_events` directly,
    while still crossing the frozen AgentControl permit/admission boundary.
    """

    def __init__(
        self,
        *,
        resolve_build: ResolveBuild,
        resolve_adapter_provider: ResolveAdapterProvider,
        session_service: BaseSessionService,
        runtime_executor: object | None = None,
        state_dir: str | Path | None = None,
        tenant_id: str = "local-studio",
        workspace_id: str = "studio-scheduler",
        poll_interval: float = 0.05,
        lease_ttl_seconds: float = 30.0,
    ) -> None:
        self._resolve_build = resolve_build
        self._resolve_adapter_provider = resolve_adapter_provider
        self._session_service = session_service
        self._runtime_executor = runtime_executor
        self._state_dir = Path(state_dir).resolve() if state_dir is not None else None
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._poll_interval = poll_interval
        self._lease_ttl_seconds = lease_ttl_seconds
        self._owner_id = uuid4().hex
        self._entries_by_build: dict[str, _RuntimeEntry] = {}
        self._build_by_instance: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def active_runtime_count(self) -> int:
        return len(self._entries_by_build)

    async def start(self) -> None:
        self._started = True

    async def ensure_build(
        self,
        build_id: str,
        *,
        expected_agent_id: str | None = None,
    ) -> StudioScheduledRuntimeTarget:
        """Start or return the exact Runtime owned by one immutable Build."""

        normalized = str(build_id).strip()
        if not normalized:
            raise StudioSchedulerRuntimeError(
                "SCHEDULER_BUILD_REQUIRED",
                "定时任务缺少不可变 Build 标识",
            )
        if not self._started:
            raise StudioSchedulerRuntimeError(
                "SCHEDULER_RUNTIME_NOT_STARTED",
                "Studio Scheduler Runtime 尚未启动",
            )
        existing = self._entries_by_build.get(normalized)
        if existing is not None:
            self._require_agent(existing.spec, expected_agent_id)
            return existing.target

        async with self._lock:
            existing = self._entries_by_build.get(normalized)
            if existing is not None:
                self._require_agent(existing.spec, expected_agent_id)
                return existing.target
            try:
                spec = self._resolve_build(normalized)
            except Exception as error:
                raise StudioSchedulerRuntimeError(
                    "SCHEDULER_BUILD_UNAVAILABLE",
                    f"定时任务绑定的 Build {normalized!r} 不可用",
                ) from error
            if spec.build_id != normalized:
                raise StudioSchedulerRuntimeError(
                    "SCHEDULER_BUILD_MISMATCH",
                    "Build 解析结果与定时任务绑定不一致",
                )
            self._require_agent(spec, expected_agent_id)
            try:
                adapter_provider = self._resolve_adapter_provider(spec)
            except Exception as error:
                raise StudioSchedulerRuntimeError(
                    "SCHEDULER_PROVIDER_UNAVAILABLE",
                    f"Build {normalized!r} 的 AgentProvider 不可用",
                ) from error

            # ``build_agent_kernel_runtime`` asks the provider once to snapshot
            # capabilities.  Retain that exact adapter for the first worker or
            # recovery acquisition instead of constructing and discarding a
            # resource-owning Codex/Harness adapter during registration.
            retained_first_adapter: RuntimeAdapter | None = None
            first_adapter_borrowed = False

            def checked_adapter_provider() -> RuntimeAdapter:
                nonlocal retained_first_adapter, first_adapter_borrowed
                if retained_first_adapter is not None and not first_adapter_borrowed:
                    first_adapter_borrowed = True
                    adapter = retained_first_adapter
                    retained_first_adapter = None
                    return adapter
                adapter = adapter_provider()
                if not isinstance(adapter, RuntimeAdapter):
                    raise StudioSchedulerRuntimeError(
                        "SCHEDULER_PROVIDER_INVALID",
                        "AgentProvider 没有返回 RuntimeAdapter",
                    )
                if retained_first_adapter is None and not first_adapter_borrowed:
                    retained_first_adapter = adapter
                return adapter

            instance_id = self._instance_id(normalized)
            kernel_db = (
                self._state_dir / "kernel" / f"{instance_id}.sqlite"
                if self._state_dir is not None
                else None
            )
            config = AgentKernelRuntimeConfig(
                agent_instance_id=instance_id,
                authority_mode="local",
                driver="sqlite" if kernel_db is not None else "memory",
                durability_tier="durable" if kernel_db is not None else "ephemeral",
                dsn=str(kernel_db) if kernel_db is not None else "",
                adapter_provider=checked_adapter_provider,
                session_service=self._session_service,
                runtime_executor=self._runtime_executor,
                launch_context=spec.launch_context,
                start_request_defaults=self._start_defaults(spec),
                tenant_id=self._tenant_id,
                workspace_id=self._workspace_id,
                poll_interval=self._poll_interval,
                lease_ttl_seconds=self._lease_ttl_seconds,
                activation_id=f"studio-scheduler:{self._owner_id}:{instance_id}",
                runtime_type=spec.launch_context.runtime_type,
                bundle_digest=spec.manifest_sha256 or normalized,
            )
            runtime = build_agent_kernel_runtime(config)
            try:
                await runtime.start()
            except Exception:
                await runtime.close()
                raise
            target = StudioScheduledRuntimeTarget(
                build_id=normalized,
                agent_id=spec.agent_id,
                tenant_id=self._tenant_id,
                agent_instance_id=instance_id,
            )
            self._entries_by_build[normalized] = _RuntimeEntry(
                target=target,
                spec=spec,
                runtime=runtime,
            )
            self._build_by_instance[instance_id] = normalized
            return target

    async def ensure_target(
        self, target: ScheduledTaskTarget
    ) -> StudioScheduledRuntimeTarget:
        build_id = str(target.agent_version_ref or "").strip()
        exact = await self.ensure_build(
            build_id,
            expected_agent_id=target.agent_id,
        )
        if (
            target.tenant_id != exact.tenant_id
            or target.agent_instance_id != exact.agent_instance_id
        ):
            raise StudioSchedulerRuntimeError(
                "SCHEDULER_TARGET_MISMATCH",
                "定时任务目标与不可变 Build 的 Kernel 身份不一致",
            )
        return exact

    async def submit(
        self,
        command: AgentControlCommand,
        permit: AgentControlPermit,
    ) -> AgentControlReceipt:
        entry = self._entry_for_instance(command.agent_instance_id)
        if command.tenant_id != entry.target.tenant_id:
            raise self._error("TARGET_MISMATCH", "AgentControl tenant 与 Build Kernel 不一致")
        prompt = command.payload.get("content")
        has_prompt = isinstance(prompt, str) and bool(prompt.strip())
        await self.ensure_session(
            entry.target.build_id,
            command.session_id,
            command.tenant_id,
            title=f"定时任务 · {prompt[:40]}" if has_prompt else None,
            metadata={
                "title_source": "scheduler",
                "first_prompt": prompt,
                "last_prompt": prompt,
            } if has_prompt else None,
        )
        return await self.submit_control(command, permit)

    async def read_events(
        self, occurrence: ScheduleOccurrence
    ) -> tuple[tuple[int, object], ...]:
        target = occurrence.target
        if target is None:
            return ()
        await self.ensure_target(target)
        entry = self._entry_for_instance(target.agent_instance_id)
        trusted = trusted_context(
            source_kind="scheduler",
            source_ref=occurrence.occurrence_id,
            tenant_id=target.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=occurrence.session_id,
            operations=("subscribe_events",),
        )
        cursor = occurrence.last_event_seq
        if cursor is None:
            cursor = occurrence.accepted_seq or 0
        subscription = SessionEventSubscription(
            tenant_id=trusted.tenant_id,
            agent_instance_id=trusted.agent_instance_id,
            session_id=occurrence.session_id,
            authorization_ref=trusted.permit.permit_id,
            after_seq=cursor,
        )
        result: list[tuple[int, object]] = []
        stream = entry.runtime.kernel.subscribe(
            subscription,
            permit=trusted.permit,
            timeout=0.05,
        )
        try:
            async for envelope in stream:
                result.append((int(envelope.seq), envelope))
                if len(result) >= 100:
                    break
        finally:
            await stream.aclose()
        return tuple(result)

__all__ = [
    "ResolveAdapterProvider",
    "ResolveBuild",
    "StudioScheduledKernelRegistry",
    "StudioScheduledRuntimeTarget",
    "StudioSchedulerRuntimeError",
]
