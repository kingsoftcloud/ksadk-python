"""Application service composing Studio modules behind one local API boundary."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Literal, cast
from urllib.parse import urlparse

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
from ksadk.runtime import RuntimeExecutor, build_default_runtime_registry
from ksadk.studio.agent_avatar_assets import AgentAvatarAssetStore
from ksadk.studio.agent_lifecycle import delete_framework_agent
from ksadk.studio.authoring_coordinator import StudioAuthoringCoordinator
from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.capabilities import builtin_tool_contracts
from ksadk.studio.cloud import (
    CloudDeploymentGateway,
    CloudDeploymentService,
    UnavailableCloudGateway,
)
from ksadk.studio.codex_agent_service import CodexAgentService, CodexDraftRepository
from ksadk.studio.codex_builder import (
    CodexBuildRecord,
    CodexBuildRepository,
    CodexStudioBuilder,
    RuntimeInspector,
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
    DeploymentRequest,
    Operation,
    OperationKind,
    RunEvent,
    RunStatus,
    RuntimeRef,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.evaluation import EvaluationRunner
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
        self.event_store.recover_interrupted()
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
        self.evaluations = EvaluationRunner(
            self.workspace,
            run_agent=self.run_build,
            event_store=self.event_store,
            build_repository=self.builds,
        )
        self.cloud = CloudDeploymentService(
            self.workspace,
            gateway=cloud_gateway or UnavailableCloudGateway(),
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
    ) -> AgentDraft:
        return self.codex_agents.update(
            agent_id,
            spec,
            expected_revision=expected_revision,
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
            runtime_input=runtime_input,
            idempotency_key=idempotency_key,
            on_event=on_event,
        )

    def delete_session(self, session_id: str) -> None:
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
        self.event_store.delete_session(session_id)

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
        draft = self.drafts.create(
            agent_id=agent_id,
            name=name,
            description=description,
            template=template,
            spec=resolved_spec,
            labels=labels,
        )
        materialize_generated_runtime_source(self.workspace, draft)
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
    ) -> AgentDraft:
        if self.is_codex_agent(agent_id):
            spec.runtime = self.agent_detail(agent_id)["draft"].spec.runtime
            return cast(
                AgentDraft,
                self.update_codex_agent(
                    agent_id,
                    spec,
                    expected_revision=expected_revision,
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
            self.update_agent(agent_id, spec, expected_revision=expected_revision),
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
    ):
        self._validate_bindings(spec.bindings)
        updated = self.drafts.update(
            agent_id,
            spec,
            expected_revision=expected_revision,
        )
        materialize_generated_runtime_source(self.workspace, updated)
        return updated

    def update_agent_bindings(
        self,
        agent_id: str,
        bindings: AgentBindings,
        *,
        expected_revision: int,
    ):
        self._validate_bindings(bindings)
        draft = self.drafts.get(agent_id)
        spec = draft.spec.model_copy(deep=True)
        spec.bindings = bindings
        return self.drafts.update(
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

        async def runner():
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
        runtime_input: Any = None,
        idempotency_key: str,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> Operation:
        async def runner():
            return await self.run_build(
                build_id,
                user_input,
                session_id,
                model=model,
                sandbox=sandbox,
                approval_mode=approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
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
        if collaboration_mode or goal_objective:
            from dataclasses import replace

            request_config = dict(spec.request_config)
            if collaboration_mode:
                request_config["collaboration_mode"] = collaboration_mode
            if goal_objective:
                request_config["goal_objective"] = goal_objective
                request_config["ephemeral"] = False
            spec = replace(spec, request_config=request_config)
        return await self.run_service.run(
            spec,
            user_input,
            runtime_input=runtime_input,
            session_id=session_id,
            on_event=on_event,
        )

    def submit_evaluation(
        self,
        build_id: str,
        suite_refs: list[str],
        *,
        fail_fast: bool,
        idempotency_key: str,
    ) -> Operation:
        async def runner():
            return await self.evaluations.run(
                build_id,
                suite_refs,
                fail_fast=fail_fast,
            )

        return self.operations.submit(
            kind=OperationKind.EVALUATION,
            resource_id=build_id,
            idempotency_key=idempotency_key,
            runner=runner,
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

        async def runner():
            try:
                report = await execute_evaluation(request)
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
            try:
                self.evaluation_storage.write_report(report)
            except EvaluationStorageError as exc:
                raise StudioError(
                    "EVALUATION_REPORT_WRITE_FAILED",
                    "评测报告写入失败",
                    status_code=500,
                ) from exc
            return report

        return self.operations.submit(
            kind=OperationKind.EVALUATION,
            resource_id=evalset.content_digest,
            idempotency_key=idempotency_key,
            runner=runner,
        )

    def list_public_evaluations(self):
        return self.evaluation_storage.list_reports()

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
        path = self.workspace.resolve(target.locator, must_exist=True)
        if not path.is_dir():
            raise StudioError(
                "EVALUATION_TARGET_INVALID",
                "本地 target 必须是工作区内的目录",
                status_code=422,
                field="target.locator",
            )
        return target.model_copy(update={"locator": str(path)})

    def submit_deployment(
        self,
        build_id: str,
        request: DeploymentRequest,
        *,
        idempotency_key: str,
    ) -> Operation:
        async def runner():
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
        async def runner():
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
            "codexProxy": os.environ.get("KSADK_CODEX_USE_PROXY", "auto"),
            "cloudAccessKey": os.environ.get("KINGSOFTCLOUD_ACCESS_KEY", ""),
            "cloudSecretKey": os.environ.get("KINGSOFTCLOUD_SECRET_KEY", ""),
            "cloudRegion": os.environ.get("KSYUN_REGION", "cn-beijing-6"),
            "traceContent": os.environ.get("KSADK_STUDIO_TRACE_CONTENT", "1") != "0",
        }
        defaults.update({k: v for k, v in data.items() if v is not None})
        return defaults

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "sandbox",
            "buildAfterCreate",
            "codexProxy",
            "cloudAccessKey",
            "cloudSecretKey",
            "cloudRegion",
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
        path = self.workspace.resolve(".agentkit/settings.yaml")
        self.workspace.atomic_write_yaml(path, data)
        self._apply_settings_to_env(data)
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
        if data.get("codexProxy"):
            os.environ["KSADK_CODEX_USE_PROXY"] = data["codexProxy"]
        if data.get("cloudAccessKey"):
            os.environ["KINGSOFTCLOUD_ACCESS_KEY"] = data["cloudAccessKey"]
        if data.get("cloudSecretKey"):
            os.environ["KINGSOFTCLOUD_SECRET_KEY"] = data["cloudSecretKey"]
        if data.get("cloudRegion"):
            os.environ["KSYUN_REGION"] = data["cloudRegion"]
        if "traceContent" in data:
            os.environ["KSADK_STUDIO_TRACE_CONTENT"] = "1" if data["traceContent"] else "0"

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
