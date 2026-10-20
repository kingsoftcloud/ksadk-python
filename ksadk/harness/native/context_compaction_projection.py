"""Pure validation and history construction for a covered compaction projection."""

from __future__ import annotations

from .context import (
    CompactionEnvelope,
    CompactionRejected,
    ExecutionContinuityState,
    continuity_digest,
    validate_compaction,
)
from .context_history import ContextRuntimeError
from .context_replay import compaction_replay_view
from .context_runtime_contracts import (
    CompactionHistory,
    CompactionProjectionOverflow,
    CompactionRequest,
    CompactionResult,
    Messages,
    _compacted_messages,
    _refs,
    _resume,
    _summary,
)


def seal_compaction_projection(
    planner,
    request: CompactionRequest,
    summary_text: str,
    current_continuity: ExecutionContinuityState,
    current_messages: Messages,
) -> CompactionResult:
    """Validate an authorized retained projection; callers prove coverage first."""
    original = _seal_retained_projection(
        planner, request, summary_text, current_continuity, current_messages,
    )
    if not request.post_summary_read_refs:
        return original
    # Coverage was proved with the original retained set. The source batch is
    # still summarized in full; retaining it as well never acknowledges missing
    # partitions or changes the immutable summary request/its source manifest.
    selected = set(request.retained_message_refs) | set(request.post_summary_read_refs)
    refs = _refs(current_messages)
    extended = request.model_copy(update={
        "retained_message_refs": tuple(ref for ref in refs if ref in selected),
        "retained_messages": tuple(
            raw for ref, raw in zip(refs, current_messages) if ref in selected
        ),
    })
    try:
        return _seal_retained_projection(
            planner, extended, summary_text, current_continuity, current_messages,
        )
    except CompactionProjectionOverflow:
        # The declared reserve was conservative, not free capacity. Keep the
        # original validated result whenever the actual complete batch cannot
        # fit; never shrink output, rewrite a receipt, or request another summary.
        return original


def _seal_retained_projection(
    planner, request, summary_text, current_continuity, current_messages,
) -> CompactionResult:
    raw_by_ref = dict(zip(_refs(current_messages), current_messages))
    retained = tuple(raw_by_ref[ref] for ref in request.retained_message_refs)
    if retained != request.retained_messages:
        raise ContextRuntimeError("context_source_changed")
    read_view = planner._read_view(
        retained,
        frozenset(request.source_reader_names),
        frozenset(request.protected_reader_call_ids),
    )
    read_view = compaction_replay_view(
        read_view, request.retained_message_refs,
        covered_refs=request.history.retained_message_refs, profile=request.profile,
        root_prefix_digest=request.history.prefix_digest,
    )
    new_metadata = planner._metadata(
        request.retained_message_refs,
        retained,
        counted_messages=read_view,
        profile=request.profile,
    )
    envelope = CompactionEnvelope(
        source_revision=request.source_revision,
        continuity=current_continuity,
        continuity_digest=continuity_digest(current_continuity),
        summary_text=summary_text,
        summary_token_count=(
            planner._count_message(_summary(summary_text))
            + (planner._count_message(_resume())
               if planner.compaction_message_projection is None else 0)
        ),
        preserved_message_refs=request.retained_message_refs,
    )
    try:
        projection = validate_compaction(
            source_revision=request.source_revision,
            continuity=current_continuity,
            old_messages=request.old_messages,
            new_messages=new_metadata,
            envelope=envelope,
            budget=request.budget,
        )
    except CompactionRejected as exc:
        if str(exc) == "context_window_exceeded":
            raise CompactionProjectionOverflow("context_compaction_validation_failed") from exc
        raise ContextRuntimeError("context_compaction_validation_failed") from exc
    # Validate unchanged source identities/costs under the previous root
    # first. The committed rewrite then starts a new dispatch-only view.
    read_view = compaction_replay_view(
        read_view, request.retained_message_refs,
        covered_refs=request.retained_message_refs, profile=request.profile,
        root_prefix_digest=request.source_context_digest,
    )
    prepared = planner._prepared(
        request.instructions,
        _compacted_messages(
            summary_text, read_view, current_continuity.goal,
            projection=planner.compaction_message_projection,
        ),
        request.budget.fixed_tokens,
        request.turn_control,
        profile=request.profile,
        goal=current_continuity.goal,
        abandoned=request.history.abandoned_replay_digests,
        replay_header_digest=request.replay_header_digest,
    )
    if prepared.counted_input_tokens + request.profile.max_output_tokens > (
        request.profile.context_window
    ):
        raise CompactionProjectionOverflow("context_compaction_validation_failed")
    history = CompactionHistory(
        abandoned_replay_digests=prepared.abandoned_replay_digests,
        prior_compactions=request.history.prior_compactions,
        count=request.history.count + 1,
        stagnant_count=(
            1
            if request.history.count == 0
            or (
                current_continuity.observed_progress_revision
                > request.history.last_observed_progress_revision
            )
            or (
                current_continuity.workspace_revision is not None
                and current_continuity.workspace_revision
                != request.history.last_workspace_revision
            )
            else request.history.stagnant_count + 1
        ),
        last_workspace_revision=current_continuity.workspace_revision,
        last_observed_progress_revision=current_continuity.observed_progress_revision,
        last_model_turn=request.model_turn,
        prefix_message_count=request.source_message_count,
        prefix_digest=request.source_context_digest,
        contract_digest=current_continuity.contract_digest,
        retained_message_refs=request.retained_message_refs,
        summary_text=summary_text,
        tokenizer_id=planner.tokenizer_id,
        provider_replay_projection_policy=request.profile.provider_replay_projection_policy,
        summary_completion_policy=request.summary_completion_policy,
        compaction_message_projection=request.compaction_message_projection,
        summary_prompt_policy=request.summary_prompt_policy,
        summary_output_limited=request.summary_output_limited,
    )
    return CompactionResult(projection=projection, prepared=prepared, history=history)
