"""Identity-aware reducer for a ConversationItem surface.

This reducer deliberately does not deduplicate by author or text.  Providers
may emit the same content in two different items, and both are user-visible
truth.  Reconnect de-duplication is limited to a previously applied source
event for the same item.
"""

from __future__ import annotations

from collections import OrderedDict

from ksadk.conversations.contracts import ConversationItem


class ConversationItemReducer:
    """Apply append/replace/completed operations while preserving item identity."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, ConversationItem] = OrderedDict()
        self._source_events: set[tuple[str, str]] = set()

    def apply(self, item: ConversationItem) -> bool:
        """Apply one item operation and return whether this changed the view."""

        keys = {(item.item_id, source_id) for source_id in item.source_event_ids}
        if keys and keys.issubset(self._source_events):
            return False

        existing = self._items.get(item.item_id)
        if existing is not None and _terminal(existing) and not _terminal(item):
            # A reconnect may replay an older delta after the terminal snapshot
            # has already arrived on the live connection.  Remember its event
            # identity, but never regress a completed item back to streaming or
            # append the stale delta a second time.
            self._source_events.update(keys)
            return False
        if existing is None:
            merged = item
        elif item.operation == "append":
            merged = item.model_copy(
                update={
                    "payload": _append_payload(existing.payload, item.payload),
                    "source_event_ids": _merge_sources(
                        existing.source_event_ids,
                        item.source_event_ids,
                    ),
                }
            )
        else:
            merged = item.model_copy(
                update={
                    "source_event_ids": _merge_sources(
                        existing.source_event_ids,
                        item.source_event_ids,
                    )
                }
            )
        self._items[item.item_id] = merged
        self._source_events.update(keys)
        return True

    def items(self) -> tuple[ConversationItem, ...]:
        """Return presentation items in first-seen order."""

        return tuple(self._items.values())


def _append_payload(
    existing: dict[str, object],
    update: dict[str, object],
) -> dict[str, object]:
    merged = dict(existing)
    for key, value in update.items():
        previous = merged.get(key)
        if key == "text" and isinstance(previous, str) and isinstance(value, str):
            merged[key] = previous + value
        elif key in {"data", "operations"} and isinstance(previous, list) and isinstance(
            value, list
        ):
            merged[key] = [*previous, *value]
        else:
            merged[key] = value
    return merged


def _merge_sources(
    current: tuple[str, ...],
    incoming: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*current, *incoming)))


def _terminal(item: ConversationItem) -> bool:
    return item.lifecycle in {"completed", "failed"}


__all__ = ["ConversationItemReducer"]
