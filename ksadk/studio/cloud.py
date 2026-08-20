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

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord: ...


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

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord:
        return deployment


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


class AgentEngineCloudDeploymentGateway:
    """Studio adapter for admitted Bundles and the existing Agent create Action.

    The only instance lifecycle call here is ``CreateAgentProduct``.  The
    preliminary Artifact request exists solely to give Server an untrusted ZIP
    stream it can re-hash, validate and place under a controlled KS3 key.  It
    must never become a parallel deployment API.
    """

    _ACTION_PREFIX = "/agentengine/api/v1"

    def __init__(
        self,
        *,
        base_url: str,
        control_plane_token: str,
        account_id: str,
        region: str,
        runtime_profile_id: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.control_plane_token = control_plane_token.strip()
        self.account_id = account_id.strip()
        self.region = region.strip()
        self.runtime_profile_id = runtime_profile_id.strip()
        self.transport = transport
        self._admissions: dict[str, dict[str, str]] = {}
        if not all((self.base_url, self.control_plane_token, self.account_id, self.region)):
            raise ValueError("AgentEngine cloud gateway requires URL, token, account and region")

    async def upload_bundle(self, **kwargs) -> str:
        bundle = kwargs["bundle"]
        provenance = dict(kwargs["provenance"])
        bundle_digest = str(kwargs["bundle_digest"])
        archive_sha256 = f"sha256:{hashlib.sha256(bundle).hexdigest()}"
        runtime_type = str(provenance.get("runtimeType") or "").strip()
        agent_id = str(provenance.get("agentId") or "").strip()
        source_revision = str(provenance.get("sourceRevision") or "").strip()
        if not agent_id or not source_revision:
            raise StudioError(
                "CLOUD_BUNDLE_METADATA_INVALID",
                "本地 Bundle 缺少 agentId 或 sourceRevision，不能上云",
                status_code=422,
            )
        profile_id = await self._runtime_profile_id(runtime_type)
        payload = await self._post_multipart(
            f"{self._ACTION_PREFIX}/CreateAgentArtifact",
            data={
                "AgentSourceId": agent_id,
                "SourceRevision": source_revision,
                "Region": self.region,
                "ClientToken": f"studio-admit-{archive_sha256.removeprefix('sha256:')}",
                "SourceArchiveSha256": archive_sha256,
                "BundleDigest": bundle_digest,
                "RuntimeProfileId": profile_id,
            },
            files={"SourceArchive": ("agent-bundle.zip", bundle, "application/zip")},
        )
        artifact_id = str(payload.get("AgentArtifactId") or "").strip()
        runtime_family = str(payload.get("RuntimeFamily") or runtime_type).strip()
        if not artifact_id:
            raise StudioError(
                "CLOUD_ADMISSION_PROTOCOL_INVALID",
                "云端 Admission 未返回 AgentArtifactId",
                status_code=502,
            )
        self._admissions[artifact_id] = {
            "agentId": agent_id,
            "runtimeFamily": runtime_family,
            "bundleDigest": bundle_digest,
        }
        return f"agentartifact://{artifact_id}"

    async def create_version(self, **kwargs) -> str:
        uri = str(kwargs["bundle_uri"])
        if not uri.startswith("agentartifact://"):
            raise StudioError(
                "CLOUD_ADMISSION_PROTOCOL_INVALID",
                "Studio 未获得受控 AgentArtifact 引用",
                status_code=502,
            )
        artifact_id = uri.removeprefix("agentartifact://")
        admission = self._admissions.get(artifact_id)
        if admission is None or admission["agentId"] != kwargs["agent_id"]:
            raise StudioError(
                "CLOUD_ADMISSION_PROTOCOL_INVALID",
                "AgentArtifact 与当前 Agent 不匹配",
                status_code=502,
            )
        if admission["bundleDigest"] != kwargs["bundle_digest"]:
            raise StudioError(
                "CLOUD_ADMISSION_PROTOCOL_INVALID",
                "AgentArtifact 与本地 Bundle digest 不匹配",
                status_code=502,
            )
        return artifact_id

    async def create_deployment(self, **kwargs) -> DeploymentRecord:
        artifact_id = str(kwargs["version_id"])
        admission = self._admissions.get(artifact_id)
        if admission is None:
            raise StudioError("CLOUD_ADMISSION_PROTOCOL_INVALID", "未知 AgentArtifact", status_code=502)
        framework = admission["runtimeFamily"].lower()
        if framework not in {"adk", "langgraph"}:
            raise StudioError(
                "CLOUD_RUNTIME_UNSUPPORTED",
                f"云端尚不支持 runtime={framework or '(empty)'} 的 Bundle 部署",
                status_code=422,
            )
        request: DeploymentRequest = kwargs["request"]
        payload = await self._post_json(
            f"{self._ACTION_PREFIX}/CreateAgentProduct",
            {
                "Name": _server_agent_name(admission["agentId"]),
                "Description": "Created by AgentKit Studio",
                "Framework": framework,
                "Region": request.target.region,
                "DeploymentType": "Code",
                "AgentArtifactId": artifact_id,
                "AutoPay": True,
            },
        )
        agent_id = str(payload.get("AgentId") or "").strip()
        instance_id = str(payload.get("InstanceId") or "").strip()
        if not agent_id or not instance_id:
            raise StudioError(
                "CLOUD_DEPLOYMENT_PROTOCOL_INVALID",
                "云端创建订单未返回 AgentId / InstanceId",
                status_code=502,
            )
        return DeploymentRecord(
            id=f"dep_{instance_id}",
            build_id=kwargs["build_id"],
            bundle_digest=kwargs["bundle_digest"],
            version_id=agent_id,
            status="DEPLOYING",
            target=request.target,
            agent_id=agent_id,
            instance_id=instance_id,
            artifact_id=artifact_id,
        )

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord:
        if not deployment.instance_id:
            return deployment
        payload = await self._post_json(
            f"{self._ACTION_PREFIX}/FetchAgentInstanceStatus",
            {"InstanceId": deployment.instance_id},
        )
        status = str(payload.get("Status") or "").upper()
        projected = {
            "RUNNING": "READY",
            "FAILED": "FAILED",
            "TERMINATED": "FAILED",
            "STARTING": "DEPLOYING",
            "CREATING": "DEPLOYING",
            "UPDATING": "DEPLOYING",
            "SCALING": "DEPLOYING",
        }.get(status, "DEPLOYING")
        return deployment.model_copy(update={"status": projected})

    async def _runtime_profile_id(self, runtime_type: str) -> str:
        if self.runtime_profile_id:
            return self.runtime_profile_id
        payload = await self._post_json(f"{self._ACTION_PREFIX}/ListAgentRuntimeProfiles", {})
        profiles = payload.get("RuntimeProfiles")
        matches = [
            item for item in profiles if isinstance(item, dict)
            and str(item.get("RuntimeFamily") or "").strip().lower() == runtime_type.lower()
        ] if isinstance(profiles, list) else []
        if len(matches) != 1:
            raise StudioError(
                "CLOUD_RUNTIME_PROFILE_UNAVAILABLE",
                "云端没有唯一匹配的不可变 RuntimeProfile",
                status_code=503,
                details={"runtimeType": runtime_type, "matches": len(matches)},
            )
        profile_id = str(matches[0].get("RuntimeProfileId") or "").strip()
        if not profile_id:
            raise StudioError("CLOUD_PROFILE_PROTOCOL_INVALID", "云端 RuntimeProfile 返回无效", status_code=502)
        return profile_id

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(path, headers=self._headers(), json=payload)
        return self._action_payload(response)

    async def _post_multipart(
        self, path: str, *, data: dict[str, str], files: dict[str, tuple[str, bytes, str]]
    ) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(path, headers=self._headers(), data=data, files=files)
        return self._action_payload(response)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, follow_redirects=False, timeout=60, transport=self.transport
        )

    def _headers(self) -> dict[str, str]:
        # The public edge validates this short-lived user credential then strips
        # it and injects X-Auth-*; Gateway itself never trusts client identity
        # headers. Account/region stay client-side correlation only.
        return {
            "Authorization": f"Bearer {self.control_plane_token}",
            "X-Ksc-Account-Id": self.account_id,
            "X-Ksc-Region": self.region,
        }

    @staticmethod
    def _action_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise StudioError("CLOUD_CONTROL_PLANE_PROTOCOL_INVALID", "云端返回非 JSON 响应", status_code=502) from exc
        if response.status_code >= 400 or not isinstance(body, dict) or int(body.get("Code") or 0) != 0:
            raise StudioError(
                "CLOUD_CONTROL_PLANE_UNAVAILABLE" if response.status_code >= 500 else "CLOUD_ADMISSION_REJECTED",
                "云端拒绝 Bundle 准入或 Agent 创建请求",
                status_code=503 if response.status_code >= 500 else 422,
                details={"upstreamStatus": response.status_code},
            )
        data = body.get("Data")
        if not isinstance(data, dict):
            raise StudioError("CLOUD_CONTROL_PLANE_PROTOCOL_INVALID", "云端 Action 未返回 Data 对象", status_code=502)
        return data


def _server_agent_name(agent_id: str) -> str:
    normalized = "".join(char if char.isalnum() or char == "-" else "-" for char in agent_id.lower())
    normalized = normalized.strip("-") or "agent"
    if not normalized[0].isalpha():
        normalized = f"agent-{normalized}"
    return f"studio-{normalized}"[:63].rstrip("-")


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
        # The Server owns the profile-to-image mapping, but it must select the
        # profile for the concrete framework the deterministic build produced.
        provenance["runtimeType"] = str(
            manifest.get("runtimeType") or build.runtime_type
        )
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

    async def refresh(self, deployment_id: str) -> DeploymentRecord:
        """Refresh only from the Server-owned instance status projection."""

        deployment = self.get(deployment_id)
        status_reader = getattr(self.gateway, "get_deployment_status", None)
        if status_reader is None:
            return deployment
        refreshed = await status_reader(deployment)
        path = self.workspace.resolve(
            Path(".agentkit/deployments") / f"{deployment_id}.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._save(
            refreshed,
            DeploymentRequest.model_validate(payload["request"]),
        )
        return refreshed

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
