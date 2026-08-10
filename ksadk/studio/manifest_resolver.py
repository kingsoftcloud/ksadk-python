"""ManifestResolver —— 统一识别工作区根 Manifest 的种类（方案 §6.1）。

当前问题（方案 §2.4 第 1 点）：标准 LangGraph/ADK 项目作为 Studio workspace 启动后，根
``agentengine.yaml`` 可能先进入 Codex Manifest 解析，导致 ``CODEX_MANIFEST_INVALID``。

本模块提供统一入口：根 manifest 存在时先读 ``framework`` / ``runtime.type`` 字段决定 kind，
只有明确 ``framework: codex`` 才走 ``CodexAgentManifest`` 解析；明确为 ADK/LangGraph 等框架时
返回 framework kind（交给 ``FrameworkDetector`` 与标准 Code 项目导入）；无法判定时返回
``MANIFEST_KIND_AMBIGUOUS``，列出候选，不猜测为 Codex。

解析顺序（方案 §6.1）：
1. 根 manifest 不存在 → ``none``
2. 显式 ``framework: codex`` 或 ``runtime.name: codex`` → ``codex``
3. 显式 ``framework: adk|langgraph|...`` 或 ``runtime.type: adk|langgraph|...`` → ``framework``
4. 仍无法判定 → ``ambiguous``（列出已读到的关键字段，便于诊断）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

ManifestKind = Literal["none", "codex", "framework", "ambiguous"]

# framework/runtime 字段的已知 codex / framework 取值。
_CODEX_FRAMEWORK_VALUES = frozenset({"codex"})
_CODEX_RUNTIME_VALUES = frozenset({"codex"})
_FRAMEWORK_VALUES = frozenset({"adk", "langgraph", "langchain", "deepagents", "hermes", "openclaw"})


@dataclass(frozen=True)
class ManifestKindResult:
    """根 manifest 识别结果。"""

    kind: ManifestKind
    path: Path
    framework: str = ""
    runtime_type: str = ""
    artifact_type: str = ""
    # ambiguous 时列出已读字段，供诊断与错误返回。
    detected_fields: dict[str, Any] = field(default_factory=dict)

    @property
    def is_codex(self) -> bool:
        return self.kind == "codex"


def _safe_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            payload = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def detect_manifest_kind(workspace_root: Path | str) -> ManifestKindResult:
    """识别工作区根 manifest 的种类（方案 §6.1）。

    ``workspace_root`` 指工作区目录；本函数查其下的 ``agentengine.yaml``（不存在时返回 ``none``）。
    """
    root = Path(workspace_root)
    path = root / "agentengine.yaml"
    if not path.is_file():
        return ManifestKindResult(kind="none", path=path)

    payload = _safe_yaml(path)
    if not payload:
        # 文件存在但无法解析为 dict → ambiguous（不猜测为 codex）
        return ManifestKindResult(kind="ambiguous", path=path, detected_fields={"unparsable": True})

    framework = str(payload.get("framework") or "").strip().lower()
    runtime = payload.get("runtime") or {}
    runtime_type = (
        str(runtime.get("type") or runtime.get("name") or "").strip().lower()
        if isinstance(runtime, dict)
        else ""
    )
    artifact_type = str(payload.get("artifact_type") or "").strip().lower()
    detected = {
        "framework": framework,
        "runtimeType": runtime_type,
        "artifactType": artifact_type,
        "topLevelKeys": sorted(payload.keys()),
    }

    # 2. 显式 codex
    if framework in _CODEX_FRAMEWORK_VALUES or runtime_type in _CODEX_RUNTIME_VALUES:
        return ManifestKindResult(
            kind="codex",
            path=path,
            framework=framework,
            runtime_type=runtime_type,
            artifact_type=artifact_type,
        )
    # 3. 显式 framework
    if framework in _FRAMEWORK_VALUES or runtime_type in _FRAMEWORK_VALUES:
        return ManifestKindResult(
            kind="framework",
            path=path,
            framework=framework or runtime_type,
            runtime_type=runtime_type,
            artifact_type=artifact_type,
        )
    # 4. 无法判定（例如只有 name/version 但无 framework/runtime）
    return ManifestKindResult(
        kind="ambiguous",
        path=path,
        framework=framework,
        runtime_type=runtime_type,
        artifact_type=artifact_type,
        detected_fields=detected,
    )


def root_manifest_is_codex(workspace_root: Path | str) -> bool:
    """便捷判定：根 manifest 是否应走 Codex 解析（方案 §6.1）。"""
    return detect_manifest_kind(workspace_root).is_codex


__all__ = [
    "ManifestKind",
    "ManifestKindResult",
    "detect_manifest_kind",
    "root_manifest_is_codex",
]
