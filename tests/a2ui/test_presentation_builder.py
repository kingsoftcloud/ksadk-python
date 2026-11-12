"""Presentation packing is shared protocol work, independent from Agent business state."""

from copy import deepcopy

import pytest

from ksadk.a2ui import A2uiCatalogRegistry, build_a2ui_presentation
from ksadk.a2ui.catalog import catalog_document, catalog_manifest
from ksadk.a2ui.legacy import legacy_catalog_document
from ksadk.a2ui.presentation import prepare_presentation
from ksadk.a2ui.registry import catalog_scope


def snapshot(catalog_id):
    return [
        {"version": "v0.9", "createSurface": {
            "surfaceId": "input", "catalogId": catalog_id,
        }},
        {"version": "v0.9", "updateComponents": {
            "surfaceId": "input", "components": [
                {"id": "root", "component": "Text", "text": "Business input"},
            ],
        }},
    ]


@pytest.mark.parametrize("catalog_id", [
    catalog_manifest()["catalog_id"], legacy_catalog_document()["catalogId"],
])
def test_builtin_presentation_uses_actual_catalog_without_mutating_messages(catalog_id):
    messages = snapshot(catalog_id)
    original = deepcopy(messages)
    result = build_a2ui_presentation(messages, title="Your choice", description="Explain")
    assert result == prepare_presentation(result)
    assert result["a2ui"]["catalog_digest"] == A2uiCatalogRegistry().describe(catalog_id)[
        "catalog_digest"
    ]
    assert result["title"] == "Your choice" and result["description"] == "Explain"
    assert result["a2ui"]["messages"] == original == messages
    messages[1]["updateComponents"]["components"][0]["text"] = "Changed"
    assert result["a2ui"]["messages"] == original
    assert "kind" not in result and "schema" not in result


def test_registered_catalog_and_application_scope_have_the_same_identity():
    document = catalog_document()
    identity = "https://example.test/custom/v1"
    document["catalogId"] = document["$id"] = identity
    registry = A2uiCatalogRegistry([document])
    direct = build_a2ui_presentation(snapshot(identity), catalog_registry=registry)
    with catalog_scope(registry):
        assert build_a2ui_presentation(snapshot(identity)) == direct
    assert prepare_presentation(direct, registry=registry) == direct
    with pytest.raises(ValueError, match="Register"):
        build_a2ui_presentation(snapshot(identity))


def test_frontend_owned_catalog_requires_an_explicit_declared_digest():
    messages = snapshot("urn:example:frontend-only")
    with pytest.raises(ValueError, match="Register"):
        build_a2ui_presentation(messages)
    result = build_a2ui_presentation(messages, catalog_digest="a" * 64)
    assert result["a2ui"]["messages"] == messages
    assert prepare_presentation(result) == result


@pytest.mark.parametrize("failure", ["digest", "component", "lifecycle", "mixed"])
def test_invalid_snapshot_is_rejected_instead_of_emitting_unbound_input(failure):
    messages = snapshot(catalog_manifest()["catalog_id"])
    kwargs = {}
    if failure == "digest":
        kwargs["catalog_digest"] = "a" * 64
    elif failure == "component":
        messages[1]["updateComponents"]["components"][0]["component"] = "Script"
    elif failure == "lifecycle":
        messages.pop(0)
    else:
        extra = snapshot(legacy_catalog_document()["catalogId"])
        extra[0]["createSurface"]["surfaceId"] = "second"
        messages.append(extra[0])
    with pytest.raises(ValueError):
        build_a2ui_presentation(messages, **kwargs)
