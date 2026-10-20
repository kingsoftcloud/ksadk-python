"""Batch and coalesce durable Studio presentation events."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable
from typing import Any

from ksadk.studio.contracts import RunEvent, RunRecord
from ksadk.studio.event_store import RunEventStore


class _ProjectedRunEventWriter:
    """Batch high-frequency presentation events before rewriting a Run JSON.

    Canonical RuntimeEvents are already durable in the session store.  The
    RunEvent JSON is a presentation projection, so writing and retaining one
    copy of the full projection envelope per token *or internal status fact*
    with an independent file replace is unnecessary and becomes quadratic as
    the response grows.  A short batching window keeps live chat responsive,
    coalesces adjacent text chunks, and commits status facts in one replace.

    The status facts themselves are not discarded: the canonical session
    trace remains the source of truth and the Studio projection still retains
    every curated fact.  Only the number of growing-file rewrites changes.
    """

    _STREAM_TYPES = frozenset({"message.delta", "thinking.delta"})

    def __init__(
        self,
        event_store: RunEventStore,
        run_id: str,
        on_event: Callable[[RunEvent], None] | None,
        *,
        flush_interval: float = 0.5,
    ) -> None:
        self._event_store = event_store
        self._run_id = run_id
        self._on_event = on_event
        self._flush_interval = flush_interval
        self._pending: list[tuple[str, dict[str, Any]]] = []
        # Publish the first chunk immediately so the UI can enter streaming
        # state without waiting for the batching window. Subsequent chunks are
        # coalesced into short durable writes.
        self._last_flush = 0.0
        self._flush_handle: asyncio.TimerHandle | None = None

    def append(
        self, event_type: str, data: dict[str, Any], *, record: RunRecord | None = None,
    ) -> RunEvent | None:
        if record is not None:
            if record.id != self._run_id:
                raise ValueError("projected event Run identity mismatch")
            # A flush could persist a cached, already terminal record before
            # its terminal fact. Publish the pending prefix in this same write.
            entries = [*self._coalesce_stream_entries(self._pending), (event_type, data)]
            stored = self._event_store.append_record_events(record, entries)
            self._pending.clear()
            if self._flush_handle is not None:
                self._flush_handle.cancel()
                self._flush_handle = None
            self._last_flush = time.monotonic()
            self._notify(stored)
            return stored[-1]
        if self._is_batchable(event_type, data):
            self._pending.append((event_type, data))
            elapsed = time.monotonic() - self._last_flush
            if elapsed < self._flush_interval:
                if self._flush_handle is None:
                    self._flush_handle = asyncio.get_running_loop().call_later(
                        self._flush_interval - elapsed, self.flush
                    )
                return None
            return self.flush()[-1]
        self.flush()
        stored = self._event_store.append(self._run_id, event_type, data)
        self._notify((stored,))
        return stored

    @classmethod
    def _is_batchable(cls, event_type: str, data: dict[str, Any]) -> bool:
        if event_type in cls._STREAM_TYPES:
            return True
        runtime_event = data.get("runtimeEvent")
        # Managed Harness publishes its execution evidence as canonical status
        # items.  Long runs can emit thousands of these in seconds; they drive
        # the activity UI but do not require one fsync/JSON rewrite per fact.
        return (
            event_type == "item.completed"
            and isinstance(runtime_event, dict)
            and runtime_event.get("item_kind") == "status"
        )

    def flush(self) -> list[RunEvent]:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if not self._pending:
            return []
        pending, self._pending = self._pending, []
        stored = self._event_store.append_many(
            self._run_id,
            self._coalesce_stream_entries(pending),
        )
        self._last_flush = time.monotonic()
        self._notify(stored)
        return stored

    @staticmethod
    def _coalesce_stream_entries(
        entries: list[tuple[str, dict[str, Any]]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Collapse adjacent chunks without changing the rendered transcript."""

        coalesced: list[tuple[str, dict[str, Any]]] = []
        for event_type, data in entries:
            if not coalesced:
                coalesced.append((event_type, copy.deepcopy(data)))
                continue
            previous_type, previous = coalesced[-1]
            if (
                event_type not in _ProjectedRunEventWriter._STREAM_TYPES
                or not _same_projected_stream(previous_type, previous, event_type, data)
            ):
                coalesced.append((event_type, copy.deepcopy(data)))
                continue

            operation = str(data.get("operation") or "append")
            if operation == "append":
                merged_text = str(previous.get("text") or "") + str(data.get("text") or "")
                merged = copy.deepcopy(data)
                _set_projected_stream_text(merged, merged_text)
                coalesced[-1] = (event_type, merged)
            else:
                coalesced[-1] = (event_type, copy.deepcopy(data))
        return coalesced

    def _notify(self, events: tuple[RunEvent, ...] | list[RunEvent]) -> None:
        if self._on_event is None:
            return
        for event in events:
            self._on_event(event)

def _same_projected_stream(
    previous_type: str,
    previous: dict[str, Any],
    event_type: str,
    data: dict[str, Any],
) -> bool:
    return (
        previous_type == event_type
        and previous.get("itemId") == data.get("itemId")
        and previous.get("partId") == data.get("partId")
        and previous.get("operation") == data.get("operation")
    )

def _set_projected_stream_text(data: dict[str, Any], text: str) -> None:
    data["text"] = text
    conversation_item = data.get("conversationItem")
    if isinstance(conversation_item, dict):
        payload = conversation_item.get("payload")
        if isinstance(payload, dict):
            payload["text"] = text
    runtime_event = data.get("runtimeEvent")
    if isinstance(runtime_event, dict):
        update = runtime_event.get("update")
        if isinstance(update, dict):
            update["text"] = text
