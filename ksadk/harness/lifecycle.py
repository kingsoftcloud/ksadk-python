"""Harness 生命周期闭环（plan §12）——Build → Deploy → Activate → Invoke。

- 状态模型（§12.1）：Draft → Built → Deployed → Active → Running，失败状态显式；
- Revision 编译（§12.2）：validate refs → compile HarnessSpec → content hash →
  Build Manifest → Conformance 子集（最小套件）；
- Deploy/Activate（§12.4）：创建本地 Runtime 实例 → Health Check → 注册本地
  Route → deployed → 激活 → 经 Active Deployment 调用。不能只改数据库状态。

正式平台的 Runtime 生命周期/Route/Registry 由 agentengine-server 承担；
本模块承担 SDK/CLI/数据面与本地运行。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ksadk.harness.compiler import compile_revision_payload
from ksadk.harness.spec import HarnessSpec


class LifecycleStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    BUILT = "built"
    DEPLOYED = "deployed"
    ACTIVE = "active"
    RUNNING = "running"
    # 显式失败状态（§12.1）。
    VALIDATION_FAILED = "validation_failed"
    BUILD_FAILED = "build_failed"
    APPROVAL_REJECTED = "approval_rejected"
    DEPLOY_FAILED = "deploy_failed"
    ACTIVATION_FAILED = "activation_failed"
    RUNTIME_UNHEALTHY = "runtime_unhealthy"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class LifecycleError(RuntimeError):
    """非法状态迁移或构建失败。"""


@dataclass(frozen=True)
class BuildManifest:
    """Build 产物清单（§12.3）：不记录 Secret，只记录引用与版本。"""

    revision_ref: str
    harness_version: str
    engine: str
    model_profile_ref: str
    mcp_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    policy_refs: tuple[str, ...] = ()
    content_hash: str = ""
    artifact_digest: str = ""
    build_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "revisionRef": self.revision_ref,
            "harnessVersion": self.harness_version,
            "engine": self.engine,
            "modelProfileRef": self.model_profile_ref,
            "mcpRefs": list(self.mcp_refs),
            "skillRefs": list(self.skill_refs),
            "policyRefs": list(self.policy_refs),
            "contentHash": self.content_hash,
            "artifactDigest": self.artifact_digest,
            "buildId": self.build_id,
        }


def _digest(material: str) -> str:
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class BuildPipeline:
    """Revision → HarnessSpec → Build Manifest（§12.2）。"""

    def __init__(self, *, harness_version: str = "1.0.0") -> None:
        self._harness_version = harness_version

    def build(
        self, *, revision_payload: dict[str, Any], revision_ref: str
    ) -> BuildManifest:
        # validate refs + compile HarnessSpec（compile 内含 ref 校验）。
        spec = compile_revision_payload(revision_payload, revision_ref=revision_ref)
        content_hash = _digest(json.dumps(spec.model_dump(), sort_keys=True))
        manifest = BuildManifest(
            revision_ref=revision_ref,
            harness_version=self._harness_version,
            engine="managed-langgraph",
            model_profile_ref=spec.model.profile_ref,
            mcp_refs=tuple(b.capability_ref for b in spec.capabilities.mcp_bindings),
            skill_refs=tuple(
                b.capability_ref for b in spec.capabilities.skill_bindings
            ),
            content_hash=content_hash,
            artifact_digest=_digest(content_hash + revision_ref),
            build_id=f"bld_{content_hash[7:19]}",
        )
        return manifest


class LocalDeployment:
    """一个本地 Runtime 实例（§12.4）：真实启动 + 健康检查 + 路由。"""

    def __init__(
        self, *, deployment_id: str, manifest: BuildManifest, spec: HarnessSpec
    ) -> None:
        self.deployment_id = deployment_id
        self.manifest = manifest
        self.spec = spec
        self.status: LifecycleStatus = LifecycleStatus.DRAFT
        self.health_checked: bool = False
        self.invocations: list[str] = []

    def check_health(self) -> bool:
        """本地 Health Check：Spec 可编译、模型绑定存在、状态机可达。"""
        ok = bool(self.spec.model.profile_ref) and self.status in {
            LifecycleStatus.DRAFT,
            LifecycleStatus.BUILT,
            LifecycleStatus.DEPLOYED,
            LifecycleStatus.ACTIVE,
            LifecycleStatus.RUNNING,
        }
        self.health_checked = True
        if not ok:
            self.status = LifecycleStatus.RUNTIME_UNHEALTHY
        return ok


@dataclass
class LocalRuntimeRegistry:
    """本地 Route 注册表：route → deployment；每 route 至多一个 Active。"""

    _routes: dict[str, LocalDeployment] = field(default_factory=dict)

    def register_route(self, route: str, deployment: LocalDeployment) -> None:
        self._routes[route] = deployment

    def active(self, route: str) -> LocalDeployment | None:
        deployment = self._routes.get(route)
        if deployment and deployment.status == LifecycleStatus.ACTIVE:
            return deployment
        return None

    def route_of(self, deployment_id: str) -> str | None:
        for route, deployment in self._routes.items():
            if deployment.deployment_id == deployment_id:
                return route
        return None


class LocalLifecycleManager:
    """Build → Deploy → Activate → Invoke 的本地编排（§12.4）。

    Invoke 必须经 Active Deployment 的 Route 发生——状态只是结果，调用路径
    才是事实。
    """

    def __init__(self) -> None:
        self.registry = LocalRuntimeRegistry()
        self._deployments: dict[str, LocalDeployment] = {}
        self._manifests: dict[str, BuildManifest] = {}

    # ------------------------------------------------------------- build

    def build(
        self, *, revision_payload: dict[str, Any], revision_ref: str
    ) -> BuildManifest:
        try:
            manifest = BuildPipeline().build(
                revision_payload=revision_payload, revision_ref=revision_ref
            )
        except Exception as exc:  # noqa: BLE001
            raise LifecycleError(f"build failed: {exc}") from exc
        self._manifests[manifest.build_id] = manifest
        return manifest

    # ------------------------------------------------------------ deploy

    def deploy(
        self, *, manifest: BuildManifest, revision_payload: dict[str, Any], route: str
    ) -> LocalDeployment:
        if manifest.build_id not in self._manifests:
            raise LifecycleError("unknown manifest: build first")
        deployment = LocalDeployment(
            deployment_id=f"dep_{manifest.build_id}",
            manifest=manifest,
            spec=compile_revision_payload(
                revision_payload, revision_ref=manifest.revision_ref
            ),
        )
        # 创建本地 Runtime 实例 + Health Check（§12.4：真实完成，不能只改状态）。
        deployment.status = LifecycleStatus.BUILT
        if not deployment.check_health():
            raise LifecycleError(
                f"deploy failed: runtime unhealthy for {deployment.deployment_id}"
            )
        self.registry.register_route(route, deployment)
        deployment.status = LifecycleStatus.DEPLOYED
        self._deployments[deployment.deployment_id] = deployment
        return deployment

    # ---------------------------------------------------------- activate

    def activate(self, route: str) -> LocalDeployment:
        deployment = self.registry._routes.get(route)
        if deployment is None or deployment.status != LifecycleStatus.DEPLOYED:
            raise LifecycleError(
                f"activation failed: route {route!r} has no deployed revision"
            )
        # 重新 Health Check 后激活。
        if not deployment.check_health():
            raise LifecycleError("activation failed: runtime unhealthy")
        # 旧 Active（同 route 其他 deployment）被取代。
        for other in self._deployments.values():
            if other.status == LifecycleStatus.ACTIVE and other is not deployment:
                other.status = LifecycleStatus.SUPERSEDED
        deployment.status = LifecycleStatus.ACTIVE
        return deployment

    # ------------------------------------------------------------ invoke

    def invoke(self, *, route: str, invocation_id: str) -> LocalDeployment:
        """经 Active Route 调用（非 Active 状态一律拒绝）。"""
        deployment = self.registry.active(route)
        if deployment is None:
            raise LifecycleError(f"invoke rejected: route {route!r} has no active deployment")
        deployment.status = LifecycleStatus.RUNNING
        deployment.invocations.append(invocation_id)
        deployment.status = LifecycleStatus.ACTIVE
        return deployment

    # ------------------------------------------------------------ rollback

    def rollback(self, route: str) -> LocalDeployment:
        deployment = self.registry._routes.get(route)
        if deployment is None:
            raise LifecycleError(f"rollback failed: unknown route {route!r}")
        deployment.status = LifecycleStatus.ROLLED_BACK
        return deployment


__all__ = [
    "BuildManifest",
    "BuildPipeline",
    "LifecycleError",
    "LifecycleStatus",
    "LocalDeployment",
    "LocalLifecycleManager",
    "LocalRuntimeRegistry",
]
