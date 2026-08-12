"""Resolve immutable ADK/LangGraph Studio builds into RuntimeLaunchContext."""

from __future__ import annotations

import json

from ksadk.detection.detector import FrameworkDetector
from ksadk.runtime import RuntimeLaunchContext
from ksadk.studio.errors import StudioError
from ksadk.studio.repository import BuildRepository
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.workspace import Workspace
from ksadk.tools.gateway import normalize_tool_approval_mode


class FrameworkRunSpecResolver:
    def __init__(
        self,
        workspace: Workspace,
        *,
        build_repository: BuildRepository | None = None,
    ) -> None:
        self.workspace = workspace
        self.builds = build_repository or BuildRepository(workspace)

    def resolve(
        self,
        build_id: str,
        *,
        model: str | None = None,
        approval_mode: str | None = None,
    ) -> StudioRunSpec:
        build = self.builds.get(build_id)
        runtime_type = build.runtime_type.strip().lower()
        if runtime_type not in {"adk", "langgraph"}:
            raise StudioError(
                "BUILD_RUNTIME_UNSUPPORTED",
                "Build 没有可由 RuntimeAdapter 启动的 ADK/LangGraph Runtime",
                status_code=422,
                details={"buildId": build_id, "runtimeType": runtime_type},
            )
        if not build.artifact_path:
            raise StudioError("BUILD_NOT_READY", "Build 尚未生成制品", status_code=409)
        artifact_root = self.workspace.resolve(build.artifact_path, must_exist=True).parent
        bundle_root = artifact_root / "agent-bundle"
        project_dir = bundle_root / "runtime"
        if not project_dir.is_dir():
            raise StudioError(
                "BUILD_RUNTIME_SOURCE_MISSING",
                "Build 缺少不可变 Runtime 源码快照",
                status_code=500,
            )
        detection = FrameworkDetector(str(project_dir)).detect()
        if not detection.is_valid or detection.type.value != runtime_type:
            raise StudioError(
                "BUILD_RUNTIME_DETECTION_MISMATCH",
                "Build 中的 Runtime 源码与 Runtime Lock 不一致",
                status_code=422,
                details={
                    "expected": runtime_type,
                    "detected": detection.type.value,
                },
            )
        selected_model = self._select_model(build.runtime_lock, model)
        resolved_path = bundle_root / "resolved-agent-spec.json"
        resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
        instructions = resolved.get("instructions") if isinstance(resolved, dict) else {}
        request_config = {
            "base_instructions": str((instructions or {}).get("system") or ""),
            "entry_point": detection.entry_point,
            "agent_variable": detection.agent_variable,
        }
        if approval_mode:
            request_config["tool_approval_mode"] = normalize_tool_approval_mode(
                approval_mode
            )
        return StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type=runtime_type,
                project_dir=project_dir,
                detection=detection,
                config=dict(detection.raw_config or {}),
            ),
            build_id=build.id,
            agent_id=build.agent_id,
            model=selected_model,
            request_config=request_config,
            manifest_sha256=build.resolved_digest,
        )

    @staticmethod
    def _select_model(runtime_lock: dict, requested: str | None) -> str:
        allowed = [str(value) for value in runtime_lock.get("models") or [] if str(value)]
        default = str(runtime_lock.get("model") or "").strip()
        if default and default not in allowed:
            allowed.insert(0, default)
        selected = str(requested or default).strip()
        if not selected:
            raise StudioError(
                "AGENT_MODEL_REQUIRED",
                "Build 没有绑定可运行模型",
                status_code=422,
            )
        if allowed and selected not in allowed:
            raise StudioError(
                "MODEL_NOT_BOUND",
                "请求模型未绑定到当前 Agent Build",
                status_code=422,
                details={"model": selected, "allowedModels": allowed},
            )
        return selected


__all__ = ["FrameworkRunSpecResolver"]
