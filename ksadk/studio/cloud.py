"""Studio Bundle upload and existing Agent lifecycle gateway contracts."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, cast
from uuid import uuid4

from pydantic import ValidationError

from ksadk.api import AgentEngineAPIError, AgentEngineClient
from ksadk.builders.ks3_uploader import KS3Uploader
from ksadk.studio.contracts import (
    BuildRecord,
    BuildStatus,
    DeploymentRecord,
    DeploymentRequest,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.hosted_kernel import preflight_hosted_kernel_bundle
from ksadk.studio.repository import BuildRepository
from ksadk.studio.workspace import Workspace

logger = logging.getLogger(__name__)

_STUDIO_CODE_COMMAND = (
    "ksadk",
    "web",
    "/app/code/runtime",
    "--port",
    "8080",
    "--host",
    "0.0.0.0",
    "--no-open",
)
# A Dashboard access link is both the user/session credential and the Agent
# binding.  Keep the hosted surface in that same link instead of opening the
# Agent image's legacy `/chat` static bundle after authentication.
_HOSTED_AGENT_UI_PATH = "/hosted-ui/chat"
_NATIVE_DASHBOARD_UI_PATH = "/"
_NATIVE_DASHBOARD_FRAMEWORKS = frozenset({"hermes", "openclaw"})


@dataclass(frozen=True)
class AccountCloudAgentReference:
    agent_id: str


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

    async def get_account_agent_dashboard_access(
        self, agent_id: str
    ) -> dict[str, str | None]: ...

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

    async def get_account_agent_dashboard_access(
        self, _agent_id: str
    ) -> dict[str, str | None]:
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

    async def get_account_agent_dashboard_access(
        self, agent_id: str
    ) -> dict[str, str | None]:
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


class DirectAgentEngineCloudDeploymentGateway:
    """Deploy a Studio Bundle with the established KS3 and signed Agent APIs.

    A local Studio has the user's existing AK/SK and therefore uses the same
    two-step path as ``agentengine build --push`` then ``agentengine deploy``:
    upload an immutable ZIP to KS3, then call ``CreateAgent`` (or
    ``UpdateAgent`` for rollback) through :class:`AgentEngineClient`.  It does
    not introduce an Artifact Action, a browser-provided trusted header, or a
    second account-control authentication scheme.
    """

    requires_hosted_kernel_bundle_preflight = True

    def __init__(
        self,
        *,
        region: str,
        client: Any | None = None,
        stream_client: Any | None = None,
        uploader_factory: Callable[..., Any] = KS3Uploader,
        bucket: str | None = None,
        ks3_credentials: dict[str, str] | None = None,
    ) -> None:
        self.region = region.strip()
        self.ks3_region = "cn-beijing-6" if self.region.lower() == "pre-online" else self.region
        self.uploader_factory = uploader_factory
        self.bucket = bucket or os.environ.get("KS3_BUCKET", "").strip() or None
        supplied_credentials = ks3_credentials or {}
        self._ks3_credentials = {
            "access_key": str(
                supplied_credentials.get("access_key")
                or os.environ.get("KSYUN_ACCESS_KEY")
                or os.environ.get("KS3_ACCESS_KEY")
                or ""
            ).strip(),
            "secret_key": str(
                supplied_credentials.get("secret_key")
                or os.environ.get("KSYUN_SECRET_KEY")
                or os.environ.get("KS3_SECRET_KEY")
                or ""
            ).strip(),
        }
        if not all(self._ks3_credentials.values()):
            raise ValueError("Studio cloud gateway requires process-only KS3 credentials")
        # The same process-only AK/SK signs AgentEngine control-plane actions.
        # Never let Studio silently fall through to an unsigned client just
        # because an internal development ingress happens to be reachable.
        self.client = client or AgentEngineClient(
            region=self.region,
            access_key=self._ks3_credentials["access_key"],
            secret_key=self._ks3_credentials["secret_key"],
        )
        # Streaming can use a dedicated Server ingress while ordinary control
        # actions continue through KOP.  Some KOP deployments buffer the
        # complete response body even when RunAgent returns SSE; both clients
        # still use the same process-only V4 credentials and Server admission.
        self.stream_client = stream_client or self.client
        self._bundles: dict[str, dict[str, str]] = {}

    async def upload_bundle(self, **kwargs) -> str:
        bundle = bytes(kwargs["bundle"])
        provenance = dict(kwargs["provenance"])
        bundle_digest = str(kwargs["bundle_digest"])
        agent_id = str(provenance.get("agentId") or "").strip()
        runtime_type = str(provenance.get("runtimeType") or "").strip().lower()
        if not agent_id or not runtime_type:
            raise StudioError(
                "CLOUD_BUNDLE_METADATA_INVALID",
                "本地 Bundle 缺少 agentId 或 runtimeType，不能上云",
                status_code=422,
            )
        archive_sha = hashlib.sha256(bundle).hexdigest()
        if not bundle_digest.startswith("sha256:"):
            raise StudioError(
                "CLOUD_BUNDLE_METADATA_INVALID",
                "本地 Bundle 缺少 sha256 digest，不能上云",
                status_code=422,
            )
        object_key = f"studio-bundles/{_safe_object_component(agent_id)}/{archive_sha}/bundle.zip"
        uploader = self.uploader_factory(region=self.ks3_region, bucket=self.bucket)
        local_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix="agentkit-studio-", suffix=".zip", delete=False
            ) as output:
                output.write(bundle)
                local_path = Path(output.name)
            bundle_uri = await uploader.upload(local_path, object_key)
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)
        if not bundle_uri:
            raise StudioError(
                "CLOUD_BUNDLE_UPLOAD_FAILED",
                "Bundle 上传到 KS3 失败，未创建云端 Agent",
                status_code=502,
            )
        uri = str(bundle_uri)
        self._bundles[uri] = {
            "agent_id": agent_id,
            "archive_sha": archive_sha,
            "bundle_digest": bundle_digest,
            "runtime_type": runtime_type,
            "bucket": str(getattr(uploader, "bucket_name", self.bucket or "")),
        }
        return uri

    async def create_version(self, **kwargs) -> str:
        bundle_uri = str(kwargs["bundle_uri"])
        bundle = self._bundles.get(bundle_uri)
        if bundle is None:
            raise StudioError(
                "CLOUD_BUNDLE_REFERENCE_INVALID",
                "未知的 KS3 Bundle 引用",
                status_code=502,
            )
        if bundle["agent_id"] != str(kwargs["agent_id"]):
            raise StudioError(
                "CLOUD_BUNDLE_REFERENCE_INVALID",
                "Bundle 与当前 Agent 不匹配",
                status_code=502,
            )
        if bundle["bundle_digest"] != str(kwargs["bundle_digest"]):
            raise StudioError(
                "CLOUD_BUNDLE_REFERENCE_INVALID",
                "Bundle digest 不匹配",
                status_code=502,
            )
        return bundle_uri

    async def create_deployment(self, **kwargs) -> DeploymentRecord:
        bundle_uri = str(kwargs["version_id"])
        bundle = self._bundle_for_deployment(bundle_uri, kwargs["bundle_digest"])
        request: DeploymentRequest = kwargs["request"]
        result = await self.client.create_agent(
            self._create_payload(bundle_uri=bundle_uri, bundle=bundle, request=request)
        )
        agent_id = str(result.get("agent_id") or "").strip()
        instance_id = str(result.get("instance_id") or "").strip() or None
        if not agent_id:
            raise StudioError(
                "CLOUD_DEPLOYMENT_PROTOCOL_INVALID",
                "CreateAgentProduct 未返回 AgentId",
                status_code=502,
            )
        return self._receipt(
            build_id=str(kwargs["build_id"]),
            bundle_digest=str(kwargs["bundle_digest"]),
            bundle_uri=bundle_uri,
            agent_id=agent_id,
            instance_id=instance_id,
            endpoint=str(result.get("endpoint") or "").strip() or None,
            target=request.target,
            status="DEPLOYING",
        )

    async def replace_deployment(
        self,
        deployment: DeploymentRecord,
        **kwargs,
    ) -> DeploymentRecord:
        if not deployment.agent_id:
            raise StudioError(
                "DEPLOYMENT_PROTOCOL_INVALID",
                "部署 receipt 缺少 AgentId",
                status_code=502,
            )
        bundle_uri = str(kwargs["version_id"])
        bundle = self._bundle_for_deployment(bundle_uri, kwargs["bundle_digest"])
        request: DeploymentRequest = kwargs["request"]
        await self.client.update_agent(
            deployment.agent_id,
            {
                "artifact_type": "Code",
                "artifact_path": bundle_uri,
                "code_checksum": bundle["archive_sha"],
                "code_command": list(_STUDIO_CODE_COMMAND),
                "ks3": self._code_config(bundle["bucket"]),
            },
        )
        return self._receipt(
            build_id=str(kwargs["build_id"]),
            bundle_digest=str(kwargs["bundle_digest"]),
            bundle_uri=bundle_uri,
            agent_id=deployment.agent_id,
            instance_id=deployment.instance_id,
            endpoint=deployment.endpoint,
            target=request.target,
            status="DEPLOYING",
        )

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord:
        if not deployment.agent_id:
            return deployment
        try:
            payload = await self.client.get_agent(agent_id=deployment.agent_id)
        except AgentEngineAPIError as exc:
            # A receipt can outlive the cloud Agent it originally created.  Do
            # not leave that receipt in DEPLOYING forever: the Server's 404 is
            # an authoritative terminal fact, not a transient readiness gap.
            if exc.code == 404 or exc.details.get("http_status") == 404:
                return deployment.model_copy(update={"status": "FAILED"})
            raise
        deployment_detail = payload.get("deployment") or {}
        kernel_ready = bool(
            payload.get("agent_kernel_ready") or deployment_detail.get("agent_kernel_ready")
        )
        status = (
            str(
                payload.get("status")
                or (payload.get("basic") or {}).get("status")
                or (payload.get("deployment") or {}).get("status")
                or ""
            )
            .strip()
            .upper()
        )
        projected = (
            "FAILED"
            if status in {"FAILED", "TERMINATED", "ERROR"}
            else "READY"
            if kernel_ready
            or (
                not deployment.requires_kernel
                and deployment.artifact_id == "managed-runtime"
                and status in {"RUNNING", "READY"}
            )
            else "DEPLOYING"
        )
        endpoint = (
            str(
                payload.get("endpoint")
                or (payload.get("basic") or {}).get("endpoint")
                or deployment_detail.get("endpoint")
                or deployment.endpoint
                or ""
            ).strip()
            or None
        )
        return deployment.model_copy(update={"status": projected, "endpoint": endpoint})

    async def get_deployment_dashboard_access(
        self, deployment: DeploymentRecord
    ) -> dict[str, str | None]:
        if not deployment.agent_id:
            raise StudioError(
                "DEPLOYMENT_DASHBOARD_UNAVAILABLE",
                "Deployment receipt 缺少云端 Agent 标识",
                status_code=409,
            )
        link = await self.client.create_dashboard_access_link(
            agent_id=deployment.agent_id,
            link_type="private",
            path=_HOSTED_AGENT_UI_PATH,
        )
        access_url = str(link.get("access_url") or "").strip()
        if not access_url:
            raise StudioError(
                "DEPLOYMENT_DASHBOARD_UNAVAILABLE",
                "云端未返回可用的 Agent UI 地址",
                status_code=502,
            )
        return {
            "access_url": access_url,
            "agent_id": deployment.agent_id,
            "instance_id": deployment.instance_id,
            "expires_at": str(link.get("expires_at") or "").strip() or None,
        }

    async def delete_deployment(self, deployment: DeploymentRecord) -> bool:
        agent_id = str(deployment.agent_id or "").strip()
        if not agent_id:
            raise StudioError(
                "CLOUD_AGENT_DELETE_UNAVAILABLE",
                "Deployment receipt 缺少云端 Agent 标识",
                status_code=409,
            )
        try:
            deleted = await self.client.delete_agent(agent_id)
        except AgentEngineAPIError as exc:
            if exc.code == 404 or exc.details.get("http_status") == 404:
                return True
            raise
        if not deleted:
            raise StudioError(
                "CLOUD_AGENT_DELETE_FAILED",
                "云端未确认 Agent 删除结果",
                status_code=502,
            )
        return True

    @staticmethod
    def _session_event_chat_declared(capabilities: Any) -> bool:
        if not isinstance(capabilities, dict):
            return False
        declaration = None
        for name in ("session_event_chat", "sessionEventChat", "SessionEventChat"):
            if name in capabilities:
                declaration = capabilities[name]
                break
        if isinstance(declaration, bool):
            return declaration
        if not isinstance(declaration, dict):
            return False
        for name in ("enabled", "Enabled", "supported", "Supported"):
            if name in declaration:
                return declaration[name] is True
        return False

    @classmethod
    def _cloud_chat_route(
        cls, *, runtime_type: str, capabilities: Any
    ) -> tuple[str, str]:
        if cls._session_event_chat_declared(capabilities):
            return (
                "studio-session-events",
                "declared-session-event-chat-capability",
            )
        if runtime_type in _NATIVE_DASHBOARD_FRAMEWORKS:
            return (
                "official-dashboard",
                "native-runtime-without-session-event-chat-capability",
            )
        return "studio-session-events", "studio-compatible-framework"

    @staticmethod
    def _account_agent_view(payload: dict[str, Any], *, fallback_id: str = "") -> dict[str, Any]:
        basic = payload.get("basic") if isinstance(payload.get("basic"), dict) else {}
        quick_access = (
            payload.get("quick_access")
            if isinstance(payload.get("quick_access"), dict)
            else payload.get("quickAccess")
            if isinstance(payload.get("quickAccess"), dict)
            else {}
        )
        deployment = (
            payload.get("deployment")
            if isinstance(payload.get("deployment"), dict)
            else {}
        )
        runtime_config = (
            deployment.get("runtime_config")
            if isinstance(deployment.get("runtime_config"), dict)
            else deployment.get("runtimeConfig")
            if isinstance(deployment.get("runtimeConfig"), dict)
            else {}
        )

        def first(*names: str) -> Any:
            for source in (payload, basic, deployment):
                for name in names:
                    value = source.get(name)
                    if value is not None and str(value).strip():
                        return value
            return None

        # GetAgent retains a few compatibility fields at the response root.
        # They can lag behind ``basic`` while Runtime-Service is reconciling;
        # presenting that stale root ``CREATING`` value in Studio hid a
        # genuinely RUNNING Agent.  Basic is the Server's lifecycle read model
        # (and is also what the CLI renders), so it is authoritative here.
        def lifecycle_first(*names: str) -> Any:
            for source in (basic, deployment, payload):
                for name in names:
                    value = source.get(name)
                    if value is not None and str(value).strip():
                        return value
            return None

        agent_id = str(
            first("agent_id", "agentId", "agent_runtime_id", "agentRuntimeId", "id")
            or fallback_id
        ).strip()
        runtime_type = str(
            first(
                "framework",
                "runtime_kind",
                "runtimeKind",
                "runtime_type",
                "runtimeType",
                "runtime_name",
                "runtimeName",
            )
            or ""
        ).strip().lower()
        capabilities = first("capabilities", "Capabilities")
        chat_transport, chat_routing_reason = (
            DirectAgentEngineCloudDeploymentGateway._cloud_chat_route(
                runtime_type=runtime_type,
                capabilities=capabilities,
            )
        )
        version_id = str(first("version_id", "versionId", "revision") or "").strip()
        manifest_sha256 = str(
            runtime_config.get("manifest_sha256")
            or runtime_config.get("manifestSha256")
            or ""
        ).strip().lower()
        if not version_id and len(manifest_sha256) == 64:
            try:
                bytes.fromhex(manifest_sha256)
            except ValueError:
                pass
            else:
                version_id = f"managed-{manifest_sha256[:16]}"
        return {
            "agentId": agent_id,
            "name": str(
                first(
                    "name",
                    "agent_name",
                    "agentName",
                    "agent_runtime_name",
                    "agentRuntimeName",
                )
                or agent_id
            ),
            # Server resolves Creator through IAM when an Agent is created.
            # It is the human-readable sub-account name; preserve it for the
            # Studio directory rather than trying to derive a name client-side.
            "creatorName": str(first("creator", "Creator") or "").strip() or None,
            "status": str(lifecycle_first("status", "phase") or "UNKNOWN").upper(),
            "endpoint": str(
                quick_access.get("public_endpoint")
                or quick_access.get("publicEndpoint")
                or quick_access.get("private_endpoint")
                or quick_access.get("privateEndpoint")
                or lifecycle_first("endpoint")
                or ""
            ).strip() or None,
            "framework": runtime_type or None,
            "runtimeType": runtime_type or None,
            "capabilities": capabilities if isinstance(capabilities, dict) else None,
            "chatTransport": chat_transport,
            "chatRoutingReason": chat_routing_reason,
            "region": str(first("region") or "").strip() or None,
            "instanceId": str(first("instance_id", "instanceId") or "").strip() or None,
            "versionId": version_id or None,
            "updatedAt": str(
                lifecycle_first("updated_at", "updatedAt", "update_time", "updateTime") or ""
            ).strip()
            or None,
        }

    async def list_account_agents(self, *, page: int, size: int) -> dict[str, Any]:
        payload = await self.client.list_agents(page=page, page_size=size)
        raw_items = payload.get("agents") or payload.get("Agents") or []
        items = [
            self._account_agent_view(item)
            for item in raw_items
            if isinstance(item, dict)
        ]
        items = [
            item
            for item in items
            if item["agentId"] and item["status"] != "DELETED"
        ]
        return {
            "items": items,
            "total": len(items),
            "page": page,
            "size": size,
        }

    async def get_account_agent(self, agent_id: str) -> dict[str, Any]:
        normalized_id = str(agent_id or "").strip()
        if not normalized_id:
            raise StudioError("CLOUD_AGENT_NOT_FOUND", "云端 Agent 标识不能为空", status_code=404)
        payload = await self.client.get_agent(agent_id=normalized_id)
        return self._account_agent_view(payload, fallback_id=normalized_id)

    @staticmethod
    def _account_agent_version_view(payload: dict[str, Any]) -> dict[str, Any]:
        def first(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in payload and payload[name] is not None:
                    return payload[name]
            return default

        return {
            "versionId": str(first("version_id", "VersionId", default="") or "").strip(),
            "versionName": str(
                first("version_name", "VersionName", default="") or ""
            ).strip(),
            "tag": str(first("tag", "Tag", default="") or "").strip(),
            "status": str(first("status", "Status", default="") or "").strip(),
            "trafficPercentage": int(
                first("traffic_percentage", "TrafficPercentage", default=0) or 0
            ),
            "canRollback": first("can_rollback", "CanRollback", default=False) is True,
            "rollbackDisabledReason": str(
                first(
                    "rollback_disabled_reason",
                    "RollbackDisabledReason",
                    default="",
                )
                or ""
            ).strip(),
            "createdAt": str(first("created_at", "CreatedAt", default="") or "").strip()
            or None,
            "createdBy": str(first("created_by", "CreatedBy", default="") or "").strip(),
        }

    async def list_account_agent_versions(
        self, agent_id: str, *, page: int, size: int
    ) -> dict[str, Any]:
        normalized_id = str(agent_id or "").strip()
        if not normalized_id:
            raise StudioError("CLOUD_AGENT_NOT_FOUND", "云端 Agent 标识不能为空", status_code=404)
        try:
            payload = await self.client.list_versions(normalized_id, page=page, size=size)
        except AgentEngineAPIError as exc:
            raise StudioError(
                "CLOUD_AGENT_VERSION_DIRECTORY_FAILED",
                exc.message,
                status_code=502,
                details={
                    "serverCode": exc.raw_code,
                    **{
                        key: value
                        for key, value in exc.details.items()
                        if key in {"request_id", "action"}
                    },
                },
            ) from exc
        raw_items = payload.get("versions") or payload.get("Versions") or []
        items = [
            self._account_agent_version_view(item)
            for item in raw_items
            if isinstance(item, dict)
        ]
        items = [item for item in items if item["versionId"]]
        current = next(
            (
                item["versionId"]
                for item in items
                if str(item["status"]).strip().lower() == "current"
            ),
            None,
        )
        return {
            "items": items,
            "total": int(
                payload.get("total_count")
                or payload.get("TotalCount")
                or len(items)
            ),
            "currentVersionId": current,
        }

    async def rollback_account_agent_version(
        self, agent_id: str, *, version_id: str
    ) -> dict[str, Any]:
        normalized_id = str(agent_id or "").strip()
        normalized_version_id = str(version_id or "").strip()
        if not normalized_id:
            raise StudioError("CLOUD_AGENT_NOT_FOUND", "云端 Agent 标识不能为空", status_code=404)
        if not normalized_version_id:
            raise StudioError(
                "CLOUD_AGENT_VERSION_REQUIRED",
                "请选择要回滚的云端版本",
                status_code=422,
                field="versionId",
            )
        try:
            payload = await self.client.rollback_version(
                normalized_id,
                target_version_id=normalized_version_id,
            )
        except AgentEngineAPIError as exc:
            raise StudioError(
                "CLOUD_AGENT_VERSION_ROLLBACK_FAILED",
                exc.message,
                status_code=502,
                details={
                    "serverCode": exc.raw_code,
                    **{
                        key: value
                        for key, value in exc.details.items()
                        if key in {"request_id", "action"}
                    },
                },
            ) from exc
        return {
            "agentId": str(payload.get("agent_id") or normalized_id),
            "targetVersionId": str(
                payload.get("target_version_id") or normalized_version_id
            ),
            "status": str(payload.get("status") or "UPDATING"),
            "noop": bool(payload.get("noop")),
        }

    async def get_account_agent_dashboard_access(
        self, agent_id: str
    ) -> dict[str, str | None]:
        detail = await self.get_account_agent(agent_id)
        path = (
            _NATIVE_DASHBOARD_UI_PATH
            if detail.get("chatTransport") == "official-dashboard"
            else _HOSTED_AGENT_UI_PATH
        )
        link = await self.client.create_dashboard_access_link(
            agent_id=agent_id,
            link_type="private",
            path=path,
        )
        access_url = str(link.get("access_url") or "").strip()
        if not access_url:
            raise StudioError(
                "DEPLOYMENT_DASHBOARD_UNAVAILABLE",
                "云端未返回可用的 Agent UI 地址",
                status_code=502,
            )
        return {
            "access_url": access_url,
            "agent_id": agent_id,
            "instance_id": None,
            "expires_at": str(link.get("expires_at") or "").strip() or None,
        }

    async def delete_account_agent(self, agent_id: str) -> bool:
        await self.get_account_agent(agent_id)
        try:
            deleted = await self.client.delete_agent(agent_id)
        except AgentEngineAPIError as exc:
            if exc.code == 404 or exc.details.get("http_status") == 404:
                return True
            raise
        if not deleted:
            raise StudioError(
                "CLOUD_AGENT_DELETE_FAILED",
                "云端未确认 Agent 删除结果",
                status_code=502,
            )
        return True

    def _chat_agent_id(
        self, deployment: DeploymentRecord | AccountCloudAgentReference
    ) -> str:
        """Bind local cloud chat to an immutable Studio deployment receipt.

        In particular, the browser cannot provide an arbitrary AgentId and
        turn the loopback Studio process into a signed control-plane proxy.
        """

        agent_id = str(deployment.agent_id or "").strip()
        if not agent_id:
            raise StudioError(
                "DEPLOYMENT_CLOUD_CHAT_UNAVAILABLE",
                "部署 receipt 缺少云端 Agent 标识",
                status_code=409,
            )
        return agent_id

    async def list_deployment_chat_sessions(
        self,
        deployment: DeploymentRecord,
        *,
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        """Read sessions through the authenticated Server projection."""

        return await self.client.list_sessions(
            self._chat_agent_id(deployment), page=page, size=size
        )

    async def create_deployment_chat_session(self, deployment: DeploymentRecord) -> dict[str, Any]:
        """Create one Server-owned session before its first cloud message."""

        return await self.client.create_session(self._chat_agent_id(deployment))

    async def list_deployment_chat_messages(
        self,
        deployment: DeploymentRecord,
        *,
        session_id: str,
        after_seq_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return the Server/Runtime message projection for a bound session."""

        try:
            return await self.client.list_session_messages(
                agent_id=self._chat_agent_id(deployment),
                session_id=session_id,
                after_seq_id=after_seq_id,
                limit=limit,
            )
        except AgentEngineAPIError as exc:
            # A poll already in flight can complete after DeleteSession.  The
            # deleted projection is an empty terminal view for Studio, not a
            # local API failure that should surface as a 500/toast.
            if exc.code == 404 or exc.details.get("http_status") == 404:
                return {"messages": [], "session_deleted": True}
            raise

    async def delete_deployment_chat_session(
        self, deployment: DeploymentRecord, *, session_id: str
    ) -> bool:
        """Delete only a session that Server scopes to the signed caller."""

        # The Server resolves session ownership from the authenticated caller;
        # the receipt binding above only establishes the enclosing Agent scope.
        self._chat_agent_id(deployment)
        deleted = await self.client.delete_session(session_id)
        if not deleted:
            raise StudioError(
                "CLOUD_CHAT_SESSION_DELETE_FAILED",
                "云端会话删除失败，请刷新后重试",
                status_code=502,
            )
        return True

    async def list_deployment_chat_events(
        self,
        deployment: DeploymentRecord,
        *,
        session_id: str,
        after_seq_id: int | None = None,
        offset: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read canonical events, including public Interaction/v1 frames."""

        try:
            return await self.client.list_session_events(
                agent_id=self._chat_agent_id(deployment),
                session_id=session_id,
                after_seq_id=after_seq_id,
                offset=offset,
                limit=limit,
            )
        except AgentEngineAPIError as exc:
            if exc.code == 404 or exc.details.get("http_status") == 404:
                return {"events": [], "session_deleted": True}
            raise

    async def send_deployment_chat_message(
        self,
        deployment: DeploymentRecord,
        *,
        session_id: str,
        content: Any,
        model: str | None = None,
        model_options: dict[str, Any] | None = None,
        tool_approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
    ) -> dict[str, Any]:
        """Submit a foreground message via RunAgent and Server admission.

        Kernel-enabled Agents return a durable receipt immediately; clients
        then read the canonical message/session event stream rather than
        treating a synchronous proxy body as the source of truth.
        """

        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "model": model,
            "model_options": model_options,
            "tool_approval_mode": tool_approval_mode,
            "collaboration_mode": collaboration_mode,
            "goal_objective": goal_objective,
        }
        return await self.client.chat(
            self._chat_agent_id(deployment),
            content,
            **kwargs,
        )

    async def stream_deployment_chat_message(
        self,
        deployment: DeploymentRecord | AccountCloudAgentReference,
        *,
        session_id: str,
        content: Any,
        model: str | None = None,
        model_options: dict[str, Any] | None = None,
        tool_approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Open the Server-admitted foreground RunAgent SSE connection."""

        try:
            return await self.stream_client.chat_stream(
                self._chat_agent_id(deployment),
                content,
                session_id=session_id,
                model=model,
                model_options=model_options,
                tool_approval_mode=tool_approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
            )
        except AgentEngineAPIError as exc:
            raise StudioError(
                "CLOUD_CHAT_STREAM_FAILED",
                exc.message,
                status_code=502,
                details={
                    "serverCode": exc.raw_code,
                    **{
                        key: value
                        for key, value in exc.details.items()
                        if key in {"request_id", "action", "http_status"}
                    },
                },
            ) from exc

    async def list_deployment_chat_models(
        self, deployment: DeploymentRecord
    ) -> dict[str, Any]:
        """Read the Server-authoritative model catalog for this Agent."""

        return await self.client.list_agent_models(
            agent_id=self._chat_agent_id(deployment)
        )

    async def submit_deployment_chat_interaction(
        self,
        deployment: DeploymentRecord,
        *,
        session_id: str,
        run_id: str,
        interaction_id: str,
        expected_revision: int,
        action: str,
        response: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Forward only Interaction/v1's caller-visible response fields."""

        return await self.client.submit_interaction(
            agent_id=self._chat_agent_id(deployment),
            session_id=session_id,
            run_id=run_id,
            interaction_id=interaction_id,
            expected_revision=expected_revision,
            action=action,
            response=response,
            idempotency_key=idempotency_key,
        )

    async def create_managed_runtime_deployment(self, **kwargs) -> DeploymentRecord:
        request: DeploymentRequest = kwargs["request"]
        digest = str(kwargs["manifest_digest"])
        runtime_environment = dict(kwargs.get("runtime_environment") or {})
        result = await self.client.create_agent(
            self._managed_runtime_payload(
                agent_name=str(kwargs["agent_name"]),
                manifest=str(kwargs["manifest"]),
                runtime_name=str(kwargs["runtime_name"]),
                runtime_version=str(kwargs["runtime_version"]),
                request=request,
                runtime_environment=runtime_environment,
            )
        )
        agent_id = str(result.get("agent_id") or "").strip()
        if not agent_id:
            raise StudioError(
                "CLOUD_DEPLOYMENT_PROTOCOL_INVALID",
                "CreateAgentProduct 未返回 AgentId",
                status_code=502,
            )
        return DeploymentRecord(
            id=f"dep_{uuid4().hex}",
            build_id=str(kwargs["build_id"]),
            bundle_digest=f"sha256:{digest}",
            version_id=f"managed-{digest[:16]}",
            status="DEPLOYING",
            target=request.target,
            agent_id=agent_id,
            instance_id=str(result.get("instance_id") or "").strip() or None,
            endpoint=str(result.get("endpoint") or "").strip() or None,
            artifact_id="managed-runtime",
            requires_kernel=True,
        )

    async def replace_managed_runtime_deployment(
        self, deployment: DeploymentRecord, **kwargs
    ) -> DeploymentRecord:
        if not deployment.agent_id:
            raise StudioError(
                "DEPLOYMENT_PROTOCOL_INVALID",
                "部署 receipt 缺少 AgentId",
                status_code=502,
            )
        request: DeploymentRequest = kwargs["request"]
        digest = str(kwargs["manifest_digest"])
        runtime_environment = dict(kwargs.get("runtime_environment") or {})
        update_payload: dict[str, Any] = {
            "artifact_type": "ManagedRuntime",
            # UpdateAgent resolves a fresh immutable runtime image from the
            # complete YAML declaration.  RuntimeConfig is the resolved
            # read-model and cannot be used as an input for a retry.
            "managed_runtime_config": {
                "runtime_name": str(kwargs["runtime_name"]),
                "runtime_version": str(kwargs["runtime_version"]),
                "manifest": str(kwargs["manifest"]),
            },
        }
        if runtime_environment:
            # A changed MCP/model binding can introduce a new credential ref.
            # Re-resolve it for every immutable revision instead of relying on
            # the environment captured by the first CreateAgent call.
            update_payload["environment_variables"] = runtime_environment
        await self.client.update_agent(
            deployment.agent_id,
            update_payload,
        )
        return DeploymentRecord(
            id=f"dep_{uuid4().hex}",
            build_id=str(kwargs["build_id"]),
            bundle_digest=f"sha256:{digest}",
            version_id=f"managed-{digest[:16]}",
            status="DEPLOYING",
            target=request.target,
            agent_id=deployment.agent_id,
            instance_id=deployment.instance_id,
            endpoint=deployment.endpoint,
            artifact_id="managed-runtime",
            requires_kernel=True,
        )

    def _bundle_for_deployment(self, bundle_uri: str, bundle_digest: Any) -> dict[str, str]:
        bundle = self._bundles.get(bundle_uri)
        if bundle is None or bundle["bundle_digest"] != str(bundle_digest):
            raise StudioError(
                "CLOUD_BUNDLE_REFERENCE_INVALID",
                "Bundle 引用或 digest 不匹配",
                status_code=502,
            )
        return bundle

    def _create_payload(
        self,
        *,
        bundle_uri: str,
        bundle: dict[str, str],
        request: DeploymentRequest,
    ) -> dict[str, Any]:
        return {
            "name": _server_agent_name(bundle["agent_id"]),
            "description": "Created by AgentKit Studio",
            "framework": bundle["runtime_type"],
            "artifact_type": "Code",
            "artifact_path": bundle_uri,
            "code_checksum": bundle["archive_sha"],
            "code_command": list(_STUDIO_CODE_COMMAND),
            "region": request.target.region,
            "ks3": self._code_config(bundle["bucket"]),
            "resources": {"cpu": 2, "memory": "4Gi"},
            "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 20},
            "auth_type": "ApiKey",
        }

    @staticmethod
    def _managed_runtime_payload(
        *,
        agent_name: str,
        manifest: str,
        runtime_name: str,
        runtime_version: str,
        request: DeploymentRequest,
        runtime_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "name": _server_agent_name(agent_name),
            "description": "Created by AgentKit Studio",
            "framework": runtime_name,
            "artifact_type": "ManagedRuntime",
            # ManagedRuntimeConfig is the public declaration accepted by
            # CreateAgent.  RuntimeConfig is only the Server-resolved state.
            "managed_runtime_config": {
                "runtime_name": runtime_name,
                "runtime_version": runtime_version,
                "manifest": manifest,
            },
            "region": request.target.region,
            # YAML agents run from a platform-owned runtime image and have no
            # user code bundle to build or unpack.  Keep their default small;
            # high-code deployments retain their separate 2 CPU / 4 GiB
            # profile in _create_payload above.
            "resources": {"cpu": 1, "memory": "2Gi"},
            "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 20},
            "auth_type": "ApiKey",
        }
        if runtime_environment:
            # Model credentials are resolved only for this in-memory deployment
            # request.  They are never written into the YAML build or local
            # deployment receipt.
            payload["environment_variables"] = dict(runtime_environment)
        return payload

    def _code_config(self, bucket: str) -> dict[str, str]:
        return {
            **self._ks3_credentials,
            "region": self.ks3_region,
            "bucket": bucket,
        }

    @staticmethod
    def _receipt(
        *,
        build_id: str,
        bundle_digest: str,
        bundle_uri: str,
        agent_id: str,
        instance_id: str | None,
        endpoint: str | None,
        target,
        status: str,
    ) -> DeploymentRecord:
        return DeploymentRecord(
            id=f"dep_{uuid4().hex}",
            build_id=build_id,
            bundle_digest=bundle_digest,
            version_id=f"bundle-{hashlib.sha256(bundle_uri.encode('utf-8')).hexdigest()[:16]}",
            status=cast(Any, status),
            target=target,
            agent_id=agent_id,
            instance_id=instance_id,
            endpoint=endpoint,
            bundle_uri=bundle_uri,
            requires_kernel=True,
        )


def _server_agent_name(agent_id: str) -> str:
    normalized = "".join(
        char if char.isalnum() or char == "-" else "-" for char in agent_id.lower()
    )
    normalized = normalized.strip("-") or "agent"
    if not normalized[0].isalpha():
        normalized = f"agent-{normalized}"
    if not normalized.startswith("studio-"):
        normalized = f"studio-{normalized}"
    return normalized[:63].rstrip("-")


def _safe_object_component(value: str) -> str:
    """Keep an immutable KS3 key below the Studio-owned prefix."""

    normalized = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in value.lower()
    ).strip("-")
    return normalized[:96] or "agent"


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
        return await self._deploy_build(build_id, request)

    async def deploy_managed_runtime(
        self,
        *,
        build_id: str,
        agent_name: str,
        manifest: str,
        runtime_name: str,
        runtime_version: str,
        manifest_digest: str,
        request: DeploymentRequest,
        runtime_environment: dict[str, str] | None = None,
        replacing: DeploymentRecord | None = None,
    ) -> DeploymentRecord:
        # A YAML/ManagedRuntime revision is deliberately not a migration
        # mechanism for a user-code Agent.  In particular, Studio may manage
        # the lifecycle of an existing Code deployment, but it must never
        # replace that deployment's artifact with a generated declaration.
        # The receipt is the local proof that this Agent was created through
        # the ManagedRuntime path in the first place.
        if replacing is not None and replacing.artifact_id != "managed-runtime":
            raise StudioError(
                "MANAGED_RUNTIME_REPLACEMENT_FORBIDDEN",
                "声明式 YAML 只能更新由 Studio 声明式路径创建的 Agent，不能覆盖高代码 Agent",
                status_code=409,
                details={"deploymentId": replacing.id},
            )
        if replacing is None:
            record = await self.gateway.create_managed_runtime_deployment(
                build_id=build_id,
                agent_name=agent_name,
                manifest=manifest,
                runtime_name=runtime_name,
                runtime_version=runtime_version,
                manifest_digest=manifest_digest,
                request=request,
                runtime_environment=runtime_environment,
            )
        else:
            record = await self.gateway.replace_managed_runtime_deployment(
                replacing,
                build_id=build_id,
                manifest=manifest,
                runtime_name=runtime_name,
                runtime_version=runtime_version,
                manifest_digest=manifest_digest,
                request=request,
                runtime_environment=runtime_environment,
            )
        self._save(record, request)
        return record

    async def _deploy_build(
        self,
        build_id: str,
        request: DeploymentRequest,
        *,
        replacing: DeploymentRecord | None = None,
    ) -> DeploymentRecord:
        build, bundle, provenance = self._prepared_build(build_id)
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
        if replacing is None:
            record = await self.gateway.create_deployment(
                build_id=build_id,
                version_id=version_id,
                bundle_digest=build.bundle_digest,
                request=request,
            )
        else:
            record = await self.gateway.replace_deployment(
                replacing,
                build_id=build_id,
                version_id=version_id,
                bundle_digest=build.bundle_digest,
                request=request,
            )
        self._save(record, request)
        return record

    def _prepared_build(self, build_id: str) -> tuple[BuildRecord, bytes, dict[str, Any]]:
        build = self.build_repository.get(build_id)
        if build.status != BuildStatus.SUCCEEDED or not build.artifact_path:
            raise StudioError(
                "BUILD_NOT_READY",
                "只有成功 Build 可以部署",
                status_code=409,
            )
        archive = self.workspace.resolve(build.artifact_path, must_exist=True)
        bundle = archive.read_bytes()
        self._reject_local_only_materializers(bundle, artifact_name=archive.name)
        checked_bundle = (
            preflight_hosted_kernel_bundle(bundle)
            if getattr(self.gateway, "requires_hosted_kernel_bundle_preflight", False)
            else None
        )
        manifest = (
            checked_bundle.manifest
            if checked_bundle is not None
            else json.loads(
                (archive.parent / "agent-bundle" / "manifest.json").read_text(encoding="utf-8")
            )
        )
        if manifest.get("bundleDigest") != build.bundle_digest:
            raise StudioError(
                "BUILD_DIGEST_MISMATCH",
                "上传前 Bundle digest 校验失败",
                status_code=422,
            )
        if checked_bundle is not None and manifest.get("agentId") != build.agent_id:
            raise StudioError(
                "BUILD_AGENT_MISMATCH",
                "上传 Bundle 的 AgentId 与 Build 记录不一致",
                status_code=422,
            )
        archive_sha256 = f"sha256:{hashlib.sha256(bundle).hexdigest()}"
        provenance = (
            dict(checked_bundle.provenance)
            if checked_bundle is not None
            else json.loads(
                (archive.parent / "agent-bundle" / "provenance.json").read_text(encoding="utf-8")
            )
        )
        provenance["archiveSha256"] = archive_sha256
        # The Server owns the profile-to-image mapping, but it must select the
        # profile for the concrete framework the deterministic build produced.
        provenance["runtimeType"] = str(manifest.get("runtimeType") or build.runtime_type)
        return build, bundle, provenance

    @staticmethod
    def _reject_local_only_materializers(
        bundle_bytes: bytes,
        *,
        artifact_name: str,
    ) -> None:
        """Check the exact bytes that will be uploaded for local-only materializers."""

        try:
            with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as bundle:
                def load_optional(name: str) -> dict[str, Any]:
                    try:
                        info = bundle.getinfo(name)
                    except KeyError:
                        return {}
                    if info.file_size > 8 * 1024 * 1024:
                        raise ValueError(f"{name} is too large")
                    payload = json.loads(bundle.read(info).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError(f"{name} must be an object")
                    return payload

                composition = load_optional("composition-profile.json")
                resolved = load_optional("resolved-agent-spec.json")
        except (
            OSError,
            UnicodeError,
            ValueError,
            zipfile.BadZipFile,
            json.JSONDecodeError,
        ) as error:
            raise StudioError(
                "BUILD_ARTIFACT_INVALID",
                "部署前无法验证 Bundle 的本地运行时依赖",
                status_code=422,
                details={"buildArtifact": artifact_name},
            ) from error

        dynamic_ref = any(
            isinstance(item, dict)
            and str(item.get("ref") or "").startswith(
                "plugin://io.ksadk.mcp.dsh-profile@"
            )
            for item in composition.get("capabilities", [])
        )
        resolved_capabilities = resolved.get("capabilities")
        dynamic_server = isinstance(resolved_capabilities, dict) and any(
            isinstance(item, dict) and item.get("materialization") == "dsh-profile"
            for item in resolved_capabilities.get("mcpServers", [])
        )
        if dynamic_ref or dynamic_server:
            raise StudioError(
                "DSH_MCP_DEPLOYMENT_UNSUPPORTED",
                "DSH Profile MCP 当前只能由本地 Studio 按 activation 物化，尚不能部署到云端",
                status_code=422,
                details={"materialization": "dsh-profile"},
            )

    def get(self, deployment_id: str) -> DeploymentRecord:
        path = self.workspace.resolve(Path(".agentkit/deployments") / f"{deployment_id}.json")
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

    def request_for(self, deployment_id: str) -> DeploymentRequest:
        """Return the immutable target stored with a deployment receipt."""

        path = self.workspace.resolve(Path(".agentkit/deployments") / f"{deployment_id}.json")
        if not path.is_file():
            self.get(deployment_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return DeploymentRequest.model_validate(payload["request"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError, ValidationError) as exc:
            raise StudioError(
                "DEPLOYMENT_RECEIPT_INVALID",
                "Deployment 回执损坏，不能执行回滚",
                status_code=409,
                details={"id": deployment_id},
            ) from exc

    def list(self) -> list[DeploymentRecord]:
        """List only valid, workspace-local deployment receipts.

        This is deliberately a local receipt read.  Refreshing every row here
        would turn opening the Studio page into unbounded control-plane calls;
        callers refresh a named receipt explicitly instead.
        """

        directory = self.workspace.resolve(".agentkit/deployments")
        if not directory.is_dir():
            return []
        resolved_directory = directory.resolve()
        records: list[DeploymentRecord] = []
        for path in sorted(directory.glob("dep_*.json"), key=lambda item: item.name, reverse=True):
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_directory)
                payload = json.loads(resolved_path.read_text(encoding="utf-8"))
                record = DeploymentRecord.model_validate(payload["record"])
                if resolved_path.name != f"{record.id}.json":
                    raise ValueError("deployment receipt filename does not match record id")
            except (
                OSError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
                ValidationError,
            ):
                logger.warning("Ignoring invalid Studio deployment receipt: %s", path.name)
                continue
            records.append(record)
        return records

    async def refresh(self, deployment_id: str) -> DeploymentRecord:
        """Refresh only from the Server-owned instance status projection."""

        deployment = self.get(deployment_id)
        status_reader = getattr(self.gateway, "get_deployment_status", None)
        if status_reader is None:
            return deployment
        refreshed = await status_reader(deployment)
        path = self.workspace.resolve(Path(".agentkit/deployments") / f"{deployment_id}.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._save(
            refreshed,
            DeploymentRequest.model_validate(payload["request"]),
        )
        return refreshed

    async def dashboard_access(self, deployment_id: str) -> dict[str, str | None]:
        """Create a private, receipt-bound Hosted UI link on explicit request."""

        deployment = self.get(deployment_id)
        dashboard_reader = getattr(self.gateway, "get_deployment_dashboard_access", None)
        if dashboard_reader is None:
            raise StudioError(
                "DEPLOYMENT_DASHBOARD_UNAVAILABLE",
                "当前云端网关不支持打开 Agent UI",
                status_code=501,
            )
        return await dashboard_reader(deployment)

    async def delete(self, deployment_id: str) -> dict[str, Any]:
        """Delete the receipt-bound cloud Agent, then remove its local receipts."""

        deployment = self.get(deployment_id)
        agent_id = str(deployment.agent_id or "").strip()
        if not agent_id:
            raise StudioError(
                "CLOUD_AGENT_DELETE_UNAVAILABLE",
                "Deployment receipt 缺少云端 Agent 标识",
                status_code=409,
            )
        deleter = getattr(self.gateway, "delete_deployment", None)
        if deleter is None:
            raise StudioError(
                "CLOUD_AGENT_DELETE_UNAVAILABLE",
                "当前云端网关不支持删除 Agent",
                status_code=501,
            )
        await deleter(deployment)

        deleted_receipts = self._delete_receipts_for_agent(
            agent_id, required=deployment
        )
        return {"agentId": agent_id, "deletedReceiptIds": deleted_receipts}

    def _delete_receipts_for_agent(
        self, agent_id: str, *, required: DeploymentRecord | None = None
    ) -> list[str]:
        related_receipts = {required.id: required} if required is not None else {}
        for record in self.list():
            if str(record.agent_id or "").strip() != agent_id:
                continue
            related_receipts[record.id] = record

        deleted_receipts: list[str] = []
        for record in related_receipts.values():
            receipt_path = self.workspace.resolve(
                Path(".agentkit/deployments") / f"{record.id}.json"
            )
            receipt_path.unlink(missing_ok=True)
            deleted_receipts.append(record.id)
        return deleted_receipts

    async def list_account_agents(self, *, page: int = 1, size: int = 100) -> dict[str, Any]:
        reader = getattr(self.gateway, "list_account_agents", None)
        if reader is None:
            raise StudioError(
                "CLOUD_AGENT_DIRECTORY_UNAVAILABLE",
                "当前云端网关不支持读取账号 Agent",
                status_code=501,
            )
        return await reader(page=page, size=size)

    async def get_account_agent(self, agent_id: str) -> dict[str, Any]:
        reader = getattr(self.gateway, "get_account_agent", None)
        if reader is None:
            raise StudioError(
                "CLOUD_AGENT_DIRECTORY_UNAVAILABLE",
                "当前云端网关不支持读取账号 Agent",
                status_code=501,
            )
        return await reader(agent_id)

    async def list_account_agent_versions(
        self, agent_id: str, *, page: int = 1, size: int = 100
    ) -> dict[str, Any]:
        reader = getattr(self.gateway, "list_account_agent_versions", None)
        if reader is None:
            raise StudioError(
                "CLOUD_AGENT_VERSION_DIRECTORY_UNAVAILABLE",
                "当前云端网关不支持读取 Agent 版本",
                status_code=501,
            )
        return await reader(agent_id, page=page, size=size)

    async def rollback_account_agent_version(
        self, agent_id: str, *, version_id: str
    ) -> dict[str, Any]:
        rollback = getattr(self.gateway, "rollback_account_agent_version", None)
        if rollback is None:
            raise StudioError(
                "CLOUD_AGENT_VERSION_ROLLBACK_UNAVAILABLE",
                "当前云端网关不支持回滚 Agent 版本",
                status_code=501,
            )
        return await rollback(agent_id, version_id=version_id)

    async def account_agent_dashboard_access(
        self, agent_id: str
    ) -> dict[str, str | None]:
        reader = getattr(self.gateway, "get_account_agent_dashboard_access", None)
        if reader is None:
            raise StudioError(
                "DEPLOYMENT_DASHBOARD_UNAVAILABLE",
                "当前云端网关不支持打开 Agent UI",
                status_code=501,
            )
        return await reader(agent_id)

    async def delete_account_agent(self, agent_id: str) -> dict[str, Any]:
        deleter = getattr(self.gateway, "delete_account_agent", None)
        if deleter is None:
            raise StudioError(
                "CLOUD_AGENT_DELETE_UNAVAILABLE",
                "当前云端网关不支持删除 Agent",
                status_code=501,
            )
        await deleter(agent_id)
        return {
            "agentId": agent_id,
            "deletedReceiptIds": self._delete_receipts_for_agent(agent_id),
        }

    async def _chat_target(
        self, target_id: str
    ) -> DeploymentRecord | AccountCloudAgentReference:
        if not target_id.startswith("account:"):
            return self.get(target_id)
        agent_id = target_id.removeprefix("account:").strip()
        detail = await self.get_account_agent(agent_id)
        resolved_id = str(detail.get("agentId") or "").strip()
        if not agent_id or resolved_id != agent_id:
            raise StudioError(
                "CLOUD_AGENT_REFERENCE_INVALID",
                "账号 Agent 引用与 Server 返回不一致",
                status_code=409,
            )
        if detail.get("chatTransport") != "studio-session-events":
            raise StudioError(
                "CLOUD_CHAT_TRANSPORT_UNSUPPORTED",
                "该类型 Agent 未声明统一 SessionEvent 会话能力，请使用官方 Dashboard",
                status_code=409,
                details={
                    "agentId": agent_id,
                    "chatTransport": detail.get("chatTransport"),
                    "reason": detail.get("chatRoutingReason"),
                },
            )
        return AccountCloudAgentReference(agent_id=agent_id)

    async def list_cloud_chat_sessions(
        self, deployment_id: str, *, page: int = 1, size: int = 50
    ) -> dict[str, Any]:
        deployment = await self._chat_target(deployment_id)
        reader = getattr(self.gateway, "list_deployment_chat_sessions", None)
        if reader is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持本地会话代理",
                status_code=501,
            )
        return await reader(deployment, page=page, size=size)

    async def create_cloud_chat_session(self, deployment_id: str) -> dict[str, Any]:
        deployment = await self._chat_target(deployment_id)
        creator = getattr(self.gateway, "create_deployment_chat_session", None)
        if creator is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持本地会话代理",
                status_code=501,
            )
        return await creator(deployment)

    async def list_cloud_chat_messages(
        self,
        deployment_id: str,
        *,
        session_id: str,
        after_seq_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        deployment = await self._chat_target(deployment_id)
        reader = getattr(self.gateway, "list_deployment_chat_messages", None)
        if reader is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持本地会话代理",
                status_code=501,
            )
        return await reader(
            deployment,
            session_id=session_id,
            after_seq_id=after_seq_id,
            limit=limit,
        )

    async def list_cloud_chat_events(
        self,
        deployment_id: str,
        *,
        session_id: str,
        after_seq_id: int | None = None,
        offset: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        deployment = await self._chat_target(deployment_id)
        reader = getattr(self.gateway, "list_deployment_chat_events", None)
        if reader is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持本地会话代理",
                status_code=501,
            )
        return await reader(
            deployment,
            session_id=session_id,
            after_seq_id=after_seq_id,
            offset=offset,
            limit=limit,
        )

    async def send_cloud_chat_message(
        self,
        deployment_id: str,
        *,
        session_id: str,
        content: Any,
        model: str | None = None,
        model_options: dict[str, Any] | None = None,
        tool_approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
    ) -> dict[str, Any]:
        deployment = await self._chat_target(deployment_id)
        sender = getattr(self.gateway, "send_deployment_chat_message", None)
        if sender is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持本地会话代理",
                status_code=501,
            )
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "content": content,
        }
        if model is not None:
            kwargs["model"] = model
        if model_options:
            kwargs["model_options"] = model_options
        if tool_approval_mode is not None:
            kwargs["tool_approval_mode"] = tool_approval_mode
        if collaboration_mode is not None:
            kwargs["collaboration_mode"] = collaboration_mode
        if goal_objective is not None:
            kwargs["goal_objective"] = goal_objective
        return await sender(deployment, **kwargs)

    async def stream_cloud_chat_message(
        self,
        deployment_id: str,
        *,
        session_id: str,
        content: Any,
        model: str | None = None,
        model_options: dict[str, Any] | None = None,
        tool_approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
    ) -> AsyncIterator[bytes]:
        deployment = await self._chat_target(deployment_id)
        sender = getattr(self.gateway, "stream_deployment_chat_message", None)
        if sender is None:
            raise StudioError(
                "CLOUD_CHAT_STREAM_UNAVAILABLE",
                "当前云端网关不支持实时会话代理",
                status_code=501,
            )
        return await sender(
            deployment,
            session_id=session_id,
            content=content,
            model=model,
            model_options=model_options,
            tool_approval_mode=tool_approval_mode,
            collaboration_mode=collaboration_mode,
            goal_objective=goal_objective,
        )

    async def list_cloud_chat_models(self, deployment_id: str) -> dict[str, Any]:
        deployment = await self._chat_target(deployment_id)
        reader = getattr(self.gateway, "list_deployment_chat_models", None)
        if reader is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持模型目录代理",
                status_code=501,
            )
        return await reader(deployment)

    async def submit_cloud_chat_interaction(
        self,
        deployment_id: str,
        *,
        session_id: str,
        run_id: str,
        interaction_id: str,
        expected_revision: int,
        action: str,
        response: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        deployment = await self._chat_target(deployment_id)
        submitter = getattr(self.gateway, "submit_deployment_chat_interaction", None)
        if submitter is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持本地会话代理",
                status_code=501,
            )
        return await submitter(
            deployment,
            session_id=session_id,
            run_id=run_id,
            interaction_id=interaction_id,
            expected_revision=expected_revision,
            action=action,
            response=response,
            idempotency_key=idempotency_key,
        )

    async def delete_cloud_chat_session(self, deployment_id: str, *, session_id: str) -> bool:
        deployment = await self._chat_target(deployment_id)
        deleter = getattr(self.gateway, "delete_deployment_chat_session", None)
        if deleter is None:
            raise StudioError(
                "CLOUD_CHAT_UNAVAILABLE",
                "当前云端网关不支持本地会话代理",
                status_code=501,
            )
        return await deleter(deployment, session_id=session_id)

    async def rollback(
        self,
        deployment_id: str,
        *,
        target_build_id: str,
    ) -> DeploymentRecord:
        path = self.workspace.resolve(Path(".agentkit/deployments") / f"{deployment_id}.json")
        if not path.is_file():
            self.get(deployment_id)
        request = self.request_for(deployment_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        deployment = DeploymentRecord.model_validate(payload["record"])
        return await self._deploy_build(
            target_build_id,
            request,
            replacing=deployment,
        )

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
                    "record": record.model_dump(by_alias=True, exclude_none=True, mode="json"),
                    "request": request.model_dump(by_alias=True, exclude_none=True, mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
