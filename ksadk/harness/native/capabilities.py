"""Host-admitted capability views and per-call fences, independent of plugins.

This is an execution projection, never an install format or lifecycle owner.
The authority callback must query the owning host for this exact activation;
the currently active *new* profile is not the status of an older retained run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import Field, model_validator

from .adapters import StrictToolExecutor, ToolAuthorizer, ToolBinding, _snapshot
from .ports import ToolDefinition, ToolPort, ToolRequest, ToolResult
from .state import Contract, RunScope

_SHA256 = r"^sha256:[0-9a-f]{64}$"


class CapabilityUnavailable(ValueError):
    """Safe, stable admission failure without plugin exception/credential text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ActivationPin(Contract):
    scope: RunScope
    run_id: str = Field(min_length=1)
    profile_digest: str = Field(pattern=_SHA256)
    plugin_lock_digest: str = Field(pattern=_SHA256)
    activation_key: str = Field(min_length=1)
    generation: str = Field(min_length=1)


class CapabilityPin(Contract):
    plugin_id: str = Field(min_length=1)
    plugin_version: str = Field(min_length=1)
    plugin_digest: str = Field(pattern=_SHA256)
    definition: str = Field(pattern=r"^[a-z][a-z0-9._-]*/v[1-9][0-9]*$")
    slot: str = Field(min_length=1)
    required_permissions: tuple[str, ...] = ()

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.plugin_id, self.definition, self.slot


class CapabilityInventory(Contract):
    activation: ActivationPin
    capabilities: tuple[CapabilityPin, ...]
    granted_permissions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_capabilities(self) -> CapabilityInventory:
        if len({pin.identity for pin in self.capabilities}) != len(self.capabilities):
            raise ValueError("duplicate capability identity")
        return self


class CapabilityStatus(Contract):
    inventory: CapabilityInventory
    available: bool = False
    revoked: bool = False


CapabilityProbe = Callable[[ActivationPin], Awaitable[CapabilityStatus]]


@dataclass(frozen=True)
class CapabilityContribution:
    pin: CapabilityPin
    tools: tuple[ToolDefinition, ...]
    tool_port: ToolPort
    # Only host-reviewed instructions belong here, never tool-returned text.
    trusted_instructions: tuple[str, ...] = ()


class AdmittedCapabilities:
    """Immutable model view plus a ToolPort with current host/policy checks.

    The supplied ports are already owned and fenced by the host. A probe cannot
    replace a generation-bound transport's atomic revocation check: the port
    must retain the host's lease/fence through the effect. No lifecycle method,
    credential service, Kernel or writable RunStore is passed to a plugin here.
    """

    def __init__(
        self,
        *,
        inventory: CapabilityInventory,
        contributions: Sequence[CapabilityContribution],
        probe: CapabilityProbe,
        authorize: ToolAuthorizer | None,
        max_result_bytes: int = 65536,
    ) -> None:
        self._inventory = _snapshot(inventory)
        self._probe = probe
        self._authorize = authorize
        self._tools: tuple[ToolDefinition, ...] = tuple(
            _snapshot(tool) for contribution in contributions for tool in contribution.tools
        )
        self._instructions = tuple(
            text for contribution in contributions for text in contribution.trusted_instructions
        )
        expected = {pin.identity: pin for pin in self._inventory.capabilities}
        bindings: list[ToolBinding] = []
        seen: set[tuple[str, str, str]] = set()
        for contribution in contributions:
            pin = _snapshot(contribution.pin)
            if expected.get(pin.identity) != pin or pin.identity in seen:
                raise CapabilityUnavailable("capability_contribution_mismatch")
            seen.add(pin.identity)
            if any(
                type(text) is not str or not text.strip()
                for text in contribution.trusted_instructions
            ):
                raise CapabilityUnavailable("capability_instructions_invalid")
            bindings.extend(
                ToolBinding(_snapshot(tool), self._callback(contribution.tool_port))
                for tool in contribution.tools
            )
        try:
            self._executor = StrictToolExecutor(
                scope=inventory.activation.scope,
                tools=bindings,
                authorize=self._policy,
                max_result_bytes=max_result_bytes,
            )
        except ValueError:
            raise CapabilityUnavailable("capability_tool_schema_invalid") from None

    @property
    def inventory(self) -> CapabilityInventory:
        return _snapshot(self._inventory)

    @property
    def model_tools(self) -> tuple[ToolDefinition, ...]:
        return tuple(_snapshot(tool) for tool in self._tools)

    @property
    def trusted_instructions(self) -> tuple[str, ...]:
        return self._instructions

    async def check_available(self) -> None:
        try:
            raw = await self._probe(_snapshot(self._inventory.activation))
            if not isinstance(raw, CapabilityStatus):
                raise ValueError("invalid status")
            current = _snapshot(raw)
        except Exception:
            raise CapabilityUnavailable("capability_status_unavailable") from None
        if current.revoked:
            raise CapabilityUnavailable("capability_revoked")
        if not current.available:
            raise CapabilityUnavailable("capability_unavailable")
        if current.inventory.activation != self._inventory.activation:
            raise CapabilityUnavailable("activation_mismatch")
        old = {pin.identity: pin for pin in self._inventory.capabilities}
        new = {pin.identity: pin for pin in current.inventory.capabilities}
        if old != new:
            raise CapabilityUnavailable("capability_drift")
        required = {permission for pin in old.values() for permission in pin.required_permissions}
        if not required.issubset(current.inventory.granted_permissions):
            raise CapabilityUnavailable("capability_permission_unavailable")

    @staticmethod
    def _denied(code: str) -> ToolResult:
        return ToolResult(status="error", content={"code": code})

    async def _policy(self, request: ToolRequest):
        # StrictToolExecutor validates scope, schema and definition first.
        if self._authorize is None:
            return "deny"
        return await self._authorize(request)

    def _callback(self, port: ToolPort):
        async def dispatch(request: ToolRequest) -> ToolResult:
            # Policy/approval may have yielded; never reuse its earlier host view.
            try:
                await self.check_available()
            except CapabilityUnavailable as error:
                return self._denied(error.code)
            return await port.execute(request)

        return dispatch

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            sealed = _snapshot(request)
        except Exception:
            return self._denied("invalid_tool_request")
        if sealed.scope != self._inventory.activation.scope:
            return self._denied("scope_mismatch")
        if sealed.run_id != self._inventory.activation.run_id:
            return self._denied("run_mismatch")
        try:
            await self.check_available()
        except CapabilityUnavailable as error:
            return self._denied(error.code)
        return await self._executor.execute(sealed)
