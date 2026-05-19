"""Resource reference resolution helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ResolvedResourceRef:
    value: str
    source: str
    resource: str = "agent"
    source_path: Optional[Path] = None

    @property
    def source_text(self) -> str:
        if self.source == "cli":
            return "命令行参数"
        if self.source.startswith("state."):
            field = self.source.split(".", 1)[1]
            return f"{self._path_name()} 的 {field}"
        if self.source.startswith("config."):
            field = self.source.split(".", 1)[1]
            return f"{self._path_name()} 的 {field}"
        return self.source

    def _path_name(self) -> str:
        if self.source_path:
            return self.source_path.name
        return "本地文件"


ResolvedAgentRef = ResolvedResourceRef


def merge_agent_inputs(
    *,
    agent_option: Optional[str] = None,
    positional_agent: Optional[str] = None,
    legacy_name: Optional[str] = None,
) -> Optional[str]:
    """Merge all agent inputs and detect conflicts."""
    candidates = [
        ("--agent", _normalize(agent_option)),
        ("位置参数", _normalize(positional_agent)),
        ("--name", _normalize(legacy_name)),
    ]
    values = [(label, value) for label, value in candidates if value]
    if not values:
        return None

    first_value = values[0][1]
    for _, value in values[1:]:
        if value != first_value:
            labels = " / ".join(label for label, _ in values)
            raise ValueError(f"检测到多个不同的 Agent 参数来源: {labels}，请只保留一个")
    return first_value


def resolve_agent_ref(
    explicit: Optional[str],
    *,
    cwd: Optional[Path] = None,
    include_state: bool = True,
    include_project_config: bool = True,
) -> Optional[ResolvedAgentRef]:
    """Resolve agent reference from CLI input and local files."""
    return resolve_resource_ref(
        explicit,
        resource="agent",
        cwd=cwd,
        include_state=include_state,
        include_project_config=include_project_config,
    )


def resolve_resource_ref(
    explicit: Optional[str],
    *,
    resource: str,
    cwd: Optional[Path] = None,
    include_state: bool = True,
    include_project_config: bool = False,
) -> Optional[ResolvedResourceRef]:
    """Resolve a resource reference from CLI input and local files."""
    if explicit:
        return ResolvedResourceRef(value=explicit, source="cli", resource=resource)

    root = cwd or Path(".")

    if include_state:
        state_path = root / ".agentengine.state"
        state_data = _read_yaml_dict(state_path)
        if state_data:
            for field_name, value in _state_candidates(state_data, resource):
                return ResolvedResourceRef(
                    value=value,
                    source=f"state.{field_name}",
                    resource=resource,
                    source_path=state_path,
                )

    if include_project_config:
        for file_name in ("agentengine.yaml", "ksadk.yaml"):
            path = root / file_name
            data = _read_yaml_dict(path)
            if not data:
                continue
            name = _normalize(data.get("name"))
            if name:
                return ResolvedResourceRef(
                    value=name,
                    source="config.name",
                    resource=resource,
                    source_path=path,
                )

    return None


def resolve_mcp_ref(
    explicit: Optional[str],
    *,
    cwd: Optional[Path] = None,
    include_state: bool = True,
) -> Optional[ResolvedResourceRef]:
    return resolve_resource_ref(
        explicit,
        resource="mcp",
        cwd=cwd,
        include_state=include_state,
        include_project_config=False,
    )


def resolve_openclaw_ref(
    explicit: Optional[str],
    *,
    cwd: Optional[Path] = None,
    include_state: bool = True,
) -> Optional[ResolvedResourceRef]:
    return resolve_resource_ref(
        explicit,
        resource="openclaw",
        cwd=cwd,
        include_state=include_state,
        include_project_config=False,
    )


def _normalize(value: Optional[object]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


def _state_candidates(state_data: dict, resource: str) -> list[tuple[str, str]]:
    state_type = _normalize(state_data.get("type"))

    if resource == "agent":
        if state_type == "mcp":
            return []
        return _pick_fields(state_data, ("agent_id", "name"))

    if resource == "mcp":
        if state_type != "mcp":
            return []
        return _pick_fields(state_data, ("mcp_id", "name"))

    if resource == "openclaw":
        if state_type != "openclaw":
            return []
        return _pick_fields(state_data, ("agent_id", "name"))

    return []


def _pick_fields(state_data: dict, fields: tuple[str, ...]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for field in fields:
        value = _normalize(state_data.get(field))
        if value:
            values.append((field, value))
    return values


def _read_yaml_dict(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        import yaml

        with open(path, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None
