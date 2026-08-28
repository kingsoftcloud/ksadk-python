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
from collections.abc import Callable, Iterable
from typing import cast

from ksadk.harness.sandbox_backend import (
    ExecuteRequest,
    ExecuteResult,
    FilesystemIsolation,
    NetworkControl,
    SandboxBackendCapabilities,
    SandboxClosedError,
    SandboxHandle,
    SandboxPolicyViolation,
    SandboxSpec,
)
from ksadk.sandbox.backends.e2b import E2BSandboxBackend
from ksadk.sandbox.backends.local_process import LocalProcessSandboxBackend
from ksadk.sandbox.base import (
    ArtifactListingSandboxSession,
    BackgroundCommandSandboxSession,
    SandboxCommandResult,
    SandboxSession,
)
from ksadk.sandbox.base import SandboxBackend as SdkSandboxBackend

ArtifactCollector = Callable[[SandboxSession], Iterable[str]]


class SessionSandboxHandle(SandboxHandle):
    """Harness handle backed by one SDK ``SandboxSession``."""

    def __init__(self, spec: SandboxSpec, session: SandboxSession) -> None:
        super().__init__(spec)
        self.session = session


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
        configured_workspace_root: str | None = None,
        prepare_workspace: bool = False,
    ) -> None:
        if artifact_collector is not None and not capabilities.artifact_collection:
            raise ValueError("提供 artifact_collector 时必须声明 artifact_collection")
        self._backend = backend
        self._capabilities = capabilities
        self._artifact_collector = artifact_collector
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
        self._handles[id(handle)] = handle
        return handle

    async def execute(self, handle: SandboxHandle, request: ExecuteRequest) -> ExecuteResult:
        owned = self._require_open_handle(handle)
        timeout = max(1, math.ceil(request.timeout_seconds))
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
        except Exception as exc:  # noqa: BLE001 - translate vendor SDK failures
            if "timeout" in type(exc).__name__.lower():
                return ExecuteResult(
                    ok=False,
                    output="",
                    exit_code=124,
                    error=f"sandbox 命令超时: {exc}",
                )
            return ExecuteResult(ok=False, output="", exit_code=1, error=str(exc))

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
        owned = self._handles.pop(id(handle), None)
        if owned is None:
            handle.closed = True
            return
        try:
            await asyncio.to_thread(owned.session.kill)
        finally:
            owned.closed = True

    def _require_open_handle(self, handle: SandboxHandle) -> SessionSandboxHandle:
        owned = self._handles.get(id(handle))
        if owned is None or owned.closed:
            raise SandboxClosedError("Sandbox Handle 已关闭或不属于当前后端")
        return owned

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


def adapt_e2b_backend(backend: E2BSandboxBackend) -> SessionSandboxBackendAdapter:
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
            execution_audit=False,
            reconnect=False,
        ),
        prepare_workspace=True,
    )


def adapt_sdk_sandbox_backend(
    backend: SdkSandboxBackend,
    *,
    capabilities: SandboxBackendCapabilities | None = None,
    artifact_collector: ArtifactCollector | None = None,
) -> SessionSandboxBackendAdapter:
    """Select a truthful built-in profile or require one for custom backends."""

    if isinstance(backend, LocalProcessSandboxBackend):
        if capabilities is not None or artifact_collector is not None:
            raise ValueError("内置 local_process profile 不接受能力覆盖")
        return adapt_local_process_backend(backend)
    if isinstance(backend, E2BSandboxBackend):
        if capabilities is not None or artifact_collector is not None:
            raise ValueError("内置 E2B profile 不接受能力覆盖")
        return adapt_e2b_backend(backend)
    if capabilities is None:
        raise ValueError("自定义 SDK SandboxBackend 必须显式提供 capabilities")
    return SessionSandboxBackendAdapter(
        backend,
        capabilities=capabilities,
        artifact_collector=artifact_collector,
    )


__all__ = [
    "ArtifactCollector",
    "SessionSandboxBackendAdapter",
    "SessionSandboxHandle",
    "adapt_e2b_backend",
    "adapt_local_process_backend",
    "adapt_sdk_sandbox_backend",
]
