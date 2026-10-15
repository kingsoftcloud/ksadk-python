"""Explicit private replay views rooted in the original sealed compaction."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Literal

from pydantic import Field

from ksadk._model_anthropic import anthropic_content
from ksadk._model_json import load_model_json

from .model import ModelProfile, ModelReplayMetadata
from .state import Contract

POLICY = "anthropic-client-compaction/v1"


class CompactionReplayView(Contract):
    version: Literal["anthropic-client-compaction/v1"] = POLICY
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    root_prefix_digest: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    origin_message_ref: str = Field(min_length=1, strict=True)
    original_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)


def _content_digest(blocks):
    return hashlib.sha256(json.dumps(blocks, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _mark(message, ref, root):
    copied = copy.deepcopy(message)
    if message.get("role") != "assistant" or message.get("provider_replay") is None:
        return copied
    replay = ModelReplayMetadata.model_validate(message["provider_replay"])
    if replay.anthropic_content_json is None:
        return copied
    blocks = anthropic_content(load_model_json(replay.anthropic_content_json))
    if not any(b["type"] in {"thinking", "redacted_thinking"} for b in blocks):
        return copied
    copied["provider_replay_projection"] = CompactionReplayView(
        profile_digest=replay.profile_digest, root_prefix_digest=root,
        origin_message_ref=ref, original_content_digest=_content_digest(blocks),
    ).model_dump(mode="json")
    return copied


def compaction_replay_view(messages, refs, *, covered_refs, profile, root_prefix_digest):
    if len(messages) != len(refs):
        raise ValueError("compaction_replay_manifest_mismatch")
    if profile.provider_replay_projection_policy != POLICY:
        return copy.deepcopy(messages)
    covered = frozenset(covered_refs)
    return tuple(_mark(m, ref, root_prefix_digest) if ref in covered else copy.deepcopy(m)
                 for ref, m in zip(refs, messages))


def imported_compaction_replay(messages, *, policy, root_prefix_digest):
    if policy != POLICY:
        return copy.deepcopy(messages)
    # Re-expanding the raw transcript also changes the prefix of post-compaction
    # replies. All inherited thinking belongs to that old request root.
    return tuple(_mark(m, f"imported-message:{i}", root_prefix_digest)
                 for i, m in enumerate(messages))


def reset_steered_replay(before, updated, configuration):
    """Record a source-bound view only when a host contract rewrites the prefix."""
    if configuration is None or configuration.profile.provider_replay_projection_policy != POLICY:
        return updated, None
    from .completion import TaskContract
    from .context_runtime_contracts import _refs, source_context_digest
    from .tool_budget import model_tool_catalog

    previous = configuration.with_task_contract(TaskContract.model_validate(before.task_contract))
    current = configuration.with_task_contract(TaskContract.model_validate(updated.task_contract))
    reexpanded = bool(before.compaction_history.get("count", 0))
    catalog_changed = model_tool_catalog(before, previous) != model_tool_catalog(updated, current)
    if not reexpanded and not catalog_changed:
        return updated, None
    root = source_context_digest(before.messages)
    refs = _refs(updated.messages)
    messages = compaction_replay_view(
        updated.messages, refs, covered_refs=refs[:len(before.messages)],
        profile=configuration.profile, root_prefix_digest=root,
    )
    if configuration.profile.operator_guidance_policy == "journaled-system/v1":
        from .operator_guidance import GUIDANCE_KIND, latest_guidance

        for index, original in enumerate(before.messages):
            if original.get("kind") != GUIDANCE_KIND:
                continue
            frame = latest_guidance((original,), configuration.profile)
            if frame is None:
                continue
            messages[index]["operator_history_projection"] = CompactionReplayView(
                profile_digest=configuration.profile.digest, root_prefix_digest=root,
                origin_message_ref=refs[index],
                original_content_digest=_content_digest(frame["content"]),
            ).model_dump(mode="json")
    return updated.model_copy(update={"messages": messages}), {
        "policy": POLICY,
        "reason": "contract_context_reexpanded" if reexpanded else "contract_tool_catalog_changed",
        "root_prefix_digest": root,
        "source_message_count": len(before.messages),
        "goal_revision": updated.goal_revision,
    }


def steered_operator_view(source, profile):
    """Quote obsolete host frames after an admitted contract prefix rewrite."""
    raw = source.get("operator_history_projection")
    if raw is None:
        return None
    from .operator_guidance import (
        GUIDANCE_KIND,
        HISTORICAL_FRAME_KIND,
        latest_guidance,
        quoted_operator_message,
    )

    view = CompactionReplayView.model_validate(raw)
    original = source
    if (source.get("kind") == HISTORICAL_FRAME_KIND
            and source.get("history_original_kind") == GUIDANCE_KIND):
        original = {**source, "kind": GUIDANCE_KIND}
    frame = latest_guidance((original,), profile)
    if (frame is None or profile.provider_replay_projection_policy != POLICY
            or view.profile_digest != profile.digest
            or view.original_content_digest != _content_digest(source.get("content"))):
        raise ValueError("steered_operator_view_mismatch")
    return quoted_operator_message(source)


def replay_content_view(source, profile: ModelProfile, replay: ModelReplayMetadata):
    blocks = anthropic_content(load_model_json(replay.anthropic_content_json))
    raw = source.get("provider_replay_projection")
    if raw is None:
        return blocks
    view = CompactionReplayView.model_validate(raw)
    if (source.get("role") != "assistant" or profile.provider_replay_projection_policy != POLICY
            or view.profile_digest != profile.digest
            or view.original_content_digest != _content_digest(blocks)):
        raise ValueError("compaction_replay_view_mismatch")
    return [b for b in blocks if b["type"] not in {"thinking", "redacted_thinking"}]
