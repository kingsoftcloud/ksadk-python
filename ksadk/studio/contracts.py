"""Pydantic contracts for local authoring, builds, runs, and deployment."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        use_enum_values=True,
    )


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Diagnostic(ContractModel):
    severity: DiagnosticSeverity
    code: str
    message: str
    field: str | None = None
    hint: str | None = None


class Instructions(ContractModel):
    system: str = Field(default="", max_length=32768)
    task: str = Field(default="", max_length=32768)


class SoulDocument(ContractModel):
    """Reviewed identity/boundary source that compiles ahead of task prompts.

    This is deliberately an immutable revision input, not a mutable memory
    file.  Runtime may read the compiled snapshot but cannot promote a new
    SoulDocument from a conversation.
    """

    schema_version: Literal["agentkit.soul/v1"] = "agentkit.soul/v1"
    identity: str = Field(min_length=1, max_length=4096)
    principles: list[str] = Field(default_factory=list, max_length=64)
    boundaries: list[str] = Field(default_factory=list, max_length=64)
    tone: str | None = Field(default=None, max_length=1024)

    @field_validator("principles", "boundaries")
    @classmethod
    def validate_nonempty_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("soul principles and boundaries must not contain empty items")
        return value


class ModelParameters(ContractModel):
    # 三者 None=未配置：请求 payload 一律不携带该字段，使用服务端默认，
    # 规避各模型族对 temperature/max_tokens 的硬约束（如 kimi 只接受默认温度）。
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    top_p: float | None = Field(default=None, gt=0, le=1)
    # 是否允许在 chat 请求中携带 response_format（json_object 结构化输出）。
    # 关闭后 compose 等结构化调用退回纯文本输出，兼容不支持该字段的网关。
    allow_json_response_format: bool = Field(default=True)


class ModelSpec(ContractModel):
    provider: str = Field(default="openai-compatible", min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    endpoint_url: str | None = None
    base_url: str | None = None
    wire_api: Literal["chat", "responses"] | None = None
    credential_ref: str = Field(min_length=1, max_length=512)
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    metadata: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_address(self) -> "ModelSpec":
        if bool(self.endpoint_url) == bool(self.base_url):
            raise ValueError("endpointUrl 和 baseUrl 必须且只能配置一个")
        return self


class CapabilityRef(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    digest: str | None = None
    enabled: bool = True


class MCPServerRef(CapabilityRef):
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    endpoint_url: str | None = None
    env_refs: dict[str, str] = Field(default_factory=dict)
    # ``dsh-profile`` is resolved at PluginHost activation time.  Only the
    # immutable profile/inventory identity is allowed into an Agent Build;
    # the loopback endpoint and bearer token remain process-scoped lease data.
    materialization: Literal["dsh-profile"] | None = None
    profile: str | None = None
    profile_digest: str | None = None
    descriptor_digest: str | None = None
    inventory_digest: str | None = None

    @model_validator(mode="after")
    def validate_transport(self) -> "MCPServerRef":
        dynamic_fields = (
            self.profile,
            self.profile_digest,
            self.descriptor_digest,
            self.inventory_digest,
        )
        if self.materialization == "dsh-profile":
            if self.transport != "http":
                raise ValueError("DSH Profile MCP 必须使用运行时 HTTP 租约")
            if self.endpoint_url or self.command or self.args or self.env_refs:
                raise ValueError("DSH Profile MCP 不能持久化 endpoint、命令或环境变量")
            if not all(dynamic_fields):
                raise ValueError("DSH Profile MCP 必须锁定 Profile 与能力摘要")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.profile or ""):
                raise ValueError("DSH Profile 名称无效")
            if any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", value or "")
                for value in dynamic_fields[1:]
            ):
                raise ValueError("DSH Profile MCP 摘要无效")
            return self
        if any(value is not None for value in dynamic_fields):
            raise ValueError("静态 MCP 不能声明 DSH Profile 运行时字段")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP 必须配置 command")
        if self.transport in {"http", "sse"} and not self.endpoint_url:
            raise ValueError("HTTP/SSE MCP 必须配置 endpointUrl")
        return self


class ToolContract(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=1024)
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    output_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    permissions: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=20, ge=1, le=3600)
    side_effect: Literal["none", "read", "write", "external"] = "none"
    approval: Literal["never", "always", "policy"] = "never"
    executor: Literal["builtin", "mcp", "deferred", "python"] = "builtin"
    mcp_server: str | None = None
    source_path: str | None = Field(default=None, min_length=1, max_length=1024)
    callable_name: str | None = Field(default=None, min_length=1, max_length=256)
    source_sha256: str | None = None
    digest: str | None = None
    group: str | None = None
    risk_level: str | None = None
    boundary: str | None = None
    backend: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_executor(self) -> "ToolContract":
        if self.executor == "mcp" and not self.mcp_server:
            raise ValueError("MCP Tool 必须配置 mcpServer")
        if self.executor != "mcp" and self.mcp_server:
            raise ValueError("只有 MCP Tool 可以配置 mcpServer")
        if self.executor == "python":
            if not self.source_path or not self.callable_name:
                raise ValueError("Python Tool 必须配置 sourcePath 和 callableName")
            normalized = self.source_path.strip().replace("\\", "/")
            if (
                normalized.startswith("/")
                or normalized == ".."
                or normalized.startswith("../")
                or "/../" in normalized
            ):
                raise ValueError("Python Tool sourcePath 必须位于工作区内")
            self.source_path = normalized
        elif self.source_path or self.callable_name or self.source_sha256:
            raise ValueError("只有 Python Tool 可以配置源码字段")
        return self


class ResourceDescriptor(ContractModel):
    resource_id: str = Field(min_length=3, max_length=256)
    kind: Literal["model", "tool", "mcp", "skill"]
    name: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    digest: str
    source: Literal["builtin", "provider", "local", "market"] = "local"
    status: Literal[
        "ready",
        "unhealthy",
        "invalid",
        "missing-secret",
        "unresolved",
    ] = "ready"
    description: str = Field(default="", max_length=4096)
    category: str = Field(default="general", max_length=64)
    installed: bool = True
    required_secret_refs: list[str] = Field(default_factory=list)
    contract: dict[str, Any] = Field(default_factory=dict)
    health: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CapabilityBinding(ContractModel):
    resource_id: str = Field(min_length=3, max_length=256)
    enabled: bool = True
    approval: Literal["never", "always", "policy"] | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class NativePluginBinding(ContractModel):
    """Immutable component selection from a host-managed native plugin."""

    ecosystem: Literal["codex", "dsh"]
    plugin_ref: str = Field(min_length=12, max_length=256)
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    components: list[str] = Field(min_length=1, max_length=128)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("plugin_ref")
    @classmethod
    def validate_plugin_ref(cls, value: str) -> str:
        if not re.fullmatch(
            r"plugin://[a-z0-9]+(?:[._-][a-z0-9]+)*@"
            r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
            value,
        ):
            raise ValueError("插件绑定必须使用 plugin://<id>@<exact-version>")
        return value

    @field_validator("components")
    @classmethod
    def validate_components(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("插件组件不能重复")
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", component)
            for component in value
        ):
            raise ValueError("插件组件 ID 无效")
        return value

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_clear_runtime_secrets(value, path="pluginBinding.config")
        return value


class AgentBindings(ContractModel):
    model_profile_id: str | None = None
    model_profile_ids: list[str] = Field(default_factory=list)
    model_parameters: ModelParameters | None = None
    policy_template: Literal["loose", "strict", "custom"] = "strict"
    tools: list[CapabilityBinding] = Field(default_factory=list)
    mcp_servers: list[CapabilityBinding] = Field(default_factory=list)
    skills: list[CapabilityBinding] = Field(default_factory=list)
    plugins: list[NativePluginBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_model_profiles(self) -> "AgentBindings":
        if len(set(self.model_profile_ids)) != len(self.model_profile_ids):
            raise ValueError("modelProfileIds 不能包含重复资源")
        if self.model_profile_ids and not self.model_profile_id:
            raise ValueError("绑定多个模型时必须指定默认 modelProfileId")
        if (
            self.model_profile_id
            and self.model_profile_ids
            and self.model_profile_id not in self.model_profile_ids
        ):
            raise ValueError("默认 modelProfileId 必须包含在 modelProfileIds 中")
        plugin_refs = [binding.plugin_ref for binding in self.plugins if binding.enabled]
        if len(plugin_refs) != len(set(plugin_refs)):
            raise ValueError("启用的插件绑定不能重复 pluginRef")
        return self


class CapabilitiesSpec(ContractModel):
    skills: list[CapabilityRef] = Field(default_factory=list)
    mcp_servers: list[MCPServerRef] = Field(default_factory=list)
    tools: list[ToolContract] = Field(default_factory=list)


class RetryPolicy(ContractModel):
    max_attempts: int = Field(default=2, ge=1, le=10)
    backoff_seconds: float = Field(default=1, ge=0, le=60)


class ExecutionSpec(ContractModel):
    strategy: Literal["direct", "plan-act-observe"] = "direct"
    max_steps: int = Field(default=12, ge=1, le=100)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    sandbox: str | None = None
    approval_mode: str | None = None


class CompactionSpec(ContractModel):
    enabled: bool = True
    threshold_ratio: float = Field(default=0.8, gt=0, le=1)
    # PCM：双阈值（方案 §8.2 / §9.1）。soft=主动整理，hard=强制压缩。
    soft_threshold_ratio: float = Field(default=0.50, gt=0, le=1)
    hard_threshold_ratio: float = Field(default=0.85, gt=0, le=1)
    preserve_working_state: bool = True
    flush_memory_before_compaction: bool = True

    @model_validator(mode="after")
    def validate_ratios(self) -> "CompactionSpec":
        if self.soft_threshold_ratio >= self.hard_threshold_ratio:
            raise ValueError("softThresholdRatio 必须小于 hardThresholdRatio")
        return self


class ContextContributorsSpec(ContractModel):
    """ContextContributor 开关与预算（方案 §5.1 / §8.7）。默认按 policy，可显式开关。"""

    workspace_rules: bool | None = None
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


class RuntimeRef(ContractModel):
    """Per-Agent reference to one registered RuntimeAdapter implementation.

    Studio is a control plane and therefore must not have a process-wide runtime
    mode.  Each Agent declares the adapter type and, for Python frameworks, the
    project entrypoint that is snapshotted by its Build.
    """

    type: Literal["codex", "adk", "langgraph", "harness", "plugin"]
    project_path: str | None = Field(default=None, min_length=1, max_length=1024)
    entry_point: str | None = Field(default=None, min_length=1, max_length=1024)
    agent_variable: str = Field(default="root_agent", min_length=1, max_length=256)
    version: str | None = Field(default=None, min_length=1, max_length=64)
    detection: Literal["declared", "auto"] = "declared"
    provider_ref: str | None = Field(default=None, min_length=12, max_length=256)
    provider_config: dict[str, Any] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
    )

    @field_validator("project_path", "entry_point")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().replace("\\", "/")
        if (
            not normalized
            or normalized.startswith("/")
            or normalized == ".."
            or normalized.startswith("../")
            or "/../" in normalized
        ):
            raise ValueError("Runtime 路径必须是工作区内的相对路径")
        return normalized

    @field_validator("provider_ref")
    @classmethod
    def validate_provider_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(
            r"plugin://[a-z0-9]+(?:[._-][a-z0-9]+)*@"
            r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
            value,
        ):
            raise ValueError("Provider 引用必须固定为 plugin://<id>@<exact-version>")
        return value

    @field_validator("provider_config")
    @classmethod
    def validate_provider_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_clear_runtime_secrets(value)
        return value

    @model_validator(mode="after")
    def validate_framework_source(self) -> "RuntimeRef":
        if self.type == "plugin":
            if not self.provider_ref:
                raise ValueError("plugin Runtime 必须配置 providerRef")
        elif self.provider_ref is not None or self.provider_config:
            raise ValueError("只有 plugin Runtime 可以配置 providerRef/providerConfig")
        if self.type in {"adk", "langgraph"}:
            if not self.project_path:
                raise ValueError(f"{self.type} Runtime 必须配置 projectPath")
            if self.detection == "declared" and not self.entry_point:
                raise ValueError(f"{self.type} Runtime 使用 declared 检测时必须配置 entryPoint")
        return self


_RUNTIME_SECRET_KEY = re.compile(
    r"(?:secret|password|token|api[_-]?key)", re.IGNORECASE
)
_RUNTIME_SECRET_REF_PREFIXES = (
    "secret://",
    "env://",
    "credential://",
    "vault://",
)


def _reject_clear_runtime_secrets(value: Any, *, path: str = "providerConfig") -> None:
    """Provider config is revision data: it may only retain secret references."""

    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if _RUNTIME_SECRET_KEY.search(str(key)) and child is not None:
                if not isinstance(child, str) or not child.startswith(
                    _RUNTIME_SECRET_REF_PREFIXES
                ):
                    raise ValueError(f"{child_path} 必须保存 Secret 引用，不能保存明文")
            _reject_clear_runtime_secrets(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_clear_runtime_secrets(child, path=f"{path}[{index}]")


class AgentSpec(ContractModel):
    description: str = Field(default="", max_length=1024)
    runtime: RuntimeRef | None = None
    instructions: Instructions = Field(default_factory=Instructions)
    soul: SoulDocument | None = None
    model: ModelSpec | None = None
    capabilities: CapabilitiesSpec = Field(default_factory=CapabilitiesSpec)
    bindings: AgentBindings = Field(default_factory=AgentBindings)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    context: ContextSpec = Field(default_factory=ContextSpec)
    memory: MemorySpec = Field(default_factory=MemorySpec)
    security: SecuritySpec = Field(default_factory=SecuritySpec)
    evaluation: EvaluationSpec = Field(default_factory=EvaluationSpec)


_AGENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_AGENT_AVATAR_URL_PATTERN = re.compile(r"^/api/v1/assets/agent-avatars/[0-9a-f]{64}\.(?:png|webp)$")


class AgentAppearance(ContractModel):
    icon: Literal["bot", "sparkles", "search", "code", "workflow"] = "bot"
    color: str = Field(default="#426ea8", pattern=r"^#[0-9a-fA-F]{6}$")
    image_url: str | None = Field(default=None, max_length=160)

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return value.lower()

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str | None) -> str | None:
        if value is not None and not _AGENT_AVATAR_URL_PATTERN.fullmatch(value):
            raise ValueError("imageUrl 必须引用当前工作区的内容寻址头像资源")
        return value


class AgentMetadata(ContractModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    labels: dict[str, str] = Field(default_factory=dict)
    appearance: AgentAppearance = Field(default_factory=AgentAppearance)

    @field_validator("id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        if not _AGENT_ID_PATTERN.fullmatch(value):
            raise ValueError("id 必须以小写字母开头，仅包含小写字母、数字和连字符，长度 3..63")
        return value


class AgentDraft(ContractModel):
    api_version: Literal["agentkit.ksyun.com/v1alpha1"] = "agentkit.ksyun.com/v1alpha1"
    kind: Literal["Agent"] = "Agent"
    metadata: AgentMetadata
    spec: AgentSpec = Field(default_factory=AgentSpec)


class AgentTemplateComposeRequest(ContractModel):
    prompt: str = Field(default="", max_length=32768)
    goal: str = Field(default="", max_length=4096)
    description: str = Field(default="", max_length=1024)
    task_prompt: str = Field(default="", max_length=32768)
    audience: str = Field(default="技术与业务决策者", min_length=1, max_length=256)
    language: Literal["zh-CN", "en-US"] = "zh-CN"
    depth: Literal["focused", "standard", "deep"] = "deep"
    output_format: Literal["brief", "report", "evidence-table"] = "report"
    model_profile_id: str | None = None
    model_profile_ids: list[str] = Field(default_factory=list)
    tool_resource_ids: list[str] = Field(default_factory=list)
    skill_resource_ids: list[str] = Field(default_factory=list)
    mcp_resource_ids: list[str] = Field(default_factory=list)
    policy_template: Literal["loose", "strict", "custom"] = "strict"
    execution_strategy: Literal["direct", "plan-act-observe"] = "direct"
    max_steps: int = Field(default=12, ge=1, le=100)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    auto_bind_tools: bool = True
    auto_bind_mcp: bool = True

    @model_validator(mode="after")
    def validate_authoring_prompt(self) -> "AgentTemplateComposeRequest":
        if not self.prompt.strip() and not self.goal.strip():
            raise ValueError("prompt 和 goal 至少填写一项")
        return self


class AgentTemplateRecommendation(ContractModel):
    kind: Literal["model", "tool", "mcp", "skill"]
    status: Literal["bound", "available", "missing", "warning"]
    title: str
    reason: str
    resource_id: str | None = None


class AgentBehaviorDesign(ContractModel):
    """Human-readable explanation of the generated Agent behavior contract."""

    role: str
    objective: str
    operating_principles: list[str] = Field(default_factory=list)
    workflow: list[str] = Field(default_factory=list)
    explicit_boundaries: list[str] = Field(default_factory=list)
    safety_boundaries: list[str] = Field(default_factory=list)
    output_expectations: list[str] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)


class AgentTemplateComposition(ContractModel):
    template_id: Literal["blank", "research"]
    spec: AgentSpec
    behavior_design: AgentBehaviorDesign | None = None
    recommendations: list[AgentTemplateRecommendation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ResolvedModel(ContractModel):
    provider: str
    model: str
    endpoint_url: str
    credential_ref: str
    parameters: ModelParameters
    wire_api: str | None = None


class ResolvedCapabilities(ContractModel):
    skills: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[ToolContract] = Field(default_factory=list)


class ResolvedAgentSpec(ContractModel):
    schema_version: Literal["agentkit.resolved/v1"] = "agentkit.resolved/v1"
    agent_id: str
    source_revision: int
    compiler_version: str = "1"
    instructions: Instructions
    soul: SoulDocument | None = None
    model: ResolvedModel
    capabilities: ResolvedCapabilities
    execution: ExecutionSpec
    context: ContextSpec
    memory: MemorySpec
    security: SecuritySpec
    evaluation: EvaluationSpec
    source_digest: str
    resolved_digest: str = ""


class ValidationResult(ContractModel):
    valid: bool
    level: Literal["schema", "build", "release"] = "build"
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class BuildStatus(str, Enum):
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    RESOLVING = "RESOLVING"
    COMPILING = "COMPILING"
    PACKAGING = "PACKAGING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class FileEntry(ContractModel):
    path: str
    sha256: str
    size: int = Field(ge=0)


class BundleManifest(ContractModel):
    # v1 remains readable for existing local Build records. Every new Studio
    # build uses v2 because Server admission requires a deterministic plugin
    # lock even when the lock is empty.
    bundle_format: Literal["agentkit.bundle/v1", "agentkit.bundle/v2"] = "agentkit.bundle/v1"
    agent_id: str
    source_revision: int
    resolved_digest: str
    runtime_type: str = ""
    source_digest: str = ""
    runtime_contract: Literal["agentkit.runtime/v1"] = "agentkit.runtime/v1"
    plugin_lock_digest: str = ""
    # A v0.8.2 bundle already used the v2 envelope without a composition.
    # Keep that wire shape readable as the explicit legacy execution profile;
    # newly built bundles write ``composition_mode`` so consumers never need
    # to infer whether PluginHost admission is required from missing files.
    composition_mode: Literal["legacy", "composed"] | None = None
    composition_profile_digest: str | None = None
    hosted_kernel_requirement_digest: str = ""
    files: list[FileEntry]
    created_at: str = "1970-01-01T00:00:00Z"
    bundle_digest: str = ""

    @model_validator(mode="after")
    def validate_composition_mode(self) -> "BundleManifest":
        if self.composition_mode == "composed" and not self.composition_profile_digest:
            raise ValueError("composed Bundle v2 requires compositionProfileDigest")
        if self.composition_mode == "legacy" and self.composition_profile_digest:
            raise ValueError("legacy Bundle v2 cannot declare compositionProfileDigest")
        return self

    @property
    def execution_profile(self) -> Literal["legacy", "composed"]:
        """Normalize historical v2 manifests without rewriting their bytes.

        ``compositionMode`` was added after the v0.8.2 envelope.  Its absence
        remains a backward-compatible projection: an embedded composition
        digest is composed; its absence selects the established runtime path.
        """

        if self.composition_mode is not None:
            return self.composition_mode
        return "composed" if self.composition_profile_digest else "legacy"


# ``BundleManifest`` is the existing, installed source type.  The explicit
# name documents that its ``agentkit.bundle/v2`` branch is the Phase 2
# AgentBundleManifest/v2 contract; it is an alias, not a parallel manifest.
AgentBundleManifest = BundleManifest


class BuildRecord(ContractModel):
    id: str
    agent_id: str
    source_revision: int
    status: BuildStatus
    resolved_digest: str = ""
    runtime_type: str = ""
    source_digest: str = ""
    runtime_lock: dict[str, Any] = Field(default_factory=dict)
    bundle_digest: str = ""
    artifact_path: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class OperationKind(str, Enum):
    BUILD = "BUILD"
    RUN = "RUN"
    EVALUATION = "EVALUATION"
    DEPLOYMENT = "DEPLOYMENT"


class OperationStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class Operation(ContractModel):
    id: str
    kind: OperationKind
    status: OperationStatus = OperationStatus.QUEUED
    resource_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class OperationEvent(ContractModel):
    id: int = Field(ge=1)
    operation_id: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_INPUT = "WAITING_INPUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    TIMED_OUT = "TIMED_OUT"


class Usage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    reported: bool = False
    source: str | None = None


class RunRecord(ContractModel):
    id: str
    build_id: str
    agent_id: str
    session_id: str
    trace_id: str
    manifest_sha256: str = ""
    runtime_type: str = ""
    model: str = ""
    collaboration_mode: str = ""
    goal_objective: str = ""
    runtime_handle: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.CREATED
    input: str
    output: str = ""
    usage: Usage = Field(default_factory=Usage)
    error: dict[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    duration_source: Literal["runtime", "studio"] | None = None
    # PR-S4：PCM evidence（方案 §6.3）。由 run_service 以 shadow 方式捕获（不进真实输入），
    # 供 Context Inspector 展示 planned/projected/actual + 精度。默认空（未捕获）。
    context_plan: dict[str, Any] | None = None
    prompt_evidence: dict[str, Any] | None = None
    working_state: dict[str, Any] | None = None


class RunEvent(ContractModel):
    id: int = Field(ge=1)
    run_id: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AssertionSpec(ContractModel):
    type: Literal[
        "contains",
        "equals",
        "notContains",
        "jsonSchema",
        "maxLatencyMs",
        "maxInputTokens",
        "maxOutputTokens",
        "toolCalled",
        "toolNotCalled",
    ]
    value: Any


class EvaluationCase(ContractModel):
    id: str = Field(min_length=1, max_length=128)
    input: str
    assertions: list[AssertionSpec] = Field(min_length=1)


class EvaluationSuite(ContractModel):
    api_version: Literal["agentkit.ksyun.com/v1alpha1"] = "agentkit.ksyun.com/v1alpha1"
    kind: Literal["EvaluationSuite"] = "EvaluationSuite"
    metadata: dict[str, Any]
    cases: list[EvaluationCase] = Field(min_length=1)


class AssertionResult(ContractModel):
    assertion: AssertionSpec
    passed: bool
    message: str = ""


class EvaluationCaseResult(ContractModel):
    case_id: str
    run_id: str
    passed: bool
    assertions: list[AssertionResult]


class EvaluationRun(ContractModel):
    id: str
    build_id: str
    status: RunStatus = RunStatus.CREATED
    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0
    results: list[EvaluationCaseResult] = Field(default_factory=list)


class DeploymentTarget(ContractModel):
    region: str = Field(min_length=1, max_length=64)
    environment: str = Field(min_length=1, max_length=64)


class EnvironmentBinding(ContractModel):
    model: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    mcp_servers: dict[str, str] = Field(default_factory=dict)


class ReleasePolicy(ContractModel):
    strategy: Literal["rolling", "recreate"] = "rolling"
    approval: Literal["none", "manual"] = "none"


class DeploymentRequest(ContractModel):
    target: DeploymentTarget
    binding: EnvironmentBinding = Field(default_factory=EnvironmentBinding)
    release_policy: ReleasePolicy = Field(default_factory=ReleasePolicy)


class DeploymentRecord(ContractModel):
    id: str
    build_id: str
    bundle_digest: str
    version_id: str
    status: Literal["ADMITTING", "DEPLOYING", "READY", "FAILED", "ROLLED_BACK"]
    target: DeploymentTarget
    # These are receipts from the existing Agent creation control plane, not
    # Studio-generated deployment identities.
    agent_id: str | None = None
    instance_id: str | None = None
    endpoint: str | None = None
    # Immutable KS3 object selected by this receipt. It is a deployment fact,
    # not a browser-supplied credential or a mutable "latest" alias.
    bundle_uri: str | None = None
    artifact_id: str | None = None
    # New direct-cloud receipts are expected to pass AgentKernel/v1 admission.
    # Older receipts intentionally default to false for read compatibility.
    requires_kernel: bool = False
