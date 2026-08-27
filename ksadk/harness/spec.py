"""HarnessSpec v1 — Revision 编译后的引擎无关运行规范（plan §6.1）。

冻结契约：本模块（及其依赖）不得 import LangGraph 或任何 Runner 专有类型，
由 ``tests/architecture/test_harness_contract.py`` 守卫。所有资源引用必须
固定版本；不含明文 Secret（只允许 Secret Reference）。
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk.harness.resource_ref import validate_resource_ref

SCHEMA_VERSION = "harness.ksadk.io/v1"
HARNESS_VERSION = "0.1.0"


class _SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelBinding(_SpecModel):
    """模型绑定：profile_ref 固定版本，可选降级链。"""

    profile_ref: str = Field(min_length=1, max_length=512)
    fallback_profile_refs: tuple[str, ...] = Field(default=(), max_length=4)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def validate_refs(self) -> "ModelBinding":
        validate_resource_ref(self.profile_ref)
        for ref in self.fallback_profile_refs:
            validate_resource_ref(ref)
        return self


class PromptSpec(_SpecModel):
    """Stable Prompt 规范：正文内联或引用，二者互斥。"""

    instructions: str | None = Field(default=None, max_length=131_072)
    instructions_ref: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_exactly_one(self) -> "PromptSpec":
        if bool(self.instructions) == bool(self.instructions_ref):
            raise ValueError("prompt 必须二选一：instructions（内联）或 instructions_ref（引用）")
        if self.instructions_ref:
            validate_resource_ref(self.instructions_ref)
        return self


class BudgetSource(str, Enum):
    MODEL_PROFILE = "model_profile"
    PROVIDER_MODELS = "provider_models"
    STATIC_METADATA = "static_metadata"
    SAFE_DEFAULT = "safe_default"


class ContextPolicy(_SpecModel):
    """上下文预算与压缩策略（plan §8.3/§8.4）。"""

    budget_source: BudgetSource = BudgetSource.MODEL_PROFILE
    budget_source_fallback: tuple[BudgetSource, ...] = Field(
        default=(BudgetSource.PROVIDER_MODELS, BudgetSource.SAFE_DEFAULT), max_length=3
    )
    proactive_compaction_threshold: float = Field(default=0.72, ge=0.1, le=0.95)
    emergency_retry_limit: int = Field(default=1, ge=1, le=3)
    reserved_output_ratio: float = Field(default=0.15, ge=0.05, le=0.5)
    safety_buffer_ratio: float = Field(default=0.05, ge=0.01, le=0.2)


class MemoryPolicy(_SpecModel):
    """Memory 作用域与写入策略（plan §9）。"""

    enabled: bool = False
    scopes: tuple[str, ...] = Field(default=("session",), max_length=5)
    core_block_refs: tuple[str, ...] = Field(default=(), max_length=16)
    max_resident_blocks: int = Field(default=8, ge=1, le=64)
    write_requires_source: bool = True

    @model_validator(mode="after")
    def validate_refs(self) -> "MemoryPolicy":
        for ref in self.core_block_refs:
            validate_resource_ref(ref)
        allowed = {"run", "session", "agent", "user", "org"}
        unknown = set(self.scopes) - allowed
        if unknown:
            raise ValueError(f"未知 memory scope: {sorted(unknown)}；允许 {sorted(allowed)}")
        return self


class CapabilityBinding(_SpecModel):
    """单个能力绑定（MCP/Skill），固定版本 + 加载策略。"""

    capability_ref: str = Field(min_length=1, max_length=512)
    required: bool = Field(default=True)
    load_policy: str = Field(default="always", pattern=r"^(always|on_demand|explicit)$")

    @model_validator(mode="after")
    def validate_ref(self) -> "CapabilityBinding":
        validate_resource_ref(self.capability_ref)
        return self


class CapabilityBindings(_SpecModel):
    mcp_bindings: tuple[CapabilityBinding, ...] = Field(default=(), max_length=32)
    skill_bindings: tuple[CapabilityBinding, ...] = Field(default=(), max_length=64)


class SubAgentBinding(_SpecModel):
    """子 Agent 声明（plan §14：多 Agent 是可选能力，经 Revision 编译）。"""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    instructions: str = Field(min_length=1, max_length=32_768)
    description: str = Field(default="", max_length=1024)
    #: 允许使用的工具名（空 = 纯文本推理）。
    tools: tuple[str, ...] = Field(default=(), max_length=64)


class ExecutionStrategyKind(str, Enum):
    SINGLE_AGENT = "single-agent"
    PLAN_EXECUTE = "plan-execute"
    PLAN_EXECUTE_REVIEW = "plan-execute-review"
    CUSTOM_IMPORTED = "custom-imported"


class ExecutionStrategySpec(_SpecModel):
    kind: ExecutionStrategyKind = ExecutionStrategyKind.SINGLE_AGENT
    config: dict[str, Any] = Field(default_factory=dict)


class ApprovalPolicy(_SpecModel):
    """高风险 Tool 审批策略（plan §11）。"""

    mode: str = Field(default="policy", pattern=r"^(never|policy|always)$")
    timeout_seconds: int | None = Field(default=None, ge=1, le=2_592_000)
    approver_roles: tuple[str, ...] = Field(default=(), max_length=16)


class SandboxPolicy(_SpecModel):
    read_only: bool = True
    backend: str = Field(default="local-readonly", max_length=64)
    network_egress: str = Field(default="deny", pattern=r"^(deny|allow|policy)$")


class ObservabilityPolicy(_SpecModel):
    emit_usage: bool = True
    emit_context_trace: bool = True
    redact_secrets: bool = True
    record_reasoning_text: bool = False


class HarnessSpec(_SpecModel):
    """Revision 编译后的不可变运行规范。"""

    schema_version: str = Field(default=SCHEMA_VERSION, frozen=True)
    harness_version: str = Field(default=HARNESS_VERSION, frozen=True)
    agent_revision_ref: str = Field(min_length=1, max_length=512)

    model: ModelBinding
    prompt: PromptSpec
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    capabilities: CapabilityBindings = Field(default_factory=CapabilityBindings)
    sub_agents: tuple[SubAgentBinding, ...] = Field(default=(), max_length=32)
    execution_strategy: ExecutionStrategySpec = Field(default_factory=ExecutionStrategySpec)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    sandbox_policy: SandboxPolicy = Field(default_factory=SandboxPolicy)
    observability_policy: ObservabilityPolicy = Field(default_factory=ObservabilityPolicy)

    @model_validator(mode="after")
    def validate_revision_ref(self) -> "HarnessSpec":
        validate_resource_ref(self.agent_revision_ref)
        return self

    def content_hash(self) -> str:
        """规范内容 Hash——同一 Spec + 版本可解释行为基线（plan §6.1）。"""
        payload = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_manifest(spec: HarnessSpec, *, engine: str = "managed-langgraph") -> dict[str, Any]:
    """Build Manifest 最小集（plan §12.3）：不记录 Secret，只记录引用与版本。"""
    return {
        "revisionRef": spec.agent_revision_ref,
        "harnessVersion": spec.harness_version,
        "schemaVersion": spec.schema_version,
        "engine": engine,
        "modelProfileRef": spec.model.profile_ref,
        "mcpRefs": [b.capability_ref for b in spec.capabilities.mcp_bindings],
        "skillRefs": [b.capability_ref for b in spec.capabilities.skill_bindings],
        "memoryPolicyRef": None,
        "policyRefs": [],
        "contentHash": spec.content_hash(),
    }


__all__ = [
    "HARNESS_VERSION",
    "SCHEMA_VERSION",
    "ApprovalPolicy",
    "BudgetSource",
    "CapabilityBinding",
    "CapabilityBindings",
    "ContextPolicy",
    "ExecutionStrategyKind",
    "ExecutionStrategySpec",
    "HarnessSpec",
    "MemoryPolicy",
    "ModelBinding",
    "ObservabilityPolicy",
    "PromptSpec",
    "SandboxPolicy",
    "SubAgentBinding",
    "build_manifest",
]
