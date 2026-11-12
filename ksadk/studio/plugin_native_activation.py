"""Bind per-turn Native options without replacing active or foreign sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.harness.native_runtime import NativeRuntimeAdapter
from ksadk.plugins.host import PluginActivationSession, PluginHostError
from ksadk.studio.errors import StudioError
from ksadk.studio.run_service import StudioRunSpec
from ksadk.tools.gateway import normalize_tool_approval_mode


@dataclass
class _NativeActivation:
    claims: dict[str, str]
    activation: PluginActivationSession
    adapter: NativeRuntimeAdapter

class StudioNativeActivationMixin:
    async def _native_activation_claims(
        self, spec: StudioRunSpec, session_id: str
    ) -> dict[str, str] | None:
        if (
            self._harness_engine_selector is None
            or spec.request_config.get("provider_runtime_type") != "harness"
            or (self._harness_native_session_enabled is not None
                and not self._harness_native_session_enabled(spec.agent_id, session_id))
        ):
            return None
        session = await self._session_service.get_session_metadata(session_id)
        if session is None or session.agent_id != spec.agent_id or not session.user_id:
            raise PluginHostError(
                "harness_native_session_scope_unavailable",
                "Native Harness session identity is unavailable or differs from the Build",
            )
        if not self._native_tenant_id or not self._native_workspace_id:
            raise PluginHostError(
                "harness_native_host_scope_unavailable",
                "Native Harness host scope is unavailable",
            )
        effort = spec.request_config.get("effort")
        if effort is not None and (
            type(effort) is not str or effort not in {"", "low", "medium", "high", "xhigh", "max"}
        ):
            raise PluginHostError(
                "harness_native_reasoning_effort_invalid",
                "Native Harness reasoning effort must be low, medium, high, xhigh or max",
            )
        return {
            "tenant_id": self._native_tenant_id,
            "workspace_id": self._native_workspace_id,
            "user_id": session.user_id,
            "agent_id": session.agent_id,
            "session_id": session_id,
            "build_id": spec.build_id,
            "manifest_sha256": spec.manifest_sha256,
            "model": str(spec.model or ""),
            "project_dir": str(spec.launch_context.project_dir.resolve()),
            "launch_config_digest": studio_mapping_digest(spec.launch_context.config),
            "request_config_digest": studio_mapping_digest(spec.request_config),
            **({"reasoning_effort": effort} if effort else {}),
            "tool_approval_mode": normalize_tool_approval_mode(
                spec.request_config.get("tool_approval_mode")
            ),
        }

    async def kernel_adapter(
        self,
        spec: StudioRunSpec,
        *,
        session_id: str,
    ) -> Any:
        """Bind a Scheduler run to the provider-owned activation adapter."""

        if spec.plugin_bundle_root is None:
            raise StudioError(
                "PLUGIN_RUNTIME_UNAVAILABLE",
                "Studio Build 没有 PluginHost Bundle",
                status_code=409,
            )
        entry = await self._host_for(spec.plugin_bundle_root)
        if entry.agent_id != spec.agent_id:
            raise PluginHostError(
                "plugin_bundle_agent_mismatch",
                "Studio run Agent does not match its immutable PluginHost Bundle",
            )
        lock = self._activation_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            claims = await self._native_activation_claims(spec, session_id)
            key = (entry.bundle.bundle_digest, session_id)
            previous = self._native_activations.get(key)
            if (previous is not None and not previous.activation.closed
                    and claims != previous.claims):
                immutable = (
                    "tenant_id", "workspace_id", "user_id", "agent_id", "session_id",
                    "build_id", "manifest_sha256", "project_dir", "launch_config_digest",
                )
                if claims is None or any(
                    previous.claims.get(name) != claims.get(name) for name in immutable
                ):
                    raise PluginHostError(
                        "harness_native_activation_identity_changed",
                        "Native activation identity changed; use a compatible session",
                    )
                if not previous.adapter.can_replace_session_activation(session_id):
                    raise PluginHostError(
                        "harness_native_activation_active",
                        "Native session activation is still active; finish its current turn first",
                    )
                await previous.activation.close()
                self._native_activations.pop(key, None)
            self._resource_write_modes[session_id] = str(
                spec.request_config.get("tool_approval_mode") or ""
            )
            try:
                activation = await entry.host.open_activation(
                    entry.bundle, activation_key=session_id, activation_claims=claims,
                )
            except BaseException:
                self._resource_write_modes.pop(session_id, None)
                raise
            try:
                adapter = await activation.runtime_adapter()
                if isinstance(adapter, NativeRuntimeAdapter) and claims is not None:
                    self._native_activations[key] = _NativeActivation(
                        dict(claims), activation, adapter,
                    )
                return adapter
            except BaseException:
                try:
                    await activation.close()
                finally:
                    self._resource_write_modes.pop(session_id, None)
                raise

    async def close_session(self, session_id: str) -> None:
        await self._close_session(session_id, preserve_active=False)

    async def close_failed_session(self, session_id: str) -> None:
        """A rejected competing start does not own an existing Native turn."""
        await self._close_session(session_id, preserve_active=True)

    async def _close_session(self, session_id: str, *, preserve_active: bool) -> None:
        async with self._lock:
            entries = tuple(self._hosts.values())
        lock = self._activation_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            retained = False
            for entry in entries:
                key = (entry.bundle.bundle_digest, session_id)
                binding = self._native_activations.get(key)
                if (preserve_active and binding is not None and not binding.activation.closed
                        and binding.adapter.has_pending_session_work(session_id)):
                    retained = True
                    continue
                await entry.host.close_activation(session_id)
                self._native_activations.pop(key, None)
            if not retained:
                self._resource_write_modes.pop(session_id, None)
