"""Model exchanges: provisional stream events and bounded persistent retry policy."""

from __future__ import annotations

from collections.abc import Callable

from ksadk._async_iterators import close_async_iterator

from .model import ModelStreamEvent, ModelTurnResult
from .model_input import input_binding_failure, input_event, observe_input
from .model_recovery_contract import unavailable_model_usage
from .model_recovery_contract import unavailable_uncertain_usage as unavailable_uncertain_usage
from .ports import ModelPort, ModelRequest, RunConfiguration
from .state import CommitBatch, EffectIntent, EffectReceipt, RunPhase, RunSnapshot, move
from .transitions import commit_transition


async def exchange(
    model: ModelPort, request: ModelRequest, emit: Callable[[ModelStreamEvent], None] | None = None
) -> ModelTurnResult:
    if request.profile.streaming:
        stream = getattr(model, "stream", None)
        if not callable(stream):
            raise ValueError("pinned streaming profile requires a stream-capable model port")
        sealed = None
        input_receipt = None
        seq = 0
        iterator = stream(request.model_copy(deep=True))
        try:
            async for raw in iterator:
                event = ModelStreamEvent.model_validate_json(raw.model_dump_json())
                if (
                    event.request_id != request.request_id
                    or event.attempt_id != request.attempt_id
                    or event.seq != seq + 1
                    or sealed is not None
                ):
                    raise ValueError("model stream identity or order mismatch")
                seq = event.seq
                if event.kind == "input_metadata":
                    if input_binding_failure(request.profile, event.input_receipt):
                        raise ValueError("model stream input profile mismatch")
                    input_receipt = event.input_receipt
                if event.kind == "final":
                    sealed = event.result
                    if input_receipt is not None and sealed.input_receipt != input_receipt:
                        raise ValueError("sealed input receipt conflicts with observed input")
                elif emit is not None:
                    emit(event)
        finally:
            await close_async_iterator(iterator, owner="native_model")
        if sealed is None:
            raise ValueError("model stream ended without a sealed result")
        result = sealed
    else:
        result = await model.complete(request.model_copy(deep=True))
    result = ModelTurnResult.model_validate_json(result.model_dump_json())
    if result.request_id != request.request_id or result.attempt_id != request.attempt_id:
        raise ValueError("model request identity mismatch")
    if input_binding_failure(request.profile, result.input_receipt):
        raise ValueError("model result input profile mismatch")
    return result


def provisional_delta(snapshot: RunSnapshot, event: ModelStreamEvent) -> CommitBatch:
    """Record a streamed delta without promoting it to an accepted answer."""
    if snapshot.pending_intent != event.request_id:
        raise ValueError("delta no longer belongs to the active request")
    if event.kind == "input_metadata":
        return commit_transition(snapshot, observe_input(snapshot, event.input_receipt),
                                 *input_event(event.input_receipt))
    if event.kind == "public_summary":
        from ksadk.harness.tool_display import display_text

        return commit_transition(snapshot, snapshot, "thinking.summary.delta", {
            "text": display_text(event.text), "request_id": event.request_id,
            "attempt_id": event.attempt_id, "stream_seq": event.seq,
        })
    if event.kind != "text_delta":
        raise ValueError("sealed result is not a provisional observation")
    return commit_transition(
        snapshot,
        snapshot,
        "text.delta",
        {
            "text": event.text,
            "phase": "provisional",
            "request_id": event.request_id,
            "attempt_id": event.attempt_id,
            "stream_seq": event.seq,
        },
    )


def retry_delay(
    result: ModelTurnResult, consecutive_errors: int, config: RunConfiguration
) -> float:
    if result.response_kind not in {
        "provider_retryable_error",
        "empty_response",
        "malformed_tool_call",
    }:
        return 0.0
    profile = config.profile
    cap = min(profile.retry_max_delay_seconds, 60.0)
    delay = profile.retry_initial_delay_seconds
    # Saturate rather than exponentiating untrusted or very large counters.
    for _ in range(min(max(0, consecutive_errors - 1), 128)):
        delay = min(cap, delay * profile.retry_backoff_multiplier)
        if delay >= cap:
            break
    if result.failure_kind == "rate_limit":
        if result.retry_after_seconds is not None:
            # A provider's explicit quota deadline takes precedence over the
            # short transport backoff, but remains bounded in this process.
            return min(300.0, max(delay, result.retry_after_seconds))
        return max(delay, profile.rate_limit_cooldown_seconds)
    return min(cap, max(delay, result.retry_after_seconds or 0.0))


def recover_timed_out_model(
    snapshot: RunSnapshot, config: RunConfiguration, operation_key: str, *, now: float
) -> RunSnapshot:
    """Retire a joined in-process model timeout through the bounded retry path.

    The caller retains the uncertain receipt and reports unknown usage. A lost
    running intent after process takeover never enters this path.
    """
    from .machine import reduce_model

    result = ModelTurnResult(
        response_kind="provider_retryable_error",
        request_id=operation_key,
        attempt_id=f"{operation_key}:attempt:1",
    )
    updated = reduce_model(snapshot, result, config)
    delay = retry_delay(result, updated.provider_errors, config)
    return updated.model_copy(update={"retry_not_before": now + delay if delay else 0})


def recover_unsealed_model_receipt(
    snapshot: RunSnapshot,
    intent: EffectIntent,
    receipt: EffectReceipt,
    config: RunConfiguration,
    *,
    now: float,
) -> tuple[RunSnapshot, tuple[str, dict[str, str]]] | None:
    """Settle joined model attempts without treating them as sealed answers.

    This is only for a known in-process timeout or an active pause. A lost
    running intent after takeover and every tool effect remain unreplayable.
    """
    if intent.kind != "model":
        return None
    key = intent.operation_key
    if (
        receipt.status == "uncertain"
        and intent.payload.get("purpose") != "compaction"
        and receipt.result == {"reason": "effect_timeout"}
    ):
        return recover_timed_out_model(snapshot, config, key, now=now), unavailable_timeout_usage(
            key
        )
    if receipt.status != "failed" or receipt.result != {"reason": "model_paused_before_result"}:
        return None
    updated = snapshot.model_copy(
        update={"pending_intent": None, "model_calls": snapshot.model_calls + 1}
    )
    if intent.payload.get("purpose") != "compaction":
        updated = move(updated, RunPhase.PREPARING_CONTEXT)
    return updated, unavailable_model_usage(key, f"{key}:attempt:1", "model_paused")


def unavailable_timeout_usage(operation_key: str) -> tuple[str, dict[str, str]]:
    # A canceled model request can still incur provider charges. Never report
    # zero tokens merely because it produced no sealed ModelTurnResult.
    return unavailable_model_usage(
        operation_key, f"{operation_key}:attempt:1", "model_effect_timeout"
    )


def sealed_model_usage_event(result: ModelTurnResult) -> tuple[str, dict]:
    if result.usage is None:
        return unavailable_model_usage(
            result.request_id, result.attempt_id, "provider_usage_missing"
        )
    return (
        "usage.reported",
        {
            "semantics": "delta",
            "request_id": result.request_id,
            "attempt_id": result.attempt_id,
            **result.usage.model_dump(mode="json"),
        },
    )
