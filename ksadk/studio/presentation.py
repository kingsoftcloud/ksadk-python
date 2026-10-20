"""Shared live/replay conversation projection for a persisted Studio Run."""

from ksadk.conversations.contracts import ConversationItem
from ksadk.conversations.projector import public_item_presentation
from ksadk.conversations.runtime_projection import (
    RuntimeConversationProjection,
    conversation_identity,
)
from ksadk.events.canonical import parse_runtime_event
from ksadk.events.content import DataContent


class StudioConversationProjection:
    def __init__(self, session_id, profile):
        self.session_id = session_id
        self.profile = profile
        self.projection = None
        self.prefix = []

    def apply(self, event):
        if event.type == "run.created":
            self.session_id = event.data.get("sessionId") or self.session_id
            return event.model_copy(
                update={"data": {**event.data, "presentationProfile": self.profile}}
            )
        raw = event.data.get("runtimeEvent")
        if not isinstance(raw, dict) or raw.get("schema_version") != 2:
            item = event.data.get("conversationItem")
            if self.projection is not None and isinstance(item, dict):
                scoped = self.projection.scope_ledger_item(ConversationItem.model_validate(item))
                scoped = scoped.model_copy(update={"run_id": event.run_id})
                return event.model_copy(
                    update={
                        "data": {
                            **event.data,
                            "conversationItem": scoped.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            ),
                        }
                    }
                )
            return event
        canonical = parse_runtime_event(raw)
        # Old persisted ConversationItems predate the display fields. Rebuild
        # only their allowlisted presentation from the original canonical fact;
        # keep identities, lifecycle, approvals and the durable log unchanged.
        existing = event.data.get("conversationItem")
        if isinstance(existing, dict):
            presentation = public_item_presentation(canonical, existing.get("kind"))
            if presentation:
                existing = {**existing, "payload": {**existing.get("payload", {}), **presentation}}
                event = event.model_copy(
                    update={"data": {**event.data, "conversationItem": existing}}
                )
        snapshot = getattr(canonical, "snapshot", None) or getattr(canonical, "initial", None)
        parts = snapshot.parts if snapshot else (getattr(canonical, "update", None),)
        managed = any(
            isinstance(part, DataContent)
            and isinstance(part.data, dict)
            and part.data.get("schema") == "execution.scope/v1"
            for part in parts
        )
        if self.projection is None and not managed:
            # Legacy providers may emit terminal-only snapshots or multiple
            # resume lifecycles. Preserve that contract until a managed scope
            # opts into the stricter canonical presentation lane.
            self.prefix.append(canonical)
            if self.profile == "agent-block-v1" and isinstance(
                event.data.get("conversationItem"), dict
            ):
                item = dict(event.data["conversationItem"])
                item["itemId"] = conversation_identity(
                    canonical.run_id, canonical.scope_id, item["itemId"]
                )
                item["nativeRef"] = {**item.get("nativeRef", {}), "scopeId": canonical.scope_id}
                return event.model_copy(update={"data": {**event.data, "conversationItem": item}})
            return event
        if self.projection is None:
            if not self.session_id:
                raise ValueError("presentation requires a persisted session identity")
            self.projection = RuntimeConversationProjection(
                self.session_id, profile=self.profile, kernel_interactions=True
            )
            for previous in self.prefix:
                self.projection.apply(previous)
            self.prefix.clear()
        item = self.projection.apply(canonical)
        data = dict(event.data)
        if item is None:
            data.pop("conversationItem", None)
        else:
            item = item.model_copy(update={"run_id": event.run_id})
            data["conversationItem"] = item.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        event_type = event.type
        if item is None and canonical.event_type in {
            "interaction.requested",
            "interaction.resolved",
        }:
            # Do not fall back to an actionable legacy form/approval event.
            event_type = "runtime.interaction_observed"
            data["interactionReadOnly"] = True
        return event.model_copy(update={"type": event_type, "data": data})
