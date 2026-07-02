from __future__ import annotations

from dataclasses import dataclass

from ksadk.runtime_context import get_current_tool_execution_context_or_default


@dataclass
class WorkspaceReadState:
    path: str
    mtime_ns: int
    size_bytes: int
    start_line: int
    end_line: int
    total_lines: int
    partial: bool


_READ_STATE: dict[tuple[str, str], WorkspaceReadState] = {}


def _session_key(session_id: str | None = None) -> str:
    if session_id is not None:
        return str(session_id or "default")
    context = get_current_tool_execution_context_or_default()
    return str(context.session_id or "default")


def record_read_state(state: WorkspaceReadState) -> None:
    _READ_STATE[(_session_key(), state.path)] = state


def get_read_state(path: str) -> WorkspaceReadState | None:
    return _READ_STATE.get((_session_key(), path))


def clear_read_state(session_id: str | None = None) -> None:
    if session_id is None:
        _READ_STATE.clear()
        return
    key = _session_key(session_id)
    for item in list(_READ_STATE):
        if item[0] == key:
            _READ_STATE.pop(item, None)
