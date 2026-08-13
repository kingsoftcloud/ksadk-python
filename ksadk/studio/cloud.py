"""Cloud Artifact Admission and Deployment gateway contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

import httpx

from ksadk.studio.contracts import (
    BuildStatus,
    DeploymentRecord,
    DeploymentRequest,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.repository import BuildRepository
from ksadk.studio.workspace import Workspace


class CloudDeploymentGateway(Protocol):
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


class UnavailableCloudGateway:
    async def upload_bundle(self, **_kwargs) -> str:
        raise StudioError(
            "CLOUD_BUNDLE_ADMISSION_UNAVAILABLE",
            "当前未配置支持 AgentBundle Admission 的云端控制面",
            status_code=501,
        )

    async def create_version(self, **_kwargs) -> str:
        raise AssertionError("upload_bundle must fail first")

    async def create_deployment(self, **_kwargs) -> DeploymentRecord:
        raise AssertionError("upload_bundle must fail first")


class InMemoryCloudGateway:
    """Contract-test gateway; it records exactly what would cross the cloud boundary."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.versions: list[dict[str, Any]] = []
        self.deployments: list[DeploymentRecord] = []

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


class HttpCloudDeploymentGateway:
    def __init__(self, *, base_url: str, bearer_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token

    async def upload_bundle(self, **kwargs) -> str:
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        async with httpx.AsyncClient(follow_redirects=False, timeout=60) as client:
            create = await client.post(
                f"{self.base_url}/v1/artifact-uploads",
                headers=headers,
                json={
                    "bundleDigest": kwargs["bundle_digest"],
                    "size": len(kwargs["bundle"]),
                    "provenance": kwargs["provenance"],
                },
            )
            self._raise(create)
            upload = create.json()
            put = await client.put(
                upload["uploadUrl"],
                content=kwargs["bundle"],
                headers=upload.get("headers") or {},
            )
            self._raise(put)
            return str(upload["artifactUri"])

    async def create_version(self, **kwargs) -> str:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/v1/agents/{kwargs['agent_id']}/versions",
                headers={"Authorization": f"Bearer {self.bearer_token}"},
                json={
                    "bundle": {
                        "uri": kwargs["bundle_uri"],
                        "digest": kwargs["bundle_digest"],
                    },
                    "provenance": kwargs["provenance"],
                },
            )
        self._raise(response)
        return str(response.json()["versionId"])

    async def create_deployment(self, **kwargs) -> DeploymentRecord:
        request: DeploymentRequest = kwargs["request"]
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/v1/deployments",
                headers={"Authorization": f"Bearer {self.bearer_token}"},
                json={
                    "versionId": kwargs["version_id"],
                    "bundleDigest": kwargs["bundle_digest"],
                    **request.model_dump(by_alias=True, mode="json"),
                },
            )
        self._raise(response)
        return cast(DeploymentRecord, DeploymentRecord.model_validate(response.json()))

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        raise StudioError(
            "CLOUD_ADMISSION_REJECTED",
            "云端拒绝 AgentBundle 或 Deployment",
            status_code=422,
            details={"upstreamStatus": response.status_code},
        )


class CloudDeploymentService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        gateway: CloudDeploymentGateway,
        build_repository: BuildRepository | None = None,
    ) -> None:
        self.workspace = workspace
        self.gateway = gateway
        self.build_repository = build_repository or BuildRepository(workspace)

    async def deploy(
        self,
        build_id: str,
        request: DeploymentRequest,
    ) -> DeploymentRecord:
        build = self.build_repository.get(build_id)
        if build.status != BuildStatus.SUCCEEDED or not build.artifact_path:
            raise StudioError(
                "BUILD_NOT_READY",
                "只有成功 Build 可以部署",
                status_code=409,
            )
        archive = self.workspace.resolve(build.artifact_path, must_exist=True)
        manifest_path = archive.parent / "agent-bundle" / "manifest.json"
        provenance_path = archive.parent / "agent-bundle" / "provenance.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("bundleDigest") != build.bundle_digest:
            raise StudioError(
                "BUILD_DIGEST_MISMATCH",
                "上传前 Bundle digest 校验失败",
                status_code=422,
            )
        bundle = archive.read_bytes()
        archive_sha256 = f"sha256:{hashlib.sha256(bundle).hexdigest()}"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["archiveSha256"] = archive_sha256
        bundle_uri = await self.gateway.upload_bundle(
            bundle=bundle,
            bundle_digest=build.bundle_digest,
            provenance=provenance,
        )
        version_id = await self.gateway.create_version(
            agent_id=build.agent_id,
            bundle_uri=bundle_uri,
            bundle_digest=build.bundle_digest,
            provenance=provenance,
        )
        record = await self.gateway.create_deployment(
            build_id=build_id,
            version_id=version_id,
            bundle_digest=build.bundle_digest,
            request=request,
        )
        self._save(record, request)
        return record

    def get(self, deployment_id: str) -> DeploymentRecord:
        path = self.workspace.resolve(
            Path(".agentkit/deployments") / f"{deployment_id}.json"
        )
        if not path.is_file():
            raise StudioError(
                "DEPLOYMENT_NOT_FOUND",
                "Deployment 不存在",
                status_code=404,
                details={"id": deployment_id},
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cast(
            DeploymentRecord,
            DeploymentRecord.model_validate(payload["record"]),
        )

    async def rollback(
        self,
        deployment_id: str,
        *,
        target_build_id: str,
    ) -> DeploymentRecord:
        path = self.workspace.resolve(
            Path(".agentkit/deployments") / f"{deployment_id}.json"
        )
        if not path.is_file():
            self.get(deployment_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        request = DeploymentRequest.model_validate(payload["request"])
        return await self.deploy(target_build_id, request)

    def _save(
        self,
        record: DeploymentRecord,
        request: DeploymentRequest,
    ) -> None:
        directory = self.workspace.resolve(".agentkit/deployments")
        directory.mkdir(parents=True, exist_ok=True)
        self.workspace.atomic_write_text(
            directory / f"{record.id}.json",
            json.dumps(
                {
                    "record": record.model_dump(
                        by_alias=True, exclude_none=True, mode="json"
                    ),
                    "request": request.model_dump(
                        by_alias=True, exclude_none=True, mode="json"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
