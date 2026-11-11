"""Model exchanges: provisional stream events and bounded persistent retry policy."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from contextlib import suppress

from ksadk._async_iterators import close_async_iterator

from .model import ModelStreamEvent, ModelTurnResult
from .model_input import input_binding_failure, input_event, observe_input
from .model_recovery_contract import unavailable_model_usage
from .model_recovery_contract import unavailable_uncertain_usage as unavailable_uncertain_usage
from .ports import ModelPort, ModelRequest, RunConfiguration
from .state import CommitBatch, EffectIntent, EffectReceipt, RunPhase, RunSnapshot, move
from .transitions import commit_transition

_DISPLAY_KINDS = {"text_delta", "public_summary", "provider_reasoning"}


class StreamFlushError(Exception):
    """A host flush callback failed during a display-event deadline tick.

    This is a local persistence failure (CAS, fencing, SQLite), not a
    model protocol or remote provider error. The caller must distinguish
    it from ordinary port failures to avoid masking local store faults
    with a generic uncertain receipt.
    """


async def exchange(
    model: ModelPort,
    request: ModelRequest,
    emit: Callable[[ModelStreamEvent], None] | None = None,
    *,
    flush_callback: Callable[[], None] | None = None,
    flush_interval: float = 0.2,
) -> ModelTurnResult:
    if request.profile.streaming:
        stream = getattr(model, "stream", None)
        if not callable(stream):
            raise ValueError("pinned streaming profile requires a stream-capable model port")
        sealed = None
        input_receipt = None
        seq = 0
        iterator = stream(request.model_copy(deep=True))
        loop = asyncio.get_running_loop()
        flush_deadline: float | None = None
        pending_anext: asyncio.Future | None = None
        # Track the first error so finally cleanup does not overwrite it.
        primary_error: BaseException | None = None
        try:
            pending_anext = asyncio.ensure_future(iterator.__anext__())
            while True:
                # Compute wait timeout for deadline tick.
                if flush_deadline is not None and flush_callback is not None:
                    remaining_to_flush = flush_deadline - loop.time()
                    if remaining_to_flush <= 0:
                        # Deadline tick: flush pending display events.
                        # flush_callback may raise (CAS/fence/store); that
                        # error becomes the primary error and propagates.
                        try:
                            flush_callback()
                        except Exception as exc:
                            primary_error = exc
                            raise
                        flush_deadline = None
                        continue
                    wait_timeout = remaining_to_flush
                else:
                    wait_timeout = None

                try:
                    await asyncio.wait(
                        (pending_anext,),
                        timeout=wait_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    raise

                if not pending_anext.done():
                    # Timeout expired but __anext__ still running — deadline tick.
                    # Do NOT cancel pending_anext; it continues to the next loop.
                    if (
                        flush_deadline is not None
                        and flush_callback is not None
                        and loop.time() >= flush_deadline
                    ):
                        try:
                            flush_callback()
                        except Exception as exc:
                            primary_error = exc
                            raise
                        flush_deadline = None
                    continue

                # __anext__ completed.
                try:
                    raw = pending_anext.result()
                except StopAsyncIteration:
                    break
                pending_anext = None

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
                    # Flush remaining display before sealing.
                    if flush_callback is not None:
                        try:
                            flush_callback()
                        except Exception as exc:
                            primary_error = exc
                            raise
                elif emit is not None:
                    emit(event)
                    # Set non-sliding deadline on first display pending.
                    # Only set if not already set (non-sliding).
                    if (
                        flush_callback is not None
                        and event.kind in _DISPLAY_KINDS
                        and flush_deadline is None
                    ):
                        flush_deadline = loop.time() + flush_interval

                # Prepare next read.
                pending_anext = asyncio.ensure_future(iterator.__anext__())
        except BaseException as exc:
            # Preserve the first error; do not let cleanup overwrite it.
            if primary_error is None:
                primary_error = exc
            raise
        finally:
            # Best-effort flush of pending display events on cancel/failure.
            # Do NOT retry if flush already failed with StreamFlushError: a
            # CAS/fence rejection must not be blindly retried in the same
            # request. On normal exit the final-event handler already flushed,
            # so this is a no-op (buffer empty). On cancel/other-failure it
            # attempts to commit any buffered display content before closing.
            if flush_callback is not None and not isinstance(primary_error, StreamFlushError):
                with suppress(Exception):
                    flush_callback()
            # Cancel and await pending_anext BEFORE closing the iterator.
            # A generator may catch CancelledError and raise a different
            # exception (e.g. RuntimeError). When a primary error exists,
            # suppress ALL exceptions from this cleanup to preserve it.
            # When no primary error (normal exit), only CancelledError is
            # expected; other exceptions propagate as genuine cleanup errors.
            if pending_anext is not None and not pending_anext.done():
                pending_anext.cancel()
                if primary_error is not None:
                    with suppress(BaseException):
                        await pending_anext
                else:
                    with suppress(asyncio.CancelledError):
                        await pending_anext
            # Close errors must not overwrite the primary error. On normal
            # exit (no primary error) close errors propagate; on error exit
            # they are suppressed to preserve the original exception.
            if primary_error is not None:
                with suppress(BaseException):
                    await close_async_iterator(iterator, owner="native_model")
            else:
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
    if event.kind in {"public_summary", "provider_reasoning"}:
        from ksadk.harness.tool_display import display_text

        kind = (
            "thinking.summary.delta" if event.kind == "public_summary"
            else "thinking.provider.delta"
        )
        return commit_transition(snapshot, snapshot, kind, {
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


# Upward-only jitter for locally-computed backoff, aligned with OpenCode's
# session retry policy (packages/opencode/src/session/retry.ts). When the
# provider gives no Retry-After directive, the deterministic backoff floor
# grows by up to this fraction of itself, so the same attempt can yield
# different bounded waits. The floor is never reduced and an explicit server
# wait is never brought forward; a delay already at the configured cap is
# unchanged. This is a process-local scheduling concern, not a product
# configuration knob, so it stays out of ModelProfile (which would change the
# profile digest and break attach/rebuild of existing runs).
_RETRY_JITTER_FACTOR = 0.25


def retry_delay(
    result: ModelTurnResult,
    consecutive_errors: int,
    config: RunConfiguration,
    *,
    random_source: Callable[[], float] | None = None,
) -> float:
    if result.response_kind not in {
        "provider_retryable_error",
        "empty_response",
        "malformed_tool_call",
    }:
        # Zero-delay policy: non-retryable outcomes never wait, regardless of
        # the jitter source.
        return 0.0
    profile = config.profile
    cap = min(profile.retry_max_delay_seconds, 60.0)
    delay = profile.retry_initial_delay_seconds
    # Saturate rather than exponentiating untrusted or very large counters.
    for _ in range(min(max(0, consecutive_errors - 1), 128)):
        delay = min(cap, delay * profile.retry_backoff_multiplier)
        if delay >= cap:
            break
    server_wait = result.retry_after_seconds
    if result.failure_kind == "rate_limit":
        if server_wait is not None:
            # A provider's explicit quota deadline (seconds, or an HTTP date
            # already resolved to seconds by the adapter) takes precedence
            # over the short transport backoff and is preserved exactly: it
            # is never jittered forward or back, and stays bounded here.
            return min(300.0, max(delay, server_wait))
        return _jitter_upward(
            max(delay, profile.rate_limit_cooldown_seconds), 300.0, random_source
        )
    if server_wait is not None:
        # Explicit server wait is preserved exactly; jitter only applies when
        # the provider gave no directive.
        return min(cap, max(delay, server_wait))
    return _jitter_upward(max(delay, 0.0), cap, random_source)


def _jitter_upward(
    base: float, cap: float, random_source: Callable[[], float] | None
) -> float:
    """Grow a locally-computed backoff floor by bounded upward jitter.

    ``base`` is the deterministic floor (transport backoff, or the rate-limit
    cooldown). The result is ``base`` plus up to ``_RETRY_JITTER_FACTOR * base``
    on top, clipped to ``cap``. The floor is never reduced, so an explicit
    cooldown or a delay already at the configured cap is unchanged and a zero
    floor (the zero-delay policy) stays zero. Sampling happens here, once per
    scheduled retry; the caller persists the returned value as
    ``retry_not_before`` and in the audit event unchanged, so a SQLite
    release/rebuild/attach never re-samples the same attempt.
    """
    if base <= 0 or base >= cap:
        return base
    draw = random_source() if random_source is not None else random.random()
    return min(cap, base + base * _RETRY_JITTER_FACTOR * draw)


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
