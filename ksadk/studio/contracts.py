"""Pydantic contracts for local authoring, builds, runs, and deployment."""

from __future__ import annotations

import re
import hashlib
import hmac
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ksadk.harness.spec import SubAgentBinding

from .lifecycle_status import (
    BuildStatus as BuildStatus,
)
from .lifecycle_status import (
    OperationKind as OperationKind,
)
from .lifecycle_status import (
    OperationStatus as OperationStatus,
)
from .lifecycle_status import (
    RunStatus as RunStatus,
)

from ksadk.studio.contract_base import ContractModel, _to_camel

from ksadk.studio.agent_configuration import (ModelParameters, ModelSpec, RetryPolicy, ExecutionSpec, CompactionSpec, ContextContributorsSpec, RolloutSpec, ContextSpec, MemoryRecallSpec, MemoryWriteSpec, MemoryScope, _default_memory_scopes, MemorySpec, NetworkPolicy, SecuritySpec, EvaluationSpec)

#: Anthropic Messages wire 的 API 版本头；探测与运行时必须保持一致。
ANTHROPIC_VERSION = "2023-06-01"


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
    # Optional deterministic per-Run budget. The Managed Harness reserves one
    # slot immediately before execution, so model retries and process restarts
    # cannot bypass the limit.
    max_calls_per_run: int | None = Field(default=None, ge=1, le=100_000)
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

    @model_validator(mode="after")
    def validate_platform_resource_config(self) -> "NativePluginBinding":
        from ksadk.resource_runtime.plugin_config import resource_plugin_config

        resource_plugin_config(self.plugin_ref, self.ecosystem, self.config, enabled=self.enabled)
        return self


