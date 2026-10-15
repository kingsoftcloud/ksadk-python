"""Repartition original history after a sealed summary exceeds its task window."""

from .context import ExecutionContinuityState
from .context_runtime import CompactionHistory, CompactionRequest, ContextPlanner
from .delivery_review import delivery_review_messages
from .ports import RunConfiguration
from .state import RunSnapshot
from .tool_budget import model_instructions, model_tool_catalog, model_turn_control

SUMMARY_REDUCTION_POLICY = "original-history-summary-reduction/v1"


def replan_summary_projection(
    planner: ContextPlanner,
    snapshot: RunSnapshot,
    updated: RunSnapshot,
    config: RunConfiguration,
    continuity: ExecutionContinuityState,
) -> RunSnapshot | None:
    # A second failure of the reduced selection must block. Model calls still
    # consume the original Run budget; this is not an extra retry allowance.
    if snapshot.force_compaction or snapshot.compaction_request is None:
        return None
    previous = CompactionRequest.model_validate(snapshot.compaction_request)
    plan = planner.plan(
        source_revision=snapshot.revision,
        model_turn=updated.model_calls,
        profile=config.profile,
        instructions=model_instructions(snapshot, config),
        tools=model_tool_catalog(snapshot, config),
        context_tools=config.tools,
        messages=delivery_review_messages(snapshot),
        continuity=continuity,
        contract=config.contract,
        history=CompactionHistory.model_validate(snapshot.compaction_history),
        force_compaction=True,
        turn_control=model_turn_control(snapshot, config),
    )
    if plan.action != "compact":
        return None
    request = plan.request
    # Every newly demoted complete group is quoted from the original history
    # by the normal coverage planner. Do not drop it from an already sealed
    # summary, alter source bytes, or repeat an identical selection.
    if (
        request.source_context_digest != previous.source_context_digest
        or request.continuity != previous.continuity
        or request.profile != previous.profile
        or not set(request.retained_message_refs) < set(previous.retained_message_refs)
    ):
        return None
    return updated.model_copy(
        update={
            "force_compaction": True,
            "compaction_request": request.model_dump(mode="json"),
            "prepared_context": None,
            "model_errors": 0,
            "provider_errors": 0,
        }
    )
