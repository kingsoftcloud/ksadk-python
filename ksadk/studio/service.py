"""Application service composing Studio modules behind one local API boundary."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.capabilities import builtin_tool_contracts
from ksadk.studio.cloud import (
    CloudDeploymentGateway,
    CloudDeploymentService,
    UnavailableCloudGateway,
)
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    AgentTemplateComposeRequest,
    AgentTemplateComposition,
    DeploymentRequest,
    ModelSpec,
    NetworkPolicy,
    Operation,
    OperationKind,
)
from ksadk.studio.evaluation import EvaluationRunner
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.model_client import CredentialResolver, OpenAICompatibleModelClient
from ksadk.studio.operations import OperationManager
from ksadk.studio.repository import AgentDraftRepository, BuildRepository
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.runtime import LocalAgentRuntime
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
    ) -> None:
        self.workspace = Workspace(root)
        self.workspace.initialize()
        self.drafts = AgentDraftRepository(self.workspace)
        self.catalog = LocalResourceCatalog(self.workspace)
        self.builds = BuildRepository(self.workspace)
        self.validator = AgentValidator()
        self.builder = AgentBundleBuilder(self.workspace, repository=self.builds)
        self.event_store = RunEventStore(self.workspace)
        self.credentials = (
            credential_resolver
            or getattr(model_client, "credential_resolver", None)
            or CredentialResolver()
        )
        runtime_model_client = model_client or OpenAICompatibleModelClient(
            credential_resolver=self.credentials
        )
        self.runtime = LocalAgentRuntime(
            self.workspace,
            model_client=runtime_model_client,
            build_repository=self.builds,
            event_store=self.event_store,
        )
        self.evaluations = EvaluationRunner(
            self.workspace,
            runtime=self.runtime,
            build_repository=self.builds,
        )
        self.cloud = CloudDeploymentService(
            self.workspace,
            gateway=cloud_gateway or UnavailableCloudGateway(),
            build_repository=self.builds,
        )
        self.operations = OperationManager(self.workspace)

    async def test_model_profile(self, resource_id: str) -> dict:
        descriptor = self.catalog.get(resource_id)
        if descriptor.kind != "model":
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "RESOURCE_KIND_INVALID",
                "连接测试只能用于 Model Profile",
                status_code=422,
                details={"resourceId": resource_id},
            )
        spec = ModelSpec.model_validate(descriptor.contract)
        resolved = self.catalog.resolver.resolve_model(spec)
        resolved.parameters = resolved.parameters.model_copy(
            update={
                "temperature": 0,
                "max_tokens": min(resolved.parameters.max_tokens, 16),
            }
        )
        host = (urlparse(resolved.endpoint_url).hostname or "").lower().rstrip(".")
        started = time.monotonic()
        response = await self.runtime.model_client.complete(
            resolved,
            messages=[
                {
                    "role": "user",
                    "content": "这是连接测试。请只回复 OK。",
                }
            ],
            network_policy=NetworkPolicy(
                mode="restricted",
                allowed_hosts=[host] if host else [],
                allow_private_network=False,
            ),
            timeout_seconds=20,
            max_attempts=1,
            backoff_seconds=0,
        )
        return {
            "ok": True,
            "resourceId": resource_id,
            "model": resolved.model,
            "finishReason": response.finish_reason,
            "latencyMs": int((time.monotonic() - started) * 1000),
        }

    def create_agent(
        self,
        *,
        agent_id: str,
        name: str,
        description: str = "",
        template: str = "blank",
        spec: AgentSpec | None = None,
    ):
        resolved_spec = spec or default_agent_spec(
            template,
            description=description,
        )
        if resolved_spec.bindings.model_profile_id:
            self._validate_bindings(resolved_spec.bindings)
        return self.drafts.create(
            agent_id=agent_id,
            name=name,
            description=description,
            template=template,
            spec=resolved_spec,
        )

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
        return self.drafts.update(
            agent_id,
            spec,
            expected_revision=expected_revision,
        )

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
        idempotency_key: str,
    ) -> Operation:
        async def runner():
            return await self.runtime.run(
                build_id,
                user_input,
                session_id=session_id,
            )

        return self.operations.submit(
            kind=OperationKind.RUN,
            resource_id=build_id,
            idempotency_key=idempotency_key,
            runner=runner,
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
