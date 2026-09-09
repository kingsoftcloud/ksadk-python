"""Credential-free resource snapshots consumed by Build activation and workers."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from ksadk.plugins.bridges.dsh import DshProfileBuildSnapshot
from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.contracts import (
    Digest,
    Identifier,
    ResourceConfig,
    validate_resource_bindings,
)


class ConnectionTarget(PluginContractModel):
    connection_ref: Identifier
    tenant_ref: Identifier
    principal_ref: Identifier
    endpoint: str
    auth_mode: Literal["signed", "sts", "token"]

    @field_validator("endpoint")
    @classmethod
    def canonical_endpoint(cls, value: str) -> str:
        if any(char.isspace() for char in value) or "\\" in value:
            raise ValueError("Invalid resource endpoint")
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "%" in url.hostname
        ):
            raise ValueError("Resource endpoint must be an explicit HTTP service URL")
        port = url.port  # validates port range and syntax
        host = url.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (url.scheme, port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        return urlunsplit((url.scheme, host, url.path.rstrip("/"), "", ""))


class SkillExecutionTarget(PluginContractModel):
    backend: Literal["e2b"] = "e2b"
    connection: ConnectionTarget
    domain: str = Field(strict=True, pattern=r"^[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9-]+)*$")
    template_id: Identifier
    timeout: int = Field(default=900, strict=True, ge=1, le=900)
    allow_internet_access: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def explicit_target(self):
        url = urlsplit(self.connection.endpoint)
        if self.connection.auth_mode != "token" or url.scheme != "https" or url.path:
            raise ValueError("Skill execution requires an explicit HTTPS token connection")
        return self


class MemoryRecallPolicy(PluginContractModel):
    """Frozen projection of Agent memory recall limits, never a second draft config."""

    top_k: int = Field(default=8, strict=True, ge=1, le=64)
    max_tokens: int = Field(default=1600, strict=True, ge=0, le=100000)
    min_score: float = Field(default=0, strict=True, ge=0, le=1)

    @model_validator(mode="after")
    def supported_score(self):
        if self.min_score != 0:
            raise ValueError(
                "MEMORY_SEARCH_POLICY_UNSUPPORTED: AICP score threshold is unavailable"
            )
        return self


class FrozenResourceBinding(PluginContractModel):
    config: ResourceConfig
    connection: ConnectionTarget
    # The connection target deliberately contains no credential references.
    # Freeze the repository revision separately so rotating those references
    # cannot make an old Build silently consume a different credential set.
    connection_revision: int = Field(default=1, strict=True, ge=1)
    skill_execution: SkillExecutionTarget | None = None
    memory_recall: MemoryRecallPolicy | None = None

    @model_validator(mode="after")
    def matching_connection(self) -> FrozenResourceBinding:
        if self.config.binding.connection_ref != self.connection.connection_ref:
            raise ValueError("Resource binding and connection reference do not match")
        if (
            self.memory_recall is not None
            and self.config.binding.resource.kind != "memory-instance"
        ):
            raise ValueError("Memory recall policy requires a memory binding")
        if self.skill_execution is not None and (
            self.config.binding.resource.kind != "skill-space"
            or self.config.execution_mode != "isolated"
            or self.skill_execution.connection.tenant_ref != self.connection.tenant_ref
        ):
            raise ValueError("Skill execution target requires an isolated Skill in the same tenant")
        return self


class ResourceSnapshot(PluginContractModel):
    schema_version: Literal[1] = 1
    plugin_lock_digest: Digest
    bindings: tuple[FrozenResourceBinding, ...]
    # Older standalone Worker fixtures may omit this. Studio Build/activation
    # requires the actual host snapshot, bound into the resource digest below.
    dsh_profile: DshProfileBuildSnapshot | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Resource snapshot version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_bindings(self) -> ResourceSnapshot:
        validate_resource_bindings(tuple(binding.config for binding in self.bindings))
        return self

    @property
    def digest(self) -> str:
        payload = {
            "schemaVersion": self.schema_version,
            "pluginLockDigest": self.plugin_lock_digest,
            **({"dshProfileDigest": self.dsh_profile.digest} if self.dsh_profile else {}),
            "bindings": [
                {
                    "config": json.loads(binding.config.canonical_bytes()),
                    "connection": binding.connection.model_dump(by_alias=True, mode="json"),
                    "connectionRevision": binding.connection_revision,
                    **(
                        {
                            "memoryRecall": binding.memory_recall.model_dump(
                                by_alias=True, mode="json"
                            )
                        }
                        if binding.memory_recall is not None
                        else {}
                    ),
                    **(
                        {
                            "skillExecution": binding.skill_execution.model_dump(
                                by_alias=True, mode="json"
                            )
                        }
                        if binding.skill_execution is not None
                        else {}
                    ),
                }
                for binding in sorted(self.bindings, key=lambda item: item.config.binding.id)
            ],
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def binding(self, binding_id: str) -> FrozenResourceBinding:
        for binding in self.bindings:
            if binding.config.binding.id == binding_id:
                return binding
        raise ValueError("RESOURCE_BINDING_NOT_IN_BUILD")

    def verify_connection(self, binding_id: str, current: ConnectionTarget) -> None:
        if self.binding(binding_id).connection != current:
            raise ValueError("RESOURCE_CONNECTION_CHANGED")
