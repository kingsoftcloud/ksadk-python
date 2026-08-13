"""Deterministic compilation from AgentDraft to ResolvedAgentSpec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ksadk.studio.capabilities import (
    CapabilityResolver,
    LocalCapabilityResolver,
    canonical_json,
    sha256_digest,
)
from ksadk.studio.contracts import (
    AgentDraft,
    CapabilitiesSpec,
    ResolvedAgentSpec,
    ResolvedCapabilities,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.validator import AgentValidator
from ksadk.studio.workspace import Workspace


@dataclass(frozen=True)
class CompileResult:
    resolved: ResolvedAgentSpec
    dependency_lock: dict[str, Any]


class AgentCompiler:
    def __init__(
        self,
        workspace: Workspace,
        *,
        resolver: CapabilityResolver | None = None,
        validator: AgentValidator | None = None,
        catalog: LocalResourceCatalog | None = None,
    ) -> None:
        self.workspace = workspace
        self.resolver = resolver or LocalCapabilityResolver(workspace)
        self.validator = validator or AgentValidator()
        self.catalog = catalog or LocalResourceCatalog(workspace)

    def compile(self, draft: AgentDraft) -> CompileResult:
        source_payload = draft.model_dump(by_alias=True, exclude_none=True, mode="json")
        source_digest = sha256_digest(canonical_json(source_payload))
        materialized = self._materialize_bindings(draft)
        self.validator.validate(materialized, level="build", raise_on_error=True)
        assert materialized.spec.model is not None

        skills = [
            self.resolver.resolve_skill(ref)
            for ref in materialized.spec.capabilities.skills
            if ref.enabled
        ]
        mcp_servers = [
            self.resolver.resolve_mcp(ref)
            for ref in materialized.spec.capabilities.mcp_servers
            if ref.enabled
        ]
        tools = [
            self.resolver.resolve_tool(tool)
            for tool in materialized.spec.capabilities.tools
        ]
        skills.sort(key=lambda item: (item["name"], item["version"], item["digest"]))
        mcp_servers.sort(key=lambda item: (item["name"], item["version"], item["digest"]))
        tools.sort(key=lambda item: (item.name, item.version, item.digest or ""))

        resolved = ResolvedAgentSpec(
            agent_id=draft.metadata.id,
            source_revision=draft.metadata.revision,
            instructions=materialized.spec.instructions,
            model=self.resolver.resolve_model(materialized.spec.model),
            capabilities=ResolvedCapabilities(
                skills=skills,
                mcp_servers=mcp_servers,
                tools=tools,
            ),
            execution=materialized.spec.execution,
            context=materialized.spec.context,
            security=materialized.spec.security,
            evaluation=materialized.spec.evaluation,
            source_digest=source_digest,
        )
        digest_payload = resolved.model_dump(
            by_alias=True,
            exclude={"resolved_digest"},
            exclude_none=True,
            mode="json",
        )
        resolved.resolved_digest = sha256_digest(canonical_json(digest_payload))
        dependency_lock = {
            "lockVersion": "agentkit.lock/v1",
            "agentId": draft.metadata.id,
            "sourceRevision": draft.metadata.revision,
            "resolvedDigest": resolved.resolved_digest,
            "model": resolved.model.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "skills": skills,
            "mcpServers": mcp_servers,
            "tools": [
                tool.model_dump(by_alias=True, exclude_none=True, mode="json")
                for tool in tools
            ],
        }
        return CompileResult(resolved=resolved, dependency_lock=dependency_lock)

    def _materialize_bindings(self, draft: AgentDraft) -> AgentDraft:
        bindings = draft.spec.bindings
        if (
            not bindings.model_profile_id
            and not bindings.tools
            and not bindings.mcp_servers
            and not bindings.skills
        ):
            return draft
        materialized = draft.model_copy(deep=True)
        model = self.catalog.resolve_model(bindings) or materialized.spec.model
        if model is None:
            raise StudioError(
                "AGENT_MODEL_REQUIRED",
                "必须选择一个可用的 Model Profile",
                status_code=422,
                field="spec.bindings.modelProfileId",
            )
        bound_tools, permissions = self.catalog.policy_preview(bindings)
        bound_mcp_tools = self.catalog.resolve_mcp_tools(bindings)
        capabilities = CapabilitiesSpec(
            skills=[
                *materialized.spec.capabilities.skills,
                *self.catalog.resolve_skills(bindings),
            ],
            mcp_servers=[
                *materialized.spec.capabilities.mcp_servers,
                *self.catalog.resolve_mcp_servers(bindings),
            ],
            tools=[
                *materialized.spec.capabilities.tools,
                *bound_tools,
                *bound_mcp_tools,
            ],
        )
        self._require_unique(
            [item.name for item in capabilities.skills],
            kind="Skill",
        )
        self._require_unique(
            [item.name for item in capabilities.mcp_servers],
            kind="MCP Server",
        )
        self._require_unique(
            [item.name for item in capabilities.tools],
            kind="Tool",
        )
        materialized.spec.model = model
        materialized.spec.capabilities = capabilities
        materialized.spec.security.allowed_permissions = sorted(
            set(materialized.spec.security.allowed_permissions)
            | set(permissions)
            | {
                permission
                for tool in bound_mcp_tools
                for permission in tool.permissions
            }
        )
        endpoint = model.endpoint_url or model.base_url or ""
        hostname = (urlparse(endpoint).hostname or "").lower().rstrip(".")
        if hostname and materialized.spec.security.network.mode == "restricted":
            materialized.spec.security.network.allowed_hosts = sorted(
                set(materialized.spec.security.network.allowed_hosts) | {hostname}
            )
        return materialized

    @staticmethod
    def _require_unique(names: list[str], *, kind: str) -> None:
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise StudioError(
                "CAPABILITY_DUPLICATE",
                f"{kind} binding 重复",
                status_code=422,
                details={"names": duplicates},
            )
