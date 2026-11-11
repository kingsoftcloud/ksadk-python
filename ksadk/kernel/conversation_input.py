"""Reuse conversation preparation without admitting client-owned execution state."""

from collections.abc import Mapping

from ksadk.runtime.adapter import ConversationPreprocessingRequest
from ksadk.runtime_context import TRUSTED_IDENTITY_METADATA_KEY


def conversation_input(command, *, invocation_identity, invocation_id):
    options = command.payload.get("runtime_options")
    raw = options.get("conversation") if isinstance(options, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    content = command.payload.get("content")
    messages = (
        list(content) if isinstance(content, list) and all(isinstance(row, dict) for row in content)
        else [{"role": "user", "content": content}]
    )
    # Never copy prepared_turn, thread/checkpoint references, input messages or
    # an arbitrary StartRequest from a wire request. Preparation and identity
    # still run inside the same Worker and the existing model-policy pipeline.
    fields = {
        key: raw[key] for key in (
            "model_metadata", "model_options", "instructions", "custom_metadata", "account_id",
        ) if key in raw and raw[key] is not None
    }
    metadata = raw.get("request_metadata")
    metadata = {
        key: metadata[key] for key in (
            "agentengine", "tool_approval_mode", "previous_response_id", "responses_conversation",
            "transport", "prompt_cache_key", "safety_identifier",
        ) if isinstance(metadata, Mapping) and key in metadata
    }
    metadata["invocation_id"] = invocation_id
    if isinstance(invocation_identity, Mapping):
        metadata[TRUSTED_IDENTITY_METADATA_KEY] = dict(invocation_identity)
    return ConversationPreprocessingRequest(
        messages=messages, request_metadata=metadata, **fields,
    ).model_dump(mode="json", exclude_none=True)
