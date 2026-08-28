"""Adapters from the SDK sandbox sessions to the Harness async contract.

``ksadk.sandbox`` deliberately exposes a small synchronous session API used by
the SDK and Skill Runtime.  The Harness owns a different, asynchronous
lifecycle contract.  This module bridges the two without claiming guarantees
that the wrapped backend cannot provide.
"""

from __future__ import annotations

import asyncio
import math
import shlex
import time
from collections.abc import Callable, Iterable
from typing import cast
from uuid import uuid4

from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    ExecuteResult,
    FilesystemIsolation,
    NetworkControl,
    SandboxAuditLog,
    SandboxBackendCapabilities,
    SandboxClosedError,
    SandboxCommandResumeToken,
    SandboxHandle,
    SandboxPolicyViolation,
    SandboxResumeToken,
    SandboxSpec,
)
from ksadk.harness.sandbox_lease import (
    SandboxLeaseConflict,
    SandboxLeaseGrant,
    SandboxLeaseProvider,
    SandboxLeaseScope,
)
from ksadk.sandbox.backends.e2b import E2BSandboxBackend
from ksadk.sandbox.backends.local_process import LocalProcessSandboxBackend
from ksadk.sandbox.base import (
    ArtifactListingSandboxSession,
    BackgroundCommandSandboxSession,
    ReconnectableCommandSandboxSession,
    ReconnectableSandboxCommandHandle,
    SandboxCommandHandle,
    SandboxCommandResult,
    SandboxSession,
)
from ksadk.sandbox.base import (
    ReconnectableSandboxBackend as SdkReconnectableSandboxBackend,
)
from ksadk.sandbox.base import SandboxBackend as SdkSandboxBackend

ArtifactCollector = Callable[[SandboxSession], Iterable[str]]


class SessionSandboxHandle(SandboxHandle):
    """Harness handle backed by one SDK ``SandboxSession``."""

    def __init__(
        self,
        spec: SandboxSpec,
        session: SandboxSession,
        *,
        lease: SandboxLeaseGrant | None = None,
    ) -> None:
        super().__init__(spec)
        self.session = session
        self.lease = lease


class SessionSandboxCommand:
    """One background command owned by a ``SessionSandboxBackendAdapter``."""

    def __init__(
        self,
        *,
        adapter: SessionSandboxBackendAdapter,
        sandbox: SessionSandboxHandle,
        request: ExecuteRequest,
        command_handle: SandboxCommandHandle,
    ) -> None:
        self._adapter = adapter
        self.sandbox = sandbox
        self.request = request
        self.command_handle = command_handle
        self.started_at = time.monotonic()
        self.completed = False


