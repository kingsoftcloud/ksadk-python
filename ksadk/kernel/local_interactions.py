"""App-owned, lazy Interaction/v1 composition for native local Web debugging."""

from __future__ import annotations

import asyncio
import os

from ksadk.kernel import ingress
from ksadk.kernel.bootstrap import (
    AgentKernelRuntimeConfig,
    build_agent_kernel_runtime,
    get_agent_kernel_runtime,
    set_agent_kernel_runtime,
)
from ksadk.runtime.factory import kernel_start_request_defaults
from ksadk.sessions.local_service import LocalSessionService


class LocalInteractionRuntime:
    """Discover on execution, not on cold UI/history reads; never enable hosted admission."""

    def __init__(
        self,
        *,
        provider,
        launch_context,
        session_service,
        agent_name,
        on_ready=None,
        adapter_owned=True,
    ):
        self.provider = provider
        self.launch = launch_context
        self.session_service = session_service
        self.agent_name = agent_name
        self.on_ready = on_ready
        self.adapter_owned = adapter_owned
        self.runtime = None
        self.adapter = None
        self._lock = asyncio.Lock()
        self._unavailable = False
        self._closed = False
        self._stopped = False
        self.prepared = False
        self.oauth = None

    def _eligible(self):
        return (
            ingress.authority_mode() == "local"
            and self.launch.deployment_mode == "local"
            and self.launch.runtime_type == "langgraph"
            and not any(os.getenv(key, "").strip() for key in (
                "KSADK_AGENT_KERNEL", "AGENT_KERNEL_ENABLED",
            ))
            and isinstance(self.session_service(), LocalSessionService)
        )

    async def _prepare_adapter(self):
        adapter = self.adapter if self.adapter is not None else self.provider()
        adopt = getattr(adapter, "adopt_app_lifetime", None)
        if callable(adopt):
            adopt()
        self.adapter = adapter
        if self.oauth is not None:
            await self.oauth.prepare_adapter(adapter)
        await adapter.preflight()
        capabilities = adapter.capabilities()
        if not (
            getattr(adapter, "confirms_native_submissions", False)
            and capabilities.submit_interaction.supported
            and capabilities.resume.supported
        ):
            self._unavailable = True
            return None
        if not self.prepared:
            self.prepared = True
            if self.on_ready is not None:
                self.on_ready()
        return adapter

    async def prepare(self):
        """Prepare actual saver capability without a Kernel, Run, or Agent execution."""
        async with self._lock:
            if self._closed or self._unavailable or not self._eligible():
                return None
            return await self._prepare_adapter()

    async def ensure(self):
        async with self._lock:
            if self._closed:
                raise RuntimeError("Local interaction runtime is closed")
            if self.runtime is not None or self._unavailable:
                return self.runtime
            if not self._eligible():
                return None
            service = self.session_service()
            if get_agent_kernel_runtime() is not None or ingress.get_agent_kernel() is not None:
                raise RuntimeError("Another Kernel already owns the process execution scope")
            try:
                adapter = await self._prepare_adapter()
                if adapter is None:
                    return None
                defaults = kernel_start_request_defaults(self.launch)
                defaults.setdefault("agent_id", self.agent_name)
                runtime = build_agent_kernel_runtime(
                    AgentKernelRuntimeConfig(
                        agent_instance_id=str(
                            self.launch.config.get("agent_instance_id")
                            or os.getenv("AGENT_INSTANCE_ID")
                            or "local-agent"
                        ),
                        tenant_id=str(self.launch.config.get("tenant_id") or "local"),
                        driver="sqlite",
                        dsn=str(service.db_path),
                        session_service=service,
                        adapter_provider=lambda: adapter,
                        capabilities=adapter.capabilities,
                        launch_context=self.launch,
                        start_request_defaults=defaults,
                        local_interactions=True,
                    )
                )
                self.runtime = runtime
                try:
                    if self.oauth is not None:
                        await self.oauth.start(runtime)
                    await runtime.start()
                except BaseException:
                    await runtime.close()
                    if self.oauth is not None:
                        await self.oauth.close()
                    self.runtime = None
                    raise
                ingress.set_agent_kernel(runtime.kernel)
                set_agent_kernel_runtime(runtime)
                if self.on_ready is not None:
                    self.on_ready()
                return runtime
            except BaseException:
                # Keep the app-owned probe for a retry or normal shutdown;
                # closing a cached adapter here would poison its next attempt.
                raise

    async def _close_adapter(self):
        adapter, self.adapter = self.adapter, None
        close = getattr(adapter, "close_app_resources", None)
        if self.adapter_owned and callable(close):
            await close()

    async def stop(self):
        async with self._lock:
            self._closed = True
            try:
                if self.runtime is not None and not self._stopped:
                    await self.runtime.close()
                    self._stopped = True
                if self.oauth is not None:
                    await self.oauth.close()
            finally:
                if get_agent_kernel_runtime() is self.runtime and self.runtime is not None:
                    from ksadk.kernel.bootstrap import clear_agent_kernel_runtime

                    clear_agent_kernel_runtime()
                    ingress.clear_agent_kernel()

    async def close(self):
        await self.stop()
        async with self._lock:
            await self._close_adapter()


__all__ = ["LocalInteractionRuntime"]
