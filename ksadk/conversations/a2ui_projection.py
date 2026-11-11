"""Reduce A2UI operation batches for the existing message-history view."""

from collections.abc import Mapping, Sequence
from typing import Any

from ksadk.agui.a2ui_projection import project_a2ui_operations


def project_a2ui_activities(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    activities: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = str(event.get("EventType") or "")
        if event_type not in {
            "a2ui.surface.begin",
            "a2ui.surface.update",
            "a2ui.surface.end",
        }:
            continue
        content = event.get("Content")
        payload = content if isinstance(content, Mapping) else {}
        operations = project_a2ui_operations(event_type, payload)
        if not operations:
            continue
        surface_id = str(payload.get("surface_id") or payload.get("surfaceId") or "")
        previous = activities.get(surface_id)
        if previous is not None:
            before = previous["Content"]["a2ui_operations"]
            if operations[:len(before)] == before:
                merged = operations
            elif before[:len(operations)] == operations:
                merged = before
            else:
                merged = [*before, *operations]
            previous["Content"]["a2ui_operations"] = merged
            if payload.get("interactionId"):
                previous["Content"]["interactionId"] = payload["interactionId"]
        else:
            activities[surface_id] = {
                "SeqId": event.get("SeqId"),
                "Type": "a2ui-surface",
                "MessageId": f"{event.get('InvocationId')}:a2ui:{surface_id}",
                "SurfaceId": surface_id,
                "Content": {
                    "surfaceId": surface_id,
                    "a2ui_operations": operations,
                    **({"interactionId": payload["interactionId"]}
                       if payload.get("interactionId") else {}),
                },
            }
    return list(activities.values())
