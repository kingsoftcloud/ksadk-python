"""Data-only resource connection configuration under the existing Studio API guard."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query
from pydantic import Field

from ksadk.api.client import AgentEngineClient
from ksadk.plugins.contracts import PluginContractModel
from ksadk.plugins.dsh_installation_digest import installation_digest
from ksadk.resource_runtime.snapshots import ConnectionTarget
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_connections import (
    ResourceConnectionDeclaration,
    ResourceCredentialReferences,
)

PlatformResourceKind = Literal["knowledge-base", "memory-instance", "skill-space"]

_PLATFORM_RESOURCE_ACTIONS = {
    "knowledge-base": ("list_knowledge_bases", "knowledge_bases"),
    "memory-instance": ("list_memory_instances", "memory_instances"),
    "skill-space": ("list_skill_workspaces", "skill_workspaces"),
}
_PLATFORM_RESOURCE_PLUGINS = {
    "knowledge-base": ("kingsoftcloud.dsh-knowledge", "dsh-knowledge"),
    "memory-instance": ("kingsoftcloud.dsh-memory", "dsh-memory"),
    "skill-space": ("kingsoftcloud.dsh-skill-center", "dsh-skill-center"),
}


class SaveResourceConnection(PluginContractModel):
    expected_revision: int = Field(strict=True, ge=0)
    connection: ResourceConnectionDeclaration


class ValidateResourceBindings(PluginContractModel):
    expected_revision: int = Field(strict=True, ge=1)


def register_resource_connection_routes(app: FastAPI, studio: Any) -> None:
    @app.get("/api/v1/platform-resources")
    async def list_platform_resources(kind: PlatformResourceKind):
        method_name, result_key = _PLATFORM_RESOURCE_ACTIONS[kind]
        client = AgentEngineClient()
        try:
            payload = await getattr(client, method_name)()
        finally:
            await client.close()
        raw_items = payload.get(result_key, [])
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise StudioError(
                "PLATFORM_RESOURCE_RESPONSE_INVALID",
                "平台资源服务返回了无效列表",
                status_code=502,
            )
        plugin_id, component_id = _PLATFORM_RESOURCE_PLUGINS[kind]
        bundle_root = (
            Path(__file__).parents[1]
            / "plugins"
            / "providers"
            / "bundles"
            / plugin_id.split(".", 1)[1]
        )
        connections = await asyncio.to_thread(studio.resource_connections.list)
        connection_ref = connections[0].target.connection_ref if connections else None
        return {
            "kind": kind,
            "connectionRef": connection_ref,
            "pluginRef": f"plugin://{plugin_id}@0.1.0",
            "pluginSnapshotDigest": installation_digest(bundle_root),
            "componentId": component_id,
            "items": [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("id") or ""),
                    "region": str(item.get("region") or ""),
                    "status": str(item.get("status") or "unknown"),
                    "disabled": bool(item.get("disabled", False)),
                    "disableReason": str(item.get("disable_reason") or ""),
                    **(
                        {"sceneId": str(item.get("scene_id") or "")}
                        if item.get("scene_id") is not None
                        else {}
                    ),
                }
                for item in raw_items
                if item.get("id")
            ],
            "total": int(payload.get("total_count") or len(raw_items)),
        }

    @app.post("/api/v1/resource-connections:bootstrap", status_code=201)
    async def bootstrap_resource_connection():
        authority = studio.resource_authority
        access_key = os.environ.get("KSYUN_ACCESS_KEY", "").strip()
        secret_key = os.environ.get("KSYUN_SECRET_KEY", "").strip()
        if authority is None or not access_key or not secret_key:
            raise StudioError(
                "RESOURCE_CONNECTION_BOOTSTRAP_UNAVAILABLE",
                "请先在设置的云端连接中填写账号凭据和控制面地址",
                status_code=503,
            )
        tenant_ref, principal_ref = await asyncio.to_thread(
            authority.resolve_signed_identity, access_key, secret_key
        )
        connection_ref = "ksyun-platform-default"
        try:
            existing = await asyncio.to_thread(studio.resource_connections.get, connection_ref)
        except StudioError as error:
            if error.status_code != 404:
                raise
        else:
            return {
                **existing.model_dump(by_alias=True, mode="json"),
                "validationState": "verified",
            }
        declaration = ResourceConnectionDeclaration(
            label="金山云平台资源",
            target=ConnectionTarget(
                connection_ref=connection_ref,
                tenant_ref=tenant_ref,
                principal_ref=principal_ref,
                endpoint=authority.policy.allowed_data_endpoints[0],
                auth_mode="signed",
            ),
            credentials=ResourceCredentialReferences(
                access_key_ref="env://KSYUN_ACCESS_KEY",
                secret_key_ref="env://KSYUN_SECRET_KEY",
            ),
        )
        record = await asyncio.to_thread(
            studio.resource_connections.save, declaration, expected_revision=0
        )
        return {
            **record.model_dump(by_alias=True, mode="json"),
            "validationState": "verified",
        }

    @app.get("/api/v1/agents/{agent_id}/resource-bindings/status")
    async def binding_status(
        agent_id: str,
        activation_id: str = Query(alias="activationId", min_length=1, max_length=256),
    ):
        return await studio.resource_binding_status(agent_id, activation_id=activation_id)

    @app.post("/api/v1/agents/{agent_id}/resource-bindings/validate")
    async def validate_bindings(agent_id: str, request: ValidateResourceBindings):
        return await asyncio.to_thread(
            studio.validate_resource_bindings,
            agent_id,
            expected_revision=request.expected_revision,
        )

    @app.get("/api/v1/resource-connections")
    async def list_connections():
        records = await asyncio.to_thread(studio.resource_connections.list)
        return {
            "items": [
                {**record.model_dump(by_alias=True, mode="json"), "validationState": "unverified"}
                for record in records
            ]
        }

    @app.put("/api/v1/resource-connections/{connection_ref:path}")
    async def save_connection(connection_ref: str, request: SaveResourceConnection):
        if request.connection.target.connection_ref != connection_ref:
            raise StudioError(
                "RESOURCE_CONNECTION_REFERENCE_MISMATCH",
                "路径与连接引用不一致",
                status_code=422,
            )
        record = await asyncio.to_thread(
            studio.resource_connections.save,
            request.connection,
            expected_revision=request.expected_revision,
        )
        return {**record.model_dump(by_alias=True, mode="json"), "validationState": "unverified"}
