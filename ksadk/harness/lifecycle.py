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
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from ksadk.harness.compiler import compile_revision_payload
from ksadk.harness.events import RuntimeEvent
from ksadk.harness.readiness import runtime_readiness
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
    fallback_model_profile_refs: tuple[str, ...] = ()
    model_provider_policy: dict[str, Any] = field(default_factory=dict)
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
            "fallbackModelProfileRefs": list(self.fallback_model_profile_refs),
            "modelProviderPolicy": self.model_provider_policy,
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

    def build(self, *, revision_payload: dict[str, Any], revision_ref: str) -> BuildManifest:
        # validate refs + compile HarnessSpec（compile 内含 ref 校验）。
        spec = compile_revision_payload(revision_payload, revision_ref=revision_ref)
        content_hash = _digest(json.dumps(spec.model_dump(), sort_keys=True))
        manifest = BuildManifest(
            revision_ref=revision_ref,
            harness_version=self._harness_version,
            engine="managed-langgraph",
            model_profile_ref=spec.model.profile_ref,
            fallback_model_profile_refs=spec.model.fallback_profile_refs,
            model_provider_policy=spec.model.provider_policy.model_dump(mode="json"),
            mcp_refs=tuple(b.capability_ref for b in spec.capabilities.mcp_bindings),
            skill_refs=tuple(b.capability_ref for b in spec.capabilities.skill_bindings),
            content_hash=content_hash,
            artifact_digest=_digest(content_hash + revision_ref),
            build_id=f"bld_{content_hash[7:19]}",
        )
        return manifest


