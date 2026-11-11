"""Pure selection of the next bounded tool group, shared with Hook admission."""

from __future__ import annotations

from .call_budget import call_room, remaining_calls
from .ports import RunConfiguration
from .state import RunSnapshot
from .tool_error_recovery import guarded_action


def scheduled_tool_calls(snapshot: RunSnapshot, config: RunConfiguration) -> tuple[dict, ...]:
    if not snapshot.pending_tools:
        return ()
    definitions = {tool.name: tool for tool in config.tools}
    remaining = remaining_calls(config.limits.max_tool_calls, snapshot.tool_budget_claimed)
    available = len(snapshot.pending_tools) if remaining is None else remaining
    parallel, delegated = [], None
    for call in snapshot.pending_tools[: min(available, config.limits.max_parallel_tools)]:
        definition = definitions.get(call["name"])
        if definition is None or not definition.concurrent_safe or guarded_action(snapshot, call):
            break
        if delegated is not None and delegated != definition.delegated_read_only:
            break
        delegated = definition.delegated_read_only
        parallel.append(call)
    if len(parallel) > 1:
        if not delegated or call_room(
            config.limits.max_model_calls, snapshot.model_budget_claimed,
            max(config.limits.finalization_reserve_model_calls, 1) + len(parallel),
        ):
            return tuple(parallel)
    return snapshot.pending_tools[:1]
