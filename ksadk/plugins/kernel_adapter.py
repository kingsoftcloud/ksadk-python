"""Provider-neutral AgentKernel adapter for PluginHost runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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

BindDelegate = Callable[[str], Awaitable[RuntimeAdapter]]
ReleaseBinding = Callable[[str], Awaitable[None]]

_PROVIDER_RUNTIME_TYPE_KEY = "_ksadk_plugin_provider_runtime_type"


class _PluginKernelRuntime(BaseRuntime):
    def __init__(self, runtime_type: str) -> None:
        self.runtime_type = runtime_type

    def native_capabilities(self) -> dict[str, Any]:
        return {
            "provider_owned": True,
            "runtime_adapter": True,
            "session_continuity": {"durable": False, "scope": "process"},
        }


class PluginKernelAdapter(RuntimeAdapter):
    """Expose a lazily activated Agent Provider through the Kernel contract."""

    def __init__(
        self,
        *,
        runtime_type: str,
        bind_delegate: BindDelegate,
        release_binding: ReleaseBinding,
    ) -> None:
        super().__init__(_PluginKernelRuntime(runtime_type))
        self._runtime_type = runtime_type
        self._bind_delegate_callback = bind_delegate
        self._release_binding_callback = release_binding
        self._delegate: RuntimeAdapter | None = None
        self._native_handle: RunHandle | None = None

    async def start(self, request: StartRequest) -> RunHandle:
        try:
            delegate = await self._bind_delegate(request.session_id)
            metadata = dict(request.metadata)
            if not metadata.get("invocation_id") and metadata.get("run_id"):
                metadata["invocation_id"] = metadata["run_id"]
            native_handle = await delegate.start(
                request.model_copy(update={"metadata": metadata})
            )
            self._native_handle = native_handle
            return self._outer_handle(native_handle)
        except BaseException:
            await self._release_failed_binding(request.session_id)
            raise

    def stream(self, handle: RunHandle):  # type: ignore[no-untyped-def]
        return self._require_delegate().stream(self._provider_handle(handle))

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return await self._require_delegate().cancel(self._provider_handle(handle))

    async def pause(self, handle: RunHandle) -> PauseResult:
        return await self._require_delegate().pause(self._provider_handle(handle))

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        await self._require_delegate().submit(self._provider_handle(handle), payload)

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        native_handle = await self._require_delegate().resume(
            self._provider_handle(handle), target, payload
        )
        self._native_handle = native_handle
        return self._outer_handle(native_handle)

    async def attach(self, handle: RunHandle) -> RunHandle:
        try:
            delegate = await self._bind_delegate(handle.session_id)
            native_handle = await delegate.attach(self._provider_handle(handle))
            self._native_handle = native_handle
            return self._outer_handle(native_handle)
        except BaseException:
            await self._release_failed_binding(handle.session_id)
            raise

    async def steer(self, handle: RunHandle, payload: SteerPayload) -> None:
        await self._require_delegate().steer(self._provider_handle(handle), payload)

    async def inject(self, handle: RunHandle, payload: InjectPayload) -> None:
        await self._require_delegate().inject(self._provider_handle(handle), payload)

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return await self._require_delegate().checkpoint(self._provider_handle(handle))

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        try:
            delegate = await self._bind_delegate(handle.session_id)
            native_handle = await delegate.durable_restore(
                self._provider_handle(handle)
            )
            self._native_handle = native_handle
            return self._outer_handle(native_handle)
        except BaseException:
            await self._release_failed_binding(handle.session_id)
            raise

    def is_handle_attached(self, handle: RunHandle) -> bool:
        return self._delegate is not None and self._delegate.is_handle_attached(
            self._provider_handle(handle)
        )

    async def close(self, handle: RunHandle) -> None:
        try:
            await self._require_delegate().close(self._provider_handle(handle))
        finally:
            await self._release_binding_callback(handle.session_id)

    def capabilities(self):  # type: ignore[no-untyped-def]
        # Provider activation is asynchronous, so admission is deliberately
        # conservative until a session has bound its delegate.
        if self._delegate is None:
            return super().capabilities()
        return self._delegate.capabilities()

    def _require_delegate(self) -> RuntimeAdapter:
        if self._delegate is None:
            raise UnsupportedControlError(
                "PluginHost RuntimeAdapter has not started a provider activation"
            )
        return self._delegate

    async def _bind_delegate(self, session_id: str) -> RuntimeAdapter:
        delegate = await self._bind_delegate_callback(session_id)
        if not isinstance(delegate, RuntimeAdapter):
            raise RuntimeError("AgentProvider returned an invalid RuntimeAdapter")
        self._delegate = delegate
        return delegate

    def _outer_handle(self, handle: RunHandle) -> RunHandle:
        native_ref = dict(handle.native_ref)
        native_ref[_PROVIDER_RUNTIME_TYPE_KEY] = handle.runtime_type
        return handle.model_copy(
            update={"runtime_type": self._runtime_type, "native_ref": native_ref}
        )

    def _provider_handle(self, handle: RunHandle) -> RunHandle:
        cached = self._native_handle
        if (
            cached is not None
            and cached.run_id == handle.run_id
            and cached.session_id == handle.session_id
        ):
            return cached
        native_ref = dict(handle.native_ref)
        runtime_type = str(native_ref.pop(_PROVIDER_RUNTIME_TYPE_KEY, "")).strip()
        if not runtime_type:
            runtime_type = self._require_delegate().runtime.runtime_type
        return handle.model_copy(
            update={"runtime_type": runtime_type, "native_ref": native_ref}
        )

    async def _release_failed_binding(self, session_id: str) -> None:
        """Close a partially opened activation even under cancellation."""

        self._delegate = None
        self._native_handle = None
        cleanup = asyncio.create_task(self._release_binding_callback(session_id))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        try:
            cleanup.result()
        except Exception:
            # Preserve the provider/delegate failure. Its owner still gets the
            # terminal suspend path for disposing a host that could not close.
            pass
        if interrupted:
            raise asyncio.CancelledError


__all__ = ["BindDelegate", "PluginKernelAdapter", "ReleaseBinding"]
