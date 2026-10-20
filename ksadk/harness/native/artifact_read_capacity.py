"""Pure capacity estimates for a declared, canonical artifact read batch.

Estimates are never receipts or facts. They apply only to ordinary completion
of the current read-only group without intervening commands or host changes.
The real ContextPlanner remains the hard admission authority after execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from .artifact_read_contract import ARTIFACT_READ_DEFINITION
from .call_budget import call_room
from .effective_configuration import effective_run_configuration
from .ports import digest
from .state import RunPhase
from .tool_batch_capacity import declared_source_group, project_source_group
from .tool_budget import model_instructions, model_tool_surface, model_turn_control

ARTIFACT_CAPACITY_POLICY = "durable-artifact-batch/v2"


@dataclass(frozen=True)
class ArtifactGroupCapacity:
    """An estimate in the bound planner's units, not persistent execution state."""

    source_units: int
    result_message_units: int
    fixed_tokens: int
    summary_tokens: int
    group_size: int


def _declared_group(snapshot):
    group = declared_source_group(snapshot, "ArtifactRead")
    return (group.declared, group.sealed) if group is not None else None


def artifact_group_capacity(snapshot, configuration, driver):
    """Quote no-command batch completion using the same pure native reducers.

    Receipts, progress digits, capped receipt digests, tool-budget controls and
    real summary framing are remeasured rather than estimated by fixed magic
    constants or by guessing the future operation-key revision width.
    """
    if (snapshot.configuration_digest != configuration.digest
            or snapshot.profile_digest != digest(configuration.profile)
            or snapshot.context_binding != driver.binding):
        raise ValueError("source_read_configuration_mismatch")
    planner = driver.planner
    if (planner is None or not planner.policy.read_working_set_count
            or snapshot.phase != RunPhase.EXECUTING_TOOLS):
        return None
    group = declared_source_group(snapshot, "ArtifactRead")
    if group is None or not snapshot.pending_tools or not snapshot.pending_intent:
        return None
    declared, sealed = group.declared, group.sealed
    # The planner's retained-source count is a bound on source identities,
    # not on the number of atomic call groups. The original declared count
    # is a safe upper bound without pretending overlapping sources are new.
    if len(declared["tool_calls"]) > planner.policy.read_working_set_count:
        return None
    config = effective_run_configuration(configuration, snapshot)

    installed = [t for t in config.tools if t.name == ARTIFACT_READ_DEFINITION.name]
    if (len(installed) != 1 or installed[0].model_copy(update={"feedback_profile_digest": None})
            != ARTIFACT_READ_DEFINITION):
        return None
    if (not call_room(config.limits.max_tool_calls,
                     snapshot.tool_calls + snapshot.descendant_tool_calls, len(snapshot.pending_tools))):
        return None
    current = project_source_group(group, config)
    if current is None:
        return None
    remaining = len(snapshot.pending_tools) - len(group.active)
    if remaining <= 0:
        return None
    fixed = []
    # Novelty can advance anywhere from zero to the remaining call count. A
    # real tokenizer need not assign monotonic costs across numeric digit runs.
    for progress in range(snapshot.observed_progress_revision,
                          snapshot.observed_progress_revision + len(snapshot.pending_tools) + 1):
        projected = current.model_copy(update={"observed_progress_revision": progress})
        fixed.append(planner.fixed_request_cost(
            instructions=model_instructions(projected, config),
            continuity=driver.continuity(projected, config), contract=config.contract,
            tools=model_tool_surface(projected, config),
            turn_control=model_turn_control(projected, config),
        ))
    profile = config.profile
    def count(message):
        return planner.count_model_message(message, profile)

    goal = {"role": "user", "content": snapshot.goal}
    # Existing sealed receipts are still in this original group. They cannot
    # disappear from the denominator or be silently charged as older history.
    size = len(declared["tool_calls"])
    available = (profile.context_window - profile.max_output_tokens - max(fixed)
                 - count(goal) - count(declared)
                 - sum(count(m) for m in snapshot.messages if m.get("role") == "system"))
    results = min(available - planner.summary_reservation(profile),
                  planner.policy.read_working_set_max_tokens - count(declared),
                  planner.policy.read_working_set_total_max_tokens - count(declared))
    result_units = min(results // size,
                       (results - sum(count(m) for m in sealed)) // remaining)
    return ArtifactGroupCapacity(
        source_units=available // 2 // size, result_message_units=result_units,
        fixed_tokens=max(fixed), summary_tokens=planner.summary_reservation(profile),
        group_size=size,
    )
