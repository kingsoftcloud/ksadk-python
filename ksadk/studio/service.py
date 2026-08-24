"""Application service composing Studio modules behind one local API boundary."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, cast
from urllib.parse import urlparse
from uuid import uuid4

from ksadk.api import AgentEngineClient
from ksadk.evaluation import (
    EvaluationConfig as PublicEvaluationConfig,
)
from ksadk.evaluation import (
    EvaluationExecutionError,
    EvaluationNotImplementedError,
    EvaluationStorage,
    EvaluationStorageError,
    TargetKind,
    TargetRef,
    execute_evaluation,
    load_evalset,
)
from ksadk.evaluation import (
    EvaluationRequest as PublicEvaluationRequest,
)
from ksadk.evaluation.evalset import EvalSetParseError
from ksadk.evaluation.evidence import EvidenceStore
from ksadk.evaluation.studio_build_adapter import (
    StudioBuildResolution,
    StudioBuildTargetAdapter,
    StudioBuildTargetError,
)
from ksadk.events.store import RuntimeEventStore
from ksadk.observability.session_log import SessionLogError, export_session_log
from ksadk.observability.trajectory import encode_sse, project_trajectory_event
from ksadk.runtime import RuntimeExecutor, build_default_runtime_registry
from ksadk.sessions.local_service import LocalSessionService
from ksadk.studio.agent_avatar_assets import AgentAvatarAssetStore
from ksadk.studio.agent_lifecycle import delete_framework_agent
from ksadk.studio.authoring_coordinator import StudioAuthoringCoordinator
from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.capabilities import builtin_tool_contracts
from ksadk.studio.cloud import (
    CloudDeploymentGateway,
    CloudDeploymentService,
    DirectAgentEngineCloudDeploymentGateway,
    UnavailableCloudGateway,
)
from ksadk.studio.codex_agent_service import CodexAgentService, CodexDraftRepository
from ksadk.studio.codex_builder import (
    CodexBuildRecord,
    CodexBuildRepository,
    CodexStudioBuilder,
    RuntimeInspector,
    current_proxy_mode,
    normalize_proxy_mode,
    proxy_mode_env_value,
)
from ksadk.studio.codex_manifest import (
    CodexAgentManifest,
    CodexManifestRepository,
)
from ksadk.studio.codex_run import CodexRunSpecResolver
from ksadk.studio.compiler import AgentCompiler
from ksadk.studio.contracts import (
    AgentAppearance,
    AgentBindings,
    AgentDraft,
    AgentSpec,
    AgentTemplateComposeRequest,
    AgentTemplateComposition,
    BuildStatus,
    DeploymentRequest,
    Operation,
    OperationKind,
    RunEvent,
    RunStatus,
    RuntimeRef,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.framework_run import FrameworkRunSpecResolver
from ksadk.studio.mcp_runtime import MCPRuntimeAdapter
from ksadk.studio.model_client import CredentialResolver, OpenAICompatibleModelClient
from ksadk.studio.model_profile_service import test_model_profile_connection
from ksadk.studio.operations import OperationManager
from ksadk.studio.repository import AgentDraftRepository, BuildRepository, load_yaml_file
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.run_service import StudioRunService
from ksadk.studio.runtime_catalog import inspect_runtime_catalog
from ksadk.studio.runtime_source import materialize_generated_runtime_source
from ksadk.studio.templates import (
    compose_blank_agent,
    compose_research_agent,
    default_agent_spec,
    list_agent_templates,
)
from ksadk.studio.validator import AgentValidator
from ksadk.studio.workspace import Workspace

_TRAJECTORY_KEEPALIVE_SECONDS = 15.0
_EVALUATION_TARGET_LABELS = {
    TargetKind.A2A: "A2A Agent",
    TargetKind.LOCAL_SOURCE: "本地源码",
    TargetKind.STUDIO_BUILD: "Studio Build",
    TargetKind.CODEX_WORKTREE: "Codex Worktree",
}


@dataclass(frozen=True)
class _OperationResource:
    id: str


class StudioService:
    def __init__(
        self,
        root: Path | str,
        *,
        model_client: OpenAICompatibleModelClient | None = None,
        credential_resolver: CredentialResolver | None = None,
        cloud_gateway: CloudDeploymentGateway | None = None,
        codex_runtime_inspector: RuntimeInspector | None = None,
        runtime_executor: RuntimeExecutor | None = None,
    ) -> None:
        self.workspace = Workspace(root)
        self.workspace.initialize()
        self._apply_persisted_settings()
        self.avatar_assets = AgentAvatarAssetStore(self.workspace)
        self.drafts = AgentDraftRepository(self.workspace)
        self.catalog = LocalResourceCatalog(self.workspace)
        self.builds = BuildRepository(self.workspace)
        self.validator = AgentValidator()
        self.builder = AgentBundleBuilder(
            self.workspace,
            compiler=AgentCompiler(
                self.workspace,
                validator=self.validator,
                catalog=self.catalog,
            ),
            repository=self.builds,
        )
        self.event_store = RunEventStore(self.workspace)
        self.session_service = LocalSessionService(project_dir=str(self.workspace.root))
        self.runtime_events = RuntimeEventStore(self.session_service)
        self.codex_manifests = CodexManifestRepository(self.workspace)
        self.codex_builds = CodexBuildRepository(self.workspace)
        self.codex_drafts = CodexDraftRepository(self.workspace)
        codex_builder_kwargs = {}
        if codex_runtime_inspector is not None:
            codex_builder_kwargs["runtime_inspector"] = codex_runtime_inspector
        self.codex_builder = CodexStudioBuilder(
            self.workspace,
            manifest_repository=self.codex_manifests,
            build_repository=self.codex_builds,
            resource_catalog=self.catalog,
            draft_repository=self.codex_drafts,
            **codex_builder_kwargs,
        )
        self.runtime_executor = runtime_executor or RuntimeExecutor(
            build_default_runtime_registry()
        )
        self.run_service = StudioRunService(
            self.workspace,
            self.runtime_executor,
            event_store=self.event_store,
            session_service=self.session_service,
            runtime_events=self.runtime_events,
        )
        self.credentials = (
            credential_resolver
            or getattr(model_client, "credential_resolver", None)
            or CredentialResolver(self.workspace)
        )
        self.codex_runs = CodexRunSpecResolver(
            self.workspace,
            build_repository=self.codex_builds,
            manifest_repository=self.codex_manifests,
            credential_resolver=self.credentials,
            resource_catalog=self.catalog,
        )
        self.framework_runs = FrameworkRunSpecResolver(
            self.workspace,
            build_repository=self.builds,
        )
        runtime_model_client = model_client or OpenAICompatibleModelClient(
            credential_resolver=self.credentials
        )
        self.model_client = runtime_model_client
        self.mcp_runtime = MCPRuntimeAdapter(self.workspace, credentials=self.credentials)
        self._cloud_gateway_override = cloud_gateway
        self.cloud = CloudDeploymentService(
            self.workspace,
            gateway=cloud_gateway or self._configured_cloud_gateway(),
            build_repository=self.builds,
        )
        self.operations = OperationManager(self.workspace)
        self.evaluation_storage = EvaluationStorage(
            self.workspace.resolve(".agentkit/evaluations")
        )
        self.authoring = StudioAuthoringCoordinator(self)
        self.codex_agents = CodexAgentService(self)

    def runtime_catalog(self) -> list[dict]:
        return inspect_runtime_catalog(self.runtime_executor)

    def codex_manifest_state(self, agent_id: str | None = None) -> dict:
        return self.codex_agents.manifest_state(agent_id)

    def save_codex_manifest(self, manifest: CodexAgentManifest) -> dict:
        return self.codex_agents.save_manifest(manifest)

    def list_codex_agents(self, *, query: str = "", limit: int = 50) -> list[AgentDraft]:
        return self.codex_agents.list(query=query, limit=limit)

    def create_codex_agent(
        self,
        *,
        agent_id: str,
        spec: AgentSpec | None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentDraft:
        return self.codex_agents.create(
            agent_id=agent_id,
            spec=spec,
            name=name,
            labels=labels,
        )

    def update_codex_agent(
        self,
        agent_id: str,
        spec: AgentSpec,
        *,
        expected_revision: int,
        name: str | None = None,
    ) -> AgentDraft:
        return self.codex_agents.update(
            agent_id,
            spec,
            expected_revision=expected_revision,
            name=name,
        )

    def delete_codex_agent(self, agent_id: str, *, purge: bool = False) -> None:
        self.codex_agents.delete(agent_id, purge=purge)

    def codex_agent_detail(self, agent_id: str | None = None) -> dict:
        return self.codex_agents.detail(agent_id)

    @staticmethod
    def codex_build_view(record: CodexBuildRecord) -> dict:
        return CodexAgentService.build_view(record)

    def submit_codex_build(
        self,
        *,
        idempotency_key: str,
        agent_id: str | None = None,
    ) -> Operation:
        return self.codex_agents.submit_build(
            idempotency_key=idempotency_key,
            agent_id=agent_id,
        )

    def submit_codex_run(
        self,
        build_id: str,
        user_input: str,
        *,
        session_id: str | None,
        model: str | None = None,
        sandbox: str | None = None,
        approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
        reasoning_effort: str | None = None,
        runtime_input: Any = None,
        idempotency_key: str,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> Operation:
        return self.codex_agents.submit_run(
            build_id,
            user_input,
            session_id=session_id,
            model=model,
            sandbox=sandbox,
            approval_mode=approval_mode,
            collaboration_mode=collaboration_mode,
            goal_objective=goal_objective,
            reasoning_effort=reasoning_effort,
            runtime_input=runtime_input,
            idempotency_key=idempotency_key,
            on_event=on_event,
        )

    async def delete_session(self, session_id: str) -> None:
        from ksadk.studio.errors import not_found

        runs = self.event_store.list_runs(session_id=session_id)
        if not runs:
            raise not_found("session", session_id)
        if any(run.status == RunStatus.RUNNING for run in runs):
            raise StudioError(
                "SESSION_RUN_ACTIVE",
                "会话仍在运行，请先停止运行后再删除",
                status_code=409,
                details={"sessionId": session_id},
            )
        await self.session_service.delete_session(session_id)
        self.event_store.delete_session(session_id)

    async def _require_runtime_session(self, session_id: str) -> None:
        if await self.session_service.get_session_metadata(session_id) is None:
            raise StudioError(
                "SESSION_NOT_FOUND",
                "session 不存在",
                status_code=404,
                details={"id": session_id},
            )

    async def trajectory_page(
        self,
        session_id: str,
        *,
        before_seq_id: int | None = None,
        invocation_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return the bounded, canonical RuntimeEvent trajectory for a session."""

        await self._require_runtime_session(session_id)
        events = await self.runtime_events.list(
            session_id,
            before_seq=before_seq_id,
            run_id=invocation_id,
            limit=limit,
        )
        oldest_seq_id = events[0].seq if events else None
        latest_seq_id = events[-1].seq if events else None
        older = (
            await self.runtime_events.list(
                session_id,
                before_seq=oldest_seq_id,
                run_id=invocation_id,
                limit=1,
            )
            if oldest_seq_id is not None
            else []
        )
        return {
            "items": [project_trajectory_event(event) for event in events],
            "page": {
                "oldestSeqId": oldest_seq_id,
                "latestSeqId": latest_seq_id,
                "hasMore": bool(older),
            },
        }

    async def stream_trajectory(
        self,
        session_id: str,
        after_seq_id: int = 0,
        *,
        invocation_id: str | None = None,
    ):
        await self._require_runtime_session(session_id)
        cursor = after_seq_id
        while True:
            async for event in self.runtime_events.subscribe_session(
                session_id,
                after_seq=cursor,
                poll_interval=min(0.25, _TRAJECTORY_KEEPALIVE_SECONDS),
                timeout=_TRAJECTORY_KEEPALIVE_SECONDS,
            ):
                cursor = event.seq
                if invocation_id is not None and event.run_id != invocation_id:
                    continue
                yield encode_sse(project_trajectory_event(event), event_id=cursor)
            yield ": keepalive\n\n"

    async def export_runtime_session(
        self,
        session_id: str,
        *,
        filename: str,
        invocation_id: str | None = None,
    ) -> dict[str, Any]:
        await self._require_runtime_session(session_id)
        relative = Path(filename)
        if (
            not filename
            or relative.is_absolute()
            or len(relative.parts) != 1
            or relative.name in {".", ".."}
        ):
            raise StudioError(
                "SESSION_EXPORT_FILENAME_INVALID",
                "导出文件名必须是普通文件名",
                status_code=422,
                field="filename",
            )
        export_dir = self.workspace.resolve(".agentkit/exports")
        export_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        export_dir.chmod(0o700)
        try:
            result = await export_session_log(
                self.session_service,
                session_id,
                export_dir / relative.name,
                invocation_id=invocation_id,
            )
        except SessionLogError as exc:
            code = str(exc).partition(":")[0]
            raise StudioError(
                code,
                str(exc),
                status_code=409 if code == "SESSION_LOG_TARGET_EXISTS" else 422,
            ) from exc
        return {
            "path": result.path.relative_to(self.workspace.root).as_posix(),
            "eventCount": result.event_count,
            "firstSeqId": result.first_seq_id,
            "lastSeqId": result.last_seq_id,
            "exportedThroughSeqId": result.exported_through_seq_id,
        }

    async def test_model_profile(self, resource_id: str) -> dict:
        return await test_model_profile_connection(
            catalog=self.catalog,
            model_client=self.model_client,
            resource_id=resource_id,
        )

    def create_agent(
        self,
        *,
        agent_id: str,
        name: str,
        description: str = "",
        template: str = "blank",
        spec: AgentSpec | None = None,
        labels: dict[str, str] | None = None,
    ):
        if self.codex_manifests.exists(agent_id):
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "AGENT_ALREADY_EXISTS",
                "Agent ID 已存在",
                status_code=409,
                details={"id": agent_id},
            )
        resolved_spec = spec or default_agent_spec(
            template,
            description=description,
        )
        self._validate_bindings(resolved_spec.bindings)
        self._validate_framework_binding_support(
            resolved_spec.runtime,
            resolved_spec.bindings,
        )
        draft = self.drafts.create(
            agent_id=agent_id,
            name=name,
            description=description,
            template=template,
            spec=resolved_spec,
            labels=labels,
        )
        materialize_generated_runtime_source(
            self.workspace,
            draft,
            catalog=self.catalog,
        )
        return draft

    def create_authored_agent(
        self,
        *,
        name: str,
        slug: str | None = None,
        runtime_type: str,
        template: str = "blank",
        description: str = "",
        spec: AgentSpec | None = None,
    ) -> AgentDraft:
        return self.authoring.create(
            name=name,
            slug=slug,
            runtime_type=runtime_type,
            description=description,
            template=template,
            spec=spec,
        )

    def inspect_agent_import(self, content: bytes, *, filename: str) -> dict:
        return self.authoring.inspect_import(content, filename=filename)

    def commit_agent_import(
        self,
        inspection_token: str,
        *,
        name: str | None = None,
        slug: str | None = None,
    ) -> AgentDraft:
        return self.authoring.commit_import(
            inspection_token,
            name=name,
            slug=slug,
        )

    def inspect_agent_project(self, project_path: str) -> dict:
        return self.authoring.inspect_project(project_path)

    def commit_agent_project(
        self,
        inspection_token: str,
        *,
        name: str | None = None,
        slug: str | None = None,
        model_profile_id: str | None = None,
    ) -> AgentDraft:
        return self.authoring.commit_project(
            inspection_token,
            name=name,
            slug=slug,
            model_profile_id=model_profile_id,
        )

    async def compose_agent_conversation(
        self,
        *,
        messages: list[dict[str, str]],
        model_profile_id: str,
    ) -> dict:
        return await self.authoring.compose_conversation(
            messages=messages,
            model_profile_id=model_profile_id,
        )

    def is_codex_agent(self, agent_id: str) -> bool:
        """Return whether one Agent is backed by the Codex YAML contract."""

        return self.codex_manifests.exists(agent_id)

    def agent_runtime_type(self, agent_id: str) -> str:
        """Resolve runtime from the Agent itself, never from Studio process state."""

        if self.is_codex_agent(agent_id):
            return "codex"
        draft = self.drafts.get(agent_id)
        runtime = draft.spec.runtime
        if runtime is None:
            # Read compatibility for Agent drafts written before RuntimeRef existed.
            framework = draft.metadata.labels.get("agentkit.ksyun.com/framework", "")
            return framework.strip().lower() or "adk"
        return runtime.type

    def _agent_prompt_sources(self, agent_id: str) -> tuple[str, str, str]:
        """Resolve the canonical prompt inputs used by preview and execution."""

        if self.is_codex_agent(agent_id):
            snapshot = self.codex_manifests.load(agent_id)
            return (
                snapshot.manifest.prompt or "",
                snapshot.manifest.task_prompt or "",
                "codex",
            )
        draft = self.drafts.get(agent_id)
        instructions = draft.spec.instructions
        return (
            str(instructions.system or ""),
            str(instructions.task or ""),
            self.agent_runtime_type(agent_id),
        )

    def compile_prompt_preview(
        self,
        agent_id: str,
        *,
        request_instructions: str = "",
        include_content: bool = False,
    ) -> dict:
        """Compile the real prompt without creating a Session, Trace, or Build."""

        from ksadk.prompts.resolved import (
            ResolvedPromptSources,
            compile_resolved_prompt_dict,
            get_default_platform_policy_source,
        )

        agent_system, agent_task, runtime_type = self._agent_prompt_sources(agent_id)
        compiled = compile_resolved_prompt_dict(
            ResolvedPromptSources(
                agent_system=agent_system,
                agent_task=agent_task,
                request_instructions=str(request_instructions or "").strip(),
                platform_policy_source=get_default_platform_policy_source(),
            )
        )
        if compiled is None:
            return {
                "promptVersion": "v1",
                "contentHash": "",
                "stablePrefixHash": "",
                "sections": [],
                "runtimeType": runtime_type,
                "warnings": ["no prompt content to compile"],
            }
        result: dict[str, Any] = {
            "promptVersion": compiled["prompt_compiler_version"],
            "contentHash": compiled["prompt_content_hash"],
            "stablePrefixHash": compiled["prompt_stable_prefix_hash"],
            "sectionHashes": compiled["prompt_section_hashes"],
            "tokensBySection": compiled["prompt_tokens_by_section"],
            "estimatedTokens": compiled["prompt_estimated_tokens"],
            "sectionCount": compiled["prompt_section_count"],
            "runtimeType": runtime_type,
            "platformPolicyVersion": compiled.get("prompt_platform_policy_version"),
            "warnings": [],
        }
        if include_content:
            result["content"] = compiled["prompt_content"]
        return result

    def reveal_run_prompt(self, run_id: str) -> dict:
        """Rebuild immutable Run prompt sections and reveal only on hash match."""

        from ksadk.prompts.resolved import (
            ResolvedPromptSources,
            compile_resolved_prompt_dict,
            get_default_platform_policy_source,
            sections_from_resolved_sources,
        )

        record = self.event_store.get(run_id)
        resolver = self.codex_runs if record.runtime_type == "codex" else self.framework_runs
        spec = resolver.resolve(record.build_id, model=record.model or None)
        config = dict(spec.request_config or {})
        sources = ResolvedPromptSources(
            agent_system=str(config.get("agent_system") or ""),
            agent_task=str(config.get("agent_task") or ""),
            request_instructions=str(config.get("instructions") or ""),
            platform_policy_source=get_default_platform_policy_source(),
        )
        compiled = compile_resolved_prompt_dict(sources)
        expected_hash = str((record.prompt_evidence or {}).get("contentHash") or "")
        actual_hash = str((compiled or {}).get("prompt_content_hash") or "")
        if not compiled or not expected_hash or actual_hash != expected_hash:
            return {
                "available": False,
                "reason": "无法证明重建内容与本次运行一致，已拒绝展示正文。",
            }
        return {
            "available": True,
            "contentHash": actual_hash,
            "sections": [
                {
                    "id": section.section_id,
                    "source": section.source,
                    "content": section.content,
                }
                for section in sections_from_resolved_sources(sources)
            ],
        }

    async def preview_context(
        self,
        agent_id: str,
        *,
        user_input: str = "",
        request_instructions: str = "",
        simulated_history: list[dict[str, str]] | None = None,
        include_content: bool = False,
    ) -> dict:
        """Run the production context planner without model or persistence side effects."""

        from ksadk.context_engine.capabilities import capabilities_for_runtime_type
        from ksadk.context_engine.hosted_pipeline import run_hosted_pipeline
        from ksadk.prompts.resolved import (
            ResolvedPromptSources,
            compile_resolved_prompt_dict,
            get_default_platform_policy_source,
        )

        agent_system, agent_task, runtime_type = self._agent_prompt_sources(agent_id)
        capabilities = capabilities_for_runtime_type(runtime_type)
        compiled_prompt = compile_resolved_prompt_dict(
            ResolvedPromptSources(
                agent_system=agent_system,
                agent_task=agent_task,
                request_instructions=str(request_instructions or "").strip(),
                platform_policy_source=get_default_platform_policy_source(),
            )
        )
        history = [
            {
                "role": item.get("role", "user"),
                "content": item.get("content", ""),
            }
            for item in (simulated_history or [])
        ]
        pipeline = await run_hosted_pipeline(
            compiled_prompt=compiled_prompt,
            user_input=str(user_input or "").strip(),
            history=history,
            working_state=None,
            model_metadata={
                "context_window_tokens": 200_000,
                "max_output_tokens": 32_000,
            },
            contributors=None,
            integration_mode=capabilities.integration_mode,
            accounting_accuracy="estimated",
            session_id=f"preview-{agent_id}",
            invocation_id=f"preview-{agent_id}",
        )
        if pipeline is None:
            return {
                "accuracy": "estimated",
                "warnings": ["no content to plan"],
                "items": [],
            }
        plan = pipeline.plan
        assembled = pipeline.assembled
        return {
            "accuracy": plan.get("accounting_accuracy", "estimated"),
            "policyVersion": plan.get("policy_version"),
            "planId": plan.get("plan_id"),
            "budget": {
                "maxInputTokens": (plan.get("budget") or {}).get("max_input_tokens"),
                "softLimitTokens": (plan.get("budget") or {}).get("soft_limit_tokens"),
                "hardLimitTokens": (plan.get("budget") or {}).get("hard_limit_tokens"),
            },
            "items": plan.get("selected", []),
            "decisions": plan.get("decisions", []),
            "totalsByKind": plan.get("tokens_by_kind", {}),
            "plannedInputTokens": plan.get("planned_input_tokens"),
            "warnings": list(assembled.warnings),
            "projection": {
                "runtimeType": runtime_type,
                "integrationMode": capabilities.integration_mode,
                "promptOwner": capabilities.prompt_owner,
            },
            **({"system": assembled.system} if include_content else {}),
        }

    def detect_importable_project(self) -> dict | None:
        """Expose a root framework project for explicit Studio import only."""
        from ksadk.studio.manifest_resolver import detect_manifest_kind

        result = detect_manifest_kind(self.workspace.root)
        if result.kind != "framework":
            return None
        import yaml

        try:
            payload = yaml.safe_load(result.path.read_text(encoding="utf-8-sig")) or {}
        except Exception:  # noqa: BLE001
            return None
        return {
            "kind": "framework",
            "runtimeType": result.framework or result.runtime_type,
            "name": str(payload.get("name") or self.workspace.root.name or "imported-agent"),
            "model": str(payload.get("model") or ""),
            "prompt": str(payload.get("prompt") or payload.get("instruction") or ""),
            "task": str(payload.get("task") or ""),
            "manifestPath": "agentengine.yaml",
            "requiresConfirmation": True,
        }

    def import_root_project(
        self,
        *,
        name: str | None = None,
        slug: str | None = None,
    ) -> AgentDraft:
        """Import an explicitly detected root Framework project as a Studio draft."""

        importable = self.detect_importable_project()
        if importable is None:
            raise StudioError(
                "PROJECT_NOT_IMPORTABLE",
                "当前工作区根没有可导入的 Framework 项目",
                status_code=422,
            )
        inspection = self.inspect_agent_project(".")
        return self.commit_agent_project(
            inspection["inspectionToken"],
            name=name or importable.get("name"),
            slug=slug or importable.get("name"),
        )

    def list_agents(self, *, query: str = "", limit: int = 50) -> list[AgentDraft]:
        """List all local Agents from one registry view across runtime types."""

        if limit < 1:
            return []
        normalized = query.strip().lower()
        combined: dict[str, AgentDraft] = {
            draft.metadata.id: draft for draft in self.list_codex_agents(query=query, limit=limit)
        }
        for draft in self.drafts.list(query=query, limit=limit):
            combined.setdefault(draft.metadata.id, draft)
        values = list(combined.values())
        if normalized:
            values = [
                item
                for item in values
                if normalized in item.metadata.id.lower()
                or normalized in item.metadata.name.lower()
            ]
        return values[:limit]

    def agent_detail(self, agent_id: str) -> dict:
        if self.is_codex_agent(agent_id):
            return self.codex_agent_detail(agent_id)
        draft = self.drafts.get(agent_id)
        return {
            "draft": draft,
            "builds": self.builds.list_for_agent(agent_id)[:10],
            "validation": self.validator.validate(draft),
        }

    def create_studio_agent(
        self,
        *,
        agent_id: str,
        name: str,
        description: str = "",
        template: str = "blank",
        spec: AgentSpec | None = None,
        runtime: RuntimeRef | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentDraft:
        """Create one Agent and dispatch from its RuntimeRef."""

        resolved_spec = (spec or default_agent_spec(template, description=description)).model_copy(
            deep=True
        )
        selected = runtime or resolved_spec.runtime
        resolved_spec.runtime = selected
        if selected is not None and selected.type == "codex":
            return cast(
                AgentDraft,
                self.create_codex_agent(
                    agent_id=agent_id,
                    spec=resolved_spec,
                    name=name,
                    labels=labels,
                ),
            )
        return cast(
            AgentDraft,
            self.create_agent(
                agent_id=agent_id,
                name=name,
                description=description,
                template=template,
                spec=resolved_spec,
                labels=labels,
            ),
        )

    def update_studio_agent(
        self,
        agent_id: str,
        spec: AgentSpec,
        *,
        expected_revision: int,
        name: str | None = None,
    ) -> AgentDraft:
        if self.is_codex_agent(agent_id):
            spec.runtime = self.agent_detail(agent_id)["draft"].spec.runtime
            return cast(
                AgentDraft,
                self.update_codex_agent(
                    agent_id,
                    spec,
                    expected_revision=expected_revision,
                    name=name,
                ),
            )
        current = self.drafts.get(agent_id)
        if spec.runtime is None:
            spec.runtime = current.spec.runtime
        elif current.spec.runtime is not None and spec.runtime.type != current.spec.runtime.type:
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "AGENT_RUNTIME_IMMUTABLE",
                "编辑 Agent 时不能直接切换 Runtime；请通过导入/迁移创建新 Agent",
                status_code=422,
                field="runtime.type",
            )
        return cast(
            AgentDraft,
            self.update_agent(
                agent_id,
                spec,
                expected_revision=expected_revision,
                name=name,
            ),
        )

    def update_studio_agent_bindings(
        self,
        agent_id: str,
        bindings: AgentBindings,
        *,
        expected_revision: int,
    ) -> AgentDraft:
        detail = self.agent_detail(agent_id)
        spec = detail["draft"].spec.model_copy(deep=True)
        spec.bindings = bindings
        return self.update_studio_agent(
            agent_id,
            spec,
            expected_revision=expected_revision,
        )

    def update_studio_agent_appearance(
        self,
        agent_id: str,
        appearance: AgentAppearance,
        *,
        expected_revision: int,
    ) -> AgentDraft:
        if self.is_codex_agent(agent_id):
            return self.codex_agents.update_appearance(
                agent_id,
                appearance,
                expected_revision=expected_revision,
            )
        return self.drafts.update_appearance(
            agent_id,
            appearance,
            expected_revision=expected_revision,
        )

    def delete_studio_agent(self, agent_id: str, *, purge: bool = False) -> None:
        if self.is_codex_agent(agent_id):
            self.delete_codex_agent(agent_id, purge=purge)
            return
        delete_framework_agent(self, agent_id, purge=purge)

    def validate_studio_agent(
        self,
        agent_id: str,
        *,
        revision: int,
        level: Literal["schema", "build", "release"] = "build",
    ):
        detail = self.agent_detail(agent_id)
        draft = detail["draft"]
        if draft.metadata.revision != revision:
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Validation revision 与当前 Agent 不一致",
                status_code=409,
            )
        if self.is_codex_agent(agent_id):
            return detail["validation"]
        return self.validator.validate(draft, level=level)

    def submit_studio_build(
        self,
        agent_id: str,
        *,
        revision: int,
        idempotency_key: str,
    ) -> Operation:
        runtime_type = self.agent_runtime_type(agent_id)
        runtime = next(
            (item for item in self.runtime_catalog() if item["runtimeType"] == runtime_type),
            None,
        )
        if runtime is None:
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "RUNTIME_NOT_REGISTERED",
                "Agent 引用的 RuntimeAdapter 未注册",
                status_code=422,
                details={"runtimeType": runtime_type},
            )
        if runtime["status"] != "ready":
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "RUNTIME_DEPENDENCY_MISSING",
                f"{runtime['displayName']} Runtime 依赖未安装",
                status_code=422,
                details={
                    "runtimeType": runtime_type,
                    "installCommand": runtime["installCommand"],
                },
            )
        if self.is_codex_agent(agent_id):
            detail = self.agent_detail(agent_id)
            if revision != detail["draft"].metadata.revision:
                from ksadk.studio.errors import StudioError

                raise StudioError(
                    "AGENT_REVISION_CONFLICT",
                    "Build revision 与当前 Agent 不一致",
                    status_code=409,
                )
            return self.submit_codex_build(
                idempotency_key=idempotency_key,
                agent_id=agent_id,
            )
        return self.submit_build(
            agent_id,
            revision=revision,
            idempotency_key=idempotency_key,
        )

    def build_view(self, build_id: str):
        try:
            return self.codex_build_view(self.codex_builds.get(build_id))
        except Exception as exc:  # repository not-found is the only fallback contract
            if getattr(exc, "status_code", None) != 404:
                raise
        return self.builds.get(build_id)

    def submit_studio_run(
        self,
        build_id: str,
        user_input: str,
        *,
        session_id: str | None,
        model: str | None,
        idempotency_key: str,
        sandbox: str | None = None,
        approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
        reasoning_effort: str | None = None,
        runtime_input: Any = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> Operation:
        try:
            self.codex_builds.get(build_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
        else:
            return self.submit_codex_run(
                build_id,
                user_input,
                session_id=session_id,
                model=model,
                sandbox=sandbox,
                approval_mode=approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
                reasoning_effort=reasoning_effort,
                runtime_input=runtime_input,
                idempotency_key=idempotency_key,
                on_event=on_event,
            )
        return self.submit_run(
            build_id,
            user_input,
            session_id=session_id,
            model=model,
            sandbox=sandbox,
            approval_mode=approval_mode,
            collaboration_mode=collaboration_mode,
            goal_objective=goal_objective,
            reasoning_effort=reasoning_effort,
            runtime_input=runtime_input,
            idempotency_key=idempotency_key,
            on_event=on_event,
        )

    def _draft_exists(self, agent_id: str) -> bool:
        try:
            self.drafts.get(agent_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return False
            raise
        return True

    @staticmethod
    def list_agent_templates() -> list[dict]:
        return list_agent_templates()

    def compose_agent_template(
        self,
        template_id: str,
        request: AgentTemplateComposeRequest,
    ) -> AgentTemplateComposition:
        composers = {
            "blank": compose_blank_agent,
            "research": compose_research_agent,
        }
        composer = composers.get(template_id)
        if composer is None:
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "AGENT_TEMPLATE_UNSUPPORTED",
                "当前模板不支持自动编排",
                status_code=422,
                field="templateId",
                details={"templateId": template_id},
            )
        composition = composer(self.workspace, self.catalog, request)
        self._validate_bindings(composition.spec.bindings)
        return composition

    def update_agent(
        self,
        agent_id: str,
        spec: AgentSpec,
        *,
        expected_revision: int,
        name: str | None = None,
    ):
        current = self.drafts.get(agent_id)
        if current.metadata.revision != expected_revision:
            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Agent 已被其他操作更新",
                status_code=409,
                field="metadata.revision",
                details={
                    "expected": expected_revision,
                    "actual": current.metadata.revision,
                },
            )
        materializable_bindings = self._materializable_update_bindings(
            current.spec.bindings,
            spec.bindings,
            runtime=spec.runtime or current.spec.runtime,
        )
        self._validate_bindings(materializable_bindings)
        self._validate_framework_binding_support(
            spec.runtime or current.spec.runtime,
            materializable_bindings,
        )
        preview = current.model_copy(deep=True)
        preview.metadata.revision += 1
        if name is not None:
            preview.metadata.name = name
        preview.spec = spec.model_copy(deep=True)
        preview.spec.bindings = materializable_bindings
        # Generated source is a materialized view of the candidate Revision.
        # Complete it before the authoritative draft write so any resolver,
        # digest or runtime-capability failure leaves Revision/content intact.
        materialize_generated_runtime_source(
            self.workspace,
            preview,
            catalog=self.catalog,
        )
        return self.drafts.update(
            agent_id,
            spec,
            expected_revision=expected_revision,
            name=name,
        )

    def update_agent_bindings(
        self,
        agent_id: str,
        bindings: AgentBindings,
        *,
        expected_revision: int,
    ):
        draft = self.drafts.get(agent_id)
        spec = draft.spec.model_copy(deep=True)
        spec.bindings = bindings
        return self.update_agent(
            agent_id,
            spec,
            expected_revision=expected_revision,
        )

    def _validate_bindings(self, bindings: AgentBindings) -> None:
        self.catalog.resolve_model(bindings)
        self.catalog.resolve_models(bindings)
        self.catalog.policy_preview(bindings)
        self.catalog.resolve_mcp_servers(bindings)
        self.catalog.resolve_mcp_tools(bindings)
        self.catalog.resolve_skills(bindings)

    def _materializable_update_bindings(
        self,
        current: AgentBindings,
        candidate: AgentBindings,
        *,
        runtime: RuntimeRef | None,
    ) -> AgentBindings:
        """Return bindings that can be safely resolved into this Revision.

        Unknown bindings written by older Studio versions remain authoritative
        draft data, but are dormant until they re-enter the resource catalog.
        A new or modified unknown binding is still rejected by the normal
        resolver. Framework MCP bindings are likewise read-only until generated
        runtime source has a real injection path.
        """

        if (
            runtime is not None
            and runtime.type in {"adk", "langgraph"}
            and candidate.mcp_servers != current.mcp_servers
        ):
            raise StudioError(
                "MCP_RUNTIME_INCOMPATIBLE",
                "当前 Runtime 尚未支持把 MCP 绑定注入生成源码；历史绑定仅可保留",
                status_code=422,
                field="spec.bindings.mcpServers",
                details={"runtimeType": runtime.type},
            )

        known = {item.resource_id for item in self.catalog.list(limit=10_000)}

        def selected_for_materialization(
            selected: list,
            historical: list,
        ) -> list:
            return [
                binding
                for binding in selected
                if binding.resource_id in known or binding not in historical
            ]

        resolved = candidate.model_copy(deep=True)
        if candidate.model_profile_id not in known:
            unchanged_model = (
                candidate.model_profile_id == current.model_profile_id
                and candidate.model_profile_ids == current.model_profile_ids
            )
            if unchanged_model:
                resolved.model_profile_id = None
                resolved.model_profile_ids = []
        if resolved.model_profile_id is not None:
            resolved.model_profile_ids = [
                resource_id
                for resource_id in resolved.model_profile_ids
                if resource_id in known
                or resource_id not in current.model_profile_ids
            ]
        resolved.skills = selected_for_materialization(
            candidate.skills,
            current.skills,
        )
        resolved.tools = selected_for_materialization(
            candidate.tools,
            current.tools,
        )
        # No framework generated source consumes MCP today. Exact historical
        # values remain in `candidate`, while this preview omits them honestly.
        if runtime is not None and runtime.type in {"adk", "langgraph"}:
            resolved.mcp_servers = []
        else:
            resolved.mcp_servers = selected_for_materialization(
                candidate.mcp_servers,
                current.mcp_servers,
            )
        return resolved

    def _validate_framework_binding_support(
        self,
        runtime: RuntimeRef | None,
        bindings: AgentBindings,
    ) -> None:
        if runtime is None or runtime.type not in {"adk", "langgraph"}:
            return
        if bindings.mcp_servers:
            raise StudioError(
                "MCP_RUNTIME_INCOMPATIBLE",
                "当前 Runtime 尚未支持把 MCP 绑定注入生成源码",
                status_code=422,
                field="spec.bindings.mcpServers",
                details={"runtimeType": runtime.type},
            )
        unsupported: list[str] = []
        for binding in bindings.tools:
            if not binding.enabled:
                continue
            descriptor = self.catalog.get(binding.resource_id)
            contract = ToolContract.model_validate(descriptor.contract)
            if contract.executor not in {"builtin", "python"}:
                unsupported.append(binding.resource_id)
        if unsupported:
            raise StudioError(
                "TOOL_RUNTIME_INCOMPATIBLE",
                "当前 Runtime 仅支持 builtin/python Tool",
                status_code=422,
                field="spec.bindings.tools",
                details={
                    "runtimeType": runtime.type,
                    "resourceIds": unsupported,
                },
            )

    def validate_agent(
        self,
        agent_id: str,
        *,
        level: Literal["schema", "build", "release"] = "build",
    ):
        return self.validator.validate(self.drafts.get(agent_id), level=level)

    def submit_build(
        self,
        agent_id: str,
        *,
        revision: int,
        idempotency_key: str,
    ) -> Operation:
        draft = self.drafts.get(agent_id)
        if draft.metadata.revision != revision:
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Build revision 与当前 Agent 不一致",
                status_code=409,
                details={"expected": revision, "actual": draft.metadata.revision},
            )
        snapshot = draft.model_copy(deep=True)

        async def runner(_operation_id: str):
            return await asyncio.to_thread(self.builder.build, snapshot)

        return self.operations.submit(
            kind=OperationKind.BUILD,
            resource_id=agent_id,
            idempotency_key=idempotency_key,
            runner=runner,
        )

    def submit_run(
        self,
        build_id: str,
        user_input: str,
        *,
        session_id: str | None,
        model: str | None = None,
        sandbox: str | None = None,
        approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
        reasoning_effort: str | None = None,
        runtime_input: Any = None,
        idempotency_key: str,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> Operation:
        async def runner(_operation_id: str):
            return await self.run_build(
                build_id,
                user_input,
                session_id,
                model=model,
                sandbox=sandbox,
                approval_mode=approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
                reasoning_effort=reasoning_effort,
                runtime_input=runtime_input,
                on_event=on_event,
            )

        return self.operations.submit(
            kind=OperationKind.RUN,
            resource_id=build_id,
            idempotency_key=idempotency_key,
            runner=runner,
        )

    async def run_build(
        self,
        build_id: str,
        user_input: str,
        session_id: str | None,
        *,
        model: str | None = None,
        sandbox: str | None = None,
        approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
        reasoning_effort: str | None = None,
        runtime_input: Any = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ):
        """Execute any immutable Studio Build through the canonical executor."""

        try:
            spec = self.codex_runs.resolve(
                build_id,
                model=model,
                sandbox=sandbox,
                approval_mode=approval_mode,
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            spec = self.framework_runs.resolve(
                build_id,
                model=model,
                approval_mode=approval_mode,
            )
        if collaboration_mode or goal_objective or reasoning_effort:
            from dataclasses import replace

            request_config = dict(spec.request_config)
            if collaboration_mode:
                request_config["collaboration_mode"] = collaboration_mode
            if goal_objective:
                request_config["goal_objective"] = goal_objective
                request_config["ephemeral"] = False
            if reasoning_effort:
                request_config["effort"] = reasoning_effort
            spec = replace(spec, request_config=request_config)
        return await self.run_service.run(
            spec,
            user_input,
            runtime_input=runtime_input,
            session_id=session_id,
            on_event=on_event,
        )

    def submit_public_evaluation(
        self,
        evalset_file: str,
        target: TargetRef,
        config: PublicEvaluationConfig,
        *,
        idempotency_key: str,
    ) -> Operation:
        """Queue the public CLI/Studio handoff without exposing adapter internals."""

        try:
            path = self.workspace.resolve(evalset_file, must_exist=True)
        except StudioError:
            raise
        if not path.is_file():
            raise StudioError(
                "EVALSET_FILE_INVALID",
                "EvalSet 必须是工作区内的文件",
                status_code=422,
                field="evalsetFile",
            )
        try:
            evalset = load_evalset(path)
        except EvalSetParseError as exc:
            raise StudioError(
                exc.code,
                str(exc),
                status_code=422,
                field="evalsetFile",
            ) from exc
        target = self._normalize_public_evaluation_target(target)
        request = PublicEvaluationRequest(
            evalset=evalset,
            target=target,
            config=config,
            report_dir=str(self.evaluation_storage.root),
        )
        evaluation_id = f"eval_{uuid4().hex}"

        async def runner(operation_id: str):
            try:
                report = await execute_evaluation(
                    request,
                    adapter=self._public_evaluation_adapter(request),
                    run_id=evaluation_id,
                    on_case_started=lambda case_id, index, total: self.operations.append(
                        operation_id,
                        "evaluation.case.started",
                        {"caseId": case_id, "index": index, "total": total},
                    ),
                )
            except EvaluationNotImplementedError as exc:
                raise StudioError(
                    "EVALUATION_EXECUTOR_UNAVAILABLE",
                    str(exc),
                    status_code=501,
                ) from exc
            except EvaluationExecutionError as exc:
                raise StudioError(
                    "EVALUATION_EXECUTION_FAILED",
                    str(exc),
                    status_code=502,
                ) from exc
            return _OperationResource(id=report.spec.id)

        return self.operations.submit(
            kind=OperationKind.EVALUATION,
            resource_id=evaluation_id,
            idempotency_key=idempotency_key,
            metadata={
                "evalset": {"name": evalset.name, "caseCount": len(evalset.cases)},
                "target": {
                    "kind": target.kind.value,
                    "label": self._evaluation_target_label(target.kind, build_id=target.locator),
                },
                "evaluators": list(config.evaluators),
            },
            runner=runner,
        )

    def list_public_evaluations(self):
        return self.evaluation_storage.list_reports()

    def list_public_evaluation_runs(self) -> list[dict[str, Any]]:
        return [
            self._public_evaluation_run(operation)
            for operation in self.operations.list(kind=OperationKind.EVALUATION)
        ]

    def get_public_evaluation_run(self, evaluation_id: str) -> dict[str, Any]:
        operation = next(
            (
                item
                for item in self.operations.list(kind=OperationKind.EVALUATION)
                if item.resource_id == evaluation_id
            ),
            None,
        )
        if operation is None:
            raise StudioError(
                "EVALUATION_RUN_NOT_FOUND",
                "Evaluation Run 不存在",
                status_code=404,
                details={"id": evaluation_id},
            )
        return self._public_evaluation_run(operation, include_report=True)

    def _public_evaluation_run(
        self, operation: Operation, *, include_report: bool = False
    ) -> dict[str, Any]:
        try:
            report = self.evaluation_storage.read_report(operation.resource_id)
        except EvaluationStorageError:
            report = None
        progress = None
        for event in self.operations.events(operation.id):
            if event.type == "evaluation.case.started":
                progress = {
                    "current": event.data.get("index", 0),
                    "total": event.data.get("total", 0),
                    "caseId": event.data.get("caseId"),
                }
        metadata = operation.metadata or {}
        evalset = metadata.get("evalset")
        target = metadata.get("target")
        evaluators = metadata.get("evaluators")
        if report is not None:
            if not evalset:
                evalset = {
                    "name": report.spec.evalset.name,
                    "caseCount": len(report.spec.evalset.cases),
                }
            if not target:
                target = {
                    "kind": report.spec.target.kind.value,
                    "label": self._evaluation_target_label(
                        report.spec.target.kind,
                        metadata=report.spec.target.metadata,
                    ),
                }
            if not evaluators:
                evaluators = list(report.spec.config.evaluators)
        payload: dict[str, Any] = {
            "id": operation.resource_id,
            "operationId": operation.id,
            "status": report.status.value if report else operation.status,
            "createdAt": operation.created_at,
            "completedAt": operation.completed_at,
            "evalset": evalset or {},
            "target": target or {},
            "evaluators": evaluators or [],
            "progress": progress,
            "summary": report.summary if report else None,
            "hasReport": report is not None,
            "error": operation.error,
        }
        if include_report:
            payload["report"] = report
        return payload

    def _evaluation_target_label(
        self,
        kind: TargetKind,
        *,
        build_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if kind is not TargetKind.STUDIO_BUILD:
            return _EVALUATION_TARGET_LABELS[kind]
        agent_id = str((metadata or {}).get("agentId") or "")
        if not agent_id and build_id:
            agent_id = self.builds.get(build_id).agent_id
        if not agent_id:
            return _EVALUATION_TARGET_LABELS[kind]
        try:
            return self.drafts.get(agent_id).metadata.name
        except StudioError as exc:
            if exc.status_code != 404:
                raise
            return agent_id

    def evaluation_catalog(self) -> dict[str, list[dict]]:
        builds = [
            {
                "id": record.id,
                "agentId": record.agent_id,
                "runtime": record.runtime_type,
                "digest": _sha256_digest(record.bundle_digest),
                "createdAt": record.created_at.isoformat().replace("+00:00", "Z"),
            }
            for record in self.builds.list()
            if record.status == BuildStatus.SUCCEEDED and record.artifact_path
        ]
        builds.sort(key=lambda item: item["createdAt"], reverse=True)
        evalsets: list[dict] = []
        candidates: set[Path] = set()
        for pattern in ("*.yaml", "*.yml", "*.json"):
            candidates.update(self.workspace.root.glob(pattern))
            candidates.update(self.workspace.root.glob(f"evaluations/**/{pattern}"))
            candidates.update(self.workspace.root.glob(f"agents/*/evaluations/**/{pattern}"))
        for path in sorted(candidates):
            try:
                evalset = load_evalset(path)
            except (EvalSetParseError, OSError):
                continue
            evalsets.append({
                "path": path.relative_to(self.workspace.root).as_posix(),
                "name": evalset.name,
                "caseCount": len(evalset.cases),
                "contentDigest": evalset.content_digest,
            })
        evalsets.sort(key=lambda item: item["path"])
        return {"builds": builds, "evalsets": evalsets}

    def import_evaluation_file(self, content: bytes, *, filename: str) -> dict:
        if len(content) > 2 * 1024 * 1024:
            raise StudioError(
                "EVALSET_FILE_TOO_LARGE",
                "EvalSet 文件不能超过 2 MiB",
                status_code=413,
                field="file",
            )
        safe_name = Path(filename.replace("\\", "/")).name or "evalset.yaml"
        if Path(safe_name).suffix.lower() not in {".yaml", ".yml", ".json"}:
            raise StudioError(
                "EVALSET_FILE_TYPE_INVALID",
                "EvalSet 只支持 YAML 或 JSON 文件",
                status_code=422,
                field="file",
            )
        target = self.workspace.resolve(
            Path("evaluations/uploads") / f"{uuid4().hex[:12]}-{safe_name}"
        )
        self.workspace.atomic_write_bytes(target, content)
        try:
            evalset = load_evalset(target)
        except EvalSetParseError as exc:
            target.unlink(missing_ok=True)
            raise StudioError(exc.code, str(exc), status_code=422, field="file") from exc
        return {
            "path": self.workspace.relative(target),
            "name": evalset.name,
            "caseCount": len(evalset.cases),
            "contentDigest": evalset.content_digest,
        }

    def get_public_evaluation(self, evaluation_id: str):
        try:
            return self.evaluation_storage.read_report(evaluation_id)
        except EvaluationStorageError as exc:
            raise StudioError(
                "EVALUATION_NOT_FOUND",
                "Evaluation 不存在",
                status_code=404,
                details={"id": evaluation_id},
            ) from exc

    def _normalize_public_evaluation_target(self, target: TargetRef) -> TargetRef:
        if target.kind == TargetKind.A2A:
            parsed = urlparse(target.locator)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise StudioError(
                    "EVALUATION_A2A_URL_INVALID",
                    "A2A target 必须是 http 或 https URL",
                    status_code=422,
                    field="target.locator",
                )
            return target
        if target.kind == TargetKind.STUDIO_BUILD:
            self._validate_evaluation_build(target.locator)
            return target
        path = self.workspace.resolve(target.locator, must_exist=True)
        if not path.is_dir():
            raise StudioError(
                "EVALUATION_TARGET_INVALID",
                "本地 target 必须是工作区内的目录",
                status_code=422,
                field="target.locator",
            )
        return target.model_copy(update={"locator": str(path)})

    def _public_evaluation_adapter(self, request: PublicEvaluationRequest):
        if request.target.kind != TargetKind.STUDIO_BUILD:
            return None
        evidence_store = EvidenceStore(request.report_dir) if request.report_dir else None
        return StudioBuildTargetAdapter(
            timeout_seconds=request.config.timeout_seconds,
            resolve_build=self._resolve_evaluation_build,
            run_service=self.run_service,
            evidence_store=evidence_store,
        )

    def _resolve_evaluation_build(self, build_id: str) -> StudioBuildResolution:
        try:
            self.codex_builds.get(build_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
        else:
            raise StudioError(
                "CODEX_BUILD_NOT_IMMUTABLE",
                "Codex Build 尚未冻结工作区源码，不能作为不可变评测 Target",
                status_code=422,
                field="target.locator",
            )
        build = self.builds.get(build_id)
        if build.status != BuildStatus.SUCCEEDED or not build.artifact_path:
            raise StudioBuildTargetError(
                "STUDIO_BUILD_NOT_READY",
                "Studio Build must be SUCCEEDED before evaluation",
            )
        run_spec = self.framework_runs.resolve(build_id)
        revision_digest = str(build.bundle_digest or build.resolved_digest).strip()
        if not revision_digest:
            raise StudioBuildTargetError(
                "STUDIO_BUILD_INVALID",
                "Studio Build is missing an immutable digest",
            )
        return StudioBuildResolution(
            build_id=build.id,
            agent_id=build.agent_id,
            revision_digest=revision_digest,
            runtime=build.runtime_type,
            model=run_spec.model,
            run_spec=run_spec,
            metadata={
                "bundleDigest": build.bundle_digest,
                "resolvedDigest": build.resolved_digest,
                "sourceDigest": build.source_digest,
                "sourceRevision": build.source_revision,
            },
        )

    def _validate_evaluation_build(self, build_id: str) -> None:
        try:
            self.codex_builds.get(build_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
        else:
            raise StudioError(
                "CODEX_BUILD_NOT_IMMUTABLE",
                "Codex Build 尚未冻结工作区源码，不能作为不可变评测 Target",
                status_code=422,
                field="target.locator",
            )
        build = self.builds.get(build_id)
        if build.status != BuildStatus.SUCCEEDED or not build.artifact_path:
            raise StudioBuildTargetError(
                "STUDIO_BUILD_NOT_READY",
                "Studio Build must be SUCCEEDED before evaluation",
            )

    def submit_deployment(
        self,
        build_id: str,
        request: DeploymentRequest,
        *,
        idempotency_key: str,
    ) -> Operation:
        try:
            codex_build = self.codex_builds.get(build_id)
        except StudioError as exc:
            if exc.status_code != 404:
                raise
        else:
            snapshot = self.codex_manifests.load(codex_build.agent_name)
            if not self.codex_builder.is_current(codex_build):
                raise StudioError(
                    "BUILD_NOT_CURRENT",
                    "Codex YAML 已变更，请重新 Build 后再部署",
                    status_code=409,
                    details={"buildId": build_id},
                )
            # The deployed YAML records only the selected model identity.  Its
            # credential stays in the local resolver and is materialized for
            # this outbound control-plane request only, never in the Build or
            # deployment receipt.
            launch = self.codex_runs.resolve(build_id)
            runtime_environment = dict(
                (launch.launch_context.config or {}).get("env") or {}
            )
            # Retrying a YAML deployment must update the Agent that the prior
            # receipt already created.  CreateAgent can have succeeded before
            # a downstream Runtime-Service start failed; creating again would
            # leave duplicate cloud Agents and make the retry path non-idempotent.
            # A YAML edit necessarily produces a new immutable Build.  Match
            # the prior receipt through that Build's immutable agent_name,
            # rather than by build_id, otherwise every edit attempts a second
            # CreateAgent and Server correctly rejects the duplicate name.
            replacement_candidates = []
            for deployment in self.cloud.list():
                if deployment.artifact_id != "managed-runtime" or not deployment.agent_id:
                    continue
                try:
                    deployed_build = self.codex_builds.get(deployment.build_id)
                except StudioError:
                    # A malformed/legacy local receipt cannot establish
                    # ownership of this Agent and must not block a new deploy.
                    continue
                if deployed_build.agent_name == codex_build.agent_name:
                    replacement_candidates.append((deployed_build, deployment))
            replacing = (
                max(
                    replacement_candidates,
                    key=lambda item: (item[0].source_revision, item[0].created_at),
                )[1]
                if replacement_candidates
                else None
            )

            async def managed_runtime_runner(_operation_id: str):
                return await self.cloud.deploy_managed_runtime(
                    build_id=build_id,
                    agent_name=codex_build.agent_name,
                    manifest=snapshot.source_bytes.decode("utf-8"),
                    runtime_name=codex_build.runtime_name,
                    runtime_version=codex_build.runtime_version,
                    # Server canonicalizes and records its own digest. This
                    # source digest remains the immutable Studio Build receipt.
                    manifest_digest=codex_build.manifest_sha256,
                    request=request,
                    runtime_environment=runtime_environment,
                    replacing=replacing,
                )

            return self.operations.submit(
                kind=OperationKind.DEPLOYMENT,
                resource_id=build_id,
                idempotency_key=idempotency_key,
                runner=managed_runtime_runner,
            )

        async def runner(_operation_id: str):
            return await self.cloud.deploy(build_id, request)

        return self.operations.submit(
            kind=OperationKind.DEPLOYMENT,
            resource_id=build_id,
            idempotency_key=idempotency_key,
            runner=runner,
        )

    def submit_rollback(
        self,
        deployment_id: str,
        *,
        target_build_id: str,
        idempotency_key: str,
    ) -> Operation:
        deployment = self.cloud.get(deployment_id)
        if deployment.artifact_id == "managed-runtime":
            deployed_build = self.codex_builds.get(deployment.build_id)
            target_build = self.codex_builds.get(target_build_id)
            if target_build.agent_name != deployed_build.agent_name:
                raise StudioError(
                    "MANAGED_RUNTIME_ROLLBACK_AGENT_MISMATCH",
                    "声明式 Agent 只能回滚到同一 Agent 的 Build",
                    status_code=409,
                    details={
                        "deploymentId": deployment_id,
                        "targetBuildId": target_build_id,
                    },
                )
            manifest = self.codex_builds.manifest_text(target_build)
            request = self.cloud.request_for(deployment_id)

            async def managed_runtime_runner(_operation_id: str):
                return await self.cloud.deploy_managed_runtime(
                    build_id=target_build.id,
                    agent_name=target_build.agent_name,
                    manifest=manifest,
                    runtime_name=target_build.runtime_name,
                    runtime_version=target_build.runtime_version,
                    manifest_digest=target_build.manifest_sha256,
                    request=request,
                    replacing=deployment,
                )

            return self.operations.submit(
                kind=OperationKind.DEPLOYMENT,
                resource_id=deployment_id,
                idempotency_key=idempotency_key,
                runner=managed_runtime_runner,
            )

        deployed_build = self.builds.get(deployment.build_id)
        target_build = self.builds.get(target_build_id)
        if target_build.agent_id != deployed_build.agent_id:
            raise StudioError(
                "DEPLOYMENT_ROLLBACK_AGENT_MISMATCH",
                "高代码 Agent 只能回滚到同一 Agent 的 Build",
                status_code=409,
                details={
                    "deploymentId": deployment_id,
                    "targetBuildId": target_build_id,
                },
            )

        async def runner(_operation_id: str):
            return await self.cloud.rollback(
                deployment_id,
                target_build_id=target_build_id,
            )

        return self.operations.submit(
            kind=OperationKind.DEPLOYMENT,
            resource_id=deployment_id,
            idempotency_key=idempotency_key,
            runner=runner,
        )

    def submit_account_agent_version_rollback(
        self,
        agent_id: str,
        *,
        version_id: str,
        idempotency_key: str,
    ) -> Operation:
        normalized_agent_id = str(agent_id or "").strip()
        normalized_version_id = str(version_id or "").strip()
        if not normalized_agent_id:
            raise StudioError(
                "CLOUD_AGENT_NOT_FOUND",
                "云端 Agent 标识不能为空",
                status_code=404,
            )
        if not normalized_version_id:
            raise StudioError(
                "CLOUD_AGENT_VERSION_REQUIRED",
                "请选择要回滚的云端版本",
                status_code=422,
                field="versionId",
            )
        resource_id = f"{normalized_agent_id}:{normalized_version_id}"

        async def runner(_operation_id: str):
            await self.cloud.rollback_account_agent_version(
                normalized_agent_id,
                version_id=normalized_version_id,
            )
            return _OperationResource(id=resource_id)

        return self.operations.submit(
            kind=OperationKind.DEPLOYMENT,
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            metadata={
                "agentId": normalized_agent_id,
                "targetVersionId": normalized_version_id,
                "source": "server-version",
            },
            runner=runner,
        )

    async def deployment_dashboard_access(self, deployment_id: str) -> dict[str, str | None]:
        """Return a receipt-bound private Hosted UI link on explicit user request."""

        return await self.cloud.dashboard_access(deployment_id)

    def deployment_operation_scope(self) -> dict[str, str]:
        """Return opaque browser storage scopes without exposing cloud credentials."""

        workspace_identity = str(self.workspace.root.resolve())
        region = (
            os.environ.get("AGENTENGINE_REGION")
            or os.environ.get("KSYUN_REGION")
            or "cn-beijing-6"
        ).strip()
        access_key = (
            os.environ.get("KSYUN_ACCESS_KEY")
            or os.environ.get("KS3_ACCESS_KEY")
            or "unsigned"
        ).strip()

        def opaque(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        return {
            "workspace": opaque(workspace_identity),
            "cloudCredential": opaque(f"{region}\0{access_key}"),
        }

    def get_settings(self) -> dict[str, Any]:
        path = self.workspace.resolve(".agentkit/settings.yaml")
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                data = load_yaml_file(path) or {}
            except Exception:
                data = {}
        defaults = {
            "sandbox": os.environ.get("KSADK_CODEX_SANDBOX", "read_only"),
            "buildAfterCreate": True,
            "codexProxy": current_proxy_mode(),
            "cloudRegion": os.environ.get(
                "AGENTENGINE_REGION", os.environ.get("KSYUN_REGION", "cn-beijing-6")
            ),
            "cloudBucket": os.environ.get("KS3_BUCKET", ""),
            "cloudSignedAccountConfigured": bool(
                (
                    os.environ.get("KSYUN_ACCESS_KEY")
                    or os.environ.get("KS3_ACCESS_KEY", "")
                ).strip()
                and (
                    os.environ.get("KSYUN_SECRET_KEY")
                    or os.environ.get("KS3_SECRET_KEY", "")
                ).strip()
            ),
            "traceContent": os.environ.get("KSADK_STUDIO_TRACE_CONTENT", "1") != "0",
        }
        defaults.update({k: v for k, v in data.items() if k in defaults and v is not None})
        defaults["codexProxy"] = normalize_proxy_mode(defaults.get("codexProxy"))
        return defaults

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "sandbox",
            "buildAfterCreate",
            "codexProxy",
            "cloudRegion",
            "cloudBucket",
            "traceContent",
        }
        data = {k: payload[k] for k in allowed if k in payload}
        if data.get("sandbox") and data["sandbox"] not in {
            "read-only",
            "workspace-write",
            "workspace-write-auto",
            "full-access",
            "read_only",
            "workspace_write",
            "workspace_write_auto",
            "full_access",
        }:
            raise StudioError("SETTINGS_INVALID", "sandbox 取值非法", status_code=422)
        if "codexProxy" in data and data["codexProxy"] not in {"auto", "forced", "direct"}:
            raise StudioError("SETTINGS_INVALID", "codexProxy 取值非法", status_code=422)
        path = self.workspace.resolve(".agentkit/settings.yaml")
        self.workspace.atomic_write_yaml(path, data)
        self._apply_settings_to_env(data)
        if self._cloud_gateway_override is None:
            self.cloud.gateway = self._configured_cloud_gateway()
        return self.get_settings()

    def _apply_persisted_settings(self) -> None:
        """启动时把 settings.yaml 回填到进程环境。

        update_settings 只在 PUT 时桥接 env;重启后 env 丢失,运行解析
        (如 _resolve_sandbox 读 KSADK_CODEX_SANDBOX)会回落默认,表现为
        「设置页显示 workspace-write-auto,实际运行 read-only」。
        """
        path = self.workspace.resolve(".agentkit/settings.yaml")
        if not path.is_file():
            return
        try:
            data = load_yaml_file(path) or {}
        except Exception:
            return
        if isinstance(data, dict):
            self._apply_settings_to_env(data)

    @staticmethod
    def _apply_settings_to_env(data: dict[str, Any]) -> None:
        if data.get("sandbox"):
            os.environ["KSADK_CODEX_SANDBOX"] = data["sandbox"]
        if "codexProxy" in data:
            proxy_env = proxy_mode_env_value(data["codexProxy"])
            if proxy_env is None:
                os.environ.pop("KSADK_CODEX_USE_PROXY", None)
            else:
                os.environ["KSADK_CODEX_USE_PROXY"] = proxy_env
        if data.get("cloudRegion"):
            os.environ["AGENTENGINE_REGION"] = data["cloudRegion"]
        if data.get("cloudBucket"):
            os.environ["KS3_BUCKET"] = data["cloudBucket"]
        if "traceContent" in data:
            os.environ["KSADK_STUDIO_TRACE_CONTENT"] = "1" if data["traceContent"] else "0"

    @staticmethod
    def _configured_cloud_gateway() -> CloudDeploymentGateway:
        """Compose the existing signed Code deployment path from process-only credentials."""

        access_key = (
            os.environ.get("KSYUN_ACCESS_KEY") or os.environ.get("KS3_ACCESS_KEY", "")
        ).strip()
        secret_key = (
            os.environ.get("KSYUN_SECRET_KEY") or os.environ.get("KS3_SECRET_KEY", "")
        ).strip()
        region = os.environ.get(
            "AGENTENGINE_REGION", os.environ.get("KSYUN_REGION", "")
        ).strip()
        if not all((access_key, secret_key, region)):
            return UnavailableCloudGateway()
        control_client = AgentEngineClient(
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )
        stream_base_url = os.environ.get("AGENTENGINE_STREAM_SERVER_URL", "").strip()
        if not stream_base_url and region.lower() == "pre-online":
            # The pre-online KOP response path currently buffers SSE until
            # EOF.  Its internal Server ingress validates the same V4
            # signature and preserves the RunAgent streaming response.
            stream_base_url = "http://agent-api-pre.kspmas-internal.ksyun.com"
        stream_client = (
            AgentEngineClient(
                base_url=stream_base_url,
                region=region,
                access_key=access_key,
                secret_key=secret_key,
            )
            if stream_base_url
            else control_client
        )
        return DirectAgentEngineCloudDeploymentGateway(
            region=region,
            client=control_client,
            stream_client=stream_client,
            bucket=os.environ.get("KS3_BUCKET", "").strip() or None,
            ks3_credentials={
                "access_key": access_key,
                "secret_key": secret_key,
            },
        )

    @staticmethod
    def builtin_capabilities():
        return list(builtin_tool_contracts().values())

    def list_capabilities(
        self,
        *,
        kind: str | None = None,
        query: str = "",
    ) -> list[dict]:
        return [
            item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for item in self.catalog.list(kind=kind, query=query, limit=200)
        ]


def _sha256_digest(value: str) -> str:
    digest = str(value or "").strip()
    return digest if digest.startswith("sha256:") else f"sha256:{digest}"
