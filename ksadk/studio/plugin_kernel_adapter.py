"""Studio wiring for the provider-neutral PluginHost Kernel adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

from ksadk.harness.native_runtime import NativeRuntimeAdapter
from ksadk.plugins.kernel_adapter import PluginKernelAdapter
from ksadk.runtime import RunHandle, RuntimeAdapter, StartRequest
from ksadk.studio.run_service import StudioRunSpec


class StudioPluginKernelAdapter(PluginKernelAdapter):
    """Bind a Studio Build/session to its profile-fenced provider activation."""

    def __init__(self, plugin_runtime: Any, spec: StudioRunSpec) -> None:
        self._studio_spec = spec
        self._native_admission: (
            Callable[
                [NativeRuntimeAdapter, StartRequest], StartRequest | Awaitable[StartRequest]
            ] | None
        ) = None
        self._policy_supported = (
            spec.request_config.get("provider_runtime_type") == "harness"
            and getattr(plugin_runtime, "execution_policy_resolver", None) is not None
        )

        async def bind_delegate(session_id: str):  # type: ignore[no-untyped-def]
            return await plugin_runtime.kernel_adapter(spec, session_id=session_id)

        async def release_binding(session_id: str) -> None:
            await plugin_runtime.close_session_if_dynamic(spec, session_id)

        async def release_failed_binding(session_id: str) -> None:
            close_session = getattr(plugin_runtime, "close_failed_session", None)
            if not callable(close_session):
                close_session = getattr(plugin_runtime, "close_session", None)
            if callable(close_session):
                await close_session(session_id)
            else:
                await release_binding(session_id)

        super().__init__(
            runtime_type=spec.launch_context.runtime_type,
            bind_delegate=bind_delegate,
            release_binding=release_binding,
            release_failed_binding=release_failed_binding,
        )

    def bind_native_admission(
        self, admission: Callable[
            [NativeRuntimeAdapter, StartRequest], StartRequest | Awaitable[StartRequest]
        ]
    ) -> None:
        if self._delegate is not None or self._native_admission is not None:
            raise RuntimeError("Native admission must be bound once before start")
        self._native_admission = admission

    async def _prepare_start_request(
        self, delegate: RuntimeAdapter, request: StartRequest
    ) -> StartRequest:
        if isinstance(delegate, NativeRuntimeAdapter):
            if self._native_admission is None:
                raise ValueError("native_studio_admission_unavailable")
            admitted = self._native_admission(delegate, request)
            return await admitted if isawaitable(admitted) else admitted
        return request

    async def _prepare_restore_handle(
        self, delegate: RuntimeAdapter, handle: RunHandle
    ) -> RunHandle:
        if isinstance(delegate, NativeRuntimeAdapter):
            spec = self._studio_spec
            delegate.admit_studio_build(
                build_id=spec.build_id,
                manifest_sha256=spec.manifest_sha256,
                model=spec.model,
                request_config=spec.request_config,
                project_dir=spec.launch_context.project_dir,
                launch_config=spec.launch_context.config,
                require_pin=True,
            )
        return handle

    async def prepare_native_restore(self, handle: RunHandle, *, expected_goal: str) -> None:
        """Validate a cold Native ledger through its original locked activation."""
        try:
            delegate = await self._bind_delegate(handle.session_id)
            if not isinstance(delegate, NativeRuntimeAdapter):
                raise ValueError("native_studio_restore_provider_changed")
            matrix = delegate.capabilities()
            if not (matrix.attach.supported and matrix.durable_restore.supported):
                raise ValueError("native_studio_durable_restore_unavailable")
            await self._prepare_restore_handle(delegate, handle)
            snapshot = delegate.snapshot(self._provider_handle(handle))
            if not snapshot.matches_initial_goal(expected_goal):
                raise ValueError("native_studio_restore_goal_mismatch")
            if delegate.session_parent_run_id(handle.session_id) != handle.run_id:
                raise ValueError("native_studio_restore_session_head_mismatch")
        except BaseException:
            await self._release_failed_binding(handle.session_id)
            raise

    async def prepare_native_notification(
        self, handle: RunHandle, *, expected_adapter: NativeRuntimeAdapter,
    ) -> RunHandle:
        """Give an already admitted wake the same provider envelope as user runs."""
        delegate = await self._bind_delegate(handle.session_id)
        if delegate is not expected_adapter:
            raise ValueError("native_notification_activation_changed")
        await self._prepare_restore_handle(delegate, handle)
        snapshot = delegate.snapshot(handle)
        if snapshot.notification_command is None:
            raise ValueError("native_notification_command_missing")
        return self._outer_handle(handle)

    def capabilities(self):  # type: ignore[no-untyped-def]
        # Studio wires a durable Workspace store before activating Harness.
        if self._delegate is None and self._runtime_type == "harness":
            from ksadk.harness.runtime_capabilities import managed_harness_capabilities

            return managed_harness_capabilities(
                durable=True, execution_policy=self._policy_supported,
            )
        return super().capabilities()


__all__ = ["StudioPluginKernelAdapter"]
