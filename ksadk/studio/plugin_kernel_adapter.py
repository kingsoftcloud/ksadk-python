"""Studio wiring for the provider-neutral PluginHost Kernel adapter."""

from __future__ import annotations

from typing import Any

from ksadk.plugins.kernel_adapter import PluginKernelAdapter
from ksadk.studio.run_service import StudioRunSpec


class StudioPluginKernelAdapter(PluginKernelAdapter):
    """Bind a Studio Build/session to its profile-fenced provider activation."""

    def __init__(self, plugin_runtime: Any, spec: StudioRunSpec) -> None:
        self._policy_supported = (
            spec.request_config.get("provider_runtime_type") == "harness"
            and getattr(plugin_runtime, "execution_policy_resolver", None) is not None
        )

        async def bind_delegate(session_id: str):  # type: ignore[no-untyped-def]
            return await plugin_runtime.kernel_adapter(spec, session_id=session_id)

        async def release_binding(session_id: str) -> None:
            await plugin_runtime.close_session_if_dynamic(spec, session_id)

        super().__init__(
            runtime_type=spec.launch_context.runtime_type,
            bind_delegate=bind_delegate,
            release_binding=release_binding,
        )

    def capabilities(self):  # type: ignore[no-untyped-def]
        # Studio wires a durable Workspace store before activating Harness.
        if self._delegate is None and self._runtime_type == "harness":
            from ksadk.harness.managed_runtime import managed_harness_capabilities
            from ksadk.kernel.contracts import RuntimeCapability

            matrix = managed_harness_capabilities(durable=True)
            return matrix.model_copy(
                update={
                    "execution_policy": RuntimeCapability(
                        supported=self._policy_supported,
                        mode="native" if self._policy_supported else "unavailable",
                        reason=None if self._policy_supported else "execution_policy_unavailable",
                    ),
                }
            )
        return super().capabilities()


__all__ = ["StudioPluginKernelAdapter"]
