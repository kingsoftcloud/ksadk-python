"""Harness 跨 Runtime 的治理能力声明。

控制动词能力仍由 ``RuntimeCapabilityMatrix`` 负责；本模块补充 Harness 关心的
输入、上下文、能力调用与事件可观测语义，避免把“能启动”误报成“行为一致”。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk.kernel.contracts import RuntimeCapabilityMatrix


class CapabilitySupport(str, Enum):
    SUPPORTED = "supported"
    EMULATED = "emulated"
    UNAVAILABLE = "unavailable"
    OPAQUE = "opaque"


class CapabilityOwner(str, Enum):
    PLATFORM = "platform"
    RUNTIME = "runtime"
    PROVIDER = "provider"
    SHARED = "shared"


class HarnessCapability(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    support: CapabilitySupport
    owner: CapabilityOwner
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()


class RuntimeHarnessCapabilities(BaseModel):
    """版本化、可序列化且可前向扩展的 Runtime Harness 声明。"""

    model_config = ConfigDict(extra="allow", frozen=True)
    schema_version: str = "harness.runtime-capabilities/v1"
    runtime_type: str = Field(min_length=1)
    capabilities: dict[str, HarnessCapability]
    metadata: dict[str, Any] = Field(default_factory=dict)

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "instructions",
        "history",
        "compaction",
        "memory",
        "tools",
        "streaming",
        "approval",
        "checkpoint",
        "recovery",
        "events",
    )

    @classmethod
    def required_capability_names(cls) -> tuple[str, ...]:
        return cls._REQUIRED

    @model_validator(mode="after")
    def require_complete_declaration(self) -> "RuntimeHarnessCapabilities":
        missing = set(self._REQUIRED) - set(self.capabilities)
        if missing:
            raise ValueError(f"runtime capability declaration missing: {sorted(missing)}")
        return self


def managed_langgraph_capabilities() -> RuntimeHarnessCapabilities:
    evidence_by_capability = {
        "instructions": ("tests/harness/test_prompt_contract.py",),
        "history": ("tests/harness/test_context_pipeline.py",),
        "compaction": ("tests/harness/test_context_pipeline.py",),
        "memory": ("tests/memory",),
        "tools": ("tests/harness/test_loop_tools.py",),
        "streaming": ("tests/harness/test_harness_reasoner.py",),
        "approval": ("tests/harness/test_engine_langgraph.py",),
        "checkpoint": ("tests/harness/test_engine_langgraph.py",),
        "recovery": ("tests/harness/test_runner_conformance_matrix.py",),
        "events": ("tests/harness/test_event_v2_envelope.py",),
    }
    return RuntimeHarnessCapabilities(
        runtime_type="managed-langgraph",
        capabilities={
            name: HarnessCapability(
                support=CapabilitySupport.SUPPORTED,
                owner=CapabilityOwner.PLATFORM,
                evidence_refs=evidence_by_capability[name],
            )
            for name in RuntimeHarnessCapabilities._REQUIRED
        },
    )


def _project_control(capability: Any, *, owner: CapabilityOwner) -> HarnessCapability:
    if not bool(getattr(capability, "supported", False)):
        return HarnessCapability(
            support=CapabilitySupport.UNAVAILABLE,
            owner=owner,
            reason=str(getattr(capability, "reason", "") or "not_implemented"),
        )
    mode = str(getattr(capability, "mode", "native") or "native")
    return HarnessCapability(
        support=(
            CapabilitySupport.EMULATED if mode == "emulated" else CapabilitySupport.SUPPORTED
        ),
        owner=owner,
    )


def project_adapter_capabilities(
    runtime_type: str, matrix: RuntimeCapabilityMatrix
) -> RuntimeHarnessCapabilities:
    """从平台动词矩阵保守投影；不可观测的数据治理能力标记 opaque。"""
    opaque = HarnessCapability(
        support=CapabilitySupport.OPAQUE,
        owner=CapabilityOwner.RUNTIME,
        reason="runtime_private_state_unobservable",
    )
    capabilities = {
        "instructions": opaque,
        "history": opaque,
        "compaction": opaque,
        "memory": opaque,
        "tools": opaque,
        "streaming": HarnessCapability(
            support=CapabilitySupport.SUPPORTED,
            owner=CapabilityOwner.RUNTIME,
            reason="RuntimeAdapter.stream",
        ),
        "approval": _project_control(matrix.resume, owner=CapabilityOwner.SHARED),
        "checkpoint": _project_control(matrix.checkpoint, owner=CapabilityOwner.RUNTIME),
        "recovery": _project_control(matrix.durable_restore, owner=CapabilityOwner.RUNTIME),
        "events": HarnessCapability(
            support=CapabilitySupport.EMULATED,
            owner=CapabilityOwner.PLATFORM,
            reason="adapter_event_projection",
        ),
    }
    return RuntimeHarnessCapabilities(
        runtime_type=runtime_type,
        capabilities=capabilities,
    )


__all__ = [
    "CapabilityOwner",
    "CapabilitySupport",
    "HarnessCapability",
    "RuntimeHarnessCapabilities",
    "managed_langgraph_capabilities",
    "project_adapter_capabilities",
]
