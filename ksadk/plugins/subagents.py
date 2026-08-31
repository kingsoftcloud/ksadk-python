"""Frozen SubagentProvider/v1 seam with explicit lineage and policy bounds.

The Kernel does not know whether a child is implemented by Codex, DSH,
Claude, another KsADK AgentProvider, or a remote service.  It only routes a
bounded request to a named provider and retains the returned child identity.
Provider-private state and event logs remain with that provider; projected
child events keep their own ids and sequence numbers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic import Field, field_validator, model_validator

from ksadk.plugins.contracts import PluginContractModel, PluginReference


class SubagentPolicy(PluginContractModel):
    """Parent-owned limits that a child provider may only narrow."""

    max_depth: int = Field(default=1, ge=1, le=16)
    timeout_seconds: int = Field(default=120, ge=1, le=86_400)
    max_steps: int = Field(default=12, ge=1, le=10_000)
    background: bool = False
    allowed_tools: tuple[str, ...] = ()
    allowed_permissions: tuple[str, ...] = ()

    @field_validator("allowed_tools", "allowed_permissions")
    @classmethod
    def validate_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("subagent policy entries must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("subagent policy entries must be unique")
        return tuple(sorted(value))


class SpawnSubagentRequest(PluginContractModel):
    """One bounded child task requested by an authenticated parent Run."""

    request_format: Literal["ksadk.subagent-spawn/v1"] = "ksadk.subagent-spawn/v1"
    provider_ref: str
    parent_session_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    task: str = Field(min_length=1, max_length=131_072)
    depth: int = Field(default=1, ge=1, le=16)
    policy: SubagentPolicy = Field(default_factory=SubagentPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider_ref")
    @classmethod
    def validate_provider_ref(cls, value: str) -> str:
        return cast(str, PluginReference(ref=value).ref)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_secret_values(value)
        return value

    @model_validator(mode="after")
    def validate_depth(self) -> "SpawnSubagentRequest":
        if self.depth > self.policy.max_depth:
            raise ValueError("subagent depth exceeds the parent policy maxDepth")
        return self


class ChildHandle(PluginContractModel):
    """Stable lineage and recovery descriptor for one provider-owned child."""

    handle_format: Literal["ksadk.child-handle/v1"] = "ksadk.child-handle/v1"
    handle_id: str = Field(min_length=1, max_length=256)
    provider_ref: str
    parent_session_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    child_session_id: str | None = Field(default=None, max_length=256)
    child_run_id: str | None = Field(default=None, max_length=256)
    depth: int = Field(ge=1, le=16)
    capabilities: tuple[str, ...] = ()
    capability_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime
    resumable: bool = False
    resume_descriptor: dict[str, Any] | None = None

    @field_validator("provider_ref")
    @classmethod
    def validate_provider_ref(cls, value: str) -> str:
        return cast(str, PluginReference(ref=value).ref)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("child capabilities must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("child capabilities must be unique")
        return tuple(sorted(value))

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("child handle createdAt must include a timezone")
        return value

    @field_validator("resume_descriptor")
    @classmethod
    def validate_resume_descriptor(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is not None:
            _reject_secret_values(value)
        return value

    @model_validator(mode="after")
    def validate_recovery_shape(self) -> "ChildHandle":
        if self.resumable and not self.resume_descriptor:
            raise ValueError("resumable child handle requires a resumeDescriptor")
        if not self.resumable and self.resume_descriptor is not None:
            raise ValueError("non-resumable child handle cannot carry a resumeDescriptor")
        return self


class SubagentStatus(PluginContractModel):
    status_format: Literal["ksadk.subagent-status/v1"] = "ksadk.subagent-status/v1"
    handle_id: str = Field(min_length=1, max_length=256)
    state: Literal[
        "accepted",
        "running",
        "waiting_input",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
        "disposed",
    ]
    last_seq: int = Field(default=0, ge=0)
    updated_at: datetime
    reason: str | None = Field(default=None, max_length=2048)

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("subagent status updatedAt must include a timezone")
        return value


class SubagentEvent(PluginContractModel):
    """Identity-preserving child event; parent projection must not merge by text."""

    event_format: Literal["ksadk.subagent-event/v1"] = "ksadk.subagent-event/v1"
    handle_id: str = Field(min_length=1, max_length=256)
    event_id: str = Field(min_length=1, max_length=256)
    seq: int = Field(ge=1)
    kind: Literal["progress", "item", "interaction", "terminal"]
    payload: dict[str, Any] = Field(default_factory=dict)
    native_ref: dict[str, str] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_secret_values(value)
        return value


class SubagentResult(PluginContractModel):
    result_format: Literal["ksadk.subagent-result/v1"] = "ksadk.subagent-result/v1"
    handle_id: str = Field(min_length=1, max_length=256)
    state: Literal["succeeded", "failed", "cancelled", "interrupted"]
    output: Any = None
    output_refs: tuple[str, ...] = ()
    error_code: str | None = Field(default=None, max_length=256)
    error_message: str | None = Field(default=None, max_length=2048)

    @field_validator("output_refs")
    @classmethod
    def validate_output_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("subagent output references must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("subagent output references must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "SubagentResult":
        if self.state == "succeeded" and (self.error_code or self.error_message):
            raise ValueError("successful subagent result cannot carry an error")
        if self.state == "failed" and not self.error_code:
            raise ValueError("failed subagent result requires errorCode")
        return self


@runtime_checkable
class SubagentProvider(Protocol):
    """Named child execution provider; every method is provider-owned."""

    async def describe(self) -> Mapping[str, Any]: ...

    async def available(self) -> bool: ...

    async def spawn(self, request: SpawnSubagentRequest) -> ChildHandle: ...

    async def followup(self, handle: ChildHandle, input: Any) -> None: ...

    async def status(self, handle: ChildHandle) -> SubagentStatus: ...

    async def interrupt(self, handle: ChildHandle) -> None: ...

    async def cancel(self, handle: ChildHandle) -> None: ...

    def subscribe(
        self, handle: ChildHandle, *, after_seq: int = 0
    ) -> AsyncIterator[SubagentEvent]: ...

    async def result(self, handle: ChildHandle) -> SubagentResult: ...

    async def dispose(self, handle: ChildHandle) -> None: ...


class SubagentProviderError(RuntimeError):
    """Stable typed failure at the provider routing boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SubagentProviderRouter:
    """Route child handles to exact providers without interpreting their state."""

    def __init__(self, providers: Mapping[str, SubagentProvider]) -> None:
        self._providers = dict(providers)

    async def spawn(self, request: SpawnSubagentRequest) -> ChildHandle:
        provider = self._provider(request.provider_ref)
        if not await provider.available():
            raise SubagentProviderError(
                "subagent_provider_unavailable",
                f"SubagentProvider {request.provider_ref} is unavailable",
            )
        handle = await provider.spawn(request)
        self._validate_handle(request, handle)
        return handle

    async def status(self, handle: ChildHandle) -> SubagentStatus:
        return await self._provider_for_handle(handle).status(handle)

    async def followup(self, handle: ChildHandle, input: Any) -> None:
        await self._provider_for_handle(handle).followup(handle, input)

    async def interrupt(self, handle: ChildHandle) -> None:
        await self._provider_for_handle(handle).interrupt(handle)

    async def cancel(self, handle: ChildHandle) -> None:
        await self._provider_for_handle(handle).cancel(handle)

    def subscribe(
        self, handle: ChildHandle, *, after_seq: int = 0
    ) -> AsyncIterator[SubagentEvent]:
        if after_seq < 0:
            raise SubagentProviderError(
                "subagent_cursor_invalid", "subagent after_seq cannot be negative"
            )
        return self._provider_for_handle(handle).subscribe(handle, after_seq=after_seq)

    async def result(self, handle: ChildHandle) -> SubagentResult:
        return await self._provider_for_handle(handle).result(handle)

    async def dispose(self, handle: ChildHandle) -> None:
        await self._provider_for_handle(handle).dispose(handle)

    def _provider_for_handle(self, handle: ChildHandle) -> SubagentProvider:
        return self._provider(handle.provider_ref)

    def _provider(self, provider_ref: str) -> SubagentProvider:
        provider = self._providers.get(provider_ref)
        if provider is None:
            raise SubagentProviderError(
                "subagent_provider_not_found",
                f"SubagentProvider {provider_ref} is not registered",
            )
        return provider

    @staticmethod
    def _validate_handle(
        request: SpawnSubagentRequest,
        handle: ChildHandle,
    ) -> None:
        if (
            handle.provider_ref != request.provider_ref
            or handle.parent_session_id != request.parent_session_id
            or handle.parent_run_id != request.parent_run_id
            or handle.depth != request.depth
        ):
            raise SubagentProviderError(
                "subagent_handle_mismatch",
                "SubagentProvider returned a child handle outside the requested lineage",
            )


_SECRET_KEY_PARTS = ("secret", "password", "token", "apikey", "api_key")
_SECRET_REF_PREFIXES = ("secret://", "env://", "credential://", "vault://")


def _reject_secret_values(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.replace("-", "").lower()
            if any(part in normalized for part in _SECRET_KEY_PARTS) and child is not None:
                if not isinstance(child, str) or not child.startswith(_SECRET_REF_PREFIXES):
                    raise ValueError(f"{path}.{key_text} must contain a secret reference")
            _reject_secret_values(child, path=f"{path}.{key_text}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _reject_secret_values(child, path=f"{path}[{index}]")


__all__ = [
    "ChildHandle",
    "SpawnSubagentRequest",
    "SubagentEvent",
    "SubagentPolicy",
    "SubagentProvider",
    "SubagentProviderError",
    "SubagentProviderRouter",
    "SubagentResult",
    "SubagentStatus",
]
