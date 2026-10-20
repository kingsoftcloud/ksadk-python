"""Versioned model, execution, context, memory, and security configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    Field,
    model_serializer,
    model_validator,
)

from ksadk.studio.contract_base import ContractModel


class ModelParameters(ContractModel):
    # 三者 None=未配置：请求 payload 一律不携带该字段，使用服务端默认，
    # 规避各模型族对 temperature/max_tokens 的硬约束（如 kimi 只接受默认温度）。
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    top_p: float | None = Field(default=None, gt=0, le=1)
    # 是否允许在 chat 请求中携带 response_format（json_object 结构化输出）。
    # 关闭后 compose 等结构化调用退回纯文本输出，兼容不支持该字段的网关。
    allow_json_response_format: bool = Field(default=True)
    # Host disclosure opt-in. Some proxies label raw reasoning as a summary;
    # enable only when the upstream guarantees a user-visible summary channel.
    public_thinking_summary: bool = Field(default=False, strict=True,
                                          exclude_if=lambda value: not value)
    provider_thinking_display: bool = Field(default=False, strict=True,
                                            exclude_if=lambda value: not value)
    thinking_mode: Literal["enabled", "disabled"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


class ModelSpec(ContractModel):
    provider: str = Field(default="openai-compatible", min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    endpoint_url: str | None = None
    base_url: str | None = None
    wire_api: Literal["chat", "responses", "anthropic"] | None = None
    credential_ref: str = Field(min_length=1, max_length=512)
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    metadata: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_address(self) -> "ModelSpec":
        if bool(self.endpoint_url) == bool(self.base_url):
            raise ValueError("endpointUrl 和 baseUrl 必须且只能配置一个")
        return self


class RetryPolicy(ContractModel):
    max_attempts: int = Field(default=2, ge=1, le=10)
    provider_max_attempts: int = Field(default=4, ge=1, le=10)
    backoff_seconds: float = Field(default=1, ge=0, le=60)


class NativeExecutionLimits(ContractModel):
    """Build-pinned Native call limits; null is unbounded, zero tools denies tools."""

    max_model_calls: int | None = Field(default=None, strict=True, ge=1)
    max_tool_calls: int | None = Field(default=None, strict=True, ge=0)


class ExecutionSpec(ContractModel):
    strategy: Literal["direct", "plan-act-observe"] = "direct"
    # Compatibility metadata retained for existing AgentDraft payloads.  It is
    # not a cross-runtime execution budget: Harness, Codex, ADK and LangGraph
    # do not share one stable definition of an internal "step".  Runtime
    # specific turn/timeout/tool/token budgets remain authoritative.
    max_steps: int = Field(default=25, ge=1)
    # New Harness Builds pin this object (including an empty/unbounded object).
    # Absence in an already sealed Build retains its legacy max_steps mapping.
    native_limits: NativeExecutionLimits | None = None
    native_command_lifecycle_policy: Literal["owned-local-command/v1"] | None = None
    native_stream_timeout_policy: Literal["idle/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    native_background_notification_policy: Literal["completed-before-model/v1"] | None = None
    native_skill_metadata_policy: Literal["bound-skill-metadata/v1"] | None = None
    native_directory_observation_policy: Literal[
        "studio-directory-observation/v2", "studio-directory-observation/v3",
    ] | None = None
    native_directory_navigation_policy: Literal["receipt-directory-progress/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    native_wall_delivery_policy: Literal[
        "deadline-delivery/v1", "deadline-delivery/v2", "deadline-delivery/v3", "deadline-delivery/v4",
        "deadline-delivery/v5",
        "deadline-delivery/v6",
        "deadline-delivery/v7",
    ] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    tool_error_recovery_policy: Literal["diagnose-before-repeat/v1"] | None = None
    tool_error_recovery_model_calls: int | None = Field(default=None, strict=True, ge=1, le=10)
    timeout_seconds: int = Field(default=300, ge=0, le=3600)
    # Harness approval waits are separate from model/tool timeouts. None keeps
    # the existing explicit-user-decision behavior for older Builds.
    approval_timeout_seconds: int | None = Field(default=None, ge=1, le=2_592_000)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    sandbox: str | None = None
    approval_mode: str | None = None

    @model_serializer(mode="wrap")
    def omit_unconfigured_native_limits(self, serialize):
        payload = serialize(self)
        if self.native_limits is None:
            payload.pop("native_limits", None)
            payload.pop("nativeLimits", None)
        if self.tool_error_recovery_policy is None:
            payload.pop("tool_error_recovery_policy", None)
            payload.pop("toolErrorRecoveryPolicy", None)
        if self.tool_error_recovery_model_calls is None:
            payload.pop("tool_error_recovery_model_calls", None)
            payload.pop("toolErrorRecoveryModelCalls", None)
        if self.native_background_notification_policy is None:
            payload.pop("native_background_notification_policy", None)
            payload.pop("nativeBackgroundNotificationPolicy", None)
        if self.native_command_lifecycle_policy is None:
            payload.pop("native_command_lifecycle_policy", None)
            payload.pop("nativeCommandLifecyclePolicy", None)
        if self.native_skill_metadata_policy is None:
            payload.pop("native_skill_metadata_policy", None)
            payload.pop("nativeSkillMetadataPolicy", None)
        if self.native_directory_observation_policy is None:
            payload.pop("native_directory_observation_policy", None)
            payload.pop("nativeDirectoryObservationPolicy", None)
        return payload


class CompactionSpec(ContractModel):
    native_message_projection: Literal["current-user-after-summary/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    native_session_history_projection: Literal["sealed-session-summary/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    native_search_fragment_policy: Literal["complete-search-fragments/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    native_task_goal_projection_policy: Literal[
        "retained-user-goal/v1", "retained-user-goal/v2",
    ] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    native_source_progress_policy: Literal[
        "receipt-source-progress/v3", "receipt-source-progress/v4", "receipt-source-progress/v5",
    ] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    # Only new Native Builds stamp this; sealed legacy Builds omit it.
    native_summary_policy: Literal[
        "rolling-work-state/v1", "rolling-work-state/v2", "rolling-work-state/v3",
    ] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    enabled: bool = True
    # Clearing a receipt does not summarize its evidence. Enable explicitly.
    prune_tool_results: bool = False
    threshold_ratio: float = Field(default=0.8, gt=0, le=1)
    # PCM：双阈值（方案 §8.2 / §9.1）。soft=主动整理，hard=强制压缩。
    soft_threshold_ratio: float = Field(default=0.50, gt=0, le=1)
    hard_threshold_ratio: float = Field(default=0.85, gt=0, le=1)
    preserve_working_state: bool = True
    flush_memory_before_compaction: bool = True

    @model_validator(mode="after")
    def validate_ratios(self) -> "CompactionSpec":
        if (self.native_session_history_projection is not None
                and self.native_message_projection != "current-user-after-summary/v1"):
            raise ValueError("session summary projection requires current-user projection")
        if self.soft_threshold_ratio >= self.hard_threshold_ratio:
            raise ValueError("softThresholdRatio 必须小于 hardThresholdRatio")
        return self


class ContextContributorsSpec(ContractModel):
    """ContextContributor 开关与预算（方案 §5.1 / §8.7）。默认按 policy，可显式开关。"""

    workspace_rules: bool | None = None
    # Consumption requires a separate host-owned workspace/principal grant.
    project_instructions: bool = False
    skill_manifest: bool | None = None
    memory_recall: bool | None = None


class RolloutSpec(ContractModel):
    """AgentVersion 级灰度/回退状态（方案 §8.5）。替代环境变量控制正式灰度。"""

    context_engine: Literal["off", "shadow", "enabled"] = "shadow"
    memory_write: Literal["off", "shadow", "enabled"] = "shadow"


class ContextSpec(ContractModel):
    max_input_tokens: int = Field(default=32000, ge=1024)
    reserve_output_tokens: int = Field(default=4096, ge=1)
    compaction: CompactionSpec = Field(default_factory=CompactionSpec)
    # prompt_ownership：标记本 Agent 的 system prompt 归属。
    # framework（默认）= 框架自带 SystemMessage，ksadk 不接管 Runner 输入；
    # ksadk = 由 ksadk 的 PromptCompiler 编译 CompiledPrompt 并接管 instructions。
    prompt_ownership: Literal["framework", "ksadk"] = "framework"
    # PCM：ownership 高阶字段（方案 §5.1）。auto=按 capability 推导，向后兼容现有
    # prompt_ownership；显式 ksadk/framework/native 时覆盖。Studio 据 capability 限制选项。
    ownership: Literal["auto", "ksadk", "framework", "native"] = "auto"
    tokenizer: Literal["auto", "heuristic"] = "auto"
    policy_version: str = Field(default="context-v2", max_length=64)
    contributors: ContextContributorsSpec = Field(default_factory=ContextContributorsSpec)
    rollout: RolloutSpec = Field(default_factory=RolloutSpec)

    @model_validator(mode="after")
    def validate_budget(self) -> "ContextSpec":
        if self.reserve_output_tokens >= self.max_input_tokens:
            raise ValueError("reserveOutputTokens 必须小于 maxInputTokens")
        # ownership 与 prompt_ownership 一致性：显式 ownership 收窄 prompt_ownership（§5.2）。
        if self.ownership == "ksadk":
            self.prompt_ownership = "ksadk"
        elif self.ownership == "framework":
            self.prompt_ownership = "framework"
        # native 不收窄 prompt_ownership（native runtime 的 prompt 投影由 Adapter 决定）。
        return self


class MemoryRecallSpec(ContractModel):
    enabled: bool = True
    max_tokens: int = Field(default=1600, ge=0)
    top_k: int = Field(default=8, ge=1, le=64)
    min_score: float = Field(default=0.45, ge=0, le=1)


class MemoryWriteSpec(ContractModel):
    mode: Literal["off", "explicit_only", "candidate"] = "candidate"
    flush_before_compaction: bool = True
    flush_after_turn: bool = True


MemoryScope = Literal["tenant", "workspace", "agent", "user"]


def _default_memory_scopes() -> list[MemoryScope]:
    return ["workspace", "agent", "user"]


class MemorySpec(ContractModel):
    """AgentVersion 级 Memory 策略（方案 §5.1 / §10）。Build 只存 providerRef，不存凭证。"""

    enabled: bool = False
    provider_ref: str = Field(default="local-default", max_length=128)
    recall: MemoryRecallSpec = Field(default_factory=MemoryRecallSpec)
    write: MemoryWriteSpec = Field(default_factory=MemoryWriteSpec)
    scopes: list[MemoryScope] = Field(default_factory=_default_memory_scopes)


class NetworkPolicy(ContractModel):
    mode: Literal["restricted", "open"] = "restricted"
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_private_network: bool = False


class SecuritySpec(ContractModel):
    tool_policy: Literal["deny-by-default", "allow-listed"] = "deny-by-default"
    allowed_permissions: list[str] = Field(default_factory=list)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)


class EvaluationSpec(ContractModel):
    suite_refs: list[str] = Field(default_factory=list)
    minimum_pass_rate: float = Field(default=1, ge=0, le=1)

def _validate_sub_agents(self: Any) -> Any:
    if self.execution.native_limits is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeLimits 当前仅支持 Harness Runtime")
    if self.execution.tool_error_recovery_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("toolErrorRecoveryPolicy 当前仅支持 Harness Runtime")
    if self.execution.tool_error_recovery_model_calls is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("toolErrorRecoveryModelCalls 当前仅支持 Harness Runtime")
    if self.execution.native_background_notification_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeBackgroundNotificationPolicy 当前仅支持 Harness Runtime")
    if self.execution.native_command_lifecycle_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeCommandLifecyclePolicy 当前仅支持 Harness Runtime")
    if self.execution.native_stream_timeout_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeStreamTimeoutPolicy 当前仅支持 Harness Runtime")
    if self.execution.native_skill_metadata_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeSkillMetadataPolicy 当前仅支持 Harness Runtime")
    if self.execution.native_directory_observation_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeDirectoryObservationPolicy 当前仅支持 Harness Runtime")
    if self.execution.native_directory_navigation_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeDirectoryNavigationPolicy 当前仅支持 Harness Runtime")
    if self.execution.native_wall_delivery_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeWallDeliveryPolicy 当前仅支持 Harness Runtime")
    if self.context.compaction.native_summary_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeSummaryPolicy 当前仅支持 Harness Runtime")
    if (self.context.compaction.native_message_projection is not None
            or self.context.compaction.native_session_history_projection is not None) and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("Native context projections require Harness Runtime")
    if self.context.compaction.native_source_progress_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeSourceProgressPolicy 当前仅支持 Harness Runtime")
    if self.context.compaction.native_search_fragment_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeSearchFragmentPolicy requires Harness Runtime")
    if self.context.compaction.native_task_goal_projection_policy is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("nativeTaskGoalProjectionPolicy 当前仅支持 Harness Runtime")
    if self.execution.timeout_seconds == 0 and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("timeoutSeconds=0 当前仅支持 Harness Runtime")
    if self.execution.approval_timeout_seconds is not None and (
        self.runtime is None or self.runtime.type != "harness"
    ):
        raise ValueError("approvalTimeoutSeconds 当前仅支持 Harness Runtime")
    if not self.sub_agents:
        return self
    if self.runtime is not None and self.runtime.type != "harness":
        raise ValueError("subAgents 当前需要 Harness Runtime")
    names = {sub.name for sub in self.sub_agents}
    if len(names) != len(self.sub_agents):
        raise ValueError("subAgents 名称不能重复")
    dependencies = {sub.name: set(sub.depends_on) for sub in self.sub_agents}
    if any(deps - names for deps in dependencies.values()):
        raise ValueError("subAgents dependsOn 引用了未声明的子 Agent")
    resolved: set[str] = set()
    while len(resolved) < len(names):
        ready = {name for name, deps in dependencies.items() if deps <= resolved} - resolved
        if not ready:
            raise ValueError("subAgents dependsOn 存在依赖环")
        resolved.update(ready)
    return self


def _validate_memory_resource_reference(self: Any) -> Any:
    if not self.memory.enabled or not self.memory.provider_ref.startswith("binding://"):
        return self
    from ksadk.resource_runtime.plugin_config import resource_plugin_config

    binding_id = self.memory.provider_ref.removeprefix("binding://")
    for binding in self.bindings.plugins:
        if not binding.enabled:
            continue
        resource = resource_plugin_config(
            binding.plugin_ref, binding.ecosystem, binding.config, enabled=True
        )
        if resource is not None and resource.binding.id == binding_id:
            if resource.binding.resource.kind != "memory-instance":
                raise ValueError("Memory providerRef must reference a memory-instance binding")
            if self.memory.scopes != ["user"]:
                raise ValueError("Platform memory requires explicit scopes: [user]")
            return self
    raise ValueError("Memory providerRef references a missing or disabled resource binding")
