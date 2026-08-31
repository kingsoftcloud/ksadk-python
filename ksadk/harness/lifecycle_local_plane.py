"""LocalLifecycleManager 到 LifecycleControlPlane 协议的服务端适配器。

把 Build → Deploy → Approve → Activate → Invoke → Rollback 的联合
Conformance（``run_lifecycle_conformance``）接到真实进程形态的本地
Runtime 上：Deploy 真实 spawn ``ksadk.harness.runtime_server``，Invoke
走真实 HTTP，Rollback 重新 Build/Deploy/Activate 目标 Revision 并下线
旧进程。不伪造任何一步。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ksadk.harness.lifecycle import BuildManifest, LocalDeployment, LocalLifecycleManager


class LocalLifecycleControlPlane:
    """适配器持有 Revision payload 解析函数；报告不含 Secret。"""

    def __init__(
        self,
        *,
        manager: LocalLifecycleManager,
        revision_payload_resolver: Callable[[str], dict[str, Any]],
        invoke_input: Any = "lifecycle-conformance-e2e",
        user_id: str = "conformance-user",
        session_id: str = "conformance-session",
        launch_process: bool = True,
    ) -> None:
        self._manager = manager
        self._resolver = revision_payload_resolver
        self._invoke_input = invoke_input
        self._user_id = user_id
        self._session_id = session_id
        self._launch_process = launch_process
        self._builds: dict[str, tuple[BuildManifest, dict[str, Any]]] = {}
        self._deployments: dict[str, LocalDeployment] = {}

    async def submit_revision(self, revision_ref: str) -> Mapping[str, Any]:
        # 本地平面不做中心化 Revision 注册：解析 payload 即完成提交，
        # 引用必须保持不可变。
        self._resolver(revision_ref)
        return {"revisionRef": revision_ref}

    async def build(self, revision_ref: str) -> Mapping[str, Any]:
        payload = self._resolver(revision_ref)
        manifest = self._manager.build(
            revision_payload=payload, revision_ref=revision_ref
        )
        self._builds[manifest.build_id] = (manifest, payload)
        return {"buildRef": manifest.build_id, "revisionRef": manifest.revision_ref}

    async def deploy(self, build_ref: str, route: str) -> Mapping[str, Any]:
        manifest, payload = self._builds[build_ref]
        deployment = self._manager.deploy(
            manifest=manifest,
            revision_payload=payload,
            route=route,
            launch_process=self._launch_process,
        )
        self._deployments[deployment.deployment_id] = deployment
        return {
            "deploymentRef": deployment.deployment_id,
            "buildRef": build_ref,
        }

    async def approve(self, deployment_ref: str) -> Mapping[str, Any]:
        deployment = self._deployments[deployment_ref]
        if deployment.status.value not in {"deployed", "active"}:
            return {"status": "rejected", "detail": deployment.status.value}
        return {"status": "approved"}

    async def activate(self, deployment_ref: str, route: str) -> Mapping[str, Any]:
        deployment = self._manager.activate(route)
        if deployment.deployment_id != deployment_ref:
            return {"deploymentRef": deployment.deployment_id}
        return {"deploymentRef": deployment.deployment_id}

    async def invoke(self, route: str, invocation_id: str) -> Mapping[str, Any]:
        deployment = self._manager.registry.active(route)
        result = self._manager.invoke_run(
            route=route,
            invocation_id=invocation_id,
            input=self._invoke_input,
            user_id=self._user_id,
            session_id=self._session_id,
        )
        return {
            "deploymentRef": deployment.deployment_id if deployment else "",
            "revisionRef": deployment.manifest.revision_ref if deployment else "",
            "status": result.get("status", ""),
        }

    async def rollback(self, route: str, revision_ref: str) -> Mapping[str, Any]:
        # 回滚 = 对目标 Revision 重新 Build → Deploy → Activate；
        # activate 会取代并下线当前 Active 进程。
        built = await self.build(revision_ref)
        deployed = await self.deploy(str(built["buildRef"]), route)
        await self.activate(str(deployed["deploymentRef"]), route)
        return {"revisionRef": revision_ref, "route": route}


__all__ = ["LocalLifecycleControlPlane"]
