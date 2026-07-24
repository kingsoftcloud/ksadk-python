from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ksadk.compat import copilotkit_a2ui


def _request(*, inject: bool):
    return SimpleNamespace(
        model=object(),
        tools=[],
        state={
            "ag-ui": {
                "inject_a2ui_tool": inject,
                "a2ui_schema": '{"catalogId":"https://example.test/catalog"}',
            }
        },
    )


def test_builds_ksadk_a2ui_tool_with_host_catalog_contract(monkeypatch):
    captured = {}
    tool = SimpleNamespace(name="generate_a2ui")

    def factory(params):
        captured.update(params)
        return tool

    monkeypatch.setattr(copilotkit_a2ui, "build_ksadk_a2ui_tool", factory)
    middleware = copilotkit_a2ui.KsadkCopilotKitMiddleware()
    request = _request(inject=True)

    assert middleware._maybe_build_a2ui_tool(request) is tool
    assert captured["model"] is request.model
    assert captured["default_catalog_id"] == "https://example.test/catalog"


def test_does_not_create_a2ui_tool_without_host_opt_in(monkeypatch):
    monkeypatch.setattr(
        copilotkit_a2ui,
        "build_ksadk_a2ui_tool",
        lambda _params: (_ for _ in ()).throw(AssertionError("factory should not run")),
    )
    middleware = copilotkit_a2ui.KsadkCopilotKitMiddleware()

    assert middleware._maybe_build_a2ui_tool(_request(inject=False)) is None


def test_uses_compact_hosted_ui_guidelines_by_default(monkeypatch):
    captured = {}
    original = copilotkit_a2ui._resolve_a2ui_tool_params

    def resolve(params):
        captured.update(params)
        return original(params)

    monkeypatch.setattr(copilotkit_a2ui, "_resolve_a2ui_tool_params", resolve)

    copilotkit_a2ui.build_ksadk_a2ui_tool({"model": object()})

    assert captured["guidelines"] is copilotkit_a2ui._KSADK_HOSTED_UI_GUIDELINES


@pytest.mark.asyncio
async def test_a2ui_generation_runs_async_model_on_parent_event_loop():
    class Model:
        loop = None

        def bind_tools(self, _tools, *, tool_choice):
            assert tool_choice == "render_a2ui"
            return self

        async def ainvoke(self, _messages):
            self.loop = asyncio.get_running_loop()
            return SimpleNamespace(
                tool_calls=[
                    {
                        "name": "render_a2ui",
                        "args": {
                            "surfaceId": "component-status",
                            "components": [
                                {
                                    "id": "root",
                                    "component": "Text",
                                    "text": "ready",
                                }
                            ],
                            "data": {},
                        },
                    }
                ]
            )

    model = Model()
    current_loop = asyncio.get_running_loop()
    envelope = await copilotkit_a2ui._generate_a2ui_envelope(
        config={
            "model": model,
            "guidelines": None,
            "default_surface_id": "dynamic-surface",
            "default_catalog_id": "https://example.test/catalog",
            "catalog": None,
            "recovery": None,
        },
        state={"ag-ui": {}},
        messages=[],
        intent="create",
        target_surface_id=None,
        changes=None,
    )

    assert model.loop is current_loop
    operations = json.loads(envelope)["a2ui_operations"]
    assert operations[0]["createSurface"] == {
        "surfaceId": "component-status",
        "catalogId": "https://example.test/catalog",
    }


@pytest.mark.asyncio
async def test_a2ui_generation_returns_error_after_bounded_timeout(monkeypatch):
    class Model:
        def bind_tools(self, _tools, *, tool_choice):
            return self

        async def ainvoke(self, _messages):
            await asyncio.Event().wait()

    monkeypatch.setattr(copilotkit_a2ui, "_generation_timeout_seconds", lambda: 0.01)
    envelope = await copilotkit_a2ui._generate_a2ui_envelope(
        config={
            "model": Model(),
            "guidelines": None,
            "default_surface_id": "dynamic-surface",
            "default_catalog_id": "https://example.test/catalog",
            "catalog": None,
            "recovery": None,
        },
        state={"ag-ui": {}},
        messages=[],
        intent="create",
        target_surface_id=None,
        changes=None,
    )

    assert json.loads(envelope)["error"].startswith("A2UI generation exceeded")
