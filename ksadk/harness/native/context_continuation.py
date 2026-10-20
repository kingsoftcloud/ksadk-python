"""Rebind a compacted prefix only for authenticated output-chain retirement."""

from pydantic import ValidationError

from .completion_text import abandon_continuation
from .context_runtime_contracts import (
    CompactionHistory,
    ContextRuntimeError,
    _refs,
    source_context_digest,
)


def restart_continuation(snapshot, last_fragment):
    messages = abandon_continuation(snapshot.messages, last_fragment)
    try:
        history = CompactionHistory.model_validate(snapshot.compaction_history)
    except ValidationError as exc:
        raise ContextRuntimeError("context_protocol_invalid") from exc
    if not history.has_projection:
        return messages, snapshot.compaction_history

    # Authenticate the original seal BEFORE changing its source. A restart is
    # not permission to bless edits, a changed goal, or forged retained refs.
    prefix = snapshot.messages[:history.prefix_message_count]
    if (len(prefix) != history.prefix_message_count
            or source_context_digest(prefix) != history.prefix_digest
            or snapshot.contract_digest != history.contract_digest):
        raise ContextRuntimeError("context_history_changed")
    refs = _refs(prefix)
    if (not set(history.retained_message_refs).issubset(refs)
            or len(set(history.retained_message_refs)) != len(history.retained_message_refs)):
        raise ContextRuntimeError("context_history_invalid")

    # abandon_continuation removes only the contiguous response-id/digest
    # authenticated suffix and appends one host restart notice. Compaction may
    # already cover part or all of that suffix. Rebind only the surviving exact
    # prefix; neither the notice nor any unsealed message becomes summarized.
    surviving = len(messages) - 1
    if surviving >= history.prefix_message_count:
        return messages, snapshot.compaction_history
    prefix = messages[:surviving]
    assert prefix == snapshot.messages[:surviving]
    retained = set(_refs(prefix))
    history = history.model_copy(update={
        "prefix_message_count": surviving,
        "prefix_digest": source_context_digest(prefix),
        "retained_message_refs": tuple(
            ref for ref in history.retained_message_refs if ref in retained
        ),
    })
    # Preserve the completed summary and every quota/progress/replay/policy
    # field. The new notice makes clear that the unfinished output did not run.
    return messages, history.model_dump(mode="json")