class SessionSandboxBackendAdapter:
    """Run a synchronous SDK sandbox backend behind the Harness contract.

    Optional session protocols provide cancellation and Artifact enumeration.
    A backend may only advertise those capabilities when each created session
    implements the corresponding protocol; otherwise execution fails loudly
    instead of silently degrading its isolation claim.
    """

    def __init__(
        self,
        backend: SdkSandboxBackend,
        *,
        capabilities: SandboxBackendCapabilities,
        artifact_collector: ArtifactCollector | None = None,
        audit_log: SandboxAuditLog | None = None,
        lease_provider: SandboxLeaseProvider | None = None,
        lease_scope: SandboxLeaseScope | None = None,
        lease_owner_id: str | None = None,
        lease_ttl_seconds: float = 120.0,
        configured_workspace_root: str | None = None,
        prepare_workspace: bool = False,
    ) -> None:
        if artifact_collector is not None and not capabilities.artifact_collection:
            raise ValueError("提供 artifact_collector 时必须声明 artifact_collection")
        if capabilities.execution_audit != (audit_log is not None):
            raise ValueError("execution_audit 能力声明必须与 audit_log 装配一致")
        if capabilities.reconnect and not isinstance(backend, SdkReconnectableSandboxBackend):
            raise ValueError("后端声明 reconnect 但未实现 reconnect_session")
        if capabilities.ownership_fencing != (lease_provider is not None):
            raise ValueError("ownership_fencing 能力声明必须与 lease_provider 装配一致")
        if (lease_provider is not None) != (lease_scope is not None):
            raise ValueError("lease_provider 与 lease_scope 必须同时装配")
        if capabilities.command_reconnect and not (
            capabilities.reconnect
            and capabilities.ownership_fencing
            and capabilities.cooperative_cancellation
        ):
            raise ValueError(
                "command_reconnect 依赖 reconnect、ownership_fencing 和 cooperative_cancellation"
            )
        if lease_provider is not None and not capabilities.reconnect:
            raise ValueError("Sandbox 租约 fencing 只适用于支持 reconnect 的后端")
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds 必须大于 0")
        self._backend = backend
        self._capabilities = capabilities
        self._artifact_collector = artifact_collector
        self._audit = audit_log
        self._lease_provider = lease_provider
        self._lease_scope = lease_scope
        self._lease_owner_id = (lease_owner_id or f"harness-{uuid4().hex}").strip()
        if lease_provider is not None and not self._lease_owner_id:
            raise ValueError("lease_owner_id 不能为空")
        self._lease_ttl_seconds = lease_ttl_seconds
        self._configured_workspace_root = configured_workspace_root
        self._prepare_workspace = prepare_workspace
        self._handles: dict[int, SessionSandboxHandle] = {}

    @property
    def capabilities(self) -> SandboxBackendCapabilities:
        return self._capabilities

    async def create(self, spec: SandboxSpec) -> SessionSandboxHandle:
        self._validate_spec(spec)
        provisional = SandboxHandle(spec)
        session = await asyncio.to_thread(
            self._backend.create_session,
            session_id=provisional.handle_id,
            env=dict(spec.env),
        )
        if self._prepare_workspace and spec.workspace_root:
            try:
                result = await asyncio.to_thread(
                    session.run_command,
                    f"mkdir -p -- {shlex.quote(spec.workspace_root)}",
                    timeout=10,
                )
            except Exception:
                await asyncio.to_thread(session.kill)
                raise
            if result.exit_code != 0:
                await asyncio.to_thread(session.kill)
                raise SandboxPolicyViolation(f"无法初始化 Sandbox workspace_root: {result.stderr}")
        handle = SessionSandboxHandle(spec, session)
        # Keep the id generated before the potentially remote create call as
        # the stable correlation id passed to the SDK backend.
        handle.handle_id = provisional.handle_id
        try:
            handle.lease = await self._acquire_lease(handle.handle_id)
        except Exception:
            await asyncio.to_thread(session.kill)
            raise
        self._handles[id(handle)] = handle
        return handle

    def export_resume_token(self, handle: SandboxHandle) -> SandboxResumeToken:
        if not self._capabilities.reconnect:
            raise SandboxPolicyViolation("当前 Sandbox 后端未声明跨进程重连")
        owned = self._require_open_handle(handle)
        locator = str(owned.session.sandbox_id or "").strip()
        if not locator:
            raise RuntimeError("Sandbox Session 未提供可持久化的 session locator")
        return SandboxResumeToken(
            backend_id=self._capabilities.backend_id,
            handle_id=owned.handle_id,
            session_locator=locator,
        )

    async def reconnect(
        self,
        token: SandboxResumeToken,
        *,
        spec: SandboxSpec,
    ) -> SessionSandboxHandle:
        if not self._capabilities.reconnect:
            raise SandboxPolicyViolation("当前 Sandbox 后端未声明跨进程重连")
        if token.backend_id != self._capabilities.backend_id:
            raise SandboxPolicyViolation("Sandbox Resume Token 与当前后端不匹配")
        self._validate_spec(spec)
        if any(
            item.handle_id == token.handle_id and not item.closed for item in self._handles.values()
        ):
            raise SandboxPolicyViolation("Sandbox Handle 已在当前进程连接")
        lease = await self._acquire_lease(token.handle_id)
        backend = cast(SdkReconnectableSandboxBackend, self._backend)
        try:
            session = await asyncio.to_thread(
                backend.reconnect_session,
                session_locator=token.session_locator,
            )
        except Exception:
            await self._release_lease(lease)
            raise
        locator = str(session.sandbox_id or "").strip()
        if locator != token.session_locator:
            await asyncio.to_thread(session.kill)
            await self._release_lease(lease)
            raise SandboxPolicyViolation("Sandbox 重连返回了不同的 session locator")
        handle = SessionSandboxHandle(spec, session, lease=lease)
        handle.handle_id = token.handle_id
        self._handles[id(handle)] = handle
        return handle

    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult:
        owned = self._require_open_handle(handle)
        timeout = max(1, math.ceil(request.timeout_seconds))
        await self._renew_lease(owned, ttl_seconds=max(self._lease_ttl_seconds, timeout + 30.0))
        started = time.monotonic()
        try:
            if self._capabilities.cooperative_cancellation:
                result = await self._execute_cancellable(owned, request, timeout)
            else:
                result = await asyncio.to_thread(
                    owned.session.run_command,
                    request.command,
                    timeout=timeout,
                    env=dict(owned.spec.env),
                    cwd=owned.spec.workspace_root or None,
                )
        except asyncio.CancelledError:
            self._append_audit(
                owned,
                request,
                ExecuteResult(ok=False, output="", exit_code=130, error="sandbox 命令已取消"),
                started,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - translate vendor SDK failures
            translated = self._translate_command_error(exc)
            self._append_audit(owned, request, translated, started)
            return translated

        translated = self._translate_command_result(result)
        self._append_audit(owned, request, translated, started)
        return translated

    async def start_execute(
        self,
        handle: SandboxHandle,
        request: ExecuteRequest,
    ) -> SessionSandboxCommand:
        """Start a recoverable command without waiting for its result.

        The caller must persist ``export_command_resume_token`` before treating
        the command as recoverable.  KsADK deliberately leaves that durable
        journal to the platform control plane.
        """

        if not self._capabilities.command_reconnect:
            raise SandboxPolicyViolation("当前 Sandbox 后端未声明运行中命令重连")
        owned = self._require_open_handle(handle)
        timeout = max(1, math.ceil(request.timeout_seconds))
        await self._renew_lease(owned, ttl_seconds=max(self._lease_ttl_seconds, timeout + 30.0))
        session = owned.session
        if not isinstance(session, ReconnectableCommandSandboxSession):
            raise RuntimeError("后端声明 command_reconnect，但 Session 未实现命令重连协议")
        command_handle = await asyncio.to_thread(
            session.start_command,
            request.command,
            timeout=timeout,
            env=dict(owned.spec.env),
            cwd=owned.spec.workspace_root or None,
        )
        if not isinstance(command_handle, ReconnectableSandboxCommandHandle):
            raise RuntimeError("后端声明 command_reconnect，但命令句柄未提供 process_id")
        # Read once now so a malformed vendor handle fails before the platform
        # records a resume token.
        _ = command_handle.process_id
        return SessionSandboxCommand(
            adapter=self,
            sandbox=owned,
            request=request,
            command_handle=command_handle,
        )

    def export_command_resume_token(
        self,
        command: SessionSandboxCommand,
    ) -> SandboxCommandResumeToken:
        owned = self._require_owned_command(command)
        command_handle = command.command_handle
        if not isinstance(command_handle, ReconnectableSandboxCommandHandle):
            raise RuntimeError("Sandbox 命令句柄未提供 process_id")
        locator = str(owned.session.sandbox_id or "").strip()
        if not locator:
            raise RuntimeError("Sandbox Session 未提供可持久化的 session locator")
        return SandboxCommandResumeToken(
            backend_id=self._capabilities.backend_id,
            handle_id=owned.handle_id,
            session_locator=locator,
            process_id=command_handle.process_id,
        )

    async def reconnect_command(
        self,
        handle: SandboxHandle,
        token: SandboxCommandResumeToken,
        *,
        request: ExecuteRequest,
    ) -> SessionSandboxCommand:
        """Reconnect a command after the Sandbox itself has been reconnected."""

        if not self._capabilities.command_reconnect:
            raise SandboxPolicyViolation("当前 Sandbox 后端未声明运行中命令重连")
        owned = self._require_open_handle(handle)
        locator = str(owned.session.sandbox_id or "").strip()
        if (
            token.backend_id != self._capabilities.backend_id
            or token.handle_id != owned.handle_id
            or token.session_locator != locator
        ):
            raise SandboxPolicyViolation("Sandbox Command Resume Token 与当前会话不匹配")
        timeout = max(1, math.ceil(request.timeout_seconds))
        await self._renew_lease(owned, ttl_seconds=max(self._lease_ttl_seconds, timeout + 30.0))
        session = owned.session
        if not isinstance(session, ReconnectableCommandSandboxSession):
            raise RuntimeError("后端声明 command_reconnect，但 Session 未实现命令重连协议")
        command_handle = await asyncio.to_thread(
            session.connect_command,
            token.process_id,
            timeout=timeout,
        )
        if command_handle.process_id != token.process_id:
            raise SandboxPolicyViolation("Sandbox 命令重连返回了不同的 process_id")
        return SessionSandboxCommand(
            adapter=self,
            sandbox=owned,
            request=request,
            command_handle=command_handle,
        )

    async def wait_command(self, command: SessionSandboxCommand) -> ExecuteResult:
        """Wait for a started or reconnected command with normal audit semantics."""

        owned = self._require_owned_command(command)
        if command.completed:
            raise SandboxPolicyViolation("Sandbox 命令结果已经被消费")
        timeout = max(1, math.ceil(command.request.timeout_seconds))
        await self._renew_lease(owned, ttl_seconds=max(self._lease_ttl_seconds, timeout + 30.0))
        try:
            result = await asyncio.to_thread(command.command_handle.wait)
        except asyncio.CancelledError:
            await self.cancel_command(command)
            raise
        except Exception as exc:  # noqa: BLE001 - translate vendor SDK failures
            translated = self._translate_command_error(exc)
        else:
            translated = self._translate_command_result(result)
        self._append_audit(owned, command.request, translated, command.started_at)
        command.completed = True
        return translated

    async def cancel_command(self, command: SessionSandboxCommand) -> ExecuteResult:
        """Terminate one recoverable command and close its audit lifecycle.

        This is also the fail-closed escape hatch used when a control-plane
        journal cannot durably persist the resume token.  A command that has
        no recovery record must not be allowed to continue as an orphan.
        """

        owned = self._require_owned_command(command)
        if command.completed:
            raise SandboxPolicyViolation("Sandbox 命令结果已经被消费")
        await asyncio.to_thread(command.command_handle.kill)
        translated = ExecuteResult(
            ok=False,
            output="",
            exit_code=130,
            error="sandbox 命令已取消",
        )
        self._append_audit(owned, command.request, translated, command.started_at)
        command.completed = True
        return translated

    @staticmethod
    def _translate_command_error(exc: Exception) -> ExecuteResult:
        if "timeout" in type(exc).__name__.lower():
            return ExecuteResult(
                ok=False,
                output="",
                exit_code=124,
                error=f"sandbox 命令超时: {exc}",
            )
        return ExecuteResult(ok=False, output="", exit_code=1, error=str(exc))

    @staticmethod
    def _translate_command_result(result: SandboxCommandResult) -> ExecuteResult:
        exit_code = int(result.exit_code) if result.exit_code is not None else 1
        error = str(result.stderr or "")
        if exit_code == 124 and "超时" not in error:
            error = f"sandbox 命令超时: {error}".rstrip()
        return ExecuteResult(
            ok=exit_code == 0,
            output=str(result.stdout or ""),
            exit_code=exit_code,
            error=error,
        )

    def _append_audit(
        self,
        handle: SessionSandboxHandle,
        request: ExecuteRequest,
        result: ExecuteResult,
        started: float,
    ) -> None:
        if self._audit is None:
            return
        self._audit.append(
            handle_id=handle.handle_id,
            command=request.command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            output_bytes=len(result.output.encode("utf-8")),
            error=result.error,
            run_id=request.run_id,
        )

    async def _execute_cancellable(
        self,
        owned: SessionSandboxHandle,
        request: ExecuteRequest,
        timeout: int,
    ) -> SandboxCommandResult:
        session = owned.session
        if not isinstance(session, BackgroundCommandSandboxSession):
            raise RuntimeError("后端声明协作取消，但 Session 未实现 start_command")
        command_handle = await asyncio.to_thread(
            session.start_command,
            request.command,
            timeout=timeout,
            env=dict(owned.spec.env),
            cwd=owned.spec.workspace_root or None,
        )
        try:
            return await asyncio.to_thread(command_handle.wait)
        except asyncio.CancelledError:
            # Cancellation is not considered complete until the remote process
            # has received a kill request.
            await asyncio.to_thread(command_handle.kill)
            raise

    async def collect_artifacts(self, handle: SandboxHandle) -> list[str]:
        owned = self._require_open_handle(handle)
        await self._renew_lease(owned)
        if self._artifact_collector is None:
            if not self._capabilities.artifact_collection:
                return list(owned.artifacts)
            session = owned.session
            if not isinstance(session, ArtifactListingSandboxSession):
                raise RuntimeError("后端声明 Artifact 收集，但 Session 未实现 list_files")
            if not owned.spec.workspace_root:
                return []
            artifacts = await asyncio.to_thread(
                session.list_files,
                owned.spec.workspace_root,
                recursive=True,
            )
            owned.artifacts = sorted(str(item) for item in artifacts)
            return list(owned.artifacts)
        artifacts = await asyncio.to_thread(self._artifact_collector, owned.session)
        return sorted(str(item) for item in artifacts)

    async def close(self, handle: SandboxHandle) -> None:
        owned = self._handles.get(id(handle))
        if owned is None:
            handle.closed = True
            return
        try:
            await self._renew_lease(owned)
        except SandboxLeaseConflict:
            self._handles.pop(id(handle), None)
            owned.closed = True
            raise
        self._handles.pop(id(handle), None)
        try:
            await asyncio.to_thread(owned.session.kill)
        finally:
            await self._release_lease(owned.lease)
            owned.closed = True

    async def detach(self, handle: SandboxHandle) -> None:
        """Release this process' lease without terminating the Sandbox.

        This is only for recoverable remote sessions.  It lets a failed
        recovery attempt relinquish fencing ownership so another worker can
        retry without destroying the still-running remote command.
        """

        if not self._capabilities.reconnect or self._lease_provider is None:
            raise SandboxPolicyViolation("detach 需要 reconnect 和 ownership_fencing")
        owned = self._require_open_handle(handle)
        self._handles.pop(id(owned), None)
        try:
            await self._release_lease(owned.lease)
        finally:
            owned.closed = True

    async def _acquire_lease(self, handle_id: str) -> SandboxLeaseGrant | None:
        if self._lease_provider is None:
            return None
        grant = await self._lease_provider.acquire(
            backend_id=self._capabilities.backend_id,
            handle_id=handle_id,
            scope=cast(SandboxLeaseScope, self._lease_scope),
            owner_id=self._lease_owner_id,
            ttl_seconds=self._lease_ttl_seconds,
        )
        self._validate_lease_grant(grant, handle_id=handle_id)
        return grant

    async def _renew_lease(
        self,
        handle: SessionSandboxHandle,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        if self._lease_provider is None:
            return
        if handle.lease is None:
            raise SandboxLeaseConflict("Sandbox Handle 缺少所有权租约")
        renewed = await self._lease_provider.renew(
            handle.lease,
            ttl_seconds=ttl_seconds or self._lease_ttl_seconds,
        )
        self._validate_lease_grant(
            renewed,
            handle_id=handle.handle_id,
            fencing_token=handle.lease.fencing_token,
        )
        handle.lease = renewed

    async def _release_lease(self, lease: SandboxLeaseGrant | None) -> None:
        if self._lease_provider is not None and lease is not None:
            await self._lease_provider.release(lease)

    def _validate_lease_grant(
        self,
        grant: SandboxLeaseGrant,
        *,
        handle_id: str,
        fencing_token: int | None = None,
    ) -> None:
        if (
            grant.backend_id != self._capabilities.backend_id
            or grant.handle_id != handle_id
            or grant.scope != self._lease_scope
            or grant.owner_id != self._lease_owner_id
            or grant.fencing_token <= 0
            or (fencing_token is not None and grant.fencing_token != fencing_token)
        ):
            raise SandboxLeaseConflict("Sandbox Lease Provider 返回了不匹配的租约")

    def _require_open_handle(self, handle: SandboxHandle) -> SessionSandboxHandle:
        owned = self._handles.get(id(handle))
        if owned is None or owned.closed:
            raise SandboxClosedError("Sandbox Handle 已关闭或不属于当前后端")
        return owned

    def _require_owned_command(
        self,
        command: SessionSandboxCommand,
    ) -> SessionSandboxHandle:
        if command._adapter is not self:
            raise SandboxClosedError("Sandbox 命令不属于当前后端 Adapter")
        return self._require_open_handle(command.sandbox)

    def _validate_spec(self, spec: SandboxSpec) -> None:
        if spec.read_only:
            raise SandboxPolicyViolation("通用 SandboxSession 不提供只读挂载保证")
        if spec.network_egress:
            raise SandboxPolicyViolation(
                "通用 SandboxSession 不支持按域名 network_egress allowlist"
            )
        if self._configured_workspace_root and spec.workspace_root:
            if spec.workspace_root != self._configured_workspace_root:
                raise SandboxPolicyViolation("Harness workspace_root 与 SDK Sandbox 后端配置不一致")


def adapt_local_process_backend(
    backend: LocalProcessSandboxBackend,
) -> SessionSandboxBackendAdapter:
    """Adapt the SDK local-process fallback without overclaiming isolation."""

    return SessionSandboxBackendAdapter(
        cast(SdkSandboxBackend, backend),
        capabilities=SandboxBackendCapabilities(
            backend_id=f"sdk-{backend.backend_name}",
            filesystem_isolation=FilesystemIsolation.SCOPED_WORKSPACE,
            # The adapter rejects requested egress, but the host process is
            # not protected by an OS network namespace.
            network_control=NetworkControl.ADMISSION_ONLY,
            process_boundary=True,
            request_timeout=True,
            cooperative_cancellation=False,
            artifact_collection=False,
            # LocalProcessSandboxSession.kill is a no-op and its shared
            # workspace survives close, so cleanup must not be advertised.
            deterministic_cleanup=False,
            execution_audit=False,
            reconnect=False,
        ),
        configured_workspace_root=str(backend.workspace_root),
    )


def adapt_e2b_backend(
    backend: E2BSandboxBackend,
    *,
    audit_log: SandboxAuditLog | None = None,
    lease_provider: SandboxLeaseProvider | None = None,
    lease_scope: SandboxLeaseScope | None = None,
    lease_owner_id: str | None = None,
    lease_ttl_seconds: float = 120.0,
) -> SessionSandboxBackendAdapter:
    """Adapt the current synchronous E2B backend.

    E2B enforces the backend's boolean internet policy, but the current SDK
    contract cannot express a per-domain allowlist.  An internet-enabled
    backend therefore declares ``NONE`` rather than pretending to provide
    fine-grained network control.
    """

    network_control = (
        NetworkControl.NONE if backend.spec.allow_internet_access else NetworkControl.ENFORCED
    )
    return SessionSandboxBackendAdapter(
        cast(SdkSandboxBackend, backend),
        capabilities=SandboxBackendCapabilities(
            backend_id="sdk-e2b",
            filesystem_isolation=FilesystemIsolation.REMOTE_SANDBOX,
            network_control=network_control,
            process_boundary=True,
            request_timeout=True,
            cooperative_cancellation=True,
            artifact_collection=True,
            deterministic_cleanup=True,
            execution_audit=audit_log is not None,
            reconnect=True,
            ownership_fencing=lease_provider is not None,
            command_reconnect=lease_provider is not None,
        ),
        audit_log=audit_log,
        lease_provider=lease_provider,
        lease_scope=lease_scope,
        lease_owner_id=lease_owner_id,
        lease_ttl_seconds=lease_ttl_seconds,
        prepare_workspace=True,
    )


def adapt_sdk_sandbox_backend(
    backend: SdkSandboxBackend,
    *,
    capabilities: SandboxBackendCapabilities | None = None,
    artifact_collector: ArtifactCollector | None = None,
    audit_log: SandboxAuditLog | None = None,
    lease_provider: SandboxLeaseProvider | None = None,
    lease_scope: SandboxLeaseScope | None = None,
    lease_owner_id: str | None = None,
    lease_ttl_seconds: float = 120.0,
) -> SessionSandboxBackendAdapter:
    """Select a truthful built-in profile or require one for custom backends."""

    if isinstance(backend, LocalProcessSandboxBackend):
        if (
            capabilities is not None
            or artifact_collector is not None
            or audit_log is not None
            or lease_provider is not None
        ):
            raise ValueError("内置 local_process profile 不接受能力覆盖")
        return adapt_local_process_backend(backend)
    if isinstance(backend, E2BSandboxBackend):
        if capabilities is not None or artifact_collector is not None:
            raise ValueError("内置 E2B profile 不接受能力覆盖")
        return adapt_e2b_backend(
            backend,
            audit_log=audit_log,
            lease_provider=lease_provider,
            lease_scope=lease_scope,
            lease_owner_id=lease_owner_id,
            lease_ttl_seconds=lease_ttl_seconds,
        )
    if capabilities is None:
        raise ValueError("自定义 SDK SandboxBackend 必须显式提供 capabilities")
    return SessionSandboxBackendAdapter(
        backend,
        capabilities=capabilities,
        artifact_collector=artifact_collector,
        audit_log=audit_log,
        lease_provider=lease_provider,
        lease_scope=lease_scope,
        lease_owner_id=lease_owner_id,
        lease_ttl_seconds=lease_ttl_seconds,
    )


__all__ = [
    "ArtifactCollector",
    "SessionSandboxBackendAdapter",
    "SessionSandboxCommand",
    "SessionSandboxHandle",
    "adapt_e2b_backend",
    "adapt_local_process_backend",
    "adapt_sdk_sandbox_backend",
]
