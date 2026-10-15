"""One pure model-request constructor for preparation, dispatch and sealing."""

from .context_runtime_contracts import CompactionRequest
from .delivery_review import delivery_review_messages
from .operator_guidance import operator_input_history_view
from .ports import ModelRequest
from .request_media import project_request_media
from .stop_hook_runtime import hook_feedback_messages
from .tool_budget import (
    has_admitted_task_tools,
    model_instructions,
    model_tool_catalog,
    model_turn_control,
    tools_exhausted,
)
from .tool_hook_runtime import tool_hook_feedback_messages
from .tool_observation import model_observation_messages
from .turn_control import ModelTurnControl


def model_request(snapshot, config, *, key, payload):
    summary = payload.get("purpose") == "compaction"
    review = payload.get("purpose") == "delivery_review"
    context = snapshot.prepared_context or {}
    compact = CompactionRequest.model_validate(snapshot.compaction_request) if summary else None
    control = None if summary or review else model_turn_control(snapshot, config)
    captured_control = payload.get("turn_control")
    if (
        not summary
        and not review
        and isinstance(captured_control, dict)
        and captured_control.get("reason") == "task_tools_exhausted"
        and not has_admitted_task_tools(config)
        and tools_exhausted(snapshot, config)
    ):
        # Older ready intents classified an empty tool surface as spent
        # budget. Keep that committed, non-authorizing control for this
        # effect only; new turns use the corrected admission projection.
        control = ModelTurnControl.model_validate(captured_control)
    if context and not summary and not review:
        captured = context.get("turn_control")
        prepared_control = ModelTurnControl.model_validate(captured) if captured else None
        if prepared_control != control:
            raise ValueError("prepared model-turn control differs from current admission")
    request = ModelRequest(
        scope=snapshot.scope,
        run_id=snapshot.run_id,
        request_id=key,
        attempt_id=f"{key}:attempt:1",
        profile=compact.profile if compact else config.profile,
        purpose="compaction" if summary else "delivery_review" if review else "task",
        max_output_tokens=compact.summary_output_tokens if compact else None,
        instructions=(
            compact.summary_instructions
            if compact
            else context.get("instructions", (
                config.instructions
                if config.profile.operator_guidance_policy == "journaled-system/v1"
                else model_instructions(snapshot, config)
            ))
        ),
        messages=(
            compact.summary_messages
            if compact
            else model_observation_messages(
                context.get("messages", tool_hook_feedback_messages(
                    snapshot, hook_feedback_messages(
                        snapshot, delivery_review_messages(snapshot)
                    )
                ))
            )
        ),
        tools=() if summary else model_tool_catalog(snapshot, config),
        turn_control=control,
    )
    request = request.model_copy(update={"messages": operator_input_history_view(
        request.messages, request.profile
    )})
    request = request.model_copy(update={"messages": project_request_media(
        request.messages, request.profile, purpose=request.purpose
    )})
    from .replay_context import project_replay_context, request_header_digest

    if request.purpose != "compaction":
        messages, _ = project_replay_context(
            request.messages, request.profile,
            abandoned=snapshot.compaction_history.get("abandoned_replay_digests", ()),
            header_digest=request_header_digest(
                request.instructions, request.tools, request.profile),
        )
        request = request.model_copy(update={"messages": messages})
    return request


def capture_request_context(snapshot, batch, config):
    """Commit the same selected view before dispatch, including failed/canceled attempts."""
    from .context_replay import POLICY
    from .replay_context import project_replay_context, request_header_digest
    from .request_media import RequestMediaError
    from .transitions import commit_transition, finish

    if config.profile.provider_replay_projection_policy != POLICY:
        return batch
    intent = batch.intents[0]
    try:
        request = model_request(batch.snapshot, config, key=intent.operation_key,
                                payload=intent.payload)
        messages, discarded = project_replay_context(
            request.messages, config.profile,
            abandoned=snapshot.compaction_history.get("abandoned_replay_digests", ()),
            header_digest=request_header_digest(
                request.instructions, request.tools, request.profile),
        )
    except RequestMediaError:
        return batch  # The existing zero-dispatch denial path consumes the original intent.
    except (TypeError, ValueError):
        return commit_transition(
            snapshot, finish(snapshot, "blocked", "model_replay_context_invalid"),
            "context.blocked", {"reason": "model_replay_context_invalid"},
        )
    context = {**(batch.snapshot.prepared_context or {}),
               "instructions": request.instructions, "messages": list(messages),
               "replay_header_digest": request_header_digest(
                   request.instructions, request.tools, request.profile),
               "turn_control": request.turn_control.model_dump(mode="json")
               if request.turn_control is not None else None}
    history = dict(batch.snapshot.compaction_history)
    if discarded:
        history["abandoned_replay_digests"] = list(discarded)
    updated = batch.snapshot.model_copy(update={
        "prepared_context": context, "compaction_history": history,
    })
    return batch.model_copy(update={"snapshot": updated})
