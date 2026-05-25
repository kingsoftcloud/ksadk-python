from __future__ import annotations

import json
import socket
import textwrap
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP
from sse_starlette.sse import AppStatus

from ksadk.detection import DetectionResult, FrameworkType


def _write_adk_project(tmp_path, source: str) -> DetectionResult:
    package_name = f"demo_agent_{uuid4().hex[:8]}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return DetectionResult(
        type=FrameworkType.ADK,
        name="demo-agent",
        entry_point=f"{package_name}/agent.py",
        package_path=str(package_dir),
        agent_variable="root_agent",
        confidence=1.0,
    )


@contextmanager
def _run_fastmcp_http_server(app):
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    if not server.started:
        raise RuntimeError("FastMCP test server failed to start")

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        AppStatus.should_exit = False
        AppStatus.should_exit_event = None


@pytest.fixture
def weather_mcp_server():
    server = FastMCP("weather")

    @server.tool
    def forecast(city: str) -> str:
        return f"forecast:{city}"

    app = server.http_app(path="/mcp", transport="streamable-http")
    with _run_fastmcp_http_server(app) as base_url:
        yield base_url


def test_load_mcp_server_configs_validates_shape():
    from ksadk.mcp_runtime import load_mcp_server_configs

    configs = load_mcp_server_configs(
        json.dumps(
            [
                {
                    "name": "weather",
                    "url": "https://example.com/mcp",
                    "api_key": "ak-123",
                    "tool_filter": ["forecast", "alerts"],
                    "tool_name_prefix": "weather",
                }
            ]
        )
    )

    assert len(configs) == 1
    assert configs[0].name == "weather"
    assert configs[0].url == "https://example.com/mcp"
    assert configs[0].api_key == "ak-123"
    assert configs[0].tool_filter == ("forecast", "alerts")
    assert configs[0].tool_name_prefix == "weather"


def test_load_mcp_server_configs_rejects_invalid_payloads():
    from ksadk.mcp_runtime import load_mcp_server_configs

    with pytest.raises(ValueError, match="JSON array"):
        load_mcp_server_configs("{}")

    with pytest.raises(ValueError, match="/mcp"):
        load_mcp_server_configs(
            json.dumps([{"name": "weather", "url": "https://example.com/api"}])
        )


def test_build_connection_params_includes_bearer_auth_header():
    from ksadk.mcp_runtime import MCPServerConfig, build_connection_params

    descriptor = MCPServerConfig(
        name="weather",
        url="https://example.com/mcp",
        api_key="ak-123",
        tool_filter=("forecast",),
        tool_name_prefix="weather",
    )

    params = build_connection_params(descriptor)

    assert params.url == "https://example.com/mcp"
    assert params.headers == {"Authorization": "Bearer ak-123"}


def test_build_connection_params_disables_proxy_for_loopback_urls(monkeypatch):
    from ksadk.mcp_runtime import MCPServerConfig, build_connection_params

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    descriptor = MCPServerConfig(name="local", url="http://127.0.0.1:8899/mcp")

    params = build_connection_params(descriptor)
    client = params.httpx_client_factory()

    try:
        assert client._trust_env is False
    finally:
        import anyio

        anyio.run(client.aclose)


@pytest.mark.asyncio
async def test_build_mcp_toolset_roundtrip_lists_and_calls_remote_tools(weather_mcp_server):
    from ksadk.mcp_runtime import MCPServerConfig, build_mcp_toolset

    headers_seen: list[dict[str, str]] = []

    def httpx_client_factory(headers=None, timeout=None, auth=None):
        headers_seen.append(dict(headers or {}))
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=True,
            trust_env=False,
        )

    descriptor = MCPServerConfig(
        name="weather",
        url=f"{weather_mcp_server}/mcp",
        api_key="secret-token",
        tool_filter=("forecast",),
        tool_name_prefix="weather",
    )
    toolset = build_mcp_toolset(
        descriptor,
        httpx_client_factory=httpx_client_factory,
    )

    tools = await toolset.get_tools_with_prefix()
    result = await tools[0]._run_async_impl(
        args={"city": "beijing"},
        tool_context=SimpleNamespace(_invocation_context=None),
        credential=None,
    )

    assert [tool.name for tool in tools] == ["weather_forecast"]
    assert result["content"][0]["text"] == "forecast:beijing"
    assert headers_seen[0]["Authorization"] == "Bearer secret-token"

    await toolset.close()


def test_adk_runner_load_agent_injects_mcp_toolsets_and_deduplicates(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeToolset:
        def __init__(self, key: str):
            self._ksadk_mcp_toolset_key = key

    monkeypatch.delenv("KSADK_ENABLE_MCP_TOOLS", raising=False)
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)
    monkeypatch.setattr(
        "ksadk.mcp_runtime.load_mcp_toolsets_from_env",
        lambda: [
            FakeToolset("https://example.com/mcp|weather"),
            FakeToolset("https://example.com/mcp|weather"),
        ],
    )

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    keys = [
        getattr(tool, "_ksadk_mcp_toolset_key", None)
        for tool in runner._agent.tools
        if getattr(tool, "_ksadk_mcp_toolset_key", None)
    ]
    assert keys == ["https://example.com/mcp|weather"]


def test_adk_runner_load_agent_skips_mcp_toolsets_when_disabled(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeToolset:
        def __init__(self, key: str):
            self._ksadk_mcp_toolset_key = key

    monkeypatch.setenv("KSADK_ENABLE_MCP_TOOLS", "0")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)
    monkeypatch.setattr(
        "ksadk.mcp_runtime.load_mcp_toolsets_from_env",
        lambda: [FakeToolset("https://example.com/mcp|weather")],
    )

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert all(
        getattr(tool, "_ksadk_mcp_toolset_key", None) is None
        for tool in runner._agent.tools
    )


@pytest.mark.asyncio
async def test_adk_runner_invoke_roundtrip_with_remote_mcp_tools(
    monkeypatch,
    tmp_path,
    weather_mcp_server,
):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Use the weather_forecast tool."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.agent = kwargs["agent"]

        async def run_async(
            self,
            *,
            session_id,
            user_id,
            new_message,
            state_delta=None,
            run_config=None,
        ):
            toolsets = [
                tool for tool in self.agent.tools if hasattr(tool, "get_tools_with_prefix")
            ]
            assert toolsets
            tools = await toolsets[0].get_tools_with_prefix()
            payload = await tools[0]._run_async_impl(
                args={"city": new_message.parts[0].text},
                tool_context=SimpleNamespace(_invocation_context=None),
                credential=None,
            )
            text = payload["content"][0]["text"]
            yield SimpleNamespace(
                content=SimpleNamespace(
                    parts=[SimpleNamespace(text=text, thought=False)]
                )
            )

    monkeypatch.delenv("KSADK_ENABLE_MCP_TOOLS", raising=False)
    monkeypatch.setenv(
        "KSADK_MCP_SERVERS",
        json.dumps(
            [
                {
                    "name": "weather",
                    "url": f"{weather_mcp_server}/mcp",
                    "api_key": "secret-token",
                    "tool_filter": ["forecast"],
                    "tool_name_prefix": "weather",
                }
            ]
        ),
    )
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    result = await runner.invoke({"input": "beijing"})

    assert result["output"] == "forecast:beijing"
    assert any(
        getattr(tool, "_ksadk_mcp_toolset_key", None)
        for tool in runner._agent.tools
    )

    for toolset in runner._runtime_toolsets:
        close = getattr(toolset, "close", None)
        if close is not None:
            await close()
