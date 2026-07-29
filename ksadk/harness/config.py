"""HarnessApp yaml 配置 — 最小子集 + 严格校验 (goal-08)。

本期硬边界(防膨胀):**只支持最小子集** ``model`` / ``prompt``(instruction)/
``mcp_tools`` / ``sandbox``。超出子集的字段(memory/knowledge/workflow/tracing 等)
**明确报"暂不支持"**,不硬翻、不静默忽略。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml  # type: ignore[import-untyped]

#: 顶层允许的最小子集字段。
_ALLOWED_TOP_LEVEL = frozenset({"model", "prompt", "mcp_tools", "sandbox", "runtime"})
#: runtime 允许的值。
_ALLOWED_RUNTIME = frozenset({"yaml", "codex"})
#: mcp_tools 单项允许字段。
_ALLOWED_MCP_TOOL = frozenset({"name", "url", "api_key", "tool_filter", "tool_name_prefix"})
#: sandbox 允许字段。
_ALLOWED_SANDBOX = frozenset({"read_only"})

#: 已知但本期不支持的字段,给出更明确的指引。
_KNOWN_UNSUPPORTED = {
    "memory": "memory(记忆)暂不支持,后续阶段提供",
    "knowledge": "knowledge(知识库)暂不支持,后续阶段提供",
    "workflow": "workflow(工作流编排)暂不支持,后续阶段提供",
    "tracing": "tracing(可观测)暂不支持,后续阶段提供",
    "skills": "skills(技能)暂不支持,后续阶段提供",
}


class HarnessConfigError(ValueError):
    """HarnessApp yaml 配置错误(含"暂不支持"指引)。"""


@dataclass(frozen=True)
class McpToolSpec:
    """MCP 工具条目(最小子集)。"""

    name: str
    url: str
    api_key: Optional[str] = None
    tool_filter: tuple[str, ...] = ()
    tool_name_prefix: Optional[str] = None


@dataclass(frozen=True)
class SandboxPolicy:
    """sandbox 策略。默认 read-only(goal-08:sandbox 默认 read-only)。"""

    read_only: bool = True


@dataclass(frozen=True)
class HarnessConfig:
    """HarnessApp 的最小 yaml 配置。"""

    model: str
    prompt: str
    mcp_tools: tuple[McpToolSpec, ...] = ()
    sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)
    runtime: str = "yaml"
    """runtime 后端:``yaml``(YamlAgentRunner+LiteLLM)| ``codex``(CodexRunner+codex CLI)。"""

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "yaml") -> "HarnessConfig":
        if not isinstance(data, dict):
            raise HarnessConfigError(f"{source}: 顶层必须是 mapping,得到 {type(data).__name__}")

        _reject_unknown_keys(data, _ALLOWED_TOP_LEVEL, where=source)

        model = _require_str(data, "model", source)
        prompt = _require_str(data, "prompt", source)

        runtime = str(data.get("runtime", "yaml")).strip().lower()
        if runtime not in _ALLOWED_RUNTIME:
            raise HarnessConfigError(
                f"{source}: runtime 仅支持 {sorted(_ALLOWED_RUNTIME)},得到 {runtime!r}"
            )

        mcp_tools = tuple(
            _parse_mcp_tool(item, index, source)
            for index, item in enumerate(
                _require_list(data.get("mcp_tools", []), "mcp_tools", source)
            )
        )
        sandbox = _parse_sandbox(data.get("sandbox", {}), source)
        return cls(
            model=model, prompt=prompt, mcp_tools=mcp_tools, sandbox=sandbox, runtime=runtime
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HarnessConfig":
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise HarnessConfigError(f"{path}: YAML 解析失败: {exc}") from exc
        if data is None:
            data = {}
        return cls.from_dict(data, source=str(path))


def _reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    for key in data:
        if key in allowed:
            continue
        if key in _KNOWN_UNSUPPORTED:
            raise HarnessConfigError(f"{where}: 字段 {key!r} —— {_KNOWN_UNSUPPORTED[key]}")
        raise HarnessConfigError(
            f"{where}: 未知字段 {key!r};本期最小子集仅支持 {sorted(allowed)},"
            "超出子集的字段不予支持(不静默忽略)"
        )


def _require_str(data: dict[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarnessConfigError(f"{where}: 字段 {key!r} 必填且必须是非空字符串")
    return value.strip()


def _require_list(value: Any, key: str, where: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HarnessConfigError(f"{where}: 字段 {key!r} 必须是 list")
    return value


def _parse_mcp_tool(item: Any, index: int, where: str) -> McpToolSpec:
    if not isinstance(item, dict):
        raise HarnessConfigError(f"{where}: mcp_tools[{index}] 必须是 mapping")
    _reject_unknown_keys(item, _ALLOWED_MCP_TOOL, where=f"{where}.mcp_tools[{index}]")
    name = _require_str(item, "name", f"{where}.mcp_tools[{index}]")
    url = _require_str(item, "url", f"{where}.mcp_tools[{index}]")
    tool_filter = item.get("tool_filter", ())
    if not isinstance(tool_filter, (list, tuple)) or not all(
        isinstance(t, str) for t in tool_filter
    ):
        raise HarnessConfigError(f"{where}.mcp_tools[{index}].tool_filter 必须是字符串 list")
    api_key = item.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise HarnessConfigError(f"{where}.mcp_tools[{index}].api_key 必须是字符串")
    prefix = item.get("tool_name_prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise HarnessConfigError(f"{where}.mcp_tools[{index}].tool_name_prefix 必须是字符串")
    return McpToolSpec(
        name=name,
        url=url,
        api_key=api_key,
        tool_filter=tuple(tool_filter),
        tool_name_prefix=prefix,
    )


def _parse_sandbox(value: Any, where: str) -> SandboxPolicy:
    if value is None:
        return SandboxPolicy()
    if not isinstance(value, dict):
        raise HarnessConfigError(f"{where}: sandbox 必须是 mapping")
    _reject_unknown_keys(value, _ALLOWED_SANDBOX, where=f"{where}.sandbox")
    read_only = value.get("read_only", True)
    if not isinstance(read_only, bool):
        raise HarnessConfigError(f"{where}.sandbox.read_only 必须是 bool")
    if not read_only:
        raise HarnessConfigError(
            f"{where}.sandbox.read_only=false 暂不支持;Harness 当前只提供执行层强制的只读策略"
        )
    return SandboxPolicy(read_only=read_only)


__all__ = [
    "HarnessConfig",
    "HarnessConfigError",
    "McpToolSpec",
    "SandboxPolicy",
]
