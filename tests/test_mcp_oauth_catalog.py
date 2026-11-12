"""Model tool discovery keeps authorization, provider schemas and policy boundaries."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from ksadk.events import run_authority
from ksadk.mcp_runtime.oauth import BrokerError, call_mcp_tool, list_mcp_tools
from ksadk.mcp_runtime.oauth import langgraph as helpers
from ksadk.tools.gateway import ToolPolicy

SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}


@pytest.fixture
def authorized(monkeypatch):
    credential = {
        "resource": "https://fixture.example/mcp",
        "config_ref": "fixture-config",
        "grant_version": 1,
        "access_token": "fixture-secret",
        "allowed_tools": ["SearchApis", "CallApi"],
    }
    extension = SimpleNamespace(
        ensure_authorization=AsyncMock(return_value={"wait_id": "wait", "status": "resuming"}),
        require_completion=AsyncMock(),
        claim_dispatch=AsyncMock(return_value=None),
        finish_dispatch=AsyncMock(),
        release_declined_dispatch=AsyncMock(),
        client=SimpleNamespace(credential=AsyncMock(return_value=credential)),
    )
    session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=ListToolsResult(
                tools=[Tool(name="CallApi", description="Provider description", inputSchema=SCHEMA)]
            )
        ),
        call_tool=AsyncMock(
            return_value=CallToolResult(content=[TextContent(type="text", text="ok")])
        ),
    )
    monkeypatch.setattr(
        run_authority, "current_run_event_authority", lambda: SimpleNamespace(run_id="run")
    )
    import langgraph.types

    interrupts = []

    def interrupt(value):
        interrupts.append(value)
        if value["kind"] == "approval":
            return {"decisions": [{"type": "approve"}]}
        return {"wait_id": "wait", "revision": 4}

    monkeypatch.setattr(langgraph.types, "interrupt", interrupt)

    @asynccontextmanager
    async def connect(value):
        assert value == credential
        yield session

    monkeypatch.setattr(helpers, "connect", connect)
    with helpers.bind_oauth_extension(extension):
        yield SimpleNamespace(
            extension=extension, session=session, credential=credential, interrupts=interrupts
        )


async def test_discovery_paginates_filters_and_returns_only_model_metadata(authorized):
    s = authorized
    s.session.list_tools.side_effect = [
        ListToolsResult(
            tools=[Tool(name="SearchApis", description="search", inputSchema=SCHEMA)],
            nextCursor="page2",
        ),
        ListToolsResult(
            tools=[
                Tool(name="CallApi", description="call", inputSchema=SCHEMA),
                Tool(name="hidden", inputSchema={}),
            ]
        ),
    ]
    tools = await list_mcp_tools("cloud", request_key="persisted-model-call")
    assert [tool["name"] for tool in tools] == ["SearchApis", "CallApi"]
    assert tools[0]["inputSchema"] == SCHEMA
    assert all(set(tool) == {"name", "description", "inputSchema"} for tool in tools)
    assert "fixture-secret" not in repr(tools)
    assert len(s.interrupts) == 1 and s.interrupts[0]["kind"] == "external_authorization"
    s.extension.ensure_authorization.assert_awaited_once_with(
        "run", "cloud", "persisted-model-call"
    )
    s.extension.require_completion.assert_awaited_once_with("wait", "run", 4)
    s.session.list_tools.assert_awaited_with(cursor="page2")
    s.session.call_tool.assert_not_awaited()


async def test_callapi_without_policy_does_not_add_execution_approval(authorized):
    result = await call_mcp_tool(
        "cloud", "CallApi", {"query": "model-selected"}, request_key="call-2"
    )
    assert result["isError"] is False
    assert [item["kind"] for item in authorized.interrupts] == ["external_authorization"]
    authorized.session.call_tool.assert_awaited_once_with("CallApi", {"query": "model-selected"})


async def test_restored_tool_returns_durable_result_without_dispatch(authorized):
    result = {"content": [{"type": "text", "text": "original result"}], "isError": False}
    authorized.extension.claim_dispatch.return_value = result
    assert await call_mcp_tool("cloud", "CallApi", {"query": "q"}, request_key="call") == result
    authorized.session.call_tool.assert_not_awaited()
    authorized.extension.finish_dispatch.assert_not_awaited()


async def test_lost_dispatch_receipt_is_not_returned_as_a_retryable_error(authorized):
    from ksadk.mcp_runtime.oauth.client import MCPToolOutcomeUnknown

    authorized.extension.finish_dispatch.side_effect = OSError("receipt write disconnected")
    with pytest.raises(MCPToolOutcomeUnknown):
        await call_mcp_tool("cloud", "CallApi", {"query": "q"}, request_key="call")
    authorized.session.call_tool.assert_awaited_once()
    authorized.extension.release_declined_dispatch.assert_not_awaited()


async def test_developer_can_explicitly_require_execution_approval(authorized):
    s = authorized
    result = await call_mcp_tool(
        "cloud",
        "CallApi",
        {"query": "model-selected"},
        request_key="call-2",
        policy=ToolPolicy(risk_level="high", approval_scopes=("mcp",)),
    )
    assert result["isError"] is False
    assert [item["kind"] for item in s.interrupts] == ["external_authorization", "approval"]
    from ksadk.events.canonical import ApprovalRequest
    from ksadk.kernel.approval_presentation import approval_presentation

    preview = approval_presentation(
        ApprovalRequest(call_id="call", kind="tool", detail=s.interrupts[1])
    )
    assert preview.title == "CallApi"
    assert "model-selected" in preview.description
    s.session.call_tool.assert_awaited_once_with("CallApi", {"query": "model-selected"})


async def test_explicit_approval_rejection_prevents_dispatch(authorized, monkeypatch):
    import langgraph.types

    monkeypatch.setattr(
        langgraph.types,
        "interrupt",
        lambda value: (
            {"decisions": [{"type": "reject"}]}
            if value["kind"] == "approval"
            else {"wait_id": "wait", "revision": 4}
        ),
    )
    with pytest.raises(BrokerError, match="TOOL_APPROVAL_DENIED"):
        await call_mcp_tool(
            "cloud",
            "CallApi",
            {"query": "x"},
            request_key="call",
            policy=ToolPolicy(risk_level="high"),
        )
    authorized.session.call_tool.assert_not_awaited()


async def test_provider_schema_rejects_invalid_input_without_tool_dispatch(authorized):
    with pytest.raises(BrokerError, match="MCP_TOOL_INPUT_INVALID"):
        await call_mcp_tool("cloud", "CallApi", {}, request_key="call", policy=ToolPolicy())
    authorized.session.call_tool.assert_not_awaited()


async def test_optional_policy_cannot_be_bypassed_by_valid_provider_schema(authorized):
    authorized.credential["tool_policies"] = {
        "CallApi": {"properties": {"query": {"const": "allowed"}}}
    }
    with pytest.raises(BrokerError, match="MCP_TOOL_ARGUMENTS_NOT_ALLOWED"):
        await call_mcp_tool("cloud", "CallApi", {"query": "other"}, request_key="call")
    authorized.session.call_tool.assert_not_awaited()


async def test_tool_error_can_return_to_model_without_changing_default_behavior(authorized):
    authorized.session.call_tool.return_value = CallToolResult(
        isError=True, content=[TextContent(type="text", text="Please correct the query")]
    )
    result = await call_mcp_tool(
        "cloud",
        "CallApi",
        {"query": "q"},
        request_key="call",
        policy=ToolPolicy(),
        raise_on_error=False,
    )
    assert result["isError"] is True
    with pytest.raises(BrokerError, match="MCP_TOOL_FAILED"):
        await call_mcp_tool(
            "cloud", "CallApi", {"query": "q"}, request_key="call", policy=ToolPolicy()
        )


async def test_discovery_still_requires_trusted_completion(authorized):
    authorized.extension.require_completion.side_effect = BrokerError(
        "AUTHORIZATION_COMPLETION_REQUIRED"
    )
    with pytest.raises(BrokerError, match="AUTHORIZATION_COMPLETION_REQUIRED"):
        await list_mcp_tools("cloud", request_key="call")
    authorized.session.list_tools.assert_not_awaited()


async def test_empty_filtered_catalog_and_repeated_cursor_are_errors(authorized):
    authorized.credential["allowed_tools"] = ["not-present"]
    with pytest.raises(BrokerError, match="TOOLS_EMPTY"):
        await list_mcp_tools("cloud", request_key="call")
    authorized.session.list_tools.return_value = ListToolsResult(tools=[], nextCursor="repeated")
    with pytest.raises(BrokerError, match="MCP_TOOL_CATALOG_INVALID"):
        await list_mcp_tools("cloud", request_key="call")


def test_provider_schema_does_not_fetch_external_references():
    with pytest.raises(BrokerError, match="MCP_TOOL_SCHEMA_INVALID"):
        helpers.validate_input_schema({"$ref": "https://untrusted.example/schema"}, {})
    helpers.validate_input_schema({"$defs": {"q": {"type": "string"}}, "$ref": "#/$defs/q"}, "ok")


async def test_omitted_filter_discovers_all_provider_tools(authorized):
    authorized.credential.pop("allowed_tools")
    authorized.session.list_tools.return_value = ListToolsResult(
        tools=[
            Tool(name="first", inputSchema={}),
            Tool(name="second", inputSchema={}),
        ]
    )
    assert [tool["name"] for tool in await list_mcp_tools("cloud", request_key="all")] == [
        "first",
        "second",
    ]
