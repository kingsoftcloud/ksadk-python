"""Cloud gateway contract and isolated in-memory/unavailable adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from ksadk.studio.contracts import DeploymentRecord, DeploymentRequest
from ksadk.studio.errors import StudioError


@dataclass(frozen=True)
class AccountCloudAgentReference:
    agent_id: str


class CloudDeploymentGateway(Protocol):
    async def get_a2a_space(self, space_id: str) -> dict[str, Any]: ...

    async def list_a2a_spaces(self, *, page: int, size: int) -> dict[str, Any]: ...

    async def list_a2a_space_available_agents(self, space_id: str) -> dict[str, Any]: ...

    async def get_a2a_agent(self, agent_id: str) -> dict[str, Any]: ...

    async def list_a2a_agent_versions(
        self, agent_id: str, *, page: int, size: int
    ) -> dict[str, Any]: ...

    async def upload_bundle(
        self,
        *,
        bundle: bytes,
        bundle_digest: str,
        provenance: dict[str, Any],
    ) -> str: ...

    async def create_version(
        self,
        *,
        agent_id: str,
        bundle_uri: str,
        bundle_digest: str,
        provenance: dict[str, Any],
    ) -> str: ...

    async def create_deployment(
        self,
        *,
        build_id: str,
        version_id: str,
        bundle_digest: str,
        request: DeploymentRequest,
    ) -> DeploymentRecord: ...

    async def replace_deployment(
        self,
        deployment: DeploymentRecord,
        *,
        build_id: str,
        version_id: str,
        bundle_digest: str,
        request: DeploymentRequest,
    ) -> DeploymentRecord: ...

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord: ...

    async def get_deployment_dashboard_access(
        self, deployment: DeploymentRecord
    ) -> dict[str, str | None]: ...

    async def delete_deployment(self, deployment: DeploymentRecord) -> bool: ...

    async def list_account_agents(self, *, page: int, size: int) -> dict[str, Any]: ...

    async def get_account_agent(self, agent_id: str) -> dict[str, Any]: ...

    async def list_account_agent_versions(
        self, agent_id: str, *, page: int, size: int
    ) -> dict[str, Any]: ...

    async def rollback_account_agent_version(
        self, agent_id: str, *, version_id: str
    ) -> dict[str, Any]: ...

    async def get_account_agent_dashboard_access(self, agent_id: str) -> dict[str, str | None]: ...

    async def delete_account_agent(self, agent_id: str) -> bool: ...

    async def create_managed_runtime_deployment(
        self,
        *,
        build_id: str,
        agent_name: str,
        manifest: str,
        runtime_name: str,
        runtime_version: str,
        manifest_digest: str,
        request: DeploymentRequest,
    ) -> DeploymentRecord: ...

    async def replace_managed_runtime_deployment(
        self,
        deployment: DeploymentRecord,
        *,
        build_id: str,
        manifest: str,
        runtime_name: str,
        runtime_version: str,
        manifest_digest: str,
        request: DeploymentRequest,
    ) -> DeploymentRecord: ...


class UnavailableCloudGateway:
    @staticmethod
    def _a2a_unavailable() -> StudioError:
        return StudioError(
            "A2A_CATALOG_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能读取 A2A 目录",
            status_code=501,
        )

    async def get_a2a_space(self, space_id: str) -> dict[str, Any]:
        raise self._a2a_unavailable()

    async def list_a2a_spaces(self, *, page: int, size: int) -> dict[str, Any]:
        raise self._a2a_unavailable()

    async def list_a2a_space_available_agents(self, space_id: str) -> dict[str, Any]:
        raise self._a2a_unavailable()

    async def get_a2a_agent(self, agent_id: str) -> dict[str, Any]:
        raise self._a2a_unavailable()

    async def list_a2a_agent_versions(
        self, agent_id: str, *, page: int, size: int
    ) -> dict[str, Any]:
        raise self._a2a_unavailable()

    async def upload_bundle(self, **_kwargs) -> str:
        raise StudioError(
            "CLOUD_BUNDLE_DEPLOYMENT_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能上传 Bundle",
            status_code=501,
        )

    async def create_version(self, **_kwargs) -> str:
        raise AssertionError("upload_bundle must fail first")

    async def create_deployment(self, **_kwargs) -> DeploymentRecord:
        raise AssertionError("upload_bundle must fail first")

    async def replace_deployment(
        self,
        _deployment: DeploymentRecord,
        **_kwargs,
    ) -> DeploymentRecord:
        raise AssertionError("upload_bundle must fail first")

    async def create_managed_runtime_deployment(self, **_kwargs) -> DeploymentRecord:
        raise StudioError(
            "CLOUD_MANAGED_RUNTIME_DEPLOYMENT_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能部署声明式 Agent",
            status_code=501,
        )

    async def replace_managed_runtime_deployment(
        self, _deployment: DeploymentRecord, **_kwargs
    ) -> DeploymentRecord:
        raise AssertionError("managed runtime deployment must fail first")

    async def get_deployment_dashboard_access(
        self, _deployment: DeploymentRecord
    ) -> dict[str, str | None]:
        raise StudioError(
            "CLOUD_DASHBOARD_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能打开云端 Agent UI",
            status_code=501,
        )

    async def delete_deployment(self, _deployment: DeploymentRecord) -> bool:
        raise StudioError(
            "CLOUD_AGENT_DELETE_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能删除云端 Agent",
            status_code=501,
        )

    async def list_account_agents(self, *, page: int, size: int) -> dict[str, Any]:
        # Local-only Studio remains usable without cloud credentials. Detail,
        # chat and mutation still fail closed when explicitly requested.
        return {
            "items": [],
            "total": 0,
            "page": page,
            "size": size,
            "available": False,
        }

    async def get_account_agent(self, _agent_id: str) -> dict[str, Any]:
        raise StudioError(
            "CLOUD_AGENT_DIRECTORY_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能读取云端 Agent",
            status_code=501,
        )

    async def list_account_agent_versions(
        self, _agent_id: str, *, page: int, size: int
    ) -> dict[str, Any]:
        raise StudioError(
            "CLOUD_AGENT_VERSION_DIRECTORY_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能读取云端版本",
            status_code=501,
        )

    async def rollback_account_agent_version(
        self, _agent_id: str, *, version_id: str
    ) -> dict[str, Any]:
        raise StudioError(
            "CLOUD_AGENT_VERSION_ROLLBACK_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能回滚云端版本",
            status_code=501,
        )

    async def get_account_agent_dashboard_access(self, _agent_id: str) -> dict[str, str | None]:
        raise StudioError(
            "CLOUD_DASHBOARD_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能打开云端 Agent UI",
            status_code=501,
        )

    async def delete_account_agent(self, _agent_id: str) -> bool:
        raise StudioError(
            "CLOUD_AGENT_DELETE_UNAVAILABLE",
            "当前未配置可用的云端签名账号，不能删除云端 Agent",
            status_code=501,
        )


class InMemoryCloudGateway:
    """Contract-test gateway; it records exactly what would cross the cloud boundary."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.versions: list[dict[str, Any]] = []
        self.deployments: list[DeploymentRecord] = []
        self.deleted_agent_ids: list[str] = []

    async def upload_bundle(self, **kwargs) -> str:
        self.uploads.append(kwargs)
        return f"memory://artifacts/{kwargs['bundle_digest']}"

    async def create_version(self, **kwargs) -> str:
        self.versions.append(kwargs)
        return f"ver_{uuid4().hex}"

    async def create_deployment(self, **kwargs) -> DeploymentRecord:
        record = DeploymentRecord(
            id=f"dep_{uuid4().hex}",
            build_id=kwargs["build_id"],
            bundle_digest=kwargs["bundle_digest"],
            version_id=kwargs["version_id"],
            status="READY",
            target=kwargs["request"].target,
        )
        self.deployments.append(record)
        return record

    async def replace_deployment(
        self,
        deployment: DeploymentRecord,
        **kwargs,
    ) -> DeploymentRecord:
        record = DeploymentRecord(
            id=f"dep_{uuid4().hex}",
            build_id=kwargs["build_id"],
            bundle_digest=kwargs["bundle_digest"],
            version_id=kwargs["version_id"],
            status="READY",
            target=kwargs["request"].target,
            agent_id=deployment.agent_id,
            instance_id=deployment.instance_id,
            endpoint=deployment.endpoint,
        )
        self.deployments.append(record)
        return record

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord:
        return deployment

    async def get_deployment_dashboard_access(
        self, deployment: DeploymentRecord
    ) -> dict[str, str | None]:
        if not deployment.agent_id:
            raise StudioError(
                "DEPLOYMENT_DASHBOARD_UNAVAILABLE",
                "Deployment receipt 缺少云端 Agent 标识",
                status_code=409,
            )
        return {
            "access_url": f"memory://dashboard/{deployment.agent_id}",
            "agent_id": deployment.agent_id,
            "instance_id": deployment.instance_id,
            "expires_at": None,
        }

    async def delete_deployment(self, deployment: DeploymentRecord) -> bool:
        if not deployment.agent_id:
            raise StudioError(
                "CLOUD_AGENT_DELETE_UNAVAILABLE",
                "Deployment receipt 缺少云端 Agent 标识",
                status_code=409,
            )
        self.deleted_agent_ids.append(deployment.agent_id)
        return True

    async def list_account_agents(self, *, page: int, size: int) -> dict[str, Any]:
        rows = [
            {
                "agentId": item.agent_id,
                "name": item.agent_id,
                "status": item.status,
                "endpoint": item.endpoint,
            }
            for item in self.deployments
            if item.agent_id
        ]
        start = (page - 1) * size
        return {"items": rows[start : start + size], "total": len(rows), "page": page, "size": size}

    async def get_account_agent(self, agent_id: str) -> dict[str, Any]:
        for item in reversed(self.deployments):
            if item.agent_id == agent_id:
                return {
                    "agentId": agent_id,
                    "name": agent_id,
                    "status": item.status,
                    "endpoint": item.endpoint,
                    "versionId": item.version_id,
                }
        raise StudioError("CLOUD_AGENT_NOT_FOUND", "云端 Agent 不存在", status_code=404)

    async def list_account_agent_versions(
        self, agent_id: str, *, page: int, size: int
    ) -> dict[str, Any]:
        detail = await self.get_account_agent(agent_id)
        version_id = str(detail.get("versionId") or "").strip()
        items = []
        if version_id:
            items.append(
                {
                    "versionId": version_id,
                    "versionName": version_id,
                    "tag": "",
                    "status": "current",
                    "trafficPercentage": 100,
                    "canRollback": False,
                    "rollbackDisabledReason": "当前版本不可回滚至自身",
                    "createdAt": None,
                    "createdBy": "",
                }
            )
        return {
            "items": items,
            "total": len(items),
            "currentVersionId": version_id or None,
        }

    async def rollback_account_agent_version(
        self, agent_id: str, *, version_id: str
    ) -> dict[str, Any]:
        await self.get_account_agent(agent_id)
        return {
            "agentId": agent_id,
            "targetVersionId": version_id,
            "status": "UPDATING",
            "noop": False,
        }

    async def get_account_agent_dashboard_access(self, agent_id: str) -> dict[str, str | None]:
        return {
            "access_url": f"memory://dashboard/{agent_id}",
            "agent_id": agent_id,
            "instance_id": None,
            "expires_at": None,
        }

    async def delete_account_agent(self, agent_id: str) -> bool:
        self.deleted_agent_ids.append(agent_id)
        return True

    async def create_managed_runtime_deployment(self, **kwargs) -> DeploymentRecord:
        digest = str(kwargs["manifest_digest"])
        record = DeploymentRecord(
            id=f"dep_{uuid4().hex}",
            build_id=str(kwargs["build_id"]),
            bundle_digest=f"sha256:{digest}",
            version_id=f"managed-{digest[:16]}",
            status="READY",
            target=kwargs["request"].target,
            artifact_id="managed-runtime",
        )
        self.deployments.append(record)
        return record

    async def replace_managed_runtime_deployment(
        self, deployment: DeploymentRecord, **kwargs
    ) -> DeploymentRecord:
        digest = str(kwargs["manifest_digest"])
        record = DeploymentRecord(
            id=f"dep_{uuid4().hex}",
            build_id=str(kwargs["build_id"]),
            bundle_digest=f"sha256:{digest}",
            version_id=f"managed-{digest[:16]}",
            status="READY",
            target=kwargs["request"].target,
            agent_id=deployment.agent_id,
            instance_id=deployment.instance_id,
            endpoint=deployment.endpoint,
            artifact_id="managed-runtime",
        )
        self.deployments.append(record)
        return record
