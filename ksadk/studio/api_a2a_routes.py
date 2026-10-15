"""Loopback Studio BFF routes for A2A catalog and binding freeze."""

from __future__ import annotations

from fastapi import FastAPI, Query
from pydantic import Field, field_validator

from ksadk.studio.contracts import A2AOrchestration, ContractModel, RemoteAgentBinding, RuntimeRef
from ksadk.studio.errors import StudioError

from ksadk.studio.a2a_bindings import A2ABindingFreezeRequest
from ksadk.studio.service import StudioService


class A2APreviewRequest(ContractModel):
    expected_revision: int = Field(ge=1)
    orchestration: A2AOrchestration
    bindings: list[RemoteAgentBinding]
    runtime: RuntimeRef

    @field_validator("bindings")
    @classmethod
    def validate_unique_bindings(cls, value):
        from ksadk.studio.contracts import AgentBindings
        AgentBindings(remote_agents=value)
        return value


def register_a2a_routes(app: FastAPI, studio: StudioService) -> None:
    @app.post("/api/v1/agents/{agent_id}/a2a:preview")
    async def preview_unsaved_a2a_orchestration(agent_id: str, payload: A2APreviewRequest):
        from ksadk.studio.a2a_orchestration import compile_orchestration
        from ksadk.studio.contracts import AgentBindings

        draft = studio.drafts.get(agent_id)
        if draft.metadata.revision != payload.expected_revision:
            raise StudioError("AGENT_REVISION_CONFLICT", "Agent 已变化，请刷新后重新预览", status_code=409)
        draft = draft.model_copy(deep=True)
        binding_data = draft.spec.bindings.model_dump()
        binding_data["remote_agents"] = [row.model_dump() for row in payload.bindings]
        draft.spec.bindings = AgentBindings.model_validate(binding_data)
        draft.spec.runtime = payload.runtime
        draft.spec.a2a_orchestration = payload.orchestration
        await studio.a2a_catalog.validate_bindings(draft.spec.bindings.remote_agents)
        compiled = compile_orchestration(draft)
        return {"plan": compiled.plan, "source": compiled.source,
                "entrySource": compiled.entry_source, "digest": compiled.digest}

    @app.get("/api/v1/agents/{agent_id}/a2a:preview")
    async def preview_a2a_orchestration(agent_id: str):
        from ksadk.studio.a2a_orchestration import compile_orchestration

        draft = studio.drafts.get(agent_id)
        await studio.a2a_catalog.validate_bindings(draft.spec.bindings.remote_agents)
        compiled = compile_orchestration(draft)
        return {"plan": compiled.plan if compiled else None,
                "source": compiled.source if compiled else None,
                "entrySource": compiled.entry_source if compiled else None,
                "digest": compiled.digest if compiled else None}

    @app.get("/api/v1/a2a/spaces")
    async def list_a2a_spaces(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
    ):
        return await studio.a2a_catalog.list_spaces(page=page, size=size)

    @app.get("/api/v1/a2a/spaces/{space_id}/agents")
    async def list_a2a_space_agents(space_id: str):
        return await studio.a2a_catalog.list_space_agents(space_id)

    @app.get("/api/v1/a2a/agents/{agent_id}")
    async def get_a2a_agent(agent_id: str):
        return await studio.a2a_catalog.get_agent(agent_id)

    @app.get("/api/v1/a2a/agents/{agent_id}/versions")
    async def list_a2a_agent_versions(
        agent_id: str,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
    ):
        return await studio.a2a_catalog.list_agent_versions(
            agent_id,
            page=page,
            size=size,
        )

    @app.post("/api/v1/a2a/bindings:freeze")
    async def freeze_a2a_binding(payload: A2ABindingFreezeRequest):
        return await studio.a2a_catalog.freeze_binding(payload)


__all__ = ["register_a2a_routes"]
