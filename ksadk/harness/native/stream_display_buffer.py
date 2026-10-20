"""Coalesce display-only stream events with a non-sliding size threshold.

No internal timer. The host (exchange) drives deadline ticks via
asyncio.wait on __anext__. This module only decides whether to flush
at push time (size threshold) and provides flush/peek/ack for the host.

State model:
  _seen_key: the key whose first event was already emitted immediately.
             None if no display event has been seen yet.
  _pending:  the coalesced event waiting to be flushed. None if nothing
             pending. Set on the *second* event for a key (the first is
             immediate, not pending), or set by set_pending for immediate
             display events that need prepared-batch retention.
  _pending_key: the key of the current _pending. None if no pending.
                Used to detect a stale pending from a failed key-change
                flush: if _pending_key != current key, the old pending
                must be flushed (and acked) before coalescing the new key.
                This check is independent of _seen_key, so _seen_key can
                safely advance to the new key even before the old pending
                is acked — the stale pending is still caught by _pending_key.
  _first_at: when the current pending was first buffered (non-sliding).
             None for events set via set_pending (immediate commits).
  _prepared_commit: tuple of (CommitBatch, expected_revision, fencing_token,
             now) retained after a commit attempt with unknown result.
             On retry, the host replays this exact batch to the store,
             which recovers the true result via commit_id idempotency.
             None if no prepared batch is retained.

Push returns (immediate, flush_now). flush_now contains the pending
event that the caller must commit. The caller MUST call ack() after
a successful commit of flush_now events. If the commit fails, do NOT
call ack — the event stays pending for retry. push does NOT remove
pending on flush_now; only ack removes it.
"""

from __future__ import annotations

from time import monotonic

from ksadk.harness.tool_output import ToolOutputObservation

from .model import ModelStreamEvent

_DISPLAY = {"text_delta", "public_summary", "provider_reasoning"}


class StreamDisplayBuffer:
    """Coalesce display-only stream events.

    The first display event for a key is emitted immediately by the caller
    (returned in the immediate list). Only subsequent events with the same
    key are buffered and coalesced into _pending.

    Keyed by (kind, request_id, attempt_id) for ModelStreamEvent or
    (operation_key, call_id) for ToolOutputObservation.
    """

    def __init__(self, *, max_chars: int = 8192, flush_interval: float = 0.2):
        self._max_chars = max_chars
        self._flush_interval = flush_interval
        self._pending: ModelStreamEvent | ToolOutputObservation | None = None
        self._pending_key: tuple | None = None
        self._seen_key: tuple | None = None
        self._first_at: float | None = None
        self._prepared_commit: tuple | None = None

    def _event_key(self, event) -> tuple | None:
        if isinstance(event, ToolOutputObservation):
            return ("tool_output", event.operation_key, event.call_id)
        if isinstance(event, ModelStreamEvent) and event.kind in _DISPLAY:
            return (event.kind, event.request_id, event.attempt_id)
        return None

    def push(self, event, *, now: float | None = None) -> tuple[list, list]:
        """Return (immediate_events, flush_events).

        immediate: events the caller must commit right now. Includes:
          - non-display pass-through (input_metadata, etc.)
          - first display for a new key
          - stale pending from a failed key-change (caller must commit+ack)

        flush: the pending event that exceeded size threshold. The caller
        must commit it and then call ack(). If commit fails, do NOT ack.

        push does NOT remove pending; only ack does. This ensures a
        failed commit retains the event for retry with the same payload.
        """
        if now is None:
            now = monotonic()

        key = self._event_key(event)
        if key is None:
            # Non-display: return pending (without removing) for caller
            # to flush, then pass through the non-display event.
            old_pending = [self._pending] if self._pending is not None else []
            return (*old_pending, event), []

        # Stale pending from a different key (failed ack after key change):
        # return old pending for flush, then the current event. Do NOT
        # merge — the old pending belongs to a different key and must be
        # committed separately. Advance _seen_key to the new key now;
        # this is safe because the stale-pending check uses _pending_key
        # (not _seen_key), so even after advancing, a retry that finds
        # _pending still set will return the old pending via _pending_key
        # != key. After ack clears _pending, _seen_key == key ensures
        # subsequent same-key events are coalesced, not re-emitted.
        if self._pending is not None and self._pending_key != key:
            self._seen_key = key
            return ([self._pending, event], [])

        # First event for this key (seen_key is None or different):
        # emit immediately, mark this key as seen. Do NOT set _pending.
        if self._seen_key != key:
            self._seen_key = key
            self._first_at = None
            return ([event], [])

        # Same key, second+ event: coalesce into pending.
        if self._pending is None:
            self._pending = event
            self._pending_key = key
            self._first_at = now
        elif isinstance(event, ModelStreamEvent) and isinstance(self._pending, ModelStreamEvent):
            self._pending = event.model_copy(
                update={"text": self._pending.text + event.text}
            )
        else:
            self._pending = event

        # Size threshold: len(str) character count.
        size = len(self._pending.text) if isinstance(self._pending, ModelStreamEvent) else 0
        if size >= self._max_chars:
            return ([], [self._pending])

        return ([], [])

    def set_pending(self, event) -> None:
        """Set an event as pending for immediate commit via _flush_stream.

        Used by _stream_delta for first-display events that need
        prepared-batch retention. The event is set as pending so
        _flush_stream can peek, commit, and ack it with the same
        retention as coalesced events. _seen_key is advanced so
        subsequent same-key events are coalesced, not re-emitted.
        _first_at is None (not a coalesced event).
        """
        key = self._event_key(event)
        self._pending = event
        self._pending_key = key
        self._seen_key = key
        self._first_at = None

    def should_flush(self, *, now: float | None = None) -> bool:
        if self._pending is None or self._first_at is None:
            return False
        if now is None:
            now = monotonic()
        return (now - self._first_at) >= self._flush_interval

    def peek(self) -> ModelStreamEvent | ToolOutputObservation | None:
        return self._pending

    def ack(self) -> list:
        """Remove and return the pending event after a successful commit."""
        if self._pending is None:
            return []
        event = self._pending
        self._pending = None
        self._pending_key = None
        self._first_at = None
        self._prepared_commit = None
        return [event]

    def store_prepared(self, prepared: tuple) -> None:
        """Retain the exact prepared batch for idempotent replay on retry.

        Stores (CommitBatch, expected_revision, fencing_token, now) after
        a commit attempt with unknown result. On retry, the host replays
        this exact batch to the store, which recovers the true result via
        commit_id idempotency (the store checks commit_id before
        expected_revision). This prevents constructing a new batch with
        a new commit_id, which would double-commit the display event.
        """
        self._prepared_commit = prepared

    def peek_prepared(self) -> tuple | None:
        return self._prepared_commit

    def clear_prepared(self) -> None:
        self._prepared_commit = None

    def flush(self) -> list:
        """Remove and return the pending event (alias for ack)."""
        return self.ack()

    @property
    def has_pending(self) -> bool:
        return self._pending is not None
