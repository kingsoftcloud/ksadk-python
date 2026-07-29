"""Project canonical A2UI RuntimeEvents into official AG-UI activity content."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ksadk.events.runtime_event import EventType


def project_a2ui_operations(event_type: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return A2UI v0.9 operations carried by an AG-UI activity event."""

    explicit = payload.get("operations", payload.get("a2ui_operations"))
    if isinstance(explicit, list):
        return [dict(operation) for operation in explicit if isinstance(operation, Mapping)]

    surface_id = str(payload.get("surface_id") or payload.get("surfaceId") or "")
    if not surface_id:
        return []
    if event_type == EventType.A2UI_SURFACE_END:
        return [{"version": "v0.9", "deleteSurface": {"surfaceId": surface_id}}]

    surface = payload.get("surface")
    surface_data = surface if isinstance(surface, Mapping) else payload
    components = _flatten_components(surface_data.get("components"))
    data_model = surface_data.get("data_model", surface_data.get("dataModel"))
    operations: list[dict[str, Any]] = []
    if event_type == EventType.A2UI_SURFACE_BEGIN:
        catalog_id = str(
            surface_data.get("catalog_id")
            or surface_data.get("catalogId")
            or payload.get("catalog_id")
            or payload.get("catalogId")
            or "https://a2ui.org/specification/v0_9/basic_catalog.json"
        )
        operations.append(
            {
                "version": "v0.9",
                "createSurface": {"surfaceId": surface_id, "catalogId": catalog_id},
            }
        )
    if components:
        operations.append(
            {
                "version": "v0.9",
                "updateComponents": {"surfaceId": surface_id, "components": components},
            }
        )
    if data_model is not None:
        operations.append(
            {
                "version": "v0.9",
                "updateDataModel": {
                    "surfaceId": surface_id,
                    "path": "/",
                    "value": data_model,
                },
            }
        )
    return operations


def _flatten_components(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    flattened: list[dict[str, Any]] = []

    def visit(component: Any) -> str:
        if not isinstance(component, Mapping):
            return ""
        component_id = str(component.get("id") or component.get("component_id") or "")
        component_type = str(component.get("component") or component.get("type") or "")
        if not component_id or not component_type:
            return ""
        children = [visit(child) for child in component.get("children") or []]
        children = [child_id for child_id in children if child_id]
        entry: dict[str, Any] = {"id": component_id, "component": component_type}
        props = component.get("props")
        if isinstance(props, Mapping):
            entry.update(dict(props))
        if children and "children" not in entry and "child" not in entry:
            entry["children"] = children
        flattened.append(entry)
        return component_id

    for item in raw:
        visit(item)
    return flattened


__all__ = ["project_a2ui_operations"]
