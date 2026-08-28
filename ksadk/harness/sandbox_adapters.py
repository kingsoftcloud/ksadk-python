"""Adapters from the SDK sandbox sessions to the Harness async contract.

``ksadk.sandbox`` deliberately exposes a small synchronous session API used by
the SDK and Skill Runtime.  The Harness owns a different, asynchronous
lifecycle contract.  This module bridges the two without claiming guarantees
that the wrapped backend cannot provide.
"""

from __future__ import annotations

import asyncio
import math
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
from ksadk.sandbox.base import SandboxBackend as SdkSandboxBackend
from ksadk.sandbox.base import SandboxSession

ArtifactCollector = Callable[[SandboxSession], Iterable[str]]


class SessionSandboxHandle(SandboxHandle):
    """Harness handle backed by one SDK ``SandboxSession``."""

    def __init__(self, spec: SandboxSpec, session: SandboxSession) -> None:
        super().__init__(spec)
        self.session = session


class SessionSandboxBackendAdapter:
    """Run a synchronous SDK sandbox backend behind the Harness contract.

    Cancellation is intentionally *not* advertised: cancelling
    ``asyncio.to_thread`` only cancels the waiter and does not prove that the
    SDK command stopped.  Likewise, artifact collection is only advertised
    when the caller provides an explicit collector.
    """

    def __init__(
        self,
        backend: SdkSandboxBackend,
        *,
        capabilities: SandboxBackendCapabilities,
        artifact_collector: ArtifactCollector | None = None,
        configured_workspace_root: str | None = None,
    ) -> None:
        if capabilities.cooperative_cancellation:
            raise ValueError("同步 SandboxSession 适配器不能声明协作取消")
        if capabilities.artifact_collection != (artifact_collector is not None):
            raise ValueError("artifact_collection 声明必须与 artifact_collector 一致")
        self._backend = backend
        self._capabilities = capabilities
        self._artifact_collector = artifact_collector
        self._configured_workspace_root = configured_workspace_root
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
        handle = SessionSandboxHandle(spec, session)
        # Keep the id generated before the potentially remote create call as
        # the stable correlation id passed to the SDK backend.
        handle.handle_id = provisional.handle_id
        self._handles[id(handle)] = handle
        return handle

    async def execute(
        self, handle: SandboxHandle, request: ExecuteRequest
    ) -> ExecuteResult:
        owned = self._require_open_handle(handle)
        timeout = max(1, math.ceil(request.timeout_seconds))
        try:
            result = await asyncio.to_thread(
                owned.session.run_command,
                request.command,
                timeout=timeout,
                env=dict(owned.spec.env),
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

    async def collect_artifacts(self, handle: SandboxHandle) -> list[str]:
        owned = self._require_open_handle(handle)
        if self._artifact_collector is None:
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
                raise SandboxPolicyViolation(
                    "Harness workspace_root 与 SDK Sandbox 后端配置不一致"
                )


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
        NetworkControl.NONE
        if backend.spec.allow_internet_access
        else NetworkControl.ENFORCED
    )
    return SessionSandboxBackendAdapter(
        cast(SdkSandboxBackend, backend),
        capabilities=SandboxBackendCapabilities(
            backend_id="sdk-e2b",
            filesystem_isolation=FilesystemIsolation.REMOTE_SANDBOX,
            network_control=network_control,
            process_boundary=True,
            request_timeout=True,
            cooperative_cancellation=False,
            artifact_collection=False,
            deterministic_cleanup=True,
            execution_audit=False,
            reconnect=False,
        ),
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
