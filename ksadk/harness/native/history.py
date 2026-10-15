"""Native session-history projection; source transcripts remain immutable.

Keep actual tool observations and complete protocol pairs across user turns.
Imported call IDs have stable historical identities so providers may reuse a
local call ID in a later turn. Summary/provisional text is never promoted into
the final answer. This is Native-to-Native projection, not a cross-runtime wire
format; other runtimes need an explicit canonical export adapter.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from pydantic import JsonValue

from .context_history import _historical_message
from .model import ModelReplayMetadata, ModelTurnResult
from .model_replay import remap_anthropic_replay, replay_fields
from .operator_guidance import HISTORICAL_FRAME_KIND, journaled_guidance
from .state import InterruptedToolCall, RunPhase, RunSnapshot
from .tool_interruption import interrupted_tool_calls

_TRANSIENT_PHASES = {"provisional", "text_delta", "final_candidate"}
# A new Session inherits user-authored turns and committed tool receipts. The
# observations below carry prior task facts; current verifier feedback is
# quoted once on import. Other typed messages are control for the old Run.
_INHERITED_OBSERVATION_KINDS = {
    "user_document_observation",
    "verification_observation",
    "historical_verification_observation",
    "historical_tool_observation",
    "background_child_observation",
}


def _retained_history_message(message: dict, *, preserve_model_frames: bool = False) -> bool:
    role, kind = message.get("role"), message.get("kind")
    if kind == "delivery_review_source":
        # The raw model/tool batch stays in the Run. A later Session receives
        # executed observations as quotes, never the reviewer's private prose.
        return False
    return (
        role in ({"system", "developer", "user", "assistant", "tool"}
                 if preserve_model_frames else {"user", "assistant", "tool"})
        and (preserve_model_frames or role == "tool" or kind in {None, "user_guidance"}
             or kind in _INHERITED_OBSERVATION_KINDS)
        and message.get("phase") not in _TRANSIENT_PHASES
        and message.get("summary") is not True
    )


def _observation_identity(message: dict) -> tuple[str, int, str] | None:
    """Identify an exact derived observation, never deduplicate arbitrary prose."""
    run, index, content = (
        message.get("source_run_id"), message.get("source_message_index"), message.get("content")
    )
    if (
        message.get("history_projection_version") == 1
        and isinstance(run, str) and run
        and type(index) is int and index >= 0
        and isinstance(content, str)
    ):
        return run, index, content
    return None


def _identity(prefix: str, *values: object) -> str:
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return prefix + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _source(message: dict, parent: RunSnapshot, index: int) -> dict:
    if message.get("history_projection_version") == 1:
        if (
            not isinstance(message.get("source_run_id"), str)
            or not message["source_run_id"]
            or type(message.get("source_message_index")) is not int
            or message["source_message_index"] < 0
        ):
            raise ValueError("invalid historical source reference")
        return {
            name: message[name]
            for name in ("history_projection_version", "source_run_id", "source_message_index")
        }
    return {
        "history_projection_version": 1,
        "source_run_id": parent.run_id,
        "source_message_index": index,
    }


def project_session_history(
    parent: RunSnapshot | None, *, profile_digest: str | None = None,
    configuration_digest: str | None = None,
) -> tuple[dict[str, JsonValue], ...]:
    """Import a terminal parent's history once, preserving bounded observations.

    Content bounding belongs to ContextPlanner and tool-result shaping. Removing
    all tool messages here would erase the facts needed for the next user turn.
    A missing result is closed as unknown, never treated as successful or safe
    to re-execute. An unchanged, uncompressed host journal retains prior model
    frames as history; only the new run's config and current frame grant control.
    """
    if parent is None:
        return ()
    if not parent.phase.terminal:
        raise ValueError("session history requires a terminal parent")
    preserve_model_frames = (
        profile_digest is not None and profile_digest == parent.profile_digest
        and configuration_digest == parent.configuration_digest
        and not parent.compaction_history.get("count", 0)
        and parent.phase == RunPhase.COMPLETED
        and journaled_guidance(parent.messages, parent.profile_digest) is not None
    )
    output: list[dict[str, JsonValue]] = []
    pending: dict[str, tuple[str, dict, InterruptedToolCall | None]] = {}
    hook_inputs: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    scope = parent.scope.model_dump(mode="json")
    inherited_observations = {
        identity
        for message in parent.messages
        if message.get("role") == "user"
        and message.get("kind") == "historical_tool_observation"
        and _retained_history_message(message)
        and (identity := _observation_identity(message)) is not None
    }
    derived_inputs: set[tuple[str, int, str]] = set()
    retained_inputs: set[tuple[str, int, str]] = set()

    def flush_hook_inputs() -> None:
        output.extend(hook_inputs)
        hook_inputs.clear()

    def close_pending() -> None:
        status = (
            "uncertain"
            if parent.phase in {RunPhase.INTERRUPTED, RunPhase.CANCELED}
            or parent.reason == "effect_outcome_unknown"
            else "not_completed"
        )
        for mapped, source, observation in pending.values():
            not_dispatched = observation is not None and (
                observation.execution_status == "not_dispatched"
            )
            data = {
                "status": "not_completed" if not_dispatched else (
                    "uncertain" if observation is not None else status
                ),
                "execution_status": "not_dispatched" if not_dispatched else "unknown",
                "reason": "not_dispatched" if not_dispatched else (
                    "previous_turn_ended_without_tool_result"
                ),
                "instruction": (
                    "The host did not dispatch this call before the turn ended. "
                    "It has no execution result. Any new attempt requires fresh admission."
                    if not_dispatched else
                    "No committed result is available. Check actual state "
                    "before assuming success or repeating any side effect."
                ),
            }
            if observation is not None:
                data["interruption_reason"] = parent.tool_batch_interruption.reason
                if observation.operation_key is not None:
                    data["operation_key"] = observation.operation_key
            output.append(
                {
                    "role": "tool",
                    "tool_call_id": mapped,
                    **source,
                    "kind": "incomplete_tool_observation",
                    "content": json.dumps(data, sort_keys=True),
                }
            )
        pending.clear()
        flush_hook_inputs()

    from .context_replay import _mark
    from .context_runtime_contracts import source_context_digest
    from .replay_context import replay_identity

    abandoned = set(parent.compaction_history.get("abandoned_replay_digests", ()))
    abandoned_root = source_context_digest(parent.messages) if abandoned else None
    for index, original in enumerate(parent.messages):
        interruptions = interrupted_tool_calls(parent, index, original)
        if original.get("role") == "assistant" and original.get("provider_replay") is not None:
            replay = ModelReplayMetadata.model_validate(original["provider_replay"])
            if abandoned and replay_identity(replay) in abandoned:
                original = _mark(original, f"inherited-message:{index}",
                                 abandoned_root)
        role = original.get("role")
        kind = original.get("kind")
        if not _retained_history_message(original, preserve_model_frames=preserve_model_frames):
            continue
        source = _source(original, parent, index)
        message = {**deepcopy(original), **source}
        if preserve_model_frames and role != "tool" and (
            role in {"system", "developer"}
            or kind not in {None, "user_guidance", *_INHERITED_OBSERVATION_KINDS}
            or kind == "verification_observation"
        ):
            # Keep the model-visible role/content where they were. Only host
            # provenance changes: the old frame is no current control, goal or
            # verifier authority. A new Run stages its own current host frame.
            message["history_original_kind"] = original.get("history_original_kind", kind)
            message["kind"] = HISTORICAL_FRAME_KIND
        if role == "user" and kind == "verification_observation" and not preserve_model_frames:
            # Preserve the old verdict exactly as quoted data. Its repair
            # instruction belongs to that old goal, not the new user turn.
            quoted, images = _historical_message(message)
            message["kind"] = "historical_verification_observation"
            content = json.dumps({
                "observation": "previous_turn_verification",
                "current_task_authority": False,
                "source_run_id": source["source_run_id"],
                "source_message_index": source["source_message_index"],
                "content": quoted.get("content"),
            }, ensure_ascii=False)
            # Keep explicit pixels for the ordinary media admission path;
            # base64 inside quoted JSON would only be prose for the model.
            message["content"] = (
                [{"type": "text", "text": content}, *images] if images else content
            )
        if role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id in pending:
                mapped, _, _ = pending.pop(call_id)
                message["tool_call_id"] = mapped
                output.append(message)
                if (not preserve_model_frames
                        and isinstance(message.get("hook_effective_arguments"), dict)):
                    observation = {
                        "role": "user", "kind": "historical_tool_observation", **source,
                        "content": json.dumps({
                            "observation": "kernel_effective_tool_input",
                            "tool_call_id": mapped, "receipt_ref": message.get("receipt_ref"),
                            "effective_arguments": message["hook_effective_arguments"],
                            "instruction": (
                                "This records the effective input for this attempt, "
                                "not an approval "
                                "or proof of successful execution. Consult the tool result."
                            ),
                        }, ensure_ascii=False),
                    }
                    # A previously imported receipt already has this derived
                    # note. Keep that source frame instead of adding one per
                    # new turn; identical prose from other receipts is distinct.
                    identity = _observation_identity(observation)
                    if identity is None:
                        raise ValueError("invalid derived historical source reference")
                    derived_inputs.add(identity)
                    if identity not in inherited_observations:
                        hook_inputs.append(observation)
                        inherited_observations.add(identity)
                        retained_inputs.add(identity)
                if not pending:
                    flush_hook_inputs()
            else:
                close_pending()
                output.append(
                    {
                        "role": "user",
                        "kind": "historical_tool_observation",
                        **source,
                        "content": json.dumps(
                            {"status": "unmatched_tool_result", "content": message.get("content")},
                            ensure_ascii=False,
                        ),
                    }
                )
            continue
        if (not preserve_model_frames and role == "user"
                and kind == "historical_tool_observation"):
            identity = _observation_identity(message)
            if identity in derived_inputs:
                # Only collapse a frame reproduced from a matching receipt.
                # Arbitrary or orphaned historical observations stay intact.
                if identity in retained_inputs:
                    continue
                retained_inputs.add(identity)
        close_pending()
        calls = message.get("tool_calls")
        if calls:
            if role != "assistant" or not isinstance(calls, list):
                raise ValueError("malformed native tool history")
            identities = {}
            for call_index, call in enumerate(calls):
                call_id = call.get("id") if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not call_id or call_id in pending:
                    raise ValueError("malformed native tool identity")
                mapped = (
                    call_id
                    if original.get("history_projection_version") == 1
                    else _identity(
                        "hist_",
                        scope,
                        source["source_run_id"],
                        source["source_message_index"],
                        call_id,
                        call_index,
                    )
                )
                if mapped in seen:
                    raise ValueError("duplicate imported tool identity")
                seen.add(mapped)
                call["id"] = mapped
                identities[call_id] = mapped
                pending[call_id] = (mapped, source, interruptions.get(call_id))
            if message.get("provider_replay") is not None:
                message.update(replay_fields(remap_anthropic_replay(
                    ModelReplayMetadata.model_validate(message["provider_replay"]), identities,
                )))
        output.append(message)
    close_pending()
    final = parent.final_text
    if final and parent.phase in {RunPhase.COMPLETED, RunPhase.INTERRUPTED}:
        last = output[-1] if output else {}
        if not (
            last.get("role") == "assistant"
            and not last.get("tool_calls")
            and last.get("content") == final
        ):
            output.append(
                {
                    "role": "assistant", "content": final,
                    **_source({}, parent, len(parent.messages)),
                    **replay_fields(
                        parent.candidate_replay
                        if (parent.candidate or {}).get("final_text") == final
                        else None
                    ),
                }
            )
    from .context_replay import imported_compaction_replay

    return imported_compaction_replay(
        tuple(output), policy=parent.compaction_history.get("provider_replay_projection_policy"),
        root_prefix_digest=parent.compaction_history.get("prefix_digest", ""),
    )


def normalize_tool_identities(snapshot: RunSnapshot, result: ModelTurnResult) -> ModelTurnResult:
    """Project only colliding new call IDs; callers persist the raw result receipt.

    The same request gets the same replacement identities. No incomplete calls
    become executable, and text/usage/provider result semantics remain unchanged.
    """
    if result.response_kind != "tool_calls":
        return result
    existing = {
        call.get("id")
        for message in snapshot.messages
        for call in message.get("tool_calls", ())
        if isinstance(call, dict)
    }
    existing.update(call.get("call_id") for call in snapshot.pending_tools)
    reserved = existing | {call.call_id for call in result.tool_calls}
    changed, projected = False, []
    for index, call in enumerate(result.tool_calls):
        if call.call_id not in existing:
            projected.append(call)
            continue
        salt = 0
        mapped = _identity("call_", result.request_id, call.call_id, index, salt)
        while mapped in reserved:
            salt += 1
            mapped = _identity("call_", result.request_id, call.call_id, index, salt)
        reserved.add(mapped)
        projected.append(call.model_copy(deep=True, update={"call_id": mapped}))
        changed = True
    if not changed:
        return result
    replay = remap_anthropic_replay(result.provider_replay, {
        original.call_id: current.call_id
        for original, current in zip(result.tool_calls, projected)
    })
    return ModelTurnResult.model_validate({
        **result.model_dump(), "tool_calls": tuple(projected), "provider_replay": replay,
    })