class RemoteAgentBinding(ContractModel):
    """Immutable, credential-free Studio binding to one A2A version."""

    model_config = ConfigDict(frozen=True)

    binding_id: str = Field(pattern=r"^a2a-binding-[0-9a-f]{32}$")
    space_id: str
    target_agent_id: str
    expected_version_id: str
    local_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    exposure: Literal["native", "tool", "auto"] = "auto"
    policy_ref: str = Field(
        min_length=10,
        max_length=256,
        pattern=r"^policy://[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("space_id", "target_agent_id", "expected_version_id")
    @classmethod
    def validate_resource_id(cls, value: str, info) -> str:
        # Studio contracts also load during core discovery startup. Managed
        # runtime/database extras are needed only when an A2A binding is used.
        from ksadk.a2a.ids import require_a2a_resource_id

        prefix = {
            "space_id": "a2a-space-",
            "target_agent_id": "a2a-agent-",
            "expected_version_id": "a2a-version-",
        }[info.field_name]
        return require_a2a_resource_id(value, prefix, field_name=info.field_name)

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def freeze(
        cls,
        *,
        space_id: str,
        target_agent_id: str,
        expected_version_id: str,
        local_name: str,
        exposure: Literal["native", "tool", "auto"] = "auto",
        policy_ref: str,
        binding_id: str | None = None,
    ) -> "RemoteAgentBinding":
        identity = {
            "localName": local_name,
            "spaceId": space_id,
            "targetAgentId": target_agent_id,
        }
        frozen_binding_id = binding_id or (
            "a2a-binding-" + hashlib.sha256(cls._canonical(identity)).hexdigest()[:32]
        )
        payload = {
            "bindingId": frozen_binding_id,
            "spaceId": space_id,
            "targetAgentId": target_agent_id,
            "expectedVersionId": expected_version_id,
            "localName": local_name,
            "exposure": exposure,
            "policyRef": policy_ref,
        }
        return cls(
            **payload,
            digest="sha256:" + hashlib.sha256(cls._canonical(payload)).hexdigest(),
        )

    @model_validator(mode="after")
    def validate_digest(self) -> "RemoteAgentBinding":
        payload = self.model_dump(
            by_alias=True,
            exclude={"digest"},
            mode="json",
        )
        expected = "sha256:" + hashlib.sha256(self._canonical(payload)).hexdigest()
        if not hmac.compare_digest(self.digest, expected):
            raise ValueError("A2A_BINDING_DIGEST_MISMATCH")
        return self


class AgentBindings(ContractModel):
    remote_agents: list[RemoteAgentBinding] = Field(default_factory=list)
    model_profile_id: str | None = None
    model_profile_ids: list[str] = Field(default_factory=list)
    # Optional image-capable model for browser observations. This is a
    # separate locked role, never a fallback for the main Agent model.
    vision_model_profile_id: str | None = Field(default=None, min_length=1)
    # ``strict`` preserves the model selected for a turn.  ``automatic``
    # permits the additional bound profiles to be compiled as an ordered
    # availability fallback chain.
    model_failover_mode: Literal["strict", "automatic"] = "strict"
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
        names = [binding.local_name for binding in self.remote_agents]
        if len(names) != len(set(names)):
            raise ValueError("remoteAgents localName must be unique")
        targets = [binding.target_agent_id for binding in self.remote_agents]
        if len(targets) != len(set(targets)):
            raise ValueError("A2A_EXPOSURE_CONFLICT")
        ids = [binding.binding_id for binding in self.remote_agents]
        if len(ids) != len(set(ids)):
            raise ValueError("remoteAgents bindingId must be unique")
        plugin_refs = [binding.plugin_ref for binding in self.plugins if binding.enabled]
        if len(plugin_refs) != len(set(plugin_refs)):
            raise ValueError("启用的插件绑定不能重复 pluginRef")
        from ksadk.resource_runtime.plugin_config import validate_resource_plugin_bindings

        validate_resource_plugin_bindings(self.plugins)
        return self


class CapabilitiesSpec(ContractModel):
    skills: list[CapabilityRef] = Field(default_factory=list)
    mcp_servers: list[MCPServerRef] = Field(default_factory=list)
    tools: list[ToolContract] = Field(default_factory=list)


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


_RUNTIME_SECRET_KEY = re.compile(r"(?:secret|password|token|api[_-]?key)", re.IGNORECASE)
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
                if not isinstance(child, str) or not child.startswith(_RUNTIME_SECRET_REF_PREFIXES):
                    raise ValueError(f"{child_path} 必须保存 Secret 引用，不能保存明文")
            _reject_clear_runtime_secrets(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_clear_runtime_secrets(child, path=f"{path}[{index}]")


class A2ADesignNode(ContractModel):
    node_id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    kind: Literal["local", "remote"]
    binding_id: str | None = None

    @model_validator(mode="after")
    def validate_binding(self):
        if (self.kind == "remote") != (self.binding_id is not None):
            raise ValueError("A2A_NODE_BINDING_REQUIRED")
        return self


class A2ADesignEdge(ContractModel):
    source: str
    target: str
    kind: Literal["sequential", "handoff"]


class A2AOrchestration(ContractModel):
    entry_node_id: str
    nodes: tuple[A2ADesignNode, ...] = Field(min_length=1, max_length=64)
    edges: tuple[A2ADesignEdge, ...] = Field(default=(), max_length=63)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = {node.node_id for node in self.nodes}
        if len(ids) != len(self.nodes) or self.entry_node_id not in ids:
            raise ValueError("A2A_NODE_ID_INVALID")
        incoming: dict[str, int] = {}
        outgoing: dict[str, list[str]] = {key: [] for key in ids}
        sequential_sources: set[str] = set()
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids:
                raise ValueError("A2A_EDGE_NODE_MISSING")
            incoming[edge.target] = incoming.get(edge.target, 0) + 1
            if incoming[edge.target] > 1:
                raise ValueError("A2A_MULTIPLE_INCOMING_UNSUPPORTED")
            if edge.kind == "sequential":
                if edge.source in sequential_sources:
                    raise ValueError("A2A_PARALLEL_UNSUPPORTED")
                sequential_sources.add(edge.source)
            outgoing[edge.source].append(edge.target)
        visited, visiting = set(), set()
        def visit(node):
            if node in visiting:
                raise ValueError("A2A_LOOP_UNSUPPORTED")
            if node in visited:
                return
            visiting.add(node)
            for target in outgoing[node]:
                visit(target)
            visiting.remove(node)
            visited.add(node)
        for node in ids:
            visit(node)
        if self.entry_node_id in incoming:
            raise ValueError("A2A_ENTRY_HAS_INCOMING")
        return self


class AgentSpec(ContractModel):
    a2a_orchestration: A2AOrchestration | None = None
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
    sub_agents: tuple[SubAgentBinding, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_sub_agents(self) -> "AgentSpec":
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

    @model_validator(mode="after")
    def validate_memory_resource_reference(self) -> "AgentSpec":
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
    # Kept wire-compatible with ExecutionSpec; see its compatibility note.
    max_steps: int = Field(default=25, ge=1)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
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
    metadata: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)


class ResolvedCapabilities(ContractModel):
    skills: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[ToolContract] = Field(default_factory=list)


class ResolvedAgentSpec(ContractModel):
    a2a_orchestration: dict[str, Any] | None = None
    remote_agents: list[RemoteAgentBinding] = Field(default_factory=list)
    schema_version: Literal["agentkit.resolved/v1"] = "agentkit.resolved/v1"
    agent_id: str
    source_revision: int
    compiler_version: str = "1"
    instructions: Instructions
    soul: SoulDocument | None = None
    model: ResolvedModel
    vision_model: ResolvedModel | None = None
    # ``model`` is the selected primary.  Additional bound Model Profiles are
    # retained for user-initiated switching; they become a runtime failover
    # chain only when ``model_failover_mode`` explicitly requests it.
    fallback_models: list[ResolvedModel] = Field(default_factory=list, max_length=4)
    model_failover_mode: Literal["strict", "automatic"] = "strict"
    capabilities: ResolvedCapabilities
    execution: ExecutionSpec
    context: ContextSpec
    memory: MemorySpec
    security: SecuritySpec
    evaluation: EvaluationSpec
    sub_agents: tuple[SubAgentBinding, ...] = Field(default=(), max_length=32)
    source_digest: str
    resolved_digest: str = ""


class ValidationResult(ContractModel):
    valid: bool
    level: Literal["schema", "build", "release"] = "build"
    diagnostics: list[Diagnostic] = Field(default_factory=list)


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
    resource_build_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    resource_snapshot_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    files: list[FileEntry]
    created_at: str = "1970-01-01T00:00:00Z"
    bundle_digest: str = ""

    @model_validator(mode="after")
    def validate_composition_mode(self) -> "BundleManifest":
        if self.composition_mode == "composed" and not self.composition_profile_digest:
            raise ValueError("composed Bundle v2 requires compositionProfileDigest")
        if self.composition_mode == "legacy" and self.composition_profile_digest:
            raise ValueError("legacy Bundle v2 cannot declare compositionProfileDigest")
        if bool(self.resource_build_digest) != bool(self.resource_snapshot_digest):
            raise ValueError(
                "resourceBuildDigest and resourceSnapshotDigest must be declared together"
            )
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
# name documents that its ``agentkit.bundle/v2`` branch is the Release
# AgentBundleManifest/v2 contract; it is an alias, not a parallel manifest.
AgentBundleManifest = BundleManifest


class BuildRecord(ContractModel):
    a2a_orchestration_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    a2a_binding_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    id: str
    agent_id: str
    source_revision: int
    status: BuildStatus
    resolved_digest: str = ""
    runtime_type: str = ""
    source_digest: str = ""
    runtime_lock: dict[str, Any] = Field(default_factory=dict)
    bundle_digest: str = ""
    resource_build_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    resource_snapshot_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    artifact_path: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


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


class Usage(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    reported: bool = False
    source: str | None = None


class NativeRunOptions(ContractModel):
    """Host-admitted selections required to rebuild the same Native activation."""

    tool_approval_mode: Literal["ask", "risk", "full"] | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None


class RunSubmissionRef(ContractModel):
    """Transport correlation, validated against the admitted Agent and Session."""

    model_config = ConfigDict(frozen=True)

    client_invocation_id: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class RunRecord(ContractModel):
    id: str
    build_id: str
    agent_id: str
    session_id: str
    trace_id: str
    submission_ref: RunSubmissionRef | None = None
    manifest_sha256: str = ""
    runtime_type: str = ""
    model: str = ""
    collaboration_mode: str = ""
    goal_objective: str = ""
    presentation_profile: Literal["flat-v1", "agent-block-v1"] = "flat-v1"
    runtime_handle: dict[str, Any] = Field(default_factory=dict)
    native_runtime_options: NativeRunOptions | None = None
    status: RunStatus = RunStatus.CREATED
    # Native completion evidence is separate from scheduler termination.
    # A completed run can still be unverified or only partially accepted.
    verification_outcome: Literal["verified", "unverified", "partial", "blocked"] | None = None
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
    # Identity observed when the cloud Agent was created, never inferred from
    # whichever credentials happen to be configured when history is read.
    created_by_name: str | None = None
    created_by_user_id: str | None = None
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
