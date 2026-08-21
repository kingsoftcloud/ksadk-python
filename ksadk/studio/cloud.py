"""Studio Bundle upload and existing Agent lifecycle gateway contracts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Protocol, cast
from uuid import uuid4

from pydantic import ValidationError

from ksadk.api import AgentEngineClient
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
        )
        self.deployments.append(record)
        return record

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord:
        return deployment

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
        uploader_factory: Callable[..., Any] = KS3Uploader,
        bucket: str | None = None,
        ks3_credentials: dict[str, str] | None = None,
    ) -> None:
        self.region = region.strip()
        self.ks3_region = "cn-beijing-6" if self.region.lower() == "pre-online" else self.region
        self.client = client or AgentEngineClient(region=self.region)
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
            target=request.target,
            status="DEPLOYING",
        )

    async def get_deployment_status(self, deployment: DeploymentRecord) -> DeploymentRecord:
        if not deployment.agent_id:
            return deployment
        payload = await self.client.get_agent(agent_id=deployment.agent_id)
        deployment_detail = payload.get("deployment") or {}
        kernel_ready = bool(
            payload.get("agent_kernel_ready")
            or deployment_detail.get("agent_kernel_ready")
        )
        status = str(
            payload.get("status")
            or (payload.get("basic") or {}).get("status")
            or (payload.get("deployment") or {}).get("status")
            or ""
        ).strip().upper()
        projected = (
            "FAILED"
            if status in {"FAILED", "TERMINATED", "ERROR"}
            else "READY"
            if kernel_ready
            else "DEPLOYING"
        )
        return deployment.model_copy(update={"status": projected})

    async def create_managed_runtime_deployment(self, **kwargs) -> DeploymentRecord:
        request: DeploymentRequest = kwargs["request"]
        digest = str(kwargs["manifest_digest"])
        result = await self.client.create_agent(
            self._managed_runtime_payload(
                agent_name=str(kwargs["agent_name"]),
                manifest=str(kwargs["manifest"]),
                runtime_name=str(kwargs["runtime_name"]),
                runtime_version=str(kwargs["runtime_version"]),
                manifest_sha256=digest,
                request=request,
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
            artifact_id="managed-runtime",
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
        await self.client.update_agent(
            deployment.agent_id,
            {
                "artifact_type": "ManagedRuntime",
                "runtime_config": {
                    "name": str(kwargs["runtime_name"]),
                    "version": str(kwargs["runtime_version"]),
                    "manifest": str(kwargs["manifest"]),
                    "manifest_sha256": digest,
                },
            },
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
            artifact_id="managed-runtime",
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
        manifest_sha256: str,
        request: DeploymentRequest,
    ) -> dict[str, Any]:
        return {
            "name": _server_agent_name(agent_name),
            "description": "Created by AgentKit Studio",
            "framework": runtime_name,
            "artifact_type": "ManagedRuntime",
            "runtime_config": {
                "name": runtime_name,
                "version": runtime_version,
                "manifest": manifest,
                "manifest_sha256": manifest_sha256,
            },
            "region": request.target.region,
            "resources": {"cpu": 2, "memory": "4Gi"},
            "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 20},
            "auth_type": "ApiKey",
        }

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
            bundle_uri=bundle_uri,
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
        replacing: DeploymentRecord | None = None,
    ) -> DeploymentRecord:
        if replacing is None:
            record = await self.gateway.create_managed_runtime_deployment(
                build_id=build_id,
                agent_name=agent_name,
                manifest=manifest,
                runtime_name=runtime_name,
                runtime_version=runtime_version,
                manifest_digest=manifest_digest,
                request=request,
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
        provenance["runtimeType"] = str(
            manifest.get("runtimeType") or build.runtime_type
        )
        return build, bundle, provenance

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

    def request_for(self, deployment_id: str) -> DeploymentRequest:
        """Return the immutable target stored with a deployment receipt."""

        path = self.workspace.resolve(
            Path(".agentkit/deployments") / f"{deployment_id}.json"
        )
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
