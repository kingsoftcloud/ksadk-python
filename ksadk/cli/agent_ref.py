"""Agent reference resolution helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ResolvedAgentRef:
    value: str
    source: str
    source_path: Optional[Path] = None

    @property
    def source_text(self) -> str:
        if self.source == "cli":
            return "命令行参数"
        if self.source == "state.agent_id":
            return f"{self._path_name()} 的 agent_id"
        if self.source == "state.name":
            return f"{self._path_name()} 的 name"
        if self.source == "config.name":
            return f"{self._path_name()} 的 name"
        return self.source

    def _path_name(self) -> str:
        if self.source_path:
            return self.source_path.name
        return "本地文件"


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
    if explicit:
        return ResolvedAgentRef(value=explicit, source="cli")

    root = cwd or Path(".")

    if include_state:
        state_path = root / ".agentengine.state"
        state_data = _read_yaml_dict(state_path)
        if state_data:
            state_agent_id = _normalize(state_data.get("agent_id"))
            if state_agent_id:
                return ResolvedAgentRef(
                    value=state_agent_id,
                    source="state.agent_id",
                    source_path=state_path,
                )
            state_name = _normalize(state_data.get("name"))
            if state_name:
                return ResolvedAgentRef(
                    value=state_name,
                    source="state.name",
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
                return ResolvedAgentRef(
                    value=name,
                    source="config.name",
                    source_path=path,
                )

    return None


def _normalize(value: Optional[object]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


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

