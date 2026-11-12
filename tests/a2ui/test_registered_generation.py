"""The real toolkit gets the same catalog that the renderer validates."""

import json
from types import SimpleNamespace

import pytest

from ksadk.a2ui.catalog import catalog_document, catalog_manifest
from ksadk.compat.copilotkit_a2ui import (
    _KSADK_HOSTED_UI_GUIDELINES,
    _generate_a2ui_envelope,
)


@pytest.mark.parametrize("first_component", ["Text", "UnregisteredWidget"])
async def test_registered_generation_validates_and_corrects_against_official_catalog(
    first_component,
):
    prompts = []
    attempts = []

    class Model:
        def bind_tools(self, tools, *, tool_choice):
            assert tool_choice == "render_a2ui" and len(tools) == 1
            return self

        async def ainvoke(self, messages):
            prompts.append(messages[0].content)
            return SimpleNamespace(tool_calls=[{
                "name": "render_a2ui", "args": {
                    "surfaceId": "generated", "data": {}, "components": [{
                        "id": "root", "component": first_component if len(prompts) == 1 else "Text",
                        "text": "Available",
                    }],
                },
            }])

    manifest = catalog_manifest()
    output = await _generate_a2ui_envelope(
        config={
            "model": Model(), "default_catalog_id": manifest["catalog_id"],
            "catalog": catalog_document(), "default_surface_id": "generated",
            "guidelines": _KSADK_HOSTED_UI_GUIDELINES, "recovery": {"maxAttempts": 2},
            "on_a2ui_attempt": attempts.append,
        }, state={}, messages=[], intent="create", target_surface_id=None, changes=None,
    )
    operations = json.loads(output)["a2ui_operations"]
    assert all(op["version"] == "v0.9.1" for op in operations)
    assert operations[0]["createSurface"]["catalogId"] == manifest["catalog_id"]
    assert attempts[-1]["ok"]
    assert len(prompts) == (1 if first_component == "Text" else 2)
    assert manifest["catalog_digest"] in prompts[0]
    assert "commonTypes" in prompts[0] and "TextField" in prompts[0]
    if first_component != "Text":
        assert not attempts[0]["ok"]
