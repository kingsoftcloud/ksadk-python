"""Client catalogs fail as generation diagnostics before entering the model."""

import json
from types import SimpleNamespace

import pytest

from ksadk.a2ui import A2uiCatalogRegistry
from ksadk.compat.copilotkit_a2ui import _generate_a2ui_envelope


def catalog():
    return {"catalogId": "urn:review:catalog", "components": {"Widget": {"type": "object"}}}


def invalid_catalog(case):
    document = catalog()
    if case == "schema":
        document["components"]["Widget"] = {"type": 5}
    elif case == "depth":
        schema = {"type": "object"}
        for _ in range(400):
            schema = {"allOf": [schema]}
        document["components"]["Widget"] = schema
    elif case == "bytes":
        document["description"] = "x" * 1_048_576
    elif case == "components":
        document["components"] = {f"Widget{i}": {"type": "object"} for i in range(2049)}
    elif case == "resources":
        document["ksadkResources"] = [
            {"$id": f"urn:resource:{i}", "type": "string"} for i in range(129)
        ]
    elif case == "dialect":
        document["$schema"] = "urn:review:unknown-schema-dialect"
    return document


@pytest.mark.parametrize("case", ["schema", "depth", "bytes", "components", "resources", "dialect"])
async def test_invalid_client_catalog_is_a_diagnostic_before_model_execution(case):
    class NoModel:
        def bind_tools(self, *args, **kwargs):
            pytest.fail("Invalid catalog reached model execution")

    document = invalid_catalog(case)
    output = await _generate_a2ui_envelope(
        config={"model": NoModel(), "default_catalog_id": document["catalogId"]},
        state={"ag-ui": {"a2ui_schema": json.dumps(document)}},
        messages=[], intent="create", target_surface_id=None, changes=None,
    )
    assert isinstance(json.loads(output)["error"], str)
    assert "a2ui_operations" not in json.loads(output)


def test_registered_invalid_component_schema_uses_the_public_value_error_contract():
    with pytest.raises(ValueError, match="schema"):
        A2uiCatalogRegistry([invalid_catalog("schema")])


def test_expanding_offline_references_have_a_bounded_linking_budget():
    document = catalog()
    document["components"]["Widget"] = {"$ref": "urn:level:0"}
    resources = [{"$id": f"urn:level:{i}", "allOf": [
        {"$ref": f"urn:level:{i + 1}"}, {"$ref": f"urn:level:{i + 1}"},
    ]} for i in range(16)]
    resources.append({"$id": "urn:level:16", "type": "object"})
    with pytest.raises(ValueError, match="budget"):
        A2uiCatalogRegistry([document], resources=resources)


def test_repeated_refs_cannot_duplicate_unbounded_opaque_default_data():
    document = catalog()
    document["components"]["Widget"] = {"$ref": "urn:opaque:0"}
    resources = [{"$id": f"urn:opaque:{i}", "allOf": [
        {"$ref": f"urn:opaque:{i + 1}"}, {"$ref": f"urn:opaque:{i + 1}"},
    ]} for i in range(9)]
    resources.append({"$id": "urn:opaque:9", "type": "object", "default": [0] * 2048})
    with pytest.raises(ValueError, match="budget"):
        A2uiCatalogRegistry([document], resources=resources)


def test_offline_reference_preserves_opaque_default_business_data():
    from referencing import Registry, Resource

    from ksadk.a2ui.linking import link_catalog

    default = {"$ref": "https://business-data.invalid/not-a-schema", "values": [None, False, 0]}
    document = catalog()
    document["components"]["Widget"] = {"$ref": "urn:opaque:data"}
    resource = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:opaque:data",
                "type": "object", "default": default}
    registry = Registry().with_resource("urn:opaque:data", Resource.from_contents(resource))
    linked = link_catalog(document, registry)
    assert linked["components"]["Widget"]["default"] == default
    assert linked["components"]["Widget"]["default"] is not default


def test_studio_approval_uses_its_actual_registered_legacy_catalog():
    from ksadk.a2ui.wire import A2uiWireState
    from ksadk.studio.run_service import StudioRunService

    events = []

    def append(run_id, event_type, data):
        event = SimpleNamespace(type=event_type, data=data)
        events.append(event)
        return event

    service = SimpleNamespace(event_store=SimpleNamespace(append=append))
    request = SimpleNamespace(detail={"command": "echo review"}, kind="tool", call_id="call")
    StudioRunService._persist_approval_surface(
        service, SimpleNamespace(id="run"),
        SimpleNamespace(interaction_id="approval", request=request), on_event=None,
    )
    state = A2uiWireState()
    state.accept(events[0].data["a2uiOperations"])
    identity = state.catalogs["approval-approval"]
    assert state.registry.describe(identity)["profile"] == "ksadk-legacy-seven-components"
    assert state.registry.describe(identity)["catalog_validation"] == "full"


def test_missing_projection_identity_uses_a_registered_catalog_and_preserves_explicit_ids():
    from ksadk.agui.a2ui_projection import project_a2ui_operations

    state = A2uiCatalogRegistry()
    operations = project_a2ui_operations("a2ui.surface.begin", {"surface_id": "test"})
    identity = operations[0]["createSurface"]["catalogId"]
    assert state.describe(identity)["catalog_validation"] == "full"
    explicit = project_a2ui_operations("a2ui.surface.begin", {
        "surface_id": "test", "catalog_id": "urn:customer:catalog",
    })
    assert explicit[0]["createSurface"]["catalogId"] == "urn:customer:catalog"
