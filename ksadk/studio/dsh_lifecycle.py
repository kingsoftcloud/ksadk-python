"""DSH startup diagnostics and companion lifecycle shared by Studio entry points."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ksadk.plugins.companions import CompanionError, DshCompanionDefinition, DshPluginCompanionManager
from ksadk.plugins.providers.dsh_capabilities import DshProfileCapabilityHost, DshMcpConnectorLease
from ksadk.studio.plugin_lifecycle import RECOVERY_URL, lifecycle_error, lifecycle_failure


class DshStartupLifecycle:
    @property
    def startup_status(self) -> dict[str, Any]:
        """Inspect lifecycle without booting Core or resolving credentials."""
        return {
            "state": "closed" if self._closed else "failed" if self._startup_failure else (
                "ready" if self._lease is not None else "not_ready"
            ),
            "stage": self._startup_stage,
            "failure": dict(self._startup_failure) if self._startup_failure else None,
            "generationId": self._generation_id,
            "recoveryUrl": RECOVERY_URL,
        }


    def configure_companions(self, definitions: Sequence[DshCompanionDefinition]) -> None:
        """Configure trusted lifecycle callbacks before this Profile starts."""
        if self._host is not None or self._closed:
            raise CompanionError("COMPANION_CONFIGURATION_BUSY")
        if any(item.profile != self._profile for item in definitions):
            raise CompanionError("COMPANION_PROFILE_MISMATCH")
        self._companion_definitions = tuple(definitions)


    @property
    def companion_manager(self) -> DshPluginCompanionManager | None:
        return self._companion_manager


    async def _ensure_ready_locked(
        self,
    ) -> tuple[DshProfileCapabilityHost, DshMcpConnectorLease]:
        try:
            result = await self._ensure_ready_generation_locked()
        except Exception as error:
            self._startup_failure = lifecycle_failure(error, self._startup_stage)
            raise lifecycle_error(error, self._startup_stage) from error
        self._startup_failure = None
        self._startup_stage = "ready"
        return result


    async def _close_companions_locked(self) -> None:
        manager, self._companion_manager = self._companion_manager, None
        if manager is not None:
            await manager.close()
