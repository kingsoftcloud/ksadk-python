"""Versioned resource declarations. Credentials are resolved outside these models."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ksadk.plugins.contracts import PluginContractModel

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"^\S+$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
ResourceKind = Literal["knowledge-base", "memory-instance", "skill-space"]


class ResourceRef(PluginContractModel):
    kind: ResourceKind
    id: Identifier
    region: Identifier


class ResourceBinding(PluginContractModel):
    id: Identifier
    connection_ref: Identifier
    resource: ResourceRef
    required: bool = Field(default=True, strict=True)


class RetrievalPolicy(PluginContractModel):
    mode: Literal["tool"] = "tool"
    top_k: int = Field(default=5, ge=1, le=100, strict=True)
    max_chars: int = Field(default=16000, ge=1, le=100000, strict=True)


class SelectedSkill(PluginContractModel):
    skill_id: Identifier
    version_id: Identifier
    content_hash: Digest


class ResourceConfig(PluginContractModel):
    schema_version: Literal[1] = 1
    binding: ResourceBinding
    retrieval: RetrievalPolicy | None = None
    selection_mode: Literal["pinned", "discovery"] | None = None
    selected_skills: tuple[SelectedSkill, ...] = ()
    include_public: bool = Field(default=False, strict=True)
    execution_mode: Literal["outer-agent", "isolated"] | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schemaVersion must be an integer")
        return value

    @model_validator(mode="after")
    def validate_resource_policy(self) -> ResourceConfig:
        kind = self.binding.resource.kind
        if kind != "knowledge-base" and self.retrieval is not None:
            raise ValueError("retrieval is only valid for knowledge-base bindings")
        if kind != "skill-space":
            if (
                self.selection_mode is not None
                or self.selected_skills
                or self.include_public
                or self.execution_mode is not None
            ):
                raise ValueError("Skill policies require a skill-space binding")
        else:
            identities = [skill.skill_id for skill in self.selected_skills]
            if len(identities) != len(set(identities)):
                raise ValueError("a Skill may only be selected once")
            if self.selection_mode == "discovery" and self.selected_skills:
                raise ValueError("discovery cannot declare pinned selectedSkills")
        return self

    def canonical_bytes(self) -> bytes:
        """Normalize omitted defaults before hashing; lists are immutable tuples."""
        data = self.model_dump(by_alias=True, mode="json", exclude_none=True)
        if self.binding.resource.kind == "knowledge-base":
            data["retrieval"] = (self.retrieval or RetrievalPolicy()).model_dump(by_alias=True)
        if self.binding.resource.kind == "skill-space":
            data["selectionMode"] = self.selection_mode or "pinned"
            data["executionMode"] = self.execution_mode or "outer-agent"
        return json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


def validate_resource_bindings(configs: tuple[ResourceConfig, ...]) -> None:
    """First release supports one binding of each resource kind per Agent."""
    ids = [config.binding.id for config in configs]
    kinds = [config.binding.resource.kind for config in configs]
    if len(ids) != len(set(ids)):
        raise ValueError("resource binding IDs must be unique within an Agent")
    if len(kinds) != len(set(kinds)):
        raise ValueError("only one resource binding of each kind is supported")


class InvocationIdentity(PluginContractModel):
    """Resolved by the trusted host, never taken from model tool arguments.

    Resource credentials and the human whose memories are accessed need not
    represent the same principal. A worker activation belongs to one identity.
    """

    tenant_ref: Identifier
    resource_principal_ref: Identifier
    actor_ref: Identifier
    memory_subject_ref: Identifier
    agent_id: Identifier
    session_ref: Identifier

    def memory_partition(self, resource: ResourceRef) -> str:
        """Stable across sessions/activations; account and resource remain isolated."""
        if resource.kind != "memory-instance":
            raise ValueError("memory partition requires a memory-instance")
        parts = [
            "memory-partition/v1",
            self.tenant_ref,
            self.resource_principal_ref,
            resource.kind,
            resource.id,
            resource.region,
            self.agent_id,
            self.memory_subject_ref,
        ]
        encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
        # AICP AgentUserId accepts at most 64 characters. Keep the versioned
        # prefix and 240 bits of the partition digest inside that wire limit.
        return "mp1-" + hashlib.sha256(encoded).hexdigest()[:60]