class LocalDeployment:
    """一个本地 Runtime 实例（§12.4）：真实启动 + 健康检查 + 路由。

    进程形态（收口 4）：``launch_process=True`` 时 Deploy 真实 spawn
    ``ksadk.harness.runtime_server`` 子进程（uvicorn HTTP 服务），Health
    Check 是对 ``/health`` 的真实 HTTP 请求；进程退出即 Runtime 不健康。
    进程内形态保留用于单测/调试（无 IO 副作用）。
    """

    def __init__(
        self,
        *,
        deployment_id: str,
        manifest: BuildManifest,
        spec: HarnessSpec,
        readiness_report: dict[str, Any] | None = None,
    ) -> None:
        self.deployment_id = deployment_id
        self.manifest = manifest
        self.spec = spec
        self.status: LifecycleStatus = LifecycleStatus.DRAFT
        self.health_checked: bool = False
        self.invocations: list[str] = []
        self.run_results: list[dict[str, Any]] = []
        self.readiness_report = readiness_report or runtime_readiness(spec)
        # 进程形态字段。
        self.port: int | None = None
        self.base_url: str = ""
        self.process: subprocess.Popen[bytes] | None = None
        self._workspace: tempfile.TemporaryDirectory[str] | None = None
        self._runtime_log: Any | None = None
        self._route: str = ""
        self._health_timeout: float = 20.0
        self._server_command: list[str] | None = None

    @property
    def process_mode(self) -> bool:
        return self.process is not None

    def http_health_check(self, *, timeout: float = 3.0) -> bool:
        """真实 HTTP Health Check（进程形态）：GET {base_url}/health。"""
        if not self.base_url:
            return False
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            self.status = LifecycleStatus.RUNTIME_UNHEALTHY
            return False
        ok = payload.get("status") == "ok" and (payload.get("deploymentId") == self.deployment_id)
        if not ok:
            self.status = LifecycleStatus.RUNTIME_UNHEALTHY
        return ok

    def check_health(self) -> bool:
        """Health Check：进程形态走真实 HTTP；进程内形态做配置级检查。"""
        if self.process_mode:
            self.health_checked = True
            return self.http_health_check()
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

    def activate_runtime(self, *, timeout: float = 5.0) -> None:
        """Activate the data plane in the deployed subprocess."""
        if not self.process_mode:
            return
        request = urllib.request.Request(
            f"{self.base_url}/control/activate",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LifecycleError(
                f"activation failed: runtime control request failed ({exc})"
            ) from exc
        if payload.get("status") != "active":
            raise LifecycleError(f"activation failed: unexpected runtime payload {payload}")

    def invoke_run(
        self,
        *,
        input: Any,
        invocation_id: str,
        user_id: str,
        session_id: str,
        agent_id: str | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Invoke the subprocess data plane and return its RuntimeEvent v2 projection."""
        if not self.process_mode:
            raise LifecycleError("invoke_run requires a process deployment")
        body = json.dumps(
            {
                "input": input,
                "userId": user_id,
                "sessionId": session_id,
                "agentId": agent_id,
                "invocationId": invocation_id,
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/runs",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LifecycleError(f"invoke failed: HTTP {exc.code} {detail}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LifecycleError(f"invoke failed: runtime request failed ({exc})") from exc
        self.invocations.append(invocation_id)
        self.run_results.append(result)
        return result

    def start_process(
        self,
        *,
        route: str,
        health_timeout: float,
        server_command: list[str] | None = None,
    ) -> None:
        """真实启动 Runtime 子进程并等待 HTTP Health Check 通过。

        自定义 ``server_command`` 可使用 ``{spec_file}``、``{route}``、
        ``{deployment_id}``、``{port}``、``{build_id}``、``{content_hash}``
        和 ``{state_dir}`` 占位符。参数逐项替换后直接交给 ``Popen``，不经
        shell；这让嵌入方可以复用同一生命周期管理器装配自定义 Runtime。
        """
        if self._workspace is not None:
            raise LifecycleError("runtime workspace already exists; use restart_process")
        self._workspace = tempfile.TemporaryDirectory(prefix=f"ksadk-deploy-{self.deployment_id}-")
        spec_file = Path(self._workspace.name) / "spec.json"
        spec_file.write_text(
            json.dumps(self.spec.model_dump(by_alias=True, mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        self._route = route
        self._health_timeout = health_timeout
        self._server_command = list(server_command) if server_command else None
        try:
            self._launch_process(
                route=route,
                health_timeout=health_timeout,
                server_command=server_command,
            )
        except Exception:
            self.terminate()
            raise

    def restart_process(self) -> None:
        """Restart a local deployment while preserving checkpoints and Run index."""
        if self._workspace is None or not self._route:
            raise LifecycleError("runtime has not been deployed in process mode")
        reactivate = self.status in {LifecycleStatus.ACTIVE, LifecycleStatus.RUNNING}
        self._stop_process()
        self._launch_process(
            route=self._route,
            health_timeout=self._health_timeout,
            server_command=self._server_command,
        )
        if reactivate:
            self.activate_runtime()
            self.status = LifecycleStatus.ACTIVE

    def _launch_process(
        self,
        *,
        route: str,
        health_timeout: float,
        server_command: list[str] | None,
    ) -> None:
        if self._workspace is None:
            raise LifecycleError("runtime workspace is not initialized")
        spec_file = Path(self._workspace.name) / "spec.json"
        state_dir = Path(self._workspace.name) / "state"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        if server_command:
            replacements = {
                "{spec_file}": str(spec_file),
                "{route}": route,
                "{deployment_id}": self.deployment_id,
                "{port}": str(self.port),
                "{build_id}": self.manifest.build_id,
                "{content_hash}": self.manifest.content_hash,
                "{state_dir}": str(state_dir),
            }
            command = []
            for argument in server_command:
                rendered = argument
                for placeholder, value in replacements.items():
                    rendered = rendered.replace(placeholder, value)
                command.append(rendered)
        else:
            command = [
                sys.executable,
                "-m",
                "ksadk.harness.runtime_server",
                "--spec-file",
                str(spec_file),
                "--route",
                route,
                "--deployment-id",
                self.deployment_id,
                "--port",
                str(self.port),
                "--build-id",
                self.manifest.build_id,
                "--content-hash",
                self.manifest.content_hash,
                "--state-dir",
                str(state_dir),
            ]
        self._runtime_log = (Path(self._workspace.name) / "runtime.log").open("ab")
        self.process = subprocess.Popen(  # noqa: S603 - 命令由本模块构造
            command,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=self._runtime_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + health_timeout
        last_error = "health check never succeeded"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                last_error = f"runtime process exited with code {self.process.returncode}"
                break
            try:
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=1.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") == "ok":
                    self.health_checked = True
                    return
                last_error = f"unexpected health payload: {payload}"
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
        self._stop_process()
        self.status = LifecycleStatus.RUNTIME_UNHEALTHY
        raise LifecycleError(f"deploy failed: runtime process health check 未通过 ({last_error})")

    def terminate(self) -> None:
        """终止 Runtime 子进程并清理临时目录。"""
        self._stop_process()
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None

    def _stop_process(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底强杀
                self.process.kill()
        self.process = None
        if self._runtime_log is not None:
            self._runtime_log.close()
            self._runtime_log = None


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

    def build(self, *, revision_payload: dict[str, Any], revision_ref: str) -> BuildManifest:
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
        self,
        *,
        manifest: BuildManifest,
        revision_payload: dict[str, Any],
        route: str,
        launch_process: bool = False,
        health_timeout: float = 20.0,
        server_command: list[str] | None = None,
        readiness_events: Sequence[RuntimeEvent] = (),
    ) -> LocalDeployment:
        if manifest.build_id not in self._manifests:
            raise LifecycleError("unknown manifest: build first")
        spec = compile_revision_payload(revision_payload, revision_ref=manifest.revision_ref)
        readiness_report = runtime_readiness(spec, readiness_events)
        if not readiness_report["deployable"]:
            blockers = ", ".join(
                str(check["reason_code"])
                for check in readiness_report["checks"]
                if check["status"] == "blocked"
            )
            raise LifecycleError(f"deploy blocked by runtime readiness: {blockers}")
        deployment = LocalDeployment(
            deployment_id=f"dep_{manifest.build_id}",
            manifest=manifest,
            spec=spec,
            readiness_report=readiness_report,
        )
        # 创建本地 Runtime 实例 + Health Check（§12.4：真实完成，不能只改状态）。
        deployment.status = LifecycleStatus.BUILT
        if launch_process:
            # 收口 4：真实启动 Runtime 子进程并做 HTTP Health Check。
            deployment.start_process(
                route=route,
                health_timeout=health_timeout,
                server_command=server_command,
            )
        if not deployment.check_health():
            deployment.status = LifecycleStatus.DEPLOY_FAILED
            deployment.terminate()
            raise LifecycleError(f"deploy failed: runtime unhealthy for {deployment.deployment_id}")
        self.registry.register_route(route, deployment)
        deployment.status = LifecycleStatus.DEPLOYED
        self._deployments[deployment.deployment_id] = deployment
        return deployment

    # ---------------------------------------------------------- activate

    def activate(self, route: str) -> LocalDeployment:
        deployment = self.registry._routes.get(route)
        if deployment is None or deployment.status != LifecycleStatus.DEPLOYED:
            raise LifecycleError(f"activation failed: route {route!r} has no deployed revision")
        # 重新 Health Check（进程形态为真实 HTTP）后激活。
        if not deployment.check_health():
            raise LifecycleError("activation failed: runtime unhealthy")
        deployment.activate_runtime()
        # 旧 Active（同 route 其他 deployment）被取代；进程形态同时下线。
        for other in self._deployments.values():
            if other.status == LifecycleStatus.ACTIVE and other is not deployment:
                other.status = LifecycleStatus.SUPERSEDED
                other.terminate()
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

    def invoke_run(
        self,
        *,
        route: str,
        invocation_id: str,
        input: Any,
        user_id: str,
        session_id: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke the active deployed process instead of only recording a status change."""
        deployment = self.registry.active(route)
        if deployment is None:
            raise LifecycleError(f"invoke rejected: route {route!r} has no active deployment")
        deployment.status = LifecycleStatus.RUNNING
        try:
            return deployment.invoke_run(
                input=input,
                invocation_id=invocation_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
            )
        finally:
            deployment.status = LifecycleStatus.ACTIVE

    # ------------------------------------------------------------ rollback

    def rollback(self, route: str) -> LocalDeployment:
        deployment = self.registry._routes.get(route)
        if deployment is None:
            raise LifecycleError(f"rollback failed: unknown route {route!r}")
        deployment.status = LifecycleStatus.ROLLED_BACK
        deployment.terminate()
        return deployment

    # ------------------------------------------------------------- close

    def close(self) -> None:
        """下线全部 Runtime 子进程（宿主关闭时调用）。"""
        for deployment in self._deployments.values():
            deployment.terminate()


__all__ = [
    "BuildManifest",
    "BuildPipeline",
    "LifecycleError",
    "LifecycleStatus",
    "LocalDeployment",
    "LocalLifecycleManager",
    "LocalRuntimeRegistry",
]
