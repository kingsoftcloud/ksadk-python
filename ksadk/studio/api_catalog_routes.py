"""Catalog, credential and capability routes for Studio."""

from __future__ import annotations

from typing import Awaitable, Callable, Literal

from fastapi import FastAPI, File, Query, UploadFile
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from ksadk.studio.api_contracts import (
    CredentialPutRequest,
    MCPResourceCreateRequest,
    ModelProfileCreateRequest,
    SecretReferenceCheckRequest,
    SkillDiscoveryCommitRequest,
    SkillDiscoveryRequest,
    ToolPolicyPreviewRequest,
    ToolResourceCreateRequest,
    ToolSchemaValidationRequest,
)
from ksadk.studio.contracts import MCPServerRef
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService

ModelCatalogLoader = Callable[[], Awaitable[tuple[list, str]]]


def register_catalog_routes(
    app: FastAPI,
    studio: StudioService,
    *,
    runtime_model_catalog: ModelCatalogLoader,
) -> None:
    """Register resource routes without expanding the main HTTP composition."""

    @app.get("/api/v1/capabilities")
    async def capabilities(kind: str | None = None, query: str = ""):
        return {"items": studio.list_capabilities(kind=kind, query=query)}

    @app.get("/api/v1/catalog/resources")
    async def catalog_resources(
        kind: Literal["model", "tool", "mcp", "skill"] | None = None,
        query: str = "",
        source: Literal["builtin", "provider", "local", "market"] | None = None,
        status: str | None = None,
        installed: bool | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return {
            "items": studio.catalog.list(
                kind=kind,
                query=query,
                source=source,
                status=status,
                installed=installed,
                limit=limit,
            ),
            "nextCursor": None,
        }

    @app.get("/api/v1/catalog/models")
    async def catalog_models():
        items, source = await runtime_model_catalog()
        return {"items": items, "source": source, "nextCursor": None}

    @app.get("/api/v1/catalog/runtimes")
    async def catalog_runtimes():
        return {"items": studio.runtime_catalog(), "nextCursor": None}

    @app.get("/api/v1/catalog/resources/{resource_id}")
    async def get_catalog_resource(resource_id: str):
        return studio.catalog.get(resource_id)

    @app.post("/api/v1/catalog/model-profiles", status_code=201)
    async def create_model_profile(payload: ModelProfileCreateRequest):
        return studio.catalog.create_model_profile(
            name=payload.name,
            display_name=payload.display_name,
            version=payload.version,
            description=payload.description,
            spec=payload.spec,
        )

    @app.post("/api/v1/catalog/mcp-servers", status_code=201)
    async def create_mcp_resource(payload: MCPResourceCreateRequest):
        return studio.catalog.create_mcp_server(
            display_name=payload.display_name,
            description=payload.description,
            server=payload.server,
        )

    @app.post("/api/v1/catalog/mcp-servers/{resource_id}:probe")
    async def probe_mcp_resource(
        resource_id: str,
        timeout_seconds: int = Query(default=10, alias="timeoutSeconds", ge=1, le=60),
    ):
        descriptor = studio.catalog.get(resource_id)
        if descriptor.kind != "mcp":
            raise StudioError(
                "RESOURCE_KIND_INVALID",
                "该 Resource 不是 MCP Server",
                status_code=422,
                details={"resourceId": resource_id},
            )
        server_payload = {
            key: value for key, value in descriptor.contract.items() if key != "discoveredTools"
        }
        server = MCPServerRef.model_validate(server_payload)
        try:
            result = await studio.mcp_runtime.probe(server, timeout_seconds=timeout_seconds)
        except StudioError as exc:
            studio.catalog.mark_probe_failed(resource_id, code=exc.code)
            raise
        return studio.catalog.save_probe(resource_id, result=result)

    @app.post("/api/v1/catalog/tools", status_code=201)
    async def create_tool_resource(payload: ToolResourceCreateRequest):
        return studio.catalog.create_tool(
            display_name=payload.display_name,
            category=payload.category,
            contract=payload.contract,
        )

    @app.post("/api/v1/catalog/skills:import", status_code=201)
    async def import_skill(file: UploadFile = File(...)):
        content = await file.read(50 * 1024 * 1024 + 1)
        return studio.catalog.import_skill_zip(content, filename=file.filename or "skill.zip")

    @app.post("/api/v1/catalog/skills:discover")
    async def discover_skills(payload: SkillDiscoveryRequest):
        return studio.catalog.discover_skills(scan_paths=payload.scan_paths or None)

    @app.post(
        "/api/v1/catalog/skills/discoveries/{inspection_token}:commit",
        status_code=201,
    )
    async def commit_discovered_skill(
        inspection_token: str,
        payload: SkillDiscoveryCommitRequest,
    ):
        return studio.catalog.commit_discovered_skill(
            inspection_token,
            payload.candidate_id,
            overwrite=payload.overwrite,
        )

    @app.post("/api/v1/tool-schemas:validate")
    async def validate_tool_schema(payload: ToolSchemaValidationRequest):
        try:
            Draft202012Validator.check_schema(payload.schema_definition)
        except SchemaError as exc:
            return {
                "valid": False,
                "diagnostics": [
                    {
                        "code": "TOOL_SCHEMA_INVALID",
                        "message": exc.message,
                        "path": list(exc.path),
                    }
                ],
            }
        errors = sorted(
            Draft202012Validator(payload.schema_definition).iter_errors(payload.sample),
            key=lambda error: list(error.absolute_path),
        )
        return {
            "valid": not errors,
            "diagnostics": [
                {
                    "code": "TOOL_SAMPLE_INVALID",
                    "message": error.message,
                    "path": list(error.absolute_path),
                }
                for error in errors
            ],
        }

    @app.post("/api/v1/tool-policies:preview")
    async def preview_tool_policy(payload: ToolPolicyPreviewRequest):
        tools, permissions = studio.catalog.policy_preview(payload.bindings)
        return {"tools": tools, "allowedPermissions": permissions}

    @app.post("/api/v1/mcp-servers:probe")
    async def probe_mcp_server(
        payload: MCPServerRef,
        timeout_seconds: int = Query(default=10, alias="timeoutSeconds", ge=1, le=60),
    ):
        return await studio.mcp_runtime.probe(payload, timeout_seconds=timeout_seconds)

    @app.post("/api/v1/secret-references:check")
    async def check_secret_reference(payload: SecretReferenceCheckRequest):
        status = studio.credentials.status(payload.ref)
        return {
            "exists": status["configured"],
            "scheme": payload.ref.partition("://")[0],
            "source": status["source"],
        }

    @app.get("/api/v1/credentials/{credential_name}")
    async def get_credential_status(credential_name: str):
        return studio.credentials.status(f"env://{credential_name}")

    @app.put("/api/v1/credentials/{credential_name}")
    async def put_credential(credential_name: str, payload: CredentialPutRequest):
        return studio.credentials.put_session(
            credential_name,
            payload.value.get_secret_value(),
        )

    @app.delete("/api/v1/credentials/{credential_name}")
    async def delete_credential(credential_name: str):
        return studio.credentials.delete_session(credential_name)

    @app.post("/api/v1/model-profiles/{resource_id}:test")
    async def test_model_profile(resource_id: str):
        return await studio.test_model_profile(resource_id)


__all__ = ["register_catalog_routes"]
