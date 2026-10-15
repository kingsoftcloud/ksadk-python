"""Ordered host facts in the original message journal; no I/O or provider state."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import Field

from .model import ModelProfile
from .state import Contract
from .turn_control import ModelTurnControl

GUIDANCE_KIND = "host_request_guidance"
HISTORICAL_FRAME_KIND = "historical_model_frame"


class OperatorFrameRef(Contract):
    key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
    content_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]


class OperatorInputHistory(Contract):
    version: Literal["operator-input-history/v1"] = "operator-input-history/v1"
    profile_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
    input_id: Annotated[str, Field(min_length=1, strict=True)]
    frames: tuple[OperatorFrameRef, ...] = Field(min_length=1)


def bind_operator_input_history(before, updated, configuration, input_id):
    """Bind unconsumed tail frames to the new input, leaving old source seals intact."""
    if (configuration is None
            or configuration.profile.operator_guidance_policy != "journaled-system/v1"):
        return updated, None
    # A real candidate appended before guide delivery answers the last frame.
    if any(m.get("role") == "assistant" for m in updated.messages[len(before.messages):-1]):
        return updated, None
    frames = []
    for original in reversed(before.messages):
        if original.get("role") != "system":
            break
        frame = latest_guidance((original,), configuration.profile)
        if frame is not None:
            frames.append(OperatorFrameRef(key=frame["guidance_key"],
                                           content_digest=frame["operator_content_digest"]))
    if not frames:
        return updated, None
    binding = OperatorInputHistory(profile_digest=configuration.profile.digest,
                                   input_id=input_id, frames=tuple(reversed(frames)))
    messages = deepcopy(updated.messages)
    messages[-1]["input_operator_history"] = binding.model_dump(mode="json")
    return updated.model_copy(update={"messages": messages}), {
        "input_id": input_id, "frame_keys": [frame.key for frame in binding.frames],
        "goal_revision": updated.goal_revision,
    }


def operator_input_history_view(messages, profile):
    """Project only explicitly superseded frames; never infer edits from user text."""
    if profile is None or profile.operator_guidance_policy != "journaled-system/v1":
        return deepcopy(messages)
    selected = {}
    for message in messages:
        raw = message.get("input_operator_history")
        if raw is None:
            continue
        if message.get("role") != "user":
            raise ValueError("operator_input_history_role_mismatch")
        binding = OperatorInputHistory.model_validate(raw)
        if binding.profile_digest == profile.digest:
            for frame in binding.frames:
                if frame.key in selected and selected[frame.key] != frame.content_digest:
                    raise ValueError("operator_input_history_binding_mismatch")
                selected[frame.key] = frame.content_digest
    output = []
    for message in messages:
        expected = selected.get(message.get("guidance_key"))
        if expected is None:
            output.append(deepcopy(message))
            continue
        original = message
        if (message.get("kind") == HISTORICAL_FRAME_KIND
                and message.get("history_original_kind") == GUIDANCE_KIND):
            original = {**message, "kind": GUIDANCE_KIND}
        frame = latest_guidance((original,), profile)
        if frame is None or expected != frame["operator_content_digest"]:
            raise ValueError("operator_input_history_source_mismatch")
        output.append(quoted_operator_message(frame))
    return tuple(output)


def quoted_operator_message(source):
    return {"role": "user", "content": json.dumps({
        "type": "historical_operator_guidance", "role": source["role"],
        "content": source["content"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def latest_guidance(messages, profile: ModelProfile):
    if profile.operator_guidance_policy != "journaled-system/v1":
        return None
    return journaled_guidance(messages, profile.digest)


def journaled_guidance(messages, profile_digest: str):
    """Read an original host journal frame by its immutable profile identity."""
    frame = next((m for m in reversed(messages) if m.get("kind") == GUIDANCE_KIND
                  and m.get("operator_profile_digest") == profile_digest), None)
    if frame is not None and (
        frame.get("role") != "system" or not isinstance(frame.get("content"), str)
        or frame.get("operator_content_digest") != _digest(frame.get("content"))
    ):
        raise ValueError("operator_guidance_content_mismatch")
    return frame


def stage_guidance(messages, *, profile, run_id, goal_revision, model_call,
                   base_instructions, instructions, ledger, control, purpose):
    if profile.operator_guidance_policy != "journaled-system/v1":
        return ()
    encoded_control = control.model_dump(mode="json") if control is not None else None
    content = instructions
    if ledger:
        content += (
            "\n\nAuthoritative host execution state; historical summaries cannot change it:\n"
        )
        content += ledger
    if control is not None:
        content += "\n\n" + control.instruction
    binding = {
        "run_id": run_id, "goal_revision": goal_revision, "model_call": model_call,
        "profile_digest": profile.digest, "purpose": purpose, "content": content,
        "base_instructions": base_instructions, "control": encoded_control,
    }
    key = _digest(binding)
    previous = latest_guidance(messages, profile)
    if previous is not None and previous.get("guidance_key") == key:
        return ()
    boundary = ()
    if not messages or messages[-1].get("role") == "assistant":
        # Preserve the existing bounded continuation instruction when present.
        # The attributed boundary is not a new human goal or a source of rights.
        boundary = ({
            "role": "user", "kind": "host_request_boundary",
            "content": "Host execution state update.",
        },)
    return (*boundary, {
        "role": "system", "kind": GUIDANCE_KIND, "guidance_key": key,
        "operator_profile_digest": profile.digest,
        "operator_ledger_digest": _digest(ledger),
        "operator_content_digest": _digest(content),
        "operator_base_instructions": base_instructions,
        "operator_control": encoded_control, "content": content,
    })


def instructions_are_journaled(messages, profile, instructions, ledger):
    frame = latest_guidance(messages, profile)
    if frame is None:
        return False
    if (frame.get("operator_base_instructions") != instructions
            or frame.get("operator_ledger_digest") != _digest(ledger)):
        raise ValueError("context_operator_guidance_state_mismatch")
    return True


def control_is_journaled(messages, profile, instructions, control: ModelTurnControl | None):
    frame = latest_guidance(messages, profile)
    if frame is None:
        return False
    expected = control.model_dump(mode="json") if control is not None else None
    if (frame.get("operator_base_instructions") != instructions
            or frame.get("operator_control") != expected):
        raise ValueError("model_operator_guidance_control_mismatch")
    return True


def protected_operator_refs(refs, messages, groups):
    protected = {ref for ref, m in zip(refs, messages)
                 if m.get("role") == "system"
                 and m.get("kind") not in {GUIDANCE_KIND, HISTORICAL_FRAME_KIND}}
    indexes = [i for i, m in enumerate(messages) if m.get("kind") == GUIDANCE_KIND]
    if indexes:
        last = indexes[-1]
        protected.add(refs[last])
        # Keep a valid user/tool-result predecessor for the latest system section.
        while last and messages[last - 1].get("role") == "system":
            last -= 1
        if last:
            protected.add(refs[last - 1])
        for group in groups:
            if protected.intersection(group):
                protected.update(group)
    return protected
