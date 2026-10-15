"""Host-selected, read-only Contributor protocol for the Native provider."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_serializer

from ksadk.harness.native.state import Contract
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.context_contributor import (
    ContextContributorCapabilities,
    ContextContributorRequest,
    ContextContributorResponse,
)


class HarnessContextContributorConfiguration(Contract):
    schema_version: Literal["ksadk.harness-context-contributor-binding/v1"]
    capabilities: ContextContributorCapabilities
    policy_ref: str
    allowed_classifications: tuple[str, ...]
    max_chars: int = Field(default=32_000, strict=True, ge=1, le=131_072)

    @field_validator("capabilities", mode="before")
    @classmethod
    def decode_profile_quota(cls, value):
        if isinstance(value, dict) and "input_limit" in value:
            if "max_tokens" in value or "maxTokens" in value:
                raise ValueError("Context binding quota cannot have two spellings")
            value = dict(value)
            value["max_tokens"] = value.pop("input_limit")
        return value

    @model_serializer(mode="wrap")
    def encode_profile_quota(self, serialize):
        result = serialize(self)
        # This private Profile binding uses an explicit numeric quota name.
        # The frozen Contributor wire remains maxTokens. Do not weaken the
        # generic Profile rule that credential/token config requires a ref.
        capabilities = result["capabilities"]
        field = "max_tokens" if "max_tokens" in capabilities else "maxTokens"
        capabilities["input_limit"] = capabilities.pop(field)
        return result

    @field_validator("policy_ref")
    @classmethod
    def pinned_policy(cls, value):
        return ContextContributorRequest.validate_policy_ref(value)

    @field_validator("allowed_classifications")
    @classmethod
    def classifications(cls, value):
        return ContextContributorRequest.validate_allowed_classifications(value)


@runtime_checkable
class HarnessContextContributorSource(Protocol):
    async def harness_context_contribution(
        self, bundle: ResolvedPluginBundle, request: ContextContributorRequest,
    ) -> ContextContributorResponse: ...
