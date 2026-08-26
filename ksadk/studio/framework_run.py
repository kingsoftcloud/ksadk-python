"""Resolve immutable ADK/LangGraph Studio builds into RuntimeLaunchContext."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ksadk.detection.detector import FrameworkDetector
from ksadk.runtime import RuntimeLaunchContext
from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import BundleManifest
from ksadk.studio.errors import StudioError
from ksadk.studio.repository import BuildRepository
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.workspace import Workspace
from ksadk.tools.gateway import normalize_tool_approval_mode


def _resolved_prompt_ownership(resolved: Any) -> str:
    """从 resolved-agent-spec.json 的 context 块读 prompt_ownership。

    resolved spec 由 ContractModel 以 ``by_alias=True`` 序列化，alias_generator 为
    camelCase，故字段名为 ``promptOwnership``；``populate_by_name`` 仅作用于输入，JSON
    输出仍为 alias。此处 camelCase 与 snake 两种写法都查，稳妥兼容。非 dict / 缺失时
    返回空串（== framework 默认，不接管 Runner 输入）。
    """
    if not isinstance(resolved, dict):
        return ""
    context = resolved.get("context")
    if not isinstance(context, dict):
        return ""
    return str(
        context.get("promptOwnership")
        or context.get("prompt_ownership")
        or context.get("ownership")
        or ""
    )


def _resolved_context_engine_rollout(resolved: Any) -> str:
    """读取 AgentVersion 固化的 Context Engine rollout。"""
    if not isinstance(resolved, dict):
        return ""
    context = resolved.get("context")
    if not isinstance(context, dict):
        return ""
    rollout = context.get("rollout")
    if not isinstance(rollout, dict):
        return ""
    return str(rollout.get("contextEngine") or rollout.get("context_engine") or "")


def _resolved_memory_recall_enabled(resolved: Any) -> bool | None:
    """读取 AgentVersion 的 Memory 召回开关；缺失时保留旧环境策略。"""
    if not isinstance(resolved, dict):
        return None
    memory = resolved.get("memory")
    if not isinstance(memory, dict) or "enabled" not in memory:
        return None
    enabled = bool(memory.get("enabled"))
    recall = memory.get("recall")
    if isinstance(recall, dict) and "enabled" in recall:
        enabled = enabled and bool(recall.get("enabled"))
    context = resolved.get("context")
    contributors = context.get("contributors") if isinstance(context, dict) else None
    if isinstance(contributors, dict):
        explicit = contributors.get("memoryRecall", contributors.get("memory_recall"))
        if explicit is not None:
            enabled = enabled and bool(explicit)
    return enabled


def _resolved_memory_write_rollout(resolved: Any) -> str:
    """读取 AgentVersion 固化的 Memory 写入 rollout。"""
    if not isinstance(resolved, dict):
        return ""
    context = resolved.get("context")
    if not isinstance(context, dict):
        return ""
    rollout = context.get("rollout")
    if not isinstance(rollout, dict):
        return ""
    return str(rollout.get("memoryWrite") or rollout.get("memory_write") or "")


class FrameworkRunSpecResolver:
    # manifest.json 是 bundle 自身的描述文件，不进入 manifest.files 清单
    # （builder 在扫描文件列表之后才写它），校验时排除。checksums.txt 在本仓
    # bundle 格式中是 manifest 声明的普通成员，参与校验。
    _BUNDLE_META_FILES = frozenset({"manifest.json"})

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
        self._verify_bundle_integrity(
            bundle_root, expected_bundle_digest=build.bundle_digest
        )
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
            # Preserve system/task as separate PCM sources while keeping the
            # framework runner's existing base_instructions projection.
            "agent_system": str((instructions or {}).get("system") or ""),
            "agent_task": str((instructions or {}).get("task") or ""),
            **(
                {"prompt_integration_mode": "ksadk_hosted"}
                if _resolved_prompt_ownership(resolved) == "ksadk"
                else {}
            ),
            "context_engine_rollout": _resolved_context_engine_rollout(resolved),
            "memory_recall_enabled": _resolved_memory_recall_enabled(resolved),
            "memory_write_rollout": _resolved_memory_write_rollout(resolved),
            "memory_enabled": _resolved_memory_enabled(resolved),
            "memory_write_mode": _resolved_memory_write_mode(resolved),
            "flush_before_compaction": _resolved_memory_flush_before_compaction(resolved),
            "provider_ref": _resolved_memory_provider_ref(resolved),
            "entry_point": detection.entry_point,
            "agent_variable": detection.agent_variable,
        }
        # AgentVersion 的 ContextSpec 预算传到 Planner（方案 §8.2）
        context_spec = resolved.get("context") if isinstance(resolved, dict) else {}
        if isinstance(context_spec, dict):
            max_input = context_spec.get("maxInputTokens") or context_spec.get("max_input_tokens")
            reserve_output = context_spec.get("reserveOutputTokens") or context_spec.get(
                "reserve_output_tokens"
            )
            if max_input is not None:
                request_config["max_input_tokens"] = int(max_input)
            if reserve_output is not None:
                request_config["reserve_output_tokens"] = int(reserve_output)
        if approval_mode:
            request_config["tool_approval_mode"] = normalize_tool_approval_mode(approval_mode)
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

    def _verify_bundle_integrity(
        self,
        bundle_dir: Path,
        *,
        expected_bundle_digest: str = "",
    ) -> None:
        """加载前校验 bundle 未被篡改：manifest 自身摘要、与 Build 记录一致、
        文件清单无增删、每个文件 sha256/size 匹配。"""
        try:
            manifest = BundleManifest.model_validate_json(
                (bundle_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise StudioError(
                "BUILD_ARTIFACT_INVALID",
                "Build 缺少有效的 Bundle Manifest",
                status_code=500,
            ) from exc
        if manifest.bundle_digest != compute_bundle_digest(manifest):
            raise StudioError(
                "BUILD_ARTIFACT_INVALID",
                "Bundle Manifest 摘要不匹配，Bundle 可能已被篡改",
                status_code=500,
                details={"bundleDigest": manifest.bundle_digest},
            )
        if expected_bundle_digest and manifest.bundle_digest != expected_bundle_digest:
            raise StudioError(
                "BUILD_ARTIFACT_INVALID",
                "Bundle 与 Build 记录的摘要不一致，Bundle 可能已被篡改",
                status_code=500,
                details={
                    "expected": expected_bundle_digest,
                    "actual": manifest.bundle_digest,
                },
            )
        declared = {entry.path for entry in manifest.files}
        actual: dict[str, Path] = {}
        for path in bundle_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(bundle_dir).as_posix()
            if relative in self._BUNDLE_META_FILES:
                continue
            actual[relative] = path
        extra = sorted(set(actual) - declared)
        if extra:
            raise StudioError(
                "BUILD_ARTIFACT_INVALID",
                "Bundle 含未在 Manifest 中声明的文件",
                status_code=500,
                details={"extraFiles": extra},
            )
        for entry in manifest.files:
            file_path = actual.get(entry.path)
            if file_path is None:
                raise StudioError(
                    "BUILD_ARTIFACT_INVALID",
                    "Bundle 缺少 Manifest 声明的文件",
                    status_code=500,
                    details={"missingFile": entry.path},
                )
            content = file_path.read_bytes()
            actual_sha = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if actual_sha != entry.sha256:
                raise StudioError(
                    "BUILD_ARTIFACT_INVALID",
                    "Bundle 文件摘要不匹配",
                    status_code=500,
                    details={
                        "path": entry.path,
                        "expected": entry.sha256,
                        "actual": actual_sha,
                    },
                )
            if len(content) != entry.size:
                raise StudioError(
                    "BUILD_ARTIFACT_INVALID",
                    "Bundle 文件大小不匹配",
                    status_code=500,
                    details={
                        "path": entry.path,
                        "expected": entry.size,
                        "actual": len(content),
                    },
                )


__all__ = ["FrameworkRunSpecResolver"]


def _resolved_memory_enabled(resolved: Any) -> bool:
    memory = resolved.get("memory") if isinstance(resolved, dict) else {}
    return bool(memory.get("enabled", False)) if isinstance(memory, dict) else False


    memory = resolved.get("memory") if isinstance(resolved, dict) else {}
    if not isinstance(memory, dict) or not memory.get("enabled", False):
        return False
    recall = memory.get("recall", {})
    return bool(recall.get("enabled", True)) if isinstance(recall, dict) else True


def _resolved_memory_write_mode(resolved: Any) -> str:
    memory = resolved.get("memory") if isinstance(resolved, dict) else {}
    write = memory.get("write", {}) if isinstance(memory, dict) else {}
    return (
        str(write.get("mode", "candidate") or "candidate")
        if isinstance(write, dict)
        else "candidate"
    )


def _resolved_memory_flush_before_compaction(resolved: Any) -> bool:
    memory = resolved.get("memory") if isinstance(resolved, dict) else {}
    write = memory.get("write", {}) if isinstance(memory, dict) else {}
    return bool(write.get("flushBeforeCompaction", True)) if isinstance(write, dict) else True


def _resolved_memory_provider_ref(resolved: Any) -> str:
    memory = resolved.get("memory") if isinstance(resolved, dict) else {}
    return (
        str(memory.get("providerRef", "local-default") or "local-default")
        if isinstance(memory, dict)
        else "local-default"
    )
