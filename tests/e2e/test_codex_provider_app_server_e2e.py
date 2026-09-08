"""Credential-free Bundle -> PluginHost -> real Codex App Server evidence."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ksadk.events.canonical import ContinuationCreated, ItemCompleted
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent
from ksadk.events.store import RuntimeEventStore
from ksadk.plugins.host import PluginHost
from ksadk.plugins.providers.codex import CodexAgentProviderFactory
from ksadk.plugins.resolver import PluginRegistry
from ksadk.sessions.in_memory import InMemorySessionService
from tests.e2e.codex_app_server_fixture import RealCodexFactory
from tests.e2e.codex_responses_stub import DeterministicResponsesStub
from tests.harness.fixtures.mcp_server import run_fixture_mcp_server
from tests.plugins.test_codex_provider_vertical import (
    _profile,
    _provider_manifest,
    _write_bundle,
)

pytestmark = pytest.mark.skipif(
    os.getenv("KSADK_CODEX_PROVIDER_E2E") != "1",
    reason="set KSADK_CODEX_PROVIDER_E2E=1 to exercise the real Codex App Server",
)


def _request_texts(request: Any) -> str:
    texts = [
        *request.input_texts("system"),
        *request.input_texts("developer"),
        *request.input_texts("user"),
        *request.input_texts("assistant"),
    ]
    instructions = request.payload.get("instructions")
    if isinstance(instructions, str):
        texts.insert(0, instructions)
    return "\n".join(texts)


@pytest.mark.asyncio
async def test_real_app_server_calls_bundle_mcp_twice_and_resumes_native_thread(
    tmp_path: Path,
) -> None:
    registry = PluginRegistry([_provider_manifest()])
    profile = _profile()
    session_service = InMemorySessionService()

    with (
        run_fixture_mcp_server(label="sunny", required_token="") as mcp,
        DeterministicResponsesStub(mcp_namespace="mcp__weather") as responses,
    ):
        bundle = _write_bundle(
            tmp_path / "bundle",
            registry,
            profile,
            approval_mode="full",
            mcp_servers=[
                {
                    "name": "weather",
                    "transport": "http",
                    "endpointUrl": mcp.url,
                    "envRefs": {},
                }
            ],
        )
        client_factory = RealCodexFactory(responses_url=responses.base_url)
        provider = CodexAgentProviderFactory(
            session_service=session_service,
            codex_client_factory=client_factory,
        )
        host = PluginHost(registry, {"io.ksadk.codex-provider": provider})
        await host.apply(profile)
        try:
            first = await host.execute(
                bundle,
                {"user_id": "provider-e2e", "input": "first provider turn"},
            )
            second = await host.execute(
                bundle,
                {
                    "user_id": "provider-e2e",
                    "session_id": first.session_id,
                    "input": "second provider turn",
                },
            )
        finally:
            await host.dispose()

    assert first.session_id == second.session_id
    assert first.output_text == "MCP weather observation: sunny:first provider turn"
    assert second.output_text == "MCP weather observation: sunny:second provider turn"
    assert first.inventory.skills == ("report-style",)
    assert first.inventory.mcp_servers == ("weather",)
    requests = responses.requests()
    assert mcp.log.calls == [
        ("lookup", "first provider turn"),
        ("lookup", "second provider turn"),
    ]

    assert len(requests) == 4
    first_text = _request_texts(requests[0])
    second_text = _request_texts(requests[2])
    assert "You are a report assistant." in first_text
    assert "report-style" in first_text
    assert "<name>report-style</name>" in first_text
    assert "Use concise reports." in first_text
    assert "first provider turn" in first_text
    assert "second provider turn" in second_text
    assert "MCP weather observation: sunny:first provider turn" in second_text
    assert all(request.payload["model"] == "fixture-codex-model" for request in requests)
    native_tools = requests[0].payload["tools"]
    weather = next(tool for tool in native_tools if tool.get("name") == "mcp__weather")
    assert weather["type"] == "namespace"
    assert [tool["name"] for tool in weather["tools"]] == ["forbidden", "lookup"]
    thread_ids = {
        request.payload["client_metadata"]["thread_id"] for request in requests
    }
    assert len(thread_ids) == 1

    events = await RuntimeEventStore(session_service).list(first.session_id)
    continuations = [event for event in events if isinstance(event, ContinuationCreated)]
    assert len(continuations) == 1
    assert continuations[0].ref["thread_id"] == requests[0].payload["client_metadata"]["thread_id"]
    assert [event.event_type for event in events].count("run.completed") == 2
    completed_messages = [
        "".join(
            part.text for part in event.snapshot.parts if isinstance(part, TextContent)
        )
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "message"
    ]
    assert completed_messages == [first.output_text, second.output_text]
    completed_tools = [
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call"
    ]
    assert len(completed_tools) == 2
    for event, value in zip(
        completed_tools,
        ("first provider turn", "second provider turn"),
        strict=True,
    ):
        call = next(part for part in event.snapshot.parts if isinstance(part, ToolCallContent))
        result = next(part for part in event.snapshot.parts if isinstance(part, ToolResultContent))
        assert call.name == "mcp.weather.lookup"
        assert call.arguments == {"value": value}
        assert result.call_id == call.call_id
        assert result.is_error is False
        assert str(result.result).find(f"sunny:{value}") >= 0

    assert len(client_factory.processes) == 2
    assert all(process.poll() is not None for process in client_factory.processes)
