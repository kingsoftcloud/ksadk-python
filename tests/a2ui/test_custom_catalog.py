"""Catalog compatibility is independent from the business/interrupt contract."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from ksadk.a2ui import A2uiCatalogRegistry
from ksadk.a2ui.catalog import catalog_document, catalog_manifest
from ksadk.a2ui.presentation import prepare_presentation
from ksadk.a2ui.registry import catalog_scope, current_catalog_registry
from ksadk.a2ui.wire import A2uiWireState
from ksadk.runtime._runner_adapter.a2ui_surface import _a2ui_surface_event
from ksadk.runtime.adapter import RunHandle

IDENTITY = "https://example.test/catalogs/forms/v1"


def custom_catalog():
    document = catalog_document()
    document["catalogId"] = document["$id"] = IDENTITY
    document["components"]["FeedbackInput"] = json.loads(
        json.dumps(document["components"]["TextField"]).replace('"TextField"', '"FeedbackInput"')
    )
    return document


def operations(identity=IDENTITY, component="FeedbackInput"):
    return [
        {"version": "v0.9.1", "createSurface": {"surfaceId": "custom", "catalogId": identity}},
        {"version": "v0.9.1", "updateComponents": {"surfaceId": "custom", "components": [
            {"id": "root", "component": component, "label": "Feedback", "value": {"path": "/answer"}},
        ]}},
        {"version": "v0.9.1", "updateDataModel": {"surfaceId": "custom", "value": {"answer": ""}}},
    ]


def test_unknown_catalog_preserves_the_original_wire_and_is_not_official():
    state = A2uiWireState()
    raw = operations()
    assert state.accept(raw) == raw
    assert state.registry.describe(IDENTITY) == {"catalog_id": IDENTITY, "catalog_validation": "protocol_only"}
    assert raw == operations()


def test_registered_catalog_validates_custom_components_and_is_an_isolated_snapshot():
    document = custom_catalog()
    registry = A2uiCatalogRegistry([document])
    document["components"].clear()
    state = A2uiWireState(registry)
    state.accept(operations())
    baseline = deepcopy(state.history)
    bad = deepcopy(operations()[1])
    bad["updateComponents"]["components"][0]["value"] = 42
    with pytest.raises(ValueError):
        state.accept([bad])
    assert state.history == baseline
    assert registry.describe(IDENTITY)["catalog_validation"] == "full"
    assert "FeedbackInput" in registry.document(IDENTITY)["components"]
    assert current_catalog_registry().describe(IDENTITY)["catalog_validation"] == "protocol_only"


@pytest.mark.parametrize("mutate", [
    lambda op: op[0]["createSurface"].update(catalogId=""),
    lambda op: op[0].update(version="v1.0"),
    lambda op: op[1]["updateComponents"]["components"].append(deepcopy(op[1]["updateComponents"]["components"][0])),
    lambda op: op[1]["updateComponents"]["components"][0].pop("component"),
    lambda op: op[2]["updateDataModel"].update(path="/bad~2path"),
    lambda op: op.append(deepcopy(op[0])),
    lambda op: op.pop(0),
])
def test_opaque_catalog_still_obeys_protocol_and_lifecycle(mutate):
    state = A2uiWireState()
    raw = operations()
    mutate(raw)
    with pytest.raises(ValueError):
        state.accept(raw)
    assert state.history == []
    assert state.catalogs == {}


def test_known_catalog_cannot_hide_invalid_components_in_an_opaque_batch():
    state = A2uiWireState()
    with pytest.raises(ValueError):
        state.accept(operations(catalog_manifest()["catalog_id"], "FeedbackInput"))
    assert state.history == []


def test_catalog_conflicts_and_remote_references_fail_before_execution():
    conflicting = catalog_document()
    conflicting["components"].pop("Text")
    with pytest.raises(ValueError, match="Conflicting"):
        A2uiCatalogRegistry([conflicting])
    document = custom_catalog()
    document["components"]["FeedbackInput"] = {"$ref": "https://example.invalid/remote-schema"}
    with pytest.raises(ValueError, match="offline reference"):
        A2uiCatalogRegistry([document])


def test_offline_resources_are_linked_for_core_without_changing_the_original_digest():
    from ksadk.a2ui.catalog import catalog_digest
    from ksadk.a2ui.generation import generation_registry, registered_generation_state

    document = custom_catalog()
    document["components"]["FeedbackInput"]["allOf"][-1]["properties"]["label"] = {"$ref": "urn:example:label"}
    resources = [{"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:example:label",
                  "type": "string", "minLength": 1}]
    registry = A2uiCatalogRegistry([document], resources=resources)
    state = A2uiWireState(registry)
    state.accept(operations())
    assert registry.describe(IDENTITY)["catalog_digest"] == catalog_digest(document)
    assert registry.document(IDENTITY) == document
    context = registered_generation_state({}, IDENTITY, registry=registry)["ag-ui"]["a2ui_schema"]
    resolved = generation_registry(IDENTITY, context)
    assert resolved.describe(IDENTITY)["catalog_digest"] == registry.describe(IDENTITY)["catalog_digest"]
    invalid = operations()
    invalid[1]["updateComponents"]["components"][0]["label"] = ""
    with pytest.raises(ValueError):
        A2uiWireState(resolved).accept(invalid)


def test_unknown_catalog_does_not_bypass_existing_budgets():
    state = A2uiWireState()
    raw = operations()
    raw[-1]["updateDataModel"]["value"] = "x" * 1_048_576
    with pytest.raises(ValueError, match="budget"):
        state.accept(raw)
    raw = operations()
    raw[1]["updateComponents"]["components"] *= 2049
    with pytest.raises(ValueError):
        state.accept(raw)
    assert state.history == []


def test_mixed_surfaces_cannot_bypass_a_registered_catalog_with_an_opaque_catalog():
    state = A2uiWireState()
    opaque = operations()
    known = [
        {"version": "v0.9.1", "createSurface": {
            "surfaceId": "known", "catalogId": catalog_manifest()["catalog_id"],
        }},
        {"version": "v0.9.1", "updateComponents": {"surfaceId": "known", "components": [
            {"id": "root", "component": "Text", "text": "Accepted"},
        ]}},
    ]
    state.accept(opaque + known)
    before = deepcopy(state.history)
    with pytest.raises(ValueError):
        state.accept([{"version": "v0.9.1", "updateComponents": {"surfaceId": "known", "components": [
            {"id": "root", "component": "FeedbackInput", "label": "Not in the official catalog"},
        ]}}])
    assert state.history == before


def test_custom_presentation_digest_and_schema_fallback_are_independent():
    registry = A2uiCatalogRegistry([custom_catalog()])
    presentation = {"title": "Feedback", "a2ui": {
        "wire_version": "0.9.1", "catalog_digest": registry.describe(IDENTITY)["catalog_digest"],
        "messages": operations(),
    }}
    assert prepare_presentation(presentation, registry=registry) == presentation
    assert prepare_presentation(presentation) == presentation
    wrong = deepcopy(presentation)
    wrong["a2ui"]["catalog_digest"] = "0" * 64
    assert prepare_presentation(wrong, registry=registry) == {"title": "Feedback"}
    # A supplied digest for an opaque catalog is a declaration, never proof.
    assert prepare_presentation(wrong) == wrong


def test_runtime_projection_does_not_fabricate_official_metadata_and_replays_once():
    for registry, expected in [(None, "protocol_only"), (A2uiCatalogRegistry([custom_catalog()]), "full")]:
        counter = iter(range(20))
        adapter = SimpleNamespace(_runtime_type="langgraph", _next_seq=lambda: next(counter),
                                  a2ui_catalog_registry=registry)
        handle = RunHandle(run_id="run", session_id="session", runtime_type="langgraph")
        chunk = {"call_id": "custom-call", "tool_output": {"a2ui_operations": operations()}}
        events = _a2ui_surface_event(adapter, handle, chunk)
        assert len(events) == 2
        metadata = events[-1].source.metadata
        assert metadata["catalog_id"] == IDENTITY and metadata["catalog_validation"] == expected
        assert ("catalog_digest" in metadata) == (expected == "full")
        assert events[-1].snapshot.parts[0].data == operations()
        assert _a2ui_surface_event(adapter, handle, chunk) == ()


async def test_catalog_context_isolated_across_tasks_and_reset_before_consumers():
    import asyncio

    registry = A2uiCatalogRegistry([custom_catalog()])

    async def describe(selected):
        with catalog_scope(selected):
            await asyncio.sleep(0)
            return current_catalog_registry().describe(IDENTITY)["catalog_validation"]

    assert await asyncio.gather(describe(registry), describe(None)) == ["full", "protocol_only"]
    assert current_catalog_registry().describe(IDENTITY)["catalog_validation"] == "protocol_only"


def test_another_adapter_never_inherits_the_callers_application_catalog():
    from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

    registry = A2uiCatalogRegistry([custom_catalog()])
    with catalog_scope(registry):
        ordinary = RunnerRuntimeAdapter(SimpleNamespace(), runtime_type="fixture")
        configured = RunnerRuntimeAdapter(SimpleNamespace(), runtime_type="fixture", a2ui_catalog_registry=registry)
    assert ordinary.a2ui_catalog_registry.document(IDENTITY) is None
    assert configured.a2ui_catalog_registry is registry


async def test_custom_generation_uses_the_full_client_schema_without_registering_it_globally():
    from ksadk.compat.copilotkit_a2ui import _generate_a2ui_envelope

    prompts = []
    attempts = []

    class Model:
        def bind_tools(self, tools, *, tool_choice):
            assert tool_choice == "render_a2ui"
            return self

        async def ainvoke(self, values):
            prompts.append(values[0].content)
            return SimpleNamespace(tool_calls=[{"name": "render_a2ui", "args": {
                "surfaceId": "generated", "data": {"answer": "draft"}, "components": [{
                    "id": "root", "component": "FeedbackInput", "label": 42 if len(prompts) == 1 else "Feedback",
                    "value": {"path": "/answer"},
                }],
            }}])

    raw = await _generate_a2ui_envelope(config={
        "model": Model(), "default_catalog_id": IDENTITY, "default_surface_id": "generated",
        "catalog": json.dumps(custom_catalog()), "guidelines": None, "recovery": {"maxAttempts": 2},
        "on_a2ui_attempt": attempts.append,
    }, state={}, messages=[], intent="create", target_surface_id=None, changes=None)
    result = json.loads(raw)
    assert result["a2ui_operations"][0]["createSurface"]["catalogId"] == IDENTITY
    assert not attempts[0]["ok"] and attempts[1]["ok"]
    assert IDENTITY in prompts[0] and "FeedbackInput" in prompts[0]
    assert current_catalog_registry().document(IDENTITY) is None


def test_middleware_passes_the_actual_schema_in_addition_to_its_identity(monkeypatch):
    from ksadk.compat import copilotkit_a2ui

    captured = {}
    def build(params):
        captured.update(params)
        return SimpleNamespace(name="generate_a2ui")
    monkeypatch.setattr(copilotkit_a2ui, "build_ksadk_a2ui_tool", build)
    request = SimpleNamespace(model=object(), tools=[], state={"ag-ui": {
        "inject_a2ui_tool": True, "a2ui_schema": json.dumps(custom_catalog()),
    }})
    copilotkit_a2ui.KsadkCopilotKitMiddleware()._maybe_build_a2ui_tool(request)
    assert json.loads(captured["catalog"]) == custom_catalog()
    assert captured["default_catalog_id"] == IDENTITY


async def test_stream_registry_scope_does_not_leak_into_the_consumer():
    from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

    registry = A2uiCatalogRegistry([custom_catalog()])
    adapter = RunnerRuntimeAdapter(SimpleNamespace(), runtime_type="fixture", a2ui_catalog_registry=registry)
    handle = RunHandle(run_id="run", session_id="session", runtime_type="fixture")
    async def stream(_):
        assert current_catalog_registry() is registry
        yield "first"
        assert current_catalog_registry() is registry
        yield "second"
    adapter._stream_events = stream
    values = []
    async for value in adapter.stream(handle):
        assert current_catalog_registry() is not registry
        values.append(value)
    assert values == ["first", "second"]


def test_app_configuration_reaches_future_and_cached_adapters_and_cannot_cross_apps(tmp_path):
    from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
    from ksadk.runtime.executor import RuntimeExecutor
    from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
    from ksadk.server.factory import RuntimeAppConfig, create_runtime_app

    catalogs = A2uiCatalogRegistry([custom_catalog()])
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _: RunnerRuntimeAdapter(SimpleNamespace(), runtime_type="fixture"))
    executor = RuntimeExecutor(registry)
    launch = RuntimeLaunchContext(runtime_type="fixture", project_dir=tmp_path)
    cached = executor.capability_adapter(launch)
    app = create_runtime_app(RuntimeAppConfig(runtime_executor=executor, launch_context=launch,
                                             route_groups=set(), a2ui_catalog_registry=catalogs))
    assert app is not None and cached.a2ui_catalog_registry is catalogs
    assert executor.create_adapter(launch).a2ui_catalog_registry is catalogs
    # A Kernel provider may return the same adapter for capability reads.
    # Reapplying the application's identical snapshot must be a no-op.
    cached._active_runs["existing"] = object()
    cached.configure_a2ui_catalog_registry(catalogs)
    executor._runs["existing"] = object()
    executor.configure_a2ui_catalog_registry(catalogs)
    with pytest.raises(RuntimeError, match="active"):
        cached.configure_a2ui_catalog_registry(A2uiCatalogRegistry())
    executor._runs.clear()
    with pytest.raises(ValueError, match="another"):
        executor.configure_a2ui_catalog_registry(A2uiCatalogRegistry())


def test_rejected_app_configuration_cannot_rebind_other_cached_adapters(tmp_path):
    from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
    from ksadk.runtime.executor import RuntimeExecutor
    from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

    existing = A2uiCatalogRegistry([custom_catalog()])
    wanted = A2uiCatalogRegistry()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _: RunnerRuntimeAdapter(SimpleNamespace(), runtime_type="fixture"))
    executor = RuntimeExecutor(registry)
    first = executor.capability_adapter(RuntimeLaunchContext(runtime_type="fixture", project_dir=tmp_path / "a"))
    second = executor.capability_adapter(RuntimeLaunchContext(runtime_type="fixture", project_dir=tmp_path / "b"))
    second.configure_a2ui_catalog_registry(existing)
    original = first.a2ui_catalog_registry
    with pytest.raises(ValueError, match="another"):
        executor.configure_a2ui_catalog_registry(wanted)
    assert first.a2ui_catalog_registry is original
    assert second.a2ui_catalog_registry is existing
    assert executor._a2ui_catalog_registry is None
