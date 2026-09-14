"""统一 Capability Registry 描述符（plan §10.1）。

Phase 0 只落描述符与注册表数据结构；发现/加载/降级在 Phase 3 落地。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk.harness.resource_ref import validate_resource_ref


class CapabilityKind(str, Enum):
    MCP = "mcp"
    SKILL = "skill"
    BUILTIN = "builtin"
    SANDBOX = "sandbox"


class LoadPolicy(str, Enum):
    ALWAYS = "always"
    ON_DEMAND = "on_demand"
    EXPLICIT = "explicit"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=512)
    kind: CapabilityKind
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=8192)
    version: str = Field(min_length=1, max_length=64)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    load_policy: LoadPolicy = LoadPolicy.ALWAYS
    dependencies: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_refs(self) -> "CapabilityDescriptor":
        validate_resource_ref(self.id)
        for dep in self.dependencies:
            validate_resource_ref(dep)
        return self


class CapabilityRegistryError(ValueError):
    """注册/解析冲突（重复 id、悬空依赖）。"""


class CapabilityRegistry:
    """进程内注册表：id 唯一，依赖必须已注册（拓扑可解析）。"""

    def __init__(self) -> None:
        self._entries: dict[str, CapabilityDescriptor] = {}

    def register(self, descriptor: CapabilityDescriptor) -> None:
        if descriptor.id in self._entries:
            raise CapabilityRegistryError(f"capability 重复注册: {descriptor.id}")
        missing = [d for d in descriptor.dependencies if d not in self._entries]
        if missing:
            raise CapabilityRegistryError(
                f"capability {descriptor.id} 依赖未注册: {sorted(missing)}"
            )
        self._entries[descriptor.id] = descriptor

    def get(self, capability_id: str) -> CapabilityDescriptor:
        try:
            return self._entries[capability_id]
        except KeyError:
            raise CapabilityRegistryError(f"unknown capability: {capability_id}") from None

    def resolve_order(self, capability_ids: list[str]) -> list[CapabilityDescriptor]:
        """按依赖拓扑排序（稳定：同层保持声明顺序）。"""
        resolved: list[CapabilityDescriptor] = []
        seen: set[str] = set()
        visiting: set[str] = set()

        def visit(cid: str) -> None:
            if cid in seen:
                return
            if cid in visiting:
                raise CapabilityRegistryError(f"capability 依赖成环: {cid}")
            visiting.add(cid)
            for dep in self.get(cid).dependencies:
                visit(dep)
            visiting.discard(cid)
            seen.add(cid)
            resolved.append(self.get(cid))

        for capability_id in capability_ids:
            visit(capability_id)
        return resolved

    def describe(self) -> list[dict[str, Any]]:
        return [d.model_dump(mode="json") for d in self._entries.values()]


__all__ = [
    "CapabilityDescriptor",
    "CapabilityKind",
    "CapabilityRegistry",
    "CapabilityRegistryError",
    "LoadPolicy",
    "RiskLevel",
]
