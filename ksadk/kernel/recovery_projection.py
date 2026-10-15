"""Reconcile provider replay with already durable root output during takeover."""

from __future__ import annotations

import json

from ksadk.events.canonical import (
    ItemCompleted,
    ItemFailed,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    RunCompleted,
    RuntimeEvent,
)


def _root_message_fingerprint(event: ItemCompleted) -> str | None:
    if event.item_kind != "message" or event.parent_scope_id is not None:
        return None
    source = event.source
    metadata = source.metadata
    stable_source = {
        "framework": source.framework,
        "native_run_id": source.native_run_id,
        "scope_id": event.scope_id,
        "path_key": metadata.get("path_key"),
        "path": metadata.get("path"),
        "author": metadata.get("author"),
    }
    return json.dumps(
        {
            "source": stable_source,
            "snapshot": event.snapshot.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class RecoveryEventReconciler:
    """Drop one replayed root message without weakening normal item identity.

    Native ADK recovery may reconstruct the final provider message with fresh
    native/event/item ids after that same complete message was durably written
    but before ``run.completed`` committed.  Buffering only the recovery
    stream's root message lets us compare the complete snapshot.  Ordinary
    live streams never use this reconciler, so equal-text distinct items remain
    distinct there.
    """

    def __init__(self, prior_events: list[RuntimeEvent]) -> None:
        self._prior: dict[str, str] = {}
        for event in prior_events:
            if isinstance(event, ItemCompleted):
                fingerprint = _root_message_fingerprint(event)
                if fingerprint is not None:
                    self._prior[fingerprint] = event.item_id
        self._pending_item_id: str | None = None
        self._pending: list[RuntimeEvent] = []
        self._aliases: dict[str, str] = {}
        self._novel_root_message_seen = False

    def accept(self, event: RuntimeEvent) -> tuple[RuntimeEvent, ...]:
        if isinstance(event, ItemStarted) and self._is_root_message(event):
            flushed = self.flush()
            self._pending_item_id = event.item_id
            self._pending = [event]
            return flushed

        if self._pending_item_id == getattr(event, "item_id", None):
            if isinstance(event, (ItemUpdated, ItemSnapshotReplaced)):
                self._pending.append(event)
                return ()
            if isinstance(event, ItemCompleted):
                fingerprint = _root_message_fingerprint(event)
                prior_item_id = self._prior.get(fingerprint or "")
                pending = tuple(self._pending)
                replayed_item_id = self._pending_item_id
                self._pending_item_id = None
                self._pending = []
                if prior_item_id is not None and not self._novel_root_message_seen:
                    self._aliases[replayed_item_id] = prior_item_id
                    return ()
                self._novel_root_message_seen = True
                if fingerprint is not None:
                    self._prior.setdefault(fingerprint, event.item_id)
                return (*pending, event)
            if isinstance(event, ItemFailed):
                pending = tuple(self._pending)
                self._pending_item_id = None
                self._pending = []
                self._novel_root_message_seen = True
                return (*pending, event)

        flushed = self.flush()
        if isinstance(event, RunCompleted) and self._aliases:
            event = event.model_copy(
                update={
                    "output_refs": tuple(
                        ref.model_copy(
                            update={"item_id": self._aliases.get(ref.item_id, ref.item_id)}
                        )
                        for ref in event.output_refs
                    )
                }
            )
        return (*flushed, event)

    def flush(self) -> tuple[RuntimeEvent, ...]:
        pending = tuple(self._pending)
        if pending:
            self._novel_root_message_seen = True
        self._pending_item_id = None
        self._pending = []
        return pending

    @staticmethod
    def _is_root_message(event: ItemStarted) -> bool:
        return event.item_kind == "message" and event.parent_scope_id is None


__all__ = ["RecoveryEventReconciler"]
