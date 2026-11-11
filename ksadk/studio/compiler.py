"""Deterministic compilation from AgentDraft to ResolvedAgentSpec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ksadk.conversations.model_context import supports_native_image_input
from ksadk.plugins.providers.legacy_catalog import CODEX_AGENT_PROVIDER_PLUGIN_ID
from ksadk.resource_runtime.plugin_config import resource_plugin_config
from ksadk.studio.capabilities import (
    CapabilityResolver,
    LocalCapabilityResolver,
    canonical_json,
    sha256_digest,
)
from ksadk.studio.contracts import (
    AgentBindings,
    AgentDraft,
    CapabilitiesSpec,
    ModelSpec,
    NativeExecutionLimits,
    RemoteAgentBinding,
    ResolvedAgentSpec,
    ResolvedCapabilities,
    ResolvedModel,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.soul import compose_system_instruction
from ksadk.studio.validator import AgentValidator
from ksadk.studio.workspace import Workspace


@dataclass(frozen=True)
class CompileResult:
    resolved: ResolvedAgentSpec
    dependency_lock: dict[str, Any]
    a2a_source: str | None = None
    a2a_entry_source: str | None = None


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
        from ksadk.studio.a2a_bindings import require_execution_policy
        from ksadk.studio.a2a_orchestration import compile_orchestration

        orchestration = compile_orchestration(draft)
        for binding in draft.spec.bindings.remote_agents:
            RemoteAgentBinding.model_validate(binding.model_dump())
            require_execution_policy(binding.policy_ref)
        unsupported_plugins = [
            binding.plugin_ref
            for binding in draft.spec.bindings.plugins
            if binding.enabled
            and resource_plugin_config(
                binding.plugin_ref,
                binding.ecosystem,
                binding.config,
                enabled=True,
            )
            is None
        ]
        if unsupported_plugins:
            raise StudioError(
                "NATIVE_PLUGIN_RUNTIME_INCOMPATIBLE",
                "Framework Agent 不能直接绑定未物化的原生插件快照",
                status_code=422,
                field="spec.bindings.plugins",
                details={"pluginRefs": sorted(unsupported_plugins)},
            )
        direct_dynamic = [
            server.name
            for server in draft.spec.capabilities.mcp_servers
            if server.enabled and server.materialization == "dsh-profile"
        ]
        if direct_dynamic:
            raise StudioError(
                "DSH_MCP_MANAGED_BINDING_REQUIRED",
                "DSH Profile MCP 必须从当前 Catalog Resource 显式绑定，不能直接写入 AgentSpec",
                status_code=422,
                field="spec.capabilities.mcpServers",
                details={"servers": sorted(direct_dynamic)},
            )
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
        tools = [self.resolver.resolve_tool(tool) for tool in materialized.spec.capabilities.tools]
        skills.sort(key=lambda item: (item["name"], item["version"], item["digest"]))
        mcp_servers.sort(key=lambda item: (item["name"], item["version"], item["digest"]))
        tools.sort(key=lambda item: (item.name, item.version, item.digest or ""))
        available_tools = {tool.name for tool in tools}
        for sub in materialized.spec.sub_agents:
            if set(sub.tools) - available_tools or sub.name in available_tools:
                raise StudioError(
                    "SUBAGENT_TOOL_SCOPE_INVALID",
                    "子 Agent 只能使用父 Agent 已绑定工具，且子 Agent 名称不能与工具重名",
                    status_code=422,
                    field="spec.subAgents",
                )

        resolved_model = self.resolver.resolve_model(materialized.spec.model)
        vision_spec = self._vision_model_spec(materialized.spec.bindings)
        vision_model = self.resolver.resolve_model(vision_spec) if vision_spec is not None else None
        if (
            vision_model is not None
            and not supports_native_image_input(vision_model.metadata)
        ):
            raise StudioError(
                "VISION_MODEL_IMAGE_INPUT_REQUIRED",
                "视觉模型资源必须显式声明图片输入能力",
                status_code=422,
                field="spec.bindings.visionModelProfileId",
            )
        bound_models = [
            self.resolver.resolve_model(model)
            for model in self.catalog.resolve_models(materialized.spec.bindings)
        ]
        fallback_models = []
        if materialized.spec.bindings.model_failover_mode == "automatic":
            fallback_models = [
                model
                for model in bound_models
                if not (
                    model.model == resolved_model.model
                    and model.endpoint_url == resolved_model.endpoint_url
                )
            ]

        # 子 Agent 模型选择：宿主只能从本 Agent 已绑定的可信 Model Profile 中选。
        # authorized_profile_ids 与父级 fallback 链同源，但子模型选择独立于
        # model_failover_mode——不触发 Native 自动切换策略。每个选定模型经 Catalog
        # 解析为 ResolvedModel 并封存进 Build；显式但越权/缺资源的声明在此失败，
        # 不静默回退父模型（对齐 ZCode resolveSubagentSelection 的失败语义）。
        authorized_profile_ids = {
            materialized.spec.bindings.model_profile_id,
            *materialized.spec.bindings.model_profile_ids,
        }
        authorized_profile_ids.discard(None)
        profile_models: dict[str, ResolvedModel] = {}
        for resource_id in authorized_profile_ids:
            selected_bindings = materialized.spec.bindings.model_copy(
                update={"model_profile_id": resource_id, "model_profile_ids": []}
            )
            selected_spec = self.catalog.resolve_model(selected_bindings)
            if selected_spec is not None:
                profile_models[resource_id] = self.resolver.resolve_model(selected_spec)
        sub_agent_models: dict[str, ResolvedModel] = {}
        for sub in materialized.spec.sub_agents:
            if sub.model_profile_id is None:
                continue
            if sub.model_profile_id not in authorized_profile_ids:
                raise StudioError(
                    "SUBAGENT_MODEL_PROFILE_OUT_OF_SCOPE",
                    "子 Agent 选定的模型必须由宿主在本 Agent 绑定中授权",
                    status_code=422,
                    field="spec.subAgents",
                    details={
                        "subAgent": sub.name,
                        "modelProfileId": sub.model_profile_id,
                    },
                )
            resolved_child_model = profile_models.get(sub.model_profile_id)
            if resolved_child_model is None:
                raise StudioError(
                    "SUBAGENT_MODEL_PROFILE_UNAVAILABLE",
                    "子 Agent 选定的模型资源当前不可用",
                    status_code=409,
                    field="spec.subAgents",
                    details={
                        "subAgent": sub.name,
                        "modelProfileId": sub.model_profile_id,
                    },
                )
            sub_agent_models[sub.name] = resolved_child_model

        execution = materialized.spec.execution
        if (materialized.spec.runtime is not None
                and materialized.spec.runtime.type == "harness"
                and execution.native_limits is None):
            # Stamp new Builds explicitly. Replaying an older sealed bundle
            # does not pass through this compiler and retains its old limits.
            execution = execution.model_copy(update={"native_limits": NativeExecutionLimits()})
        if (materialized.spec.runtime is not None
                and materialized.spec.runtime.type == "harness"):
            execution = execution.model_copy(update={
                "native_command_lifecycle_policy": "owned-local-command/v1",
                "native_stream_timeout_policy": "idle/v1",
                "tool_error_recovery_policy": "diagnose-before-repeat/v1",
                "native_skill_metadata_policy": "bound-skill-metadata/v1" if skills else None,
                "native_directory_observation_policy": "studio-directory-observation/v3",
                "native_directory_navigation_policy": "receipt-directory-progress/v1",
                "native_wall_delivery_policy": "deadline-delivery/v7",
                "native_background_notification_policy": (
                    "completed-before-model/v1" if all(
                        any(tool.name == name and tool.executor == "builtin" for tool in tools)
                        for name in ("run_command", "read_command_output", "stop_command")
                    ) else None
                ),
            })

        context = materialized.spec.context
        if (materialized.spec.runtime is not None
                and materialized.spec.runtime.type == "harness"):
            context = context.model_copy(update={
                "compaction": context.compaction.model_copy(update={
                    "native_summary_policy": "rolling-work-state/v3",
                    "native_message_projection": "current-user-after-summary/v1",
                    "native_session_history_projection": "sealed-session-summary/v1",
                    "native_source_progress_policy": "receipt-source-progress/v5",
                    "native_task_goal_projection_policy": "retained-user-goal/v2",
                    "native_search_fragment_policy": "complete-search-fragments/v1",
                }),
            })

        resolved = ResolvedAgentSpec(
            a2a_orchestration=({**orchestration.plan, "digest": orchestration.digest} if orchestration else None),
            agent_id=draft.metadata.id,
            source_revision=draft.metadata.revision,
            instructions=compose_system_instruction(
                materialized.spec.instructions,
                materialized.spec.soul,
            ),
            soul=materialized.spec.soul,
            model=resolved_model,
            vision_model=vision_model,
            fallback_models=fallback_models[:4],
            model_failover_mode=materialized.spec.bindings.model_failover_mode,
            capabilities=ResolvedCapabilities(
                skills=skills,
                mcp_servers=mcp_servers,
                tools=tools,
            ),
            remote_agents=sorted(materialized.spec.bindings.remote_agents, key=lambda item: (item.local_name, item.binding_id)),
            execution=execution,
            context=context,
            memory=materialized.spec.memory,
            security=materialized.spec.security,
            evaluation=materialized.spec.evaluation,
            sub_agents=materialized.spec.sub_agents,
            sub_agent_models=sub_agent_models,
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
            "fallbackModels": [
                model.model_dump(by_alias=True, exclude_none=True, mode="json")
                for model in resolved.fallback_models
            ],
            **(
                {
                    "visionModel": vision_model.model_dump(
                        by_alias=True, exclude_none=True, mode="json"
                    )
                }
                if vision_model is not None
                else {}
            ),
            "skills": skills,
            "mcpServers": mcp_servers,
            "tools": [
                tool.model_dump(by_alias=True, exclude_none=True, mode="json") for tool in tools
            ],
        }
        dependency_lock["remoteAgents"] = [binding.model_dump(by_alias=True, mode="json") for binding in resolved.remote_agents]
        if orchestration:
            dependency_lock["a2aOrchestration"] = {**orchestration.plan, "digest": orchestration.digest}
        return CompileResult(resolved=resolved, dependency_lock=dependency_lock,
                             a2a_source=orchestration.source if orchestration else None,
                             a2a_entry_source=orchestration.entry_source if orchestration else None)

    def _vision_model_spec(self, bindings: AgentBindings) -> ModelSpec | None:
        profile_id = bindings.vision_model_profile_id
        if profile_id is None:
            return None
        selected = bindings.model_copy(
            update={
                "model_profile_id": profile_id,
                "model_profile_ids": [],
                "model_parameters": None,
                "vision_model_profile_id": None,
            }
        )
        model = self.catalog.resolve_model(selected)
        if model is None:
            raise StudioError(
                "VISION_MODEL_PROFILE_REQUIRED",
                "视觉模型资源不可用",
                status_code=422,
                field="spec.bindings.visionModelProfileId",
            )
        return model

    def _materialize_bindings(self, draft: AgentDraft) -> AgentDraft:
        runtime = draft.spec.runtime
        uses_codex_native_tools = bool(
            runtime
            and (
                runtime.type == "codex"
                or (
                    runtime.type == "plugin"
                    and runtime.provider_ref
                    and runtime.provider_ref.startswith(
                        f"plugin://{CODEX_AGENT_PROVIDER_PLUGIN_ID}@"
                    )
                )
            )
        )
        bindings = draft.spec.bindings
        if uses_codex_native_tools and bindings.tools:
            bindings = bindings.model_copy(update={"tools": []})
        if (
            not uses_codex_native_tools
            and not bindings.model_profile_id
            and not bindings.vision_model_profile_id
            and not bindings.tools
            and not bindings.mcp_servers
            and not bindings.skills
        ):
            return draft
        runtime_type = draft.spec.runtime.type if draft.spec.runtime is not None else None
        for binding in bindings.mcp_servers:
            if not binding.enabled:
                continue
            descriptor = self.catalog.get(binding.resource_id)
            if (
                descriptor.contract.get("materialization") == "dsh-profile"
                and runtime_type != "harness"
            ):
                raise StudioError(
                    "DSH_MCP_RUNTIME_INCOMPATIBLE",
                    "DSH Profile MCP 当前只支持 Harness Runtime",
                    status_code=422,
                    field="spec.runtime.type",
                    details={"runtimeType": runtime_type},
                )
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
                *([] if uses_codex_native_tools else materialized.spec.capabilities.tools),
                *bound_tools,
                # Non-Codex runtimes consume the probed MCP contracts through
                # the generic Tool execution path. Codex connects to the MCP
                # server natively, so expanding those contracts here turns a
                # valid MCP binding into unsupported third-party tools.
                *([] if uses_codex_native_tools else bound_mcp_tools),
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
            | {permission for tool in bound_mcp_tools for permission in tool.permissions}
        )
        network_models = self.catalog.resolve_models(bindings)
        vision_spec = self._vision_model_spec(bindings)
        if vision_spec is not None:
            network_models.append(vision_spec)
        model_hosts = {
            (urlparse(item.endpoint_url or item.base_url or "").hostname or "")
            .lower()
            .rstrip(".")
            for item in network_models
        }
        model_hosts.discard("")
        if model_hosts and materialized.spec.security.network.mode == "restricted":
            materialized.spec.security.network.allowed_hosts = sorted(
                set(materialized.spec.security.network.allowed_hosts) | model_hosts
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
