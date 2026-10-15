"""Private model protocol projection; no I/O, task verdicts or public text."""
from __future__ import annotations

import copy
import json

from ksadk._model_anthropic import anthropic_content, anthropic_projection
from ksadk._model_json import load_model_json

from .model import ModelProfile, ModelReplayMetadata, ModelTurnResult


def replay_binding_failure(
    profile: ModelProfile, replay: ModelReplayMetadata | None, *, required: bool
) -> str | None:
    if profile.provider_replay_mode != "none" and required and replay is None:
        return "model_replay_unavailable"
    if replay is not None and (
        profile.provider_replay_mode == "none" or replay.profile_digest != profile.digest
        or (profile.provider_replay_mode == "anthropic_content")
        != (replay.anthropic_content_json is not None)
    ):
        return "model_replay_profile_mismatch"
    return None


def seal_model_replay(
    result: ModelTurnResult, profile: ModelProfile, reasoning_content: str
) -> ModelTurnResult:
    if profile.provider_replay_mode != "reasoning_content" or result.response_kind not in {
        "tool_calls", "final_candidate", "output_limit",
    }:
        return result
    replay = ModelReplayMetadata(
        profile_digest=profile.digest, reasoning_content=reasoning_content
    )
    return ModelTurnResult.model_validate({
        **result.model_dump(), "provider_replay": replay.model_dump(),
    })


def seal_anthropic_replay(
    result: ModelTurnResult, profile: ModelProfile, content: object,
) -> ModelTurnResult:
    if profile.provider_replay_mode != "anthropic_content" or result.response_kind not in {
        "tool_calls", "final_candidate", "output_limit",
    }:
        return result
    blocks = anthropic_content(content)
    replay = ModelReplayMetadata(
        version="model-replay/v2", profile_digest=profile.digest, reasoning_content="",
        anthropic_content_json=json.dumps(blocks, ensure_ascii=False, allow_nan=False,
                                          separators=(",", ":")),
    )
    return ModelTurnResult.model_validate({
        **result.model_dump(), "provider_replay": replay.model_dump(),
    })


def replay_fields(replay: ModelReplayMetadata | None) -> dict:
    return {"provider_replay": replay.model_dump(mode="json")} if replay is not None else {}


def remap_anthropic_replay(
    replay: ModelReplayMetadata | None, identities: dict[str, str],
) -> ModelReplayMetadata | None:
    if replay is None or replay.anthropic_content_json is None:
        return replay
    _, calls = anthropic_projection(load_model_json(replay.anthropic_content_json))
    previous = dict(replay.anthropic_tool_id_map)
    pairs = []
    for call in calls:
        original = call["call_id"]
        current = previous.get(original, original)
        projected = identities.get(current, current)
        if projected != original:
            pairs.append((original, projected))
    return ModelReplayMetadata.model_validate({
        **replay.model_dump(), "anthropic_tool_id_map": tuple(pairs),
    })


def project_model_replay(source: dict, profile: ModelProfile) -> dict:
    if profile.provider_replay_mode == "none" or source.get("provider_replay") is None:
        return {}
    replay = ModelReplayMetadata.model_validate(source["provider_replay"])
    # A new Build/model may inherit visible history. Private protocol material
    # from a different pinned profile does not cross that boundary.
    if replay.profile_digest != profile.digest:
        return {}
    if profile.provider_replay_mode == "anthropic_content":
        if replay.anthropic_content_json is None:
            raise ValueError("original Anthropic replay unavailable")
        from .context_replay import replay_content_view

        return {
            "anthropic_content": replay_content_view(source, profile, replay),
            "anthropic_tool_id_map": [list(pair) for pair in replay.anthropic_tool_id_map],
        }
    if replay.anthropic_content_json is not None:
        raise ValueError("Anthropic replay cannot cross a Chat profile")
    return {"reasoning_content": replay.reasoning_content}


def project_model_message(source: dict, profile: ModelProfile) -> dict:
    """Detach provider message fields from canonical host provenance.

    Both admission counters and model adapters consume this projection. Audit
    IDs and artifact metadata remain in the ledger; private replay only crosses
    the exact pinned profile. Tool content is never interpreted as provenance.
    Media content stays typed for the adapter's separate framing and budget.
    """
    if source.get("operator_history_projection") is not None:
        from .context_replay import steered_operator_view

        return steered_operator_view(source, profile)
    role = source.get("role")
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        raise ValueError("unsupported canonical message role")
    fields = {"role", "content", "name"}
    if role == "assistant":
        fields.add("tool_calls")
    elif role == "tool":
        fields.add("tool_call_id")
    message = copy.deepcopy({key: value for key, value in source.items() if key in fields})
    if role == "assistant":
        message.update(project_model_replay(source, profile))
    return message
