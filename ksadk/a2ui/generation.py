"""Generation uses a request-local catalog, without modifying Runtime trust."""

import json
from copy import deepcopy

from ksadk.a2ui.budget import bounded_catalog_input
from ksadk.a2ui.catalog import _asset
from ksadk.a2ui.registry import A2uiCatalogRegistry, current_catalog_registry
from ksadk.a2ui.wire import A2uiWireState


def generation_registry(catalog_id, catalog=None):
    registry = current_catalog_registry()
    if catalog is not None:
        catalog = bounded_catalog_input(catalog)
    if isinstance(catalog, dict):
        resources = catalog.get("ksadkResources") or []
        catalog = {key: value for key, value in catalog.items()
                   if key not in {"ksadkManifest", "commonTypes", "ksadkResources"}}
        if catalog.get("catalogId") != catalog_id:
            raise ValueError("A2UI generation catalog identity mismatch")
        existing = registry.document(catalog_id)
        if existing is not None:
            if existing != catalog:
                raise ValueError("A2UI generation conflicts with a registered catalog")
        else:
            registry = A2uiCatalogRegistry([catalog], resources=resources)
    if registry.document(catalog_id) is None:
        raise ValueError("A2UI generation requires the custom catalog Schema")
    return registry


def registered_generation_state(state, catalog_id, *, registry=None):
    registry = registry or generation_registry(catalog_id)
    context = {
        **registry.document(catalog_id),
        "commonTypes": _asset("common-types-v0_9.json"),
        "ksadkManifest": registry.describe(catalog_id),
        "ksadkResources": registry.resources,
    }
    return {**state, "ag-ui": {
        **(state.get("ag-ui") or {}),
        "a2ui_schema": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
    }}


def finalize_registered_envelope(raw, *, catalog_id, prior=None, surface_id=None, registry=None):
    registry = registry or generation_registry(catalog_id)
    envelope = json.loads(raw)
    operations = envelope["a2ui_operations"]
    for operation in operations:
        operation["version"] = "v0.9.1"
        if "createSurface" in operation and operation["createSurface"].get("catalogId") != catalog_id:
            raise ValueError("Generated A2UI uses another catalog")
    state = A2uiWireState(registry)
    if prior is not None:
        if prior.get("catalogId") != catalog_id:
            raise ValueError("Update target belongs to another catalog")
        state.accept([
            {"version": "v0.9.1", "createSurface": {
                "surfaceId": surface_id, "catalogId": catalog_id,
            }},
            *([{"version": "v0.9.1", "updateComponents": {
                "surfaceId": surface_id, "components": deepcopy(prior["components"]),
            }}] if prior.get("components") else []),
            {"version": "v0.9.1", "updateDataModel": {
                "surfaceId": surface_id, "path": "/", "value": prior.get("data", {}),
            }},
        ])
    state.accept(operations)
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
