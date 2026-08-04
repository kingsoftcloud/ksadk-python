"""Application-level commits for Agent authoring inspections."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, cast

from ksadk.studio.authoring import AgentAuthoringService
from ksadk.studio.codex_manifest import CodexAgentManifest
from ksadk.studio.contracts import (
    AgentBindings,
    AgentDraft,
    AgentSpec,
    Instructions,
    ModelSpec,
    RuntimeRef,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.templates import default_agent_spec


class StudioAuthoringCoordinator:
    """Coordinates repositories without expanding the StudioService façade."""

    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self.backend = AgentAuthoringService(studio.workspace)

    def create(
        self,
        *,
        name: str,
        slug: str,
        runtime_type: str,
        template: str = "blank",
        description: str = "",
        spec: AgentSpec | None = None,
    ) -> AgentDraft:
        agent_id = self.backend.allocate_agent_id(slug)
        resolved = (spec or default_agent_spec(template, description=description)).model_copy(
            deep=True
        )
        resolved.runtime = self.backend.runtime_ref(agent_id, runtime_type)
        if description:
            resolved.description = description
        draft = self.studio.create_studio_agent(
            agent_id=agent_id,
            name=name,
            description=description,
            template=template,
            spec=resolved,
        )
        if self.studio.is_codex_agent(agent_id):
            return cast(AgentDraft, draft)
        draft.metadata.labels["agentkit.ksyun.com/slug"] = self.backend.normalize_slug(slug)
        return cast(AgentDraft, self.studio.drafts.replace(draft))

    def inspect_import(self, content: bytes, *, filename: str) -> dict:
        return self.backend.inspect_import(content, filename=filename)

    def commit_import(
        self,
        inspection_token: str,
        *,
        name: str | None = None,
        slug: str | None = None,
    ) -> AgentDraft:
        inspection = self.backend.load_import(inspection_token)
        display_name = str(name or inspection.display_name).strip()
        resolved_slug = slug or display_name
        agent_id = self.backend.allocate_agent_id(resolved_slug)
        if inspection.kind == "codex-manifest":
            created = self._commit_codex_import(
                agent_id,
                display_name,
                CodexAgentManifest.model_validate(inspection.payload),
            )
            self.backend.consume_import(inspection_token)
            return created

        imported = AgentDraft.model_validate(inspection.payload)
        spec = imported.spec.model_copy(deep=True)
        source = self.backend.source_directory(inspection)
        if spec.runtime is None:
            raise StudioError(
                "AGENT_RUNTIME_REQUIRED",
                "导入 Agent 必须声明 RuntimeRef",
                status_code=422,
            )
        if spec.runtime.type in {"adk", "langgraph"}:
            spec.runtime = self.backend.runtime_ref(agent_id, spec.runtime.type)
        created = self.studio.create_agent(
            agent_id=agent_id,
            name=display_name,
            description=spec.description,
            template=imported.metadata.labels.get(
                "agentkit.ksyun.com/template",
                "blank",
            ),
            spec=spec,
            labels={
                "agentkit.ksyun.com/slug": self.backend.normalize_slug(resolved_slug),
                "agentkit.ksyun.com/source": "import",
                "agentkit.ksyun.com/source-digest": inspection.source_digest,
            },
        )
        if source is not None and created.spec.runtime and created.spec.runtime.project_path:
            target = self.studio.workspace.resolve(created.spec.runtime.project_path)
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(
                source,
                target,
                symlinks=False,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
        self.backend.consume_import(inspection_token)
        return cast(AgentDraft, self.studio.drafts.get(agent_id))

    def inspect_project(self, project_path: str) -> dict:
        return self.backend.inspect_project(project_path)

    def commit_project(
        self,
        inspection_token: str,
        *,
        name: str | None = None,
        slug: str | None = None,
        model_profile_id: str | None = None,
    ) -> AgentDraft:
        inspection = self.backend.load_project(inspection_token)
        runtime_type = str(inspection["runtimeType"])
        display_name = str(name or inspection.get("name") or "Imported Agent").strip()
        resolved_slug = slug or display_name
        if runtime_type == "codex":
            manifest_path = self.studio.workspace.resolve(
                Path(str(inspection["projectPath"])) / "agentengine.yaml",
                must_exist=True,
            )
            created = self.commit_import(
                self.inspect_import(
                    manifest_path.read_bytes(),
                    filename="agentengine.yaml",
                )["inspectionToken"],
                name=display_name,
                slug=resolved_slug,
            )
            self.backend.consume_project(inspection_token)
            return created

        agent_id = self.backend.allocate_agent_id(resolved_slug)
        config = inspection.get("evidence", {}).get("config", {})
        prompt = str(
            config.get("prompt")
            or config.get("instruction")
            or "You are a reliable assistant imported from an existing project."
        )
        runtime = RuntimeRef(
            type=cast(Any, runtime_type),
            project_path=str(inspection["projectPath"]),
            entry_point=str(inspection.get("entryPoint") or "agent.py"),
            agent_variable=str(
                inspection.get("agentVariable")
                or ("graph" if runtime_type == "langgraph" else "root_agent")
            ),
            detection="auto",
        )
        created = self.studio.create_agent(
            agent_id=agent_id,
            name=display_name,
            spec=AgentSpec(
                runtime=runtime,
                instructions=Instructions(system=prompt),
                bindings=AgentBindings(model_profile_id=model_profile_id),
            ),
            labels={
                "agentkit.ksyun.com/slug": self.backend.normalize_slug(resolved_slug),
                "agentkit.ksyun.com/source": "project-detection",
                "agentkit.ksyun.com/source-digest": str(inspection["sourceDigest"]),
            },
        )
        self.backend.consume_project(inspection_token)
        return cast(AgentDraft, created)

    async def compose_conversation(
        self,
        *,
        messages: list[dict[str, str]],
        model_profile_id: str,
    ) -> dict:
        model_spec = self.studio.catalog.resolve_model(
            AgentBindings(model_profile_id=model_profile_id)
        )
        if model_spec is None:
            raise StudioError(
                "AGENT_MODEL_REQUIRED",
                "对话构建需要选择一个 Model Profile",
                status_code=422,
            )
        model = self.studio.catalog.resolver.resolve_model(model_spec)
        response = await self.studio.model_client.complete(
            model,
            messages=self.backend.conversation_messages(messages),
            network_policy=self.backend.authoring_network_policy(model.endpoint_url),
            timeout_seconds=60,
            max_attempts=2,
            backoff_seconds=1,
        )
        proposal = self.backend.parse_conversation_proposal(response.content)
        return {
            "proposal": proposal.model_dump(mode="json"),
            "requiresConfirmation": True,
            "usage": response.usage.model_dump(by_alias=True, mode="json"),
        }

    def _commit_codex_import(
        self,
        agent_id: str,
        display_name: str,
        manifest: CodexAgentManifest,
    ) -> AgentDraft:
        spec = AgentSpec(
            runtime=RuntimeRef(type="codex", version=manifest.runtime.version),
            instructions=Instructions(system=manifest.prompt),
            model=ModelSpec(
                model=manifest.model,
                endpoint_url="https://kspmas.ksyun.com/v1/chat/completions",
                credential_ref="env://AGENTKIT_MODEL_API_KEY",
            ),
        )
        return cast(
            AgentDraft,
            self.studio.create_codex_agent(
                agent_id=agent_id,
                spec=spec,
                name=display_name,
            ),
        )


__all__ = ["StudioAuthoringCoordinator"]
