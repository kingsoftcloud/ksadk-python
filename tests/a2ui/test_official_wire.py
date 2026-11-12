"""Official asset/protocol validation and generic tool carrier projections."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ksadk.a2ui.catalog import catalog_digest, catalog_document, catalog_manifest
from ksadk.a2ui.payload import extract_operations
from ksadk.a2ui.presentation import prepare_presentation
from ksadk.a2ui.wire import A2uiWireState
from ksadk.events.canonical import ContentSnapshot, ItemCompleted
from ksadk.events.content import ToolResultContent
from ksadk.runtime._runner_adapter.a2ui_surface import _a2ui_surface_event
from ksadk.runtime.adapter import RunHandle


def create(surface="card"):
    return {"version": "v0.9.1", "createSurface": {
        "surfaceId": surface, "catalogId": catalog_manifest()["catalog_id"],
    }}


def components(items, surface="card"):
    return {"version": "v0.9.1", "updateComponents": {"surfaceId": surface, "components": items}}


def test_full_catalog_digest_and_explicit_schema_resolution_alias():
    document = catalog_document()
    assert catalog_digest(document) == catalog_manifest()["catalog_digest"]
    altered = deepcopy(document)
    altered["functions"]["formatString"]["required"] = []
    assert catalog_digest(altered) != catalog_digest(document)
    A2uiWireState().accept([create(), components([{
        "id": "root", "component": "Text", "text": {
            "call": "formatString", "args": {"value": "Hello ${/name}"},
        },
    }])])


@pytest.mark.parametrize("catalog_id", [
    "https://example.test/unregistered",
    "https://a2ui.org/specification/v0_9/basic_catalog.json",
])
def test_unknown_renderer_catalog_id_is_forwarded_without_claiming_a_schema_alias(catalog_id):
    operation = create()
    operation["createSurface"]["catalogId"] = catalog_id
    state = A2uiWireState()
    assert state.accept([operation]) == [operation]
    assert state.registry.describe(catalog_id) == {
        "catalog_id": catalog_id, "catalog_validation": "protocol_only",
    }


def test_incremental_children_root_and_rebuild_are_validated_as_one_surface_history():
    state = A2uiWireState()
    state.accept([create(), components([{"id": "child", "component": "Text", "text": "Ready"}])])
    state.accept([components([{"id": "root", "component": "Column", "children": ["child"]}])])
    accepted = deepcopy(state.history)
    with pytest.raises(ValueError):
        state.accept([components([{"id": "bad", "component": "Script"}])])
    assert state.history == accepted
    state.accept([{"version": "v0.9.1", "deleteSurface": {"surfaceId": "card"}}, create()])


@pytest.mark.parametrize("carrier", [
    lambda ops: {"a2ui_operations": ops},
    lambda ops: json.dumps({"a2ui_operations": ops}),
    lambda ops: {"content": [{"type": "text", "text": json.dumps({"a2ui_operations": ops})}]},
    lambda ops: {"structuredContent": {"a2ui_operations": ops}, "content": [
        {"type": "text", "text": "ignored duplicate"},
    ]},
    lambda ops: {"mimeType": "application/a2ui+json", "data": {"messages": ops}},
])
def test_declared_tool_and_mcp_carriers_have_one_identical_meaning(carrier):
    ops = [create()]
    assert extract_operations(carrier(ops)) == ops
    assert extract_operations("ordinary text") == []
    assert extract_operations({"business": {"createSurface": {"surfaceId": "card"}}}) == []


def test_invalid_optional_presentation_falls_back_without_altering_schema_or_chat():
    presentation = {"title": "Input", "a2ui": {
        "wire_version": "0.9.1", "catalog_digest": "unknown", "messages": [create()],
    }}
    assert prepare_presentation(presentation) == {"title": "Input"}
    assert "a2ui" in presentation


def test_canonical_tool_results_project_each_surface_once_and_preserve_native_scope():
    counter = iter(range(1, 30))
    adapter = SimpleNamespace(_runtime_type="langgraph", _next_seq=lambda: next(counter))
    handle = RunHandle(run_id="run", session_id="session", runtime_type="langgraph")
    result = ItemCompleted(
        schema_version=2, event_id="tool-event", seq=0, timestamp=1, run_id="run",
        scope_id="native-child", parent_scope_id="native-root", source={"framework": "langgraph"},
        item_id="tool", item_kind="tool_result", snapshot=ContentSnapshot(parts=[ToolResultContent(
            part_id="result", call_id="tool",
            result={"a2ui_operations": [create(), create("second")]},
        )]),
    )
    events = _a2ui_surface_event(adapter, handle, result)
    assert len(events) == 4
    assert {event.source.metadata["surface_id"] for event in events} == {"card", "second"}
    assert all(event.scope_id == "native-child" and event.parent_scope_id == "native-root"
               for event in events)
    assert _a2ui_surface_event(adapter, handle, result) == ()
