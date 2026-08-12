"""HTTP request contracts used by the AgentKit Studio control plane."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SecretStr

from ksadk.evaluation import EvaluationConfig as PublicEvaluationConfig
from ksadk.evaluation import TargetRef
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    ContractModel,
    MCPServerRef,
    ModelSpec,
    RuntimeRef,
    ToolContract,
)


class SessionExchangeRequest(ContractModel):
    token: str = Field(min_length=16, max_length=512)


class WorkspaceOpenRequest(ContractModel):
    path: str
    create: bool = False


class CreateAgentRequest(ContractModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,62}$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    template: str = "blank"
    runtime: RuntimeRef | None = None
    spec: AgentSpec | None = None


class ValidationRequest(ContractModel):
    revision: int = Field(ge=1)
    level: Literal["schema", "build", "release"] = "build"


class BuildRequest(ContractModel):
    revision: int = Field(ge=1)
    run_evaluation: bool = False
    evaluation_suite_refs: list[str] = Field(default_factory=list)


class MessageInput(ContractModel):
    role: str = "user"
    content: str = Field(min_length=1, max_length=1_000_000)


class QuickAuthoringRequest(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=1, max_length=63)
    runtime_type: Literal["codex", "adk", "langgraph"]
    template: Literal["blank", "research"] = "blank"
    description: str = Field(default="", max_length=1024)
    spec: AgentSpec | None = None


class AuthoringConversationMessage(ContractModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)


class ConversationAuthoringRequest(ContractModel):
    messages: list[AuthoringConversationMessage] = Field(min_length=1, max_length=100)
    model_profile_id: str = Field(min_length=3, max_length=256)


class AuthoringCommitRequest(ContractModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=1, max_length=63)
    model_profile_id: str | None = Field(default=None, min_length=3, max_length=256)


class ProjectInspectRequest(ContractModel):
    path: str = Field(min_length=1, max_length=4096)


class RunRequest(ContractModel):
    session_id: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=256)
    input: MessageInput
    environment: str = "local"
    stream: bool = True
    sandbox: str | None = None


class InteractionSubmitRequest(ContractModel):
    name: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


class EvaluationRequest(ContractModel):
    suite_refs: list[str] = Field(min_length=1)
    concurrency: int = Field(default=1, ge=1, le=4)
    fail_fast: bool = False


class StudioEvaluationCreate(ContractModel):
    """Request for the shared CLI/Studio evaluation executor."""

    evalset_file: str = Field(min_length=1, max_length=4096)
    target: TargetRef
    config: PublicEvaluationConfig = Field(default_factory=PublicEvaluationConfig)


class SecretReferenceCheckRequest(ContractModel):
    ref: str


class CredentialPutRequest(ContractModel):
    value: SecretStr = Field(min_length=1, max_length=16_384)
    persistence: Literal["session"] = "session"


class RollbackRequest(ContractModel):
    target_build_id: str


class ModelProfileCreateRequest(ContractModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9._-]{1,127}$")
    display_name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    description: str = Field(default="", max_length=4096)
    spec: ModelSpec


class ModelEndpointProbeRequest(ContractModel):
    url: str = Field(min_length=8, max_length=1024)
    credential_ref: str | None = Field(default=None, max_length=512)
    api_key: SecretStr | None = None


class MCPResourceCreateRequest(ContractModel):
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4096)
    server: MCPServerRef


class ToolResourceCreateRequest(ContractModel):
    display_name: str = Field(min_length=1, max_length=128)
    category: str = Field(default="custom", max_length=64)
    contract: ToolContract


class PythonToolCommitRequest(ContractModel):
    display_name: str = Field(min_length=1, max_length=128)
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    callable_name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    description: str = Field(default="", max_length=1024)


class ToolSchemaValidationRequest(ContractModel):
    schema_definition: dict[str, Any] = Field(alias="schema")
    sample: Any = None


class ToolPolicyPreviewRequest(ContractModel):
    bindings: AgentBindings


class SkillDiscoveryRequest(ContractModel):
    scan_paths: list[str] = Field(default_factory=list, max_length=20)


class SkillDiscoveryCommitRequest(ContractModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    overwrite: bool = False


__all__ = [name for name in globals() if name.endswith("Request")]
