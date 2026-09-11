"""Shared immutable Build -> Kernel registry for trusted Studio ingress.

One Build owns one RuntimeAdapter factory and one Kernel admission/event log.
Consumers provide their own session display metadata and trusted permits; this
module does not own scheduler, conversation, or domain policy.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
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
)
from ksadk.runtime import RuntimeAdapter
from ksadk.sessions.base import BaseSessionService, Session
from ksadk.sessions.local_service import LocalSessionService
from ksadk.studio.kernel_sqlite_migration import KernelMigrationError, migrate_legacy_kernel
from ksadk.studio.run_service import StudioRunSpec

ResolveBuild = Callable[[str], StudioRunSpec]
ResolveAdapterProvider = Callable[[StudioRunSpec], Callable[[], RuntimeAdapter]]


class StudioBuildKernelError(RuntimeError):
    """Stable failure from the local Build registry."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StudioBuildRuntimeTarget:
    """Server-owned immutable Build routing facts."""

    build_id: str
    agent_id: str
    tenant_id: str
    agent_instance_id: str


@dataclass
class _RuntimeEntry:
    target: StudioBuildRuntimeTarget
    spec: StudioRunSpec
    runtime: AgentKernelRuntime


class StudioBuildKernelRegistry:
    """Own exact per-Build Kernel runtimes for one Studio process.

    The registry does not register anything in ``ksadk.kernel.ingress`` and
    therefore cannot redirect normal conversations or hosted HTTP traffic.
    Consumers dispatch through :meth:`submit_control`, crossing the frozen
    AgentControl permit/admission boundary.
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
        self._session_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._admission_open = True
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def active_runtime_count(self) -> int:
        return len(self._entries_by_build)

    async def unsettled_sessions(self) -> tuple[str, ...]:
        """Current command/Run facts for a host maintenance admission check."""
        pending: set[str] = set()
        for entry in tuple(self._entries_by_build.values()):
            messages = await entry.runtime.kernel_store.list_messages(
                entry.target.agent_instance_id
            )
            for session_id in {message.session_id for message in messages}:
                active = await entry.runtime.kernel_store.find_active_run(
                    entry.target.agent_instance_id, session_id
                )
                if active or any(
                    message.session_id == session_id and message.status in {"accepted", "claimed"}
                    for message in messages
                ):
                    pending.add(session_id)
        return tuple(sorted(pending))

    async def start(self) -> None:
        self._started = True

    async def set_admission_open(self, enabled: bool) -> None:
        """Fence new ingress and finish prior admission before maintenance."""
        async with self._admission_lock:
            async with self._lock:
                self._admission_open = enabled

    async def ensure_build(
        self,
        build_id: str,
        *,
        expected_agent_id: str | None = None,
    ) -> StudioBuildRuntimeTarget:
        """Start or return the exact Runtime owned by one immutable Build."""

        normalized = str(build_id).strip()
        if not normalized:
            raise self._error(
                "BUILD_REQUIRED",
                "缺少不可变 Build 标识",
            )
        if not self._started:
            raise self._error(
                "RUNTIME_NOT_STARTED",
                "Studio Build Kernel 尚未启动",
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
            if not self._admission_open:
                raise self._error("MAINTENANCE", "插件宿主正在维护，暂不启动新的 Build Kernel")
            try:
                spec = self._resolve_build(normalized)
            except Exception as error:
                raise self._error(
                    "BUILD_UNAVAILABLE",
                    f"绑定的 Build {normalized!r} 不可用",
                ) from error
            if spec.build_id != normalized:
                raise self._error(
                    "BUILD_MISMATCH",
                    "Build 解析结果与绑定不一致",
                )
            self._require_agent(spec, expected_agent_id)
            try:
                adapter_provider = self._resolve_adapter_provider(spec)
            except Exception as error:
                raise self._error(
                    "PROVIDER_UNAVAILABLE",
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
                    raise self._error(
                        "PROVIDER_INVALID",
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
            if isinstance(self._session_service, LocalSessionService):
                shared_db = self._session_service.db_path
                if kernel_db is not None:
                    try:
                        await asyncio.to_thread(
                            migrate_legacy_kernel, kernel_db, shared_db, instance_id
                        )
                    except KernelMigrationError as error:
                        raise self._error("MIGRATION_REQUIRED", str(error)) from error
                kernel_db = shared_db
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
            target = StudioBuildRuntimeTarget(
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

    def runtime_for_build(self, build_id: str) -> AgentKernelRuntime:
        entry = self._entries_by_build.get(str(build_id))
        if entry is None:
            raise self._error(
                "TARGET_UNAVAILABLE",
                "目标 Build Kernel 未注册",
            )
        return entry.runtime

    def spec_for_build(self, build_id: str) -> StudioRunSpec:
        """Return the exact spec already pinned by :meth:`ensure_build`."""

        entry = self._entries_by_build.get(str(build_id))
        if entry is None:
            raise self._error("TARGET_UNAVAILABLE", "目标 Build Kernel 未注册")
        return entry.spec

    async def ensure_session(
        self,
        build_id: str,
        session_id: str,
        owner_subject: str,
        *,
        title: str | None = None,
        metadata: Mapping[str, str | None] | None = None,
    ) -> Session:
        """Create a session or verify its owner and Agent without retitling it.

        Metadata is limited to display fields. It never authorizes execution;
        callers still pass a trusted AgentControl permit to submit_control.
        """

        if not session_id or not owner_subject:
            raise self._error("SESSION_IDENTITY_REQUIRED", "会话与所有者标识不能为空")
        display = dict(metadata or {})
        allowed = {"title_source", "summary", "first_prompt", "last_prompt"}
        if (
            set(display) - allowed
            or any(value is not None and not isinstance(value, str) for value in display.values())
            or (title is not None and not isinstance(title, str))
        ):
            raise self._error("SESSION_METADATA_INVALID", "会话 metadata 只接受展示字段")
        target = await self.ensure_build(build_id)
        async with self._session_lock:
            session = await self._session_service.get_session_metadata(session_id)
            created = session is None
            if session is None:
                session = await self._session_service.create_session(
                    agent_id=target.agent_id,
                    user_id=owner_subject,
                    session_id=session_id,
                )
            # create_session may return an existing record after an external
            # create race, so fence its result as well as the initial lookup.
            if session.user_id != owner_subject or session.agent_id != target.agent_id:
                raise self._error("SESSION_OWNER_MISMATCH", "会话不属于当前所有者和 Agent")
            if created and (title is not None or display):
                session = await self._session_service.update_session_metadata(
                    session_id,
                    title=title,
                    **display,
                )
            return session

    async def submit_control(
        self,
        command: AgentControlCommand,
        permit: AgentControlPermit,
    ) -> AgentControlReceipt:
        """Submit to the pinned Kernel; no scheduler/session metadata is inferred."""

        async with self._admission_lock:
            if not self._admission_open:
                raise self._error("MAINTENANCE", "插件宿主正在维护，暂不接受新执行")
            return await self._submit_control(command, permit)

    async def _submit_control(
        self, command: AgentControlCommand, permit: AgentControlPermit
    ) -> AgentControlReceipt:

        entry = self._entry_for_instance(command.agent_instance_id)
        if command.tenant_id != entry.target.tenant_id:
            raise self._error("TARGET_MISMATCH", "AgentControl tenant 与 Build Kernel 不一致")
        session = await self._session_service.get_session_metadata(command.session_id)
        if session is None or session.agent_id != entry.target.agent_id:
            raise self._error("SESSION_UNAVAILABLE", "Build Kernel 会话尚未注册或 Agent 不匹配")
        return await entry.runtime.kernel.submit(command, permit=permit)

    @classmethod
    def _error(cls, code: str, message: str) -> StudioBuildKernelError:
        return StudioBuildKernelError(f"STUDIO_KERNEL_{code}", message)

    async def close(self) -> None:
        async with self._lock:
            entries = list(self._entries_by_build.values())
            self._entries_by_build.clear()
            self._build_by_instance.clear()
            self._started = False
        first_error: BaseException | None = None
        for entry in reversed(entries):
            try:
                await entry.runtime.close()
            except BaseException as error:  # cleanup must continue for other Builds
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _entry_for_instance(self, instance_id: str) -> _RuntimeEntry:
        build_id = self._build_by_instance.get(str(instance_id))
        entry = self._entries_by_build.get(build_id or "")
        if entry is None:
            raise self._error(
                "TARGET_UNAVAILABLE",
                "目标 Build Kernel 未注册",
            )
        return entry

    def _instance_id(self, build_id: str) -> str:
        digest = hashlib.sha256(build_id.encode("utf-8")).hexdigest()[:24]
        # Preserve existing Scheduler SQLite/AgentInstance identities when
        # its registry is shared by additional trusted Studio consumers.
        return f"studio-schedule-{digest}"

    @classmethod
    def _require_agent(cls, spec: StudioRunSpec, expected_agent_id: str | None) -> None:
        if expected_agent_id and spec.agent_id != expected_agent_id:
            raise cls._error(
                "AGENT_MISMATCH",
                "Agent 与不可变 Build 不一致",
            )

    @staticmethod
    def _start_defaults(spec: StudioRunSpec) -> dict[str, object]:
        defaults: dict[str, object] = {
            "agent_id": spec.agent_id,
            "config": dict(spec.request_config),
        }
        if spec.model:
            defaults["model"] = spec.model
            defaults["allowed_models"] = [spec.model]
        return defaults


__all__ = [
    "ResolveAdapterProvider",
    "ResolveBuild",
    "StudioBuildKernelError",
    "StudioBuildKernelRegistry",
    "StudioBuildRuntimeTarget",
]
