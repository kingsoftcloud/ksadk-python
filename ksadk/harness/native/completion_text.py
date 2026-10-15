"""Assemble only a contiguous, host-authenticated output continuation chain."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue

from .model_replay import replay_fields

if TYPE_CHECKING:
    from .state import RunSnapshot


def candidate_model_message(
    snapshot: RunSnapshot,
    *,
    kind: Literal[
        "rejected_final_candidate", "delivery_review_candidate"
    ] = "rejected_final_candidate",
) -> dict[str, JsonValue]:
    """Keep the actual draft available to same-turn review and repair.

    This typed message is model input, not a tool observation or an accepted
    final answer. Public state and future Session history exclude it.
    """
    assert snapshot.candidate is not None
    return {
        "role": "assistant",
        "kind": kind,
        "content": snapshot.candidate["final_text"],
        "candidate_id": snapshot.candidate["candidate_id"],
        "candidate_status": "unverified" if kind == "delivery_review_candidate" else "rejected",
        **replay_fields(snapshot.candidate_replay),
    }


def fragment_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def complete_continuation(messages: tuple[dict[str, JsonValue], ...], final_suffix: str) -> str:
    """Prepend contiguous output-limit fragments to their final suffix.

    A task-resume frame, user message, tool batch, feedback, or malformed host
    metadata breaks the chain. The model is explicitly asked for a suffix, so
    this function does not guess at fuzzy overlap or rewrite model text.
    """
    fragments: list[str] = []
    index = len(messages) - 1
    while index >= 1:
        observation = messages[index]
        fragment = messages[index - 1]
        if (
            observation.get("role") != "user"
            or observation.get("kind") != "continuation_observation"
            or observation.get("continuation_mode", "suffix") != "suffix"
            or fragment.get("role") != "assistant"
            or fragment.get("kind") != "continuation_fragment"
        ):
            break
        content = fragment.get("content")
        response_id = fragment.get("continuation_response_id")
        if (
            not isinstance(content, str)
            or not isinstance(response_id, str)
            or observation.get("continuation_response_id") != response_id
            or observation.get("continuation_fragment_digest") != fragment_digest(content)
        ):
            break
        fragments.append(content)
        index -= 2
    return "".join(reversed(fragments)) + final_suffix


def abandon_continuation(
    messages: tuple[dict[str, JsonValue], ...], last_fragment: str
) -> tuple[dict[str, JsonValue], ...]:
    """Retire one unfinished chain without leaving executable tool JSON in context."""
    prefix = list(messages)
    discarded = [last_fragment]
    while len(prefix) >= 2:
        observation, fragment = prefix[-1], prefix[-2]
        if (
            observation.get("role") != "user"
            or observation.get("kind") != "continuation_observation"
            or fragment.get("role") != "assistant"
            or fragment.get("kind") != "continuation_fragment"
        ):
            break
        content = fragment.get("content")
        response_id = fragment.get("continuation_response_id")
        if (
            not isinstance(content, str)
            or not isinstance(response_id, str)
            or observation.get("continuation_response_id") != response_id
            or observation.get("continuation_fragment_digest") != fragment_digest(content)
        ):
            break
        discarded.append(content)
        del prefix[-2:]
    fingerprint = fragment_digest("".join(reversed(discarded)))
    return (
        *prefix,
        {
            "role": "user",
            "kind": "output_limit_restart_observation",
            "content": (
                "The previous response exceeded the output limit repeatedly. "
                "Its unfinished tool call was not executed. Use smaller independent "
                "tool calls or files; do not repeat one oversized call. "
                f"Discarded output fingerprint: {fingerprint}."
            ),
        },
    )
