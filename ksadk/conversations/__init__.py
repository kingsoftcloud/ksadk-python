"""Side-effect-free public exports for conversation preparation and persistence."""

from __future__ import annotations

from importlib import import_module
from typing import Any


def _exports(module: str, *names: str) -> dict[str, tuple[str, str]]:
    return {name: (module, name) for name in names}


_EXPORTS = {
    **_exports(
        "ksadk.conversations.contracts",
        "ConversationCapability",
        "ConversationInput",
        "ConversationAttachmentPart",
        "ConversationItem",
        "ConversationSurface",
        "ConversationTextPart",
        "validate_conversation_input",
        "validate_surface_input",
    ),
    **_exports(
        "ksadk.conversations.projector",
        "project_conversation_item",
    ),
    **_exports(
        "ksadk.conversations.reducer",
        "ConversationItemReducer",
    ),
    **_exports(
        "ksadk.conversations.attachments",
        "decode_inline_data",
        "resolve_attachment_storage_path",
        "resolve_uploads_dir",
    ),
    **_exports(
        "ksadk.conversations.context",
        "CANONICAL_EVENT_TYPES",
        "build_history_from_events",
        "build_request_history",
        "canonical_event_type",
        "compacted_until_seq_id",
        "extract_event_text",
        "extract_text_from_event_parts",
        "group_events_by_api_round",
        "project_model_messages",
        "summarize_event_groups",
    ),
    **_exports(
        "ksadk.conversations.normalize",
        "attachment_from_part",
        "attachment_prompt_text",
        "compact_attachment_for_session",
        "display_content_from_parts",
        "extract_user_input_from_parts",
        "normalize_kop_message_content",
        "normalize_kop_messages",
        "normalize_parts_content",
        "normalize_responses_input",
    ),
    **_exports(
        "ksadk.conversations.runtime",
        "CompactionPlan",
        "PreparedConversationTurn",
        "append_context_checkpoint_event",
        "append_conversation_event",
        "append_reasoning_event",
        "append_run_checkpoint_event",
        "append_run_resume_event",
        "append_run_status_event",
        "build_chat_completions_payload",
        "build_compaction_sse_event",
        "build_responses_payload",
        "build_run_input",
        "compact_conversation_history",
        "ensure_conversation_session",
        "extract_responses_resume_input",
        "invoke_conversation_once",
        "preview_auto_compaction",
        "prime_session_metadata_for_user_turn",
        "stream_conversation_turn",
        "stream_responses_conversation_turn",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name == "runtime":
        value = import_module("ksadk.conversations.runtime")
        globals()[name] = value
        return value
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
