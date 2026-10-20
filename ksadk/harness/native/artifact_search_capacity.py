"""Pure quote for complete next requests after a canonical Search group.

No summaries, old receipts or source bytes are changed. Synthetic result shapes
exist only in a detached reducer projection, never as Run facts. The estimate
covers ordinary completion without intervening commands or host changes.
"""

from dataclasses import dataclass

from .artifact_search_contract import ARTIFACT_SEARCH_DEFINITION
from .call_budget import call_room
from .context_runtime_contracts import CompactionHistory
from .delivery_review import delivery_review_messages
from .effective_configuration import effective_run_configuration
from .ports import digest
from .state import RunPhase
from .tool_batch_capacity import declared_source_group, project_source_group
from .tool_budget import model_instructions, model_tool_surface, model_turn_control

SEARCH_CAPACITY_POLICY = "durable-next-search-request/v2"


@dataclass(frozen=True)
class SearchGroupCapacity:
    result_message_units: int
    group_size: int
    pending_count: int
    maximum_fixed_and_history_units: int


def search_group_capacity(snapshot, configuration, driver):
    if (
        snapshot.configuration_digest != configuration.digest
        or snapshot.profile_digest != digest(configuration.profile)
        or snapshot.context_binding != driver.binding
    ):
        raise ValueError("search_configuration_mismatch")
    planner = driver.planner
    if (
        planner is None
        or snapshot.phase != RunPhase.EXECUTING_TOOLS
        or not snapshot.pending_tools
        or not snapshot.pending_intent
        or snapshot.force_compaction
    ):
        return None
    group = declared_source_group(snapshot, "ArtifactSearch")
    if group is None:
        return None
    declared, sealed = group.declared, group.sealed
    config = effective_run_configuration(configuration, snapshot)
    installed = [t for t in config.tools if t.name == "ArtifactSearch"]
    if (
        len(installed) != 1
        or installed[0].model_copy(update={"feedback_profile_digest": None})
        != ARTIFACT_SEARCH_DEFINITION.model_copy(update={"feedback_profile_digest": None})
        or not call_room(config.limits.max_tool_calls,
                         snapshot.tool_calls + snapshot.descendant_tool_calls, len(snapshot.pending_tools))
    ):
        return None
    current = project_source_group(group, config)
    if current is None:
        return None
    pending_ids = {c["call_id"] for c in snapshot.pending_tools}
    history = CompactionHistory.model_validate(current.compaction_history)
    remaining, costs = len(pending_ids) - len(group.active), []
    if remaining <= 0:
        return None
    for progress in range(
        snapshot.observed_progress_revision,
        snapshot.observed_progress_revision + len(pending_ids) + 1,
    ):
        projected = current.model_copy(update={"observed_progress_revision": progress})
        plan = planner.plan(
            source_revision=projected.revision,
            model_turn=projected.model_calls,
            profile=config.profile,
            instructions=model_instructions(projected, config),
            tools=model_tool_surface(projected, config),
            context_tools=config.tools,
            messages=delivery_review_messages(projected),
            continuity=driver.continuity(projected, config),
            contract=config.contract,
            history=history,
            force_compaction=False,
            turn_control=model_turn_control(projected, config),
            allow_compaction=False,
        )
        if plan.action != "ready" or plan.prepared is None:
            return None
        synthetic = [
            m
            for m in plan.prepared.messages
            if m.get("role") == "tool" and m.get("tool_call_id") in pending_ids
        ]
        if len(synthetic) != len(pending_ids):
            return None
        costs.append(
            plan.prepared.counted_input_tokens
            - sum(planner.count_model_message(m, config.profile) for m in synthetic)
        )
    available = config.profile.context_window - config.profile.max_output_tokens - max(costs)
    sealed_cost = sum(planner.count_model_message(m, config.profile) for m in sealed)
    active_cost = sum(planner.count_model_message(m, config.profile) for m in group.active)
    units = min((available - active_cost) // remaining,
                (available + sealed_cost - active_cost) // len(declared["tool_calls"]))
    return SearchGroupCapacity(units, len(declared["tool_calls"]), remaining, max(costs))
