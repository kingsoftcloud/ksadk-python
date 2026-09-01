"""AgentKernel adapter for an immutable Studio PluginHost Build."""

from __future__ import annotations

from typing import Any

from ksadk.kernel.contracts import InjectPayload, SteerPayload
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointDescriptor,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.studio.run_service import StudioRunSpec


class _PluginKernelRuntime(BaseRuntime):
    def __init__(self, runtime_type: str) -> None:
        self.runtime_type = runtime_type

    def native_capabilities(self) -> dict[str, Any]:
        return {
            "provider_owned": True,
            "runtime_adapter": True,
            "session_continuity": {"durable": False, "scope": "process"},
        }


class StudioPluginKernelAdapter(RuntimeAdapter):
    """Lazily bind one Worker run to its profile-fenced provider activation."""

    def __init__(self, plugin_runtime: Any, spec: StudioRunSpec) -> None:
        super().__init__(_PluginKernelRuntime(spec.launch_context.runtime_type))
        self._plugin_runtime = plugin_runtime
        self._spec = spec
        self._delegate: RuntimeAdapter | None = None

    async def start(self, request: StartRequest) -> RunHandle:
        delegate = await self._plugin_runtime.kernel_adapter(
            self._spec,
            session_id=request.session_id,
        )
        if not isinstance(delegate, RuntimeAdapter):
            raise RuntimeError("AgentProvider returned an invalid RuntimeAdapter")
        self._delegate = delegate
        metadata = dict(request.metadata)
        if not metadata.get("invocation_id") and metadata.get("run_id"):
            metadata["invocation_id"] = metadata["run_id"]
        return await delegate.start(request.model_copy(update={"metadata": metadata}))

    def stream(self, handle: RunHandle):  # type: ignore[no-untyped-def]
        return self._require_delegate().stream(handle)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return await self._require_delegate().cancel(handle)

    async def pause(self, handle: RunHandle) -> PauseResult:
        return await self._require_delegate().pause(handle)

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        await self._require_delegate().submit(handle, payload)

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        return await self._require_delegate().resume(handle, target, payload)

    async def attach(self, handle: RunHandle) -> RunHandle:
        delegate = await self._plugin_runtime.kernel_adapter(
            self._spec,
            session_id=handle.session_id,
        )
        if not isinstance(delegate, RuntimeAdapter):
            raise RuntimeError("AgentProvider returned an invalid RuntimeAdapter")
        self._delegate = delegate
        return await delegate.attach(handle)

    async def steer(self, handle: RunHandle, payload: SteerPayload) -> None:
        await self._require_delegate().steer(handle, payload)

    async def inject(self, handle: RunHandle, payload: InjectPayload) -> None:
        await self._require_delegate().inject(handle, payload)

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return await self._require_delegate().checkpoint(handle)

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        delegate = await self._plugin_runtime.kernel_adapter(
            self._spec,
            session_id=handle.session_id,
        )
        if not isinstance(delegate, RuntimeAdapter):
            raise RuntimeError("AgentProvider returned an invalid RuntimeAdapter")
        self._delegate = delegate
        return await delegate.durable_restore(handle)

    def is_handle_attached(self, handle: RunHandle) -> bool:
        return self._delegate is not None and self._delegate.is_handle_attached(handle)

    async def close(self, handle: RunHandle) -> None:
        await self._require_delegate().close(handle)

    def capabilities(self):  # type: ignore[no-untyped-def]
        # Before ``start`` the provider activation is async and not yet bound.
        # Keep admission conservative; enqueue remains available and the live
        # execution delegates supported controls after binding.
        if self._delegate is None:
            return super().capabilities()
        return self._delegate.capabilities()

    def _require_delegate(self) -> RuntimeAdapter:
        if self._delegate is None:
            raise UnsupportedControlError(
                "PluginHost RuntimeAdapter has not started a provider activation"
            )
        return self._delegate


__all__ = ["StudioPluginKernelAdapter"]
