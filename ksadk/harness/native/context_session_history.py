"""Reuse a terminal turn's sealed model view; never replace canonical history."""

from typing import TYPE_CHECKING

from pydantic import JsonValue

from .context import _tool_groups
from .context_runtime_contracts import (
    CompactionHistory,
    SessionSummarySource,
    _refs,
    source_context_digest,
)
from .effective_configuration import effective_run_configuration
from .history import _source
from .ports import RunConfiguration, digest
from .state import RunPhase, RunSnapshot

if TYPE_CHECKING:
    from .context_driver import ContextDriver


def inherit_session_summary(
    parent: RunSnapshot | None, destination: RunSnapshot, configuration: RunConfiguration,
    driver: "ContextDriver", *, imported_count: int,
) -> dict[str, JsonValue] | None:
    """Return an optional source-bound projection for an ordinary new Run.

    Build/protocol mismatches fall back to the complete imported transcript;
    malformed matching retained proofs fail closed before creating the Run.
    This is not task continuation: budgets, evidence and current control stay fresh.
    Opaque provider replay and derived-only sealed prefixes need their own
    remapping proofs and deliberately take that full-history path for now.
    """
    planner = driver.planner
    if (parent is None or planner is None
            or planner.session_history_projection != "sealed-session-summary/v1"
            or parent.phase != RunPhase.COMPLETED or parent.pending_intent is not None
            or parent.profile_digest != configuration.profile.digest
            or parent.configuration_digest != configuration.digest
            or parent.context_binding != destination.context_binding
            or configuration.profile.provider_replay_projection_policy is not None):
        return None
    history = CompactionHistory.model_validate(parent.compaction_history)
    if not history.has_projection:
        return None
    if any(message.get("provider_replay") is not None or message.get("kind") in {
        "incomplete_tool_observation",
    } for message in (*parent.messages, *destination.messages)):
        return None
    # A hook may have sealed a derived model view rather than this canonical
    # prefix. Do not infer a mapping from similar text or alter that seal.
    prefix = parent.messages[:history.prefix_message_count]
    if (len(prefix) != history.prefix_message_count
            or source_context_digest(prefix) != history.prefix_digest):
        return None
    parent_configuration = effective_run_configuration(configuration, parent)
    selected_refs, active, _goal_ref = planner._active(
        parent.messages, driver.continuity(parent, parent_configuration), history,
    )
    _groups, pending, _call_ids = _tool_groups(
        planner._metadata(selected_refs, active, profile=configuration.profile),
    )
    if pending:
        return None
    # Resolve retained messages by host provenance, not substring matching.
    # Complete protocol batches selected by the planner keep both sides, and
    # derived historical observations from a retained receipt remain quoted.
    active_refs = set(selected_refs)
    sources = {
        (source["source_run_id"], source["source_message_index"])
        for index, (ref, message) in enumerate(zip(_refs(parent.messages), parent.messages))
        if ref in active_refs
        for source in (_source(message, parent, index),)
    }
    sources.add((parent.run_id, len(parent.messages)))  # Appended terminal final text.
    # The new goal can be a document-backed multi-message input. Its exact
    # history boundary is carried by start(), not inferred from message roles.
    imported = destination.messages[:imported_count]
    retained = tuple(
        ref for ref, message in zip(_refs(imported), imported)
        if (message.get("source_run_id"), message.get("source_message_index")) in sources
    )
    return CompactionHistory(
        inherited_summary_source=SessionSummarySource(
            run_id=parent.run_id, revision=parent.revision, snapshot_digest=digest(parent),
        ),
        prefix_message_count=len(imported), prefix_digest=source_context_digest(imported),
        contract_digest=destination.contract_digest, retained_message_refs=retained,
        summary_text=history.summary_text, tokenizer_id=history.tokenizer_id,
        last_workspace_revision=history.last_workspace_revision,
        summary_completion_policy=history.summary_completion_policy,
        summary_prompt_policy=history.summary_prompt_policy,
        compaction_message_projection=history.compaction_message_projection,
        summary_output_limited=history.summary_output_limited,
    ).model_dump(mode="json")
