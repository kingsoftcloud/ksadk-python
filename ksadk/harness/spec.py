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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ksadk.harness.resource_ref import validate_resource_ref

SCHEMA_VERSION = "harness.ksadk.io/v1"
HARNESS_VERSION = "0.1.0"


class _SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelFailureCategory(str, Enum):
    """平台稳定分类，避免按厂商异常类直接决定重试与降级。"""

    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    TRANSPORT = "transport"


class ModelProviderPolicy(_SpecModel):
    """模型提供方失败策略。

    只允许对瞬时故障配置重试/降级。鉴权、权限、请求参数、上下文超限和
    未知错误不会被此合同静默包装为可重试故障。
    """

    max_attempts_per_model: int = Field(default=2, ge=1, le=5)
    total_attempt_budget: int = Field(default=6, ge=1, le=20)
    initial_backoff_ms: int = Field(default=200, ge=0, le=30_000)
    max_backoff_ms: int = Field(default=2_000, ge=0, le=60_000)
    retryable_categories: tuple[ModelFailureCategory, ...] = (
        ModelFailureCategory.RATE_LIMIT,
        ModelFailureCategory.TIMEOUT,
        ModelFailureCategory.UNAVAILABLE,
        ModelFailureCategory.TRANSPORT,
    )
    failover_categories: tuple[ModelFailureCategory, ...] = (
        ModelFailureCategory.RATE_LIMIT,
        ModelFailureCategory.TIMEOUT,
        ModelFailureCategory.UNAVAILABLE,
        ModelFailureCategory.TRANSPORT,
    )

    @model_validator(mode="after")
    def validate_backoff(self) -> "ModelProviderPolicy":
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("max_backoff_ms 不得小于 initial_backoff_ms")
        return self


class ModelBinding(_SpecModel):
    """模型绑定：profile_ref 固定版本，可选降级链。"""

    profile_ref: str = Field(min_length=1, max_length=512)
    fallback_profile_refs: tuple[str, ...] = Field(default=(), max_length=4)
    provider_policy: ModelProviderPolicy = Field(default_factory=ModelProviderPolicy)
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

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        alias_generator=lambda value: (
            value.split("_")[0] + "".join(part.capitalize() for part in value.split("_")[1:])
        ),
    )

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    instructions: str = Field(min_length=1, max_length=32_768)
    description: str = Field(default="", max_length=1024)
    #: 允许使用的工具名（空 = 纯文本推理）。
    tools: tuple[str, ...] = Field(default=(), max_length=64)
    #: 单次委派的墙钟超时；防止子 Agent 阻塞父 Run。
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    #: 子 Agent 自己可执行的最大模型轮数，独立于父 Agent。
    max_turns: int = Field(default=4, ge=1, le=32)
    #: 子 Agent 模型调用的独立总 Token 上限；None 表示仅受父级/Provider 预算。
    max_total_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    #: 子 Agent 可创建的独立 Artifact 数量上限。
    max_artifacts: int | None = Field(default=None, ge=0, le=10_000)
    #: 子 Agent 最多完成的 Tool Call 数量。
    max_tool_calls: int | None = Field(default=None, ge=0, le=100_000)
    #: 子 Agent 最终输出的 JSON Schema；未声明时保留文本输出。
    output_schema: dict[str, Any] | None = None
    #: 同一委派批次内必须先完成的子 Agent 名称。
    depends_on: tuple[str, ...] = Field(default=(), max_length=32)
    #: ``propagate`` 将失败作为 Tool error 回流父 Agent；``return_error``
    #: 返回结构化失败文本，适合可选审查/检索子任务。
    failure_policy: str = Field(default="propagate", pattern=r"^(propagate|return_error|retry)$")
    max_retries: int = Field(default=0, ge=0, le=3)
    #: Skill 属于只读知识能力，默认继承；MCP 默认不继承，避免子 Agent
    #: 在未显式授权时扩大外部系统访问面。
    inherit_skills: bool = True
    inherit_mcp: bool = False

    @field_validator("output_schema")
    @classmethod
    def validate_output_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        from jsonschema.validators import validator_for

        try:
            validator_for(value).check_schema(value)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"output_schema 不是有效 JSON Schema: {exc}") from exc
        return value


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
        names = {sub.name for sub in self.sub_agents}
        if len(names) != len(self.sub_agents):
            raise ValueError("sub-agent names must be unique")
        for sub in self.sub_agents:
            unknown = set(sub.depends_on) - names
            if unknown:
                raise ValueError(
                    f"sub-agent {sub.name!r} has unknown dependencies: {sorted(unknown)}"
                )
            if sub.name in sub.depends_on:
                raise ValueError(f"sub-agent dependency cycle: {sub.name} -> {sub.name}")
        dependencies = {sub.name: set(sub.depends_on) for sub in self.sub_agents}
        remaining = set(dependencies)
        resolved: set[str] = set()
        while remaining:
            ready = {name for name in remaining if dependencies[name] <= resolved}
            if not ready:
                raise ValueError(f"sub-agent dependency cycle: {sorted(remaining)}")
            resolved.update(ready)
            remaining -= ready
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
        "fallbackModelProfileRefs": list(spec.model.fallback_profile_refs),
        "modelProviderPolicy": spec.model.provider_policy.model_dump(mode="json"),
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
    "ModelFailureCategory",
    "ModelProviderPolicy",
    "ObservabilityPolicy",
    "PromptSpec",
    "SandboxPolicy",
    "SubAgentBinding",
    "build_manifest",
]
