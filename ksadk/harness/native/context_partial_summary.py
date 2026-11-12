"""Commit acknowledged summary coverage while retaining every uncovered unit.

The pending quote binds the cumulative, previously completed summary and its
coverage in one durable request. Its current response/continuation is never
used here. This is a context projection, not semantic or task completion.
"""

import json

from .context_history import ContextRuntimeError
from .context_partition import source_manifest

PARTIAL_SUMMARY_POLICY = "completed-prefix-retain-suffix/v1"


def commit_completed_prefix(
    controller, request, *, current_revision, current_continuity, current_messages,
):
    controller._validate_source(request, current_revision, current_continuity, current_messages)
    state = request.summary_partition
    if state is None or not state.accepted or state.pending is None or not state.covered_count:
        return None
    manifest, raw = source_manifest(
        current_messages, request.old_messages, request.retained_message_refs,
        capture_reader_names=request.source_capture_reader_names,
        search_fragment_policy=request.search_fragment_policy,
        capture_search_names=request.source_capture_search_names,
        fresh_tool_observation_policy=request.fresh_tool_observation_policy,
        fresh_tool_message_refs=request.fresh_tool_message_refs,
        fresh_text_fragment_policy=request.fresh_text_fragment_policy,
        fresh_text_excluded_tool_names=request.source_reader_names,
    )
    covered = set(manifest["dropped"][:state.covered_count])
    # A receipt or captured-source cursor may stop within a complete executed
    # batch. Retain the entire original batch, including already quoted parts.
    for batch in manifest["batches"].values():
        members = set(batch["refs"])
        if members.intersection(covered) and not members.issubset(covered):
            covered.difference_update(members)
    if not covered:
        return None
    messages = controller._base_summary_messages(request)
    if len(messages) != 1:
        raise ContextRuntimeError("compaction_partial_summary_unbound")
    content = messages[0]["content"]
    text = content[0]["text"] if isinstance(content, list) else content
    # _validate_source verifies this exact pending projection digest, source
    # manifest, accepted cursor and quote. Never accept caller-supplied text.
    body = json.loads(text)
    summary = body.get("previous_summary")
    controller._validate_summary(request, summary)
    refs = tuple(item.message_id for item in request.old_messages if item.message_id not in covered)
    partial = request.model_copy(update={
        "retained_message_refs": refs,
        "retained_messages": tuple(raw[ref] for ref in refs),
    })
    # This does not acknowledge the pending quote or relax require_complete.
    # The ordinary seal path remains full-coverage-only. Both paths validate
    # original continuity, complete tool protocol and the unchanged window.
    return controller._seal_projection(partial, summary, current_continuity, current_messages)
