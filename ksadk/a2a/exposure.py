"""Compile remote declarations to one conflict-free consumer call surface."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from typing_extensions import Self

from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.a2a.invocation_types import RemoteAgentSpec

ResolvedExposure = Literal["native", "expert_tool", "dynamic_tool"]


class InvocationExposureEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    spec: RemoteAgentSpec
    exposure: ResolvedExposure


class InvocationExposurePlan(BaseModel):
    """A deterministic local projection, never an authorization token."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    consumer_id: str
    framework: str
    entries: tuple[InvocationExposureEntry, ...]

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        if not self.consumer_id.strip() or self.consumer_id != self.consumer_id.strip():
            raise ValueError("consumer_id must be non-blank")
        if not self.framework.strip() or self.framework != self.framework.strip():
            raise ValueError("framework must be a non-blank canonical name")
        targets: set[str] = set()
        names: set[str] = set()
        bindings: set[str] = set()
        for entry in self.entries:
            spec = RemoteAgentSpec.model_validate(entry.spec.model_dump())
            if spec.target_agent_id in targets:
                raise ValueError("A2A_EXPOSURE_CONFLICT")
            if spec.local_name in names:
                raise ValueError("A2A_LOCAL_NAME_CONFLICT")
            if spec.binding_id in bindings:
                raise ValueError("A2A_BINDING_ID_CONFLICT")
            targets.add(spec.target_agent_id)
            names.add(spec.local_name)
            bindings.add(spec.binding_id)
            if entry.exposure != _resolve_exposure(spec, self.framework):
                raise ValueError("A2A_EXPOSURE_STRATEGY_MISMATCH")
        return self

    def assert_generic_target_allowed(self, target_agent_id: str) -> None:
        require_a2a_resource_id(target_agent_id, "a2a-agent-", field_name="target_agent_id")
        if any(
            entry.spec.target_agent_id == target_agent_id and entry.exposure != "dynamic_tool"
            for entry in self.entries
        ):
            raise ValueError("A2A_EXPOSURE_CONFLICT")


def _resolve_exposure(spec: RemoteAgentSpec, framework: str) -> ResolvedExposure:
    if spec.expected_version_id is None:
        raise ValueError("A2A_VERSION_PIN_REQUIRED")
    if spec.exposure == "tool":
        return "dynamic_tool"
    if framework in {"adk", "langgraph"}:
        return "native"
    if spec.exposure == "native":
        raise ValueError("A2A_NATIVE_UNSUPPORTED")
    return "expert_tool" if framework == "codex" else "dynamic_tool"


def compile_exposure_plan(
    specs: Sequence[RemoteAgentSpec],
    *,
    framework: str,
    consumer_id: str,
) -> InvocationExposurePlan:
    if not isinstance(framework, str) or not framework.strip() or framework != framework.strip():
        raise ValueError("framework must be a non-blank canonical name")
    if (
        not isinstance(consumer_id, str)
        or not consumer_id.strip()
        or consumer_id != consumer_id.strip()
    ):
        raise ValueError("consumer_id must be non-blank")
    entries = []
    for declaration in specs:
        spec = RemoteAgentSpec.model_validate(declaration.model_dump())
        entries.append(
            InvocationExposureEntry(
                spec=spec,
                exposure=_resolve_exposure(spec, framework),
            )
        )
    return InvocationExposurePlan(
        consumer_id=consumer_id,
        framework=framework,
        entries=tuple(sorted(entries, key=lambda entry: entry.spec.local_name)),
    )


__all__ = ["InvocationExposureEntry", "InvocationExposurePlan", "compile_exposure_plan"]
