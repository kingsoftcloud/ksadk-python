from __future__ import annotations

import asyncio
import base64
import json
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from ksadk._mcp_compat import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import connect

from ksadk.tunnel import (
    LocalMCPService,
    TunnelConnector,
    TunnelError,
    TunnelLimits,
    TunnelRegistry,
)
from ksadk.tunnel.protocol import decode, encode
from ksadk.tunnel.registry import ConnectorConnection
from ksadk.tunnel.runtime import TunnelBinding, bind_runner
from ksadk.tunnel.server import TunnelAgentPolicy, mount_tunnel


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await eventually(lambda: server.started or task.done())
        if task.done():
            await task
            raise RuntimeError("test server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sock.close()


@asynccontextmanager
async def cloud(*, limits=None):
    registry = TunnelRegistry()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await registry.close()

    app = FastAPI(lifespan=lifespan)
    headers = []

    # A real ASGI gateway-style handshake authorizer, separate from tunnel auth.
    class Gateway:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "websocket":
                headers.append(dict(scope["headers"]))
            await app(scope, receive, send)

    mount_tunnel(
        app,
        policies=[TunnelAgentPolicy("deployment-17", "fake-tunnel-token", ("erp",))],
        registry=registry,
        limits=limits,
    )
    async with serve(Gateway()) as url:
        yield app, registry, url, headers


@asynccontextmanager
async def local_mcp():
    # SSE-Starlette shares Uvicorn's shutdown flag across apps in one Python process.
    # Each fixture represents a fresh enterprise service.
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False
    server = MCPServer("enterprise")
    calls = []
    started = asyncio.Event()
    auth = []

    @server.tool()
    async def echo(text: str) -> CallToolResult:
        calls.append(text)
        return CallToolResult(
            content=[
                TextContent(type="text", text=text),
                ImageContent(
                    type="image",
                    data=base64.b64encode(b"\x00\xff\x80").decode(),
                    mimeType="image/png",
                ),
            ],
            structuredContent={"text": text},
            isError=False,
        )

    @server.tool()
    async def wait_forever() -> str:
        calls.append("wait")
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    @server.tool()
    async def forbidden() -> str:
        calls.append("forbidden")
        return "not admitted"

    from tests.mcp_http_fixture import streamable_app

    mcp_app = streamable_app(server, json_response=False)

    class Capture:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                auth.append(
                    (
                        dict(scope["headers"]).get(b"authorization", b"").decode(),
                        scope["query_string"].decode(),
                    )
                )
            await mcp_app(scope, receive, send)

    async with serve(Capture()) as url:
        yield url + "/mcp?credential=fake-local-query", calls, started, auth


def connector(url, endpoint, *, limits=None, filters=("echo", "wait_forever")):
    return TunnelConnector(
        url,
        agent_id="deployment-17",
        token="fake-tunnel-token",
        services=[
            LocalMCPService(
                "erp",
                endpoint,
                headers={"Authorization": "Bearer fake-local-only"},
                tool_filter=filters,
            )
        ],
        gateway_headers={"Authorization": "Bearer fake-gateway-only"},
        limits=limits,
        reconnect_min=0.01,
        reconnect_max=0.05,
    )


@asynccontextmanager
async def connected(client):
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(client.connected.wait(), 5)
        yield task
    finally:
        await client.close()
        await asyncio.wait_for(task, 5)


async def test_real_websocket_mcp_dynamic_discovery_call_and_secret_boundary():
    from ksadk.tunnel import TunnelToolset

    async with (
        local_mcp() as (endpoint, calls, _, local_auth),
        cloud() as (app, reg, url, handshake),
    ):
        toolset = TunnelToolset("deployment-17", reg)
        assert await toolset.get_tools() == []
        client = connector(url, endpoint)
        async with connected(client):
            tools = await toolset.get_tools_with_prefix()
            assert len(tools) == 2
            tool = next(t for t in tools if t._descriptor.name == "echo")
            assert (
                tool._get_declaration().parameters_json_schema["properties"]["text"]["type"]
                == "string"
            )
            result = await tool.run_async(args={"text": "你好🌏"}, tool_context=None)
            assert result["content"][0]["text"] == "你好🌏"
            assert base64.b64decode(result["content"][1]["data"]) == b"\x00\xff\x80"
            assert result["structuredContent"] == {"text": "你好🌏"}
            assert calls == ["你好🌏"]
            assert all(header == "Bearer fake-local-only" for header, _ in local_auth)
            assert all(query == "credential=fake-local-query" for _, query in local_auth)
            assert handshake[0][b"authorization"] == b"Bearer fake-gateway-only"
            assert b"fake-tunnel-token" not in repr(handshake).encode()
            async with httpx.AsyncClient(trust_env=False) as http:
                assert (await http.post(url + "/tunnel/mcp/deployment-17/erp")).status_code == 404
                assert (await http.get(url + "/tunnel/servers")).status_code == 404
            assert await TunnelToolset("other-deployment", reg).get_tools() == []
            with pytest.raises(TunnelError, match="forbidden"):
                await reg.call_tool("deployment-17", "erp", "forbidden", {})
            # The connector itself checks the filter even for a direct HTTP request.
            connection = reg.connections("deployment-17")[0][1]
            denied = httpx.Request(
                "POST",
                "http://tunnel/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "forbidden", "arguments": {}},
                },
            )
            with pytest.raises(TunnelError, match="forbidden"):
                await connection.forward("erp", denied)
            assert calls == ["你好🌏"]
        await eventually(lambda: reg.connections("deployment-17") == [])
        assert await toolset.get_tools_with_prefix() == []
        with pytest.raises(TunnelError, match="disconnected"):
            await tool.run_async(args={"text": "no retry"}, tool_context=None)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"token": "wrong"}, 4403),
        ({"agent_id": "other"}, 4403),
        ({"services": [{"name": "not-admitted", "protocol": "mcp"}]}, 4403),
        ({"services": []}, 4403),
        ({"url": "http://arbitrary.invalid"}, 4400),
        ({"version": 1, "services": ["erp"]}, 4400),
        ({"version": 99}, 4400),
    ],
)
async def test_registration_auth_and_explicit_alias_policy(changes, code):
    async with cloud() as (_, reg, url, _):
        async with connect(
            url.replace("http:", "ws:") + "/tunnel/connect?token=fake-tunnel-token",
            extra_headers={"Authorization": "Bearer fake-tunnel-token"},
        ) as ws:
            frame = {
                "type": "register",
                "version": 2,
                "agent_id": "deployment-17",
                "token": "fake-tunnel-token",
                "services": [{"name": "erp", "protocol": "mcp", "tool_filter": None}],
                **changes,
            }
            await ws.send(json.dumps(frame))
            if code == 4403:
                assert json.loads(await ws.recv()) == {
                    "type": "registration_rejected",
                    "version": 2,
                    "error": "unauthorized",
                }
            with pytest.raises(ConnectionClosed) as caught:
                await ws.recv()
            assert caught.value.code == code
        assert reg.connections("deployment-17") == []


async def test_gateway_header_and_query_cannot_replace_register_token():
    async with cloud() as (_, _, url, _):
        async with connect(
            url.replace("http:", "ws:") + "/tunnel/connect?token=fake-tunnel-token",
            extra_headers={"Authorization": "Bearer fake-tunnel-token"},
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "version": 2,
                        "agent_id": "deployment-17",
                        "services": [{"name": "erp", "protocol": "mcp", "tool_filter": None}],
                    }
                )
            )
            with pytest.raises(ConnectionClosed) as caught:
                await ws.recv()
            assert caught.value.code == 4400


async def test_duplicate_does_not_replace_and_stale_handle_cannot_rebind_after_reconnect():
    from ksadk.tunnel import TunnelToolset

    async with local_mcp() as (endpoint, calls, _, _), cloud() as (_, reg, url, _):
        client = connector(url, endpoint)
        async with connected(client):
            old_tool = next(
                t
                for t in await TunnelToolset("deployment-17", reg).get_tools()
                if t._descriptor.name == "echo"
            )
            old_conn = reg.connections("deployment-17")[0][1]
            duplicate = connector(url, endpoint)
            with pytest.raises(TunnelError, match="duplicate_service"):
                await duplicate.run(reconnect=False)
            assert reg.connections("deployment-17")[0][1] is old_conn
            await old_conn.websocket.close(code=1012)
            await eventually(
                lambda: bool(reg.connections("deployment-17"))
                and reg.connections("deployment-17")[0][1] is not old_conn
            )
            with pytest.raises(TunnelError, match="disconnected"):
                await old_tool.run_async(args={"text": "stale"}, tool_context=None)
            fresh = next(
                t
                for t in await TunnelToolset("deployment-17", reg).get_tools()
                if t._descriptor.name == "echo"
            )
            assert (await fresh.run_async(args={"text": "fresh"}, tool_context=None))["content"][0][
                "text"
            ] == "fresh"
            assert calls == ["fresh"]


async def test_disconnect_marks_indeterminate_call_and_never_replays_it():
    async with local_mcp() as (endpoint, calls, started, _), cloud() as (_, reg, url, _):
        async with connected(connector(url, endpoint)):
            old_conn = reg.connections("deployment-17")[0][1]
            request = asyncio.create_task(reg.call_tool("deployment-17", "erp", "wait_forever", {}))
            await asyncio.wait_for(started.wait(), 3)
            await old_conn.websocket.close(code=1012)
            with pytest.raises(TunnelError) as caught:
                await request
            assert caught.value.indeterminate
            await eventually(
                lambda: bool(reg.connections("deployment-17"))
                and reg.connections("deployment-17")[0][1] is not old_conn
            )
            await asyncio.sleep(0.1)
            assert calls == ["wait"]
            assert old_conn.pending == {}


async def test_timeout_inflight_bound_concurrent_progress_and_cleanup():
    bounds = TunnelLimits(
        max_inflight=2, request_timeout=0.6, heartbeat_interval=0.05, idle_timeout=0.4
    )
    async with local_mcp() as (endpoint, _, started, _), cloud(limits=bounds) as (_, reg, url, _):
        async with connected(connector(url, endpoint, limits=bounds)):
            conn = reg.connections("deployment-17")[0][1]
            request = asyncio.create_task(reg.call_tool("deployment-17", "erp", "wait_forever", {}))
            await asyncio.wait_for(started.wait(), 3)
            other = await reg.call_tool("deployment-17", "erp", "echo", {"text": "concurrent"})
            assert other["content"][0]["text"] == "concurrent"
            with pytest.raises(TunnelError) as caught:
                await request
            assert caught.value.indeterminate
            await eventually(lambda: not conn.pending)


async def test_registration_deadline_and_malformed_frame_cleanup():
    bounds = TunnelLimits(registration_timeout=0.05)
    async with cloud(limits=bounds) as (_, reg, url, _):
        async with connect(url.replace("http:", "ws:") + "/tunnel/connect") as ws:
            with pytest.raises(ConnectionClosed):
                await ws.recv()
        async with connect(url.replace("http:", "ws:") + "/tunnel/connect") as ws:
            await ws.send("[]")
            with pytest.raises(ConnectionClosed):
                await ws.recv()
        assert reg.connections("deployment-17") == []


def test_frame_limits_redaction_and_unambiguous_tool_names():
    from ksadk.tunnel.toolset import _tool_name

    assert _tool_name("a__b", "c") != _tool_name("a", "b__c")
    assert len(_tool_name("service", "x" * 300)) <= 64
    with pytest.raises(TunnelError, match="frame_too_large"):
        encode({"result": "x" * 200}, TunnelLimits(max_frame_bytes=50))
    with pytest.raises(ValueError):
        decode('{"value": NaN}', TunnelLimits())
    service = LocalMCPService(
        "erp", "http://localhost/mcp?credential=fake", headers={"key": "fake"}
    )
    assert "fake" not in repr(service)
    with pytest.raises(ValueError):
        LocalMCPService("erp", "http://user:secret@localhost/mcp")


def test_connector_core_import_without_adk():
    source = """
import importlib.abc
import sys
class NoAdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "google.adk" or fullname.startswith("google.adk."):
            raise ModuleNotFoundError("ADK absent", name=fullname)
sys.meta_path.insert(0, NoAdk())
from ksadk.tunnel import LocalMCPService, TunnelConnector
assert not any(name.startswith("google.adk") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_runtime_factory_owns_registry_and_binds_every_new_runner(tmp_path, monkeypatch):
    from ksadk.runtime.executor import RuntimeExecutor
    from ksadk.runtime.factory import build_default_runtime_registry
    from ksadk.runtime.launch import RuntimeLaunchContext
    from ksadk.server.factory import RuntimeAppConfig, create_runtime_app

    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fake-token")
    (tmp_path / "declared.py").write_text(
        "from ksadk import Agent\nfrom types import SimpleNamespace\n"
        "root_agent = Agent(SimpleNamespace(), enable_tunnel=True)\n"
    )
    context = RuntimeLaunchContext(
        "adk",
        tmp_path,
        detection=SimpleNamespace(entry_point="declared.py", agent_variable="root_agent"),
    )
    config = RuntimeAppConfig(
        runtime_executor=RuntimeExecutor(build_default_runtime_registry()),
        launch_context=context,
        route_groups=set(),
    )
    first = create_runtime_app(config)
    second = create_runtime_app(config)
    first_slot = first.state.runtime.launch_context.services.tunnel_binding
    second_slot = second.state.runtime.launch_context.services.tunnel_binding
    assert first_slot.for_connector().registry is not second_slot.for_connector().registry
    assert config.launch_context is context
    assert context.services.tunnel_binding is None
    bound = first.state.runtime.launch_context
    assert bound.services.tunnel_binding is first_slot
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(context.detection, str(tmp_path))
    launch = __import__("dataclasses").replace(
        bound,
        services=__import__("dataclasses").replace(
            bound.services, runner_factory=lambda *_: runner
        ),
    )
    adapter = config.runtime_executor.create_adapter(launch)
    assert adapter._runner._deferred_tunnel is first_slot
    assert first_slot.binding.policy.agent_id == "root"


async def test_runners_isolate_tunnel_tools_from_shared_module_and_other_apps(
    tmp_path, monkeypatch
):
    from ksadk.compat.adk_compat import Agent
    from ksadk.runners.adk_runner import ADKRunner
    from ksadk.tunnel import TunnelToolset

    monkeypatch.setenv("KSADK_ENABLE_MCP_TOOLS", "0")
    shared = Agent(name="shared", model="fixture", tools=[])
    monkeypatch.setattr("ksadk.runners.adk_runner.load_agent_module", lambda *_: (shared, None))
    for method in (
        "_apply_json_patch",
        "_apply_mcp_result_patch",
        "_init_short_term_memory",
        "_init_long_term_memory",
        "_init_knowledge_base",
        "_build_runner",
        "_inject_skill_runtime_tools",
        "_inject_builtin_tools",
        "_inject_mcp_toolsets",
    ):
        monkeypatch.setattr(ADKRunner, method, lambda *_: None)
    detection = SimpleNamespace(entry_point="agent.py", agent_variable="root_agent")
    one, two, disabled = (ADKRunner(detection, str(tmp_path)) for _ in range(3))
    first = TunnelBinding(TunnelAgentPolicy("first", "fake-first", ("erp",)), TunnelRegistry())
    second = TunnelBinding(TunnelAgentPolicy("second", "fake-second", ("erp",)), TunnelRegistry())
    bind_runner(one, first)
    bind_runner(two, second)
    for runner in (one, two, disabled):
        runner.load_agent()
    assert shared.tools == []
    assert disabled._agent is shared
    assert one._agent is not shared and two._agent is not one._agent
    toolset_one, toolset_two = one._agent.tools[0], two._agent.tools[0]
    assert isinstance(toolset_one, TunnelToolset)
    assert toolset_one._registry is first.registry
    assert toolset_two._registry is second.registry
    await one.close()
    assert toolset_one._closed
    assert not toolset_two._closed
    await two.close()
    await disabled.close()


async def test_connector_retries_transient_gateway_and_never_forwards_credentials_on_redirect():
    app = FastAPI()
    reg = mount_tunnel(
        app, policies=[TunnelAgentPolicy("deployment-17", "fake-tunnel-token", ("erp",))]
    )
    attempts = []

    class TransientGateway:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "websocket":
                attempts.append(scope["path"])
                if len(attempts) <= 2:
                    await receive()
                    await send(
                        {"type": "websocket.http.response.start", "status": 503, "headers": []}
                    )
                    await send({"type": "websocket.http.response.body", "body": b"retry"})
                    return
            await app(scope, receive, send)

    async with serve(TransientGateway()) as url:
        async with connected(connector(url, "http://127.0.0.1:1/mcp")):
            assert attempts == ["/tunnel/connect"] * 3
        await reg.close()

    received = []
    sink = FastAPI()

    class CredentialSink:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "websocket":
                received.append(dict(scope["headers"]))
            await sink(scope, receive, send)

    async with serve(CredentialSink()) as destination:

        class RedirectGateway:
            async def __call__(self, scope, receive, send):
                if scope["type"] == "websocket":
                    await receive()
                    await send(
                        {
                            "type": "websocket.http.response.start",
                            "status": 307,
                            "headers": [(b"location", (destination + "/tunnel/connect").encode())],
                        }
                    )
                    await send({"type": "websocket.http.response.body", "body": b""})
                    return
                await sink(scope, receive, send)

        async with serve(RedirectGateway()) as url:
            with pytest.raises(TunnelError, match="gateway_redirect_rejected"):
                await connector(url, "http://127.0.0.1:1/mcp").run()
        assert received == []


async def test_cloud_and_connector_inflight_limits_and_registry_shutdown():
    bounds = TunnelLimits(max_inflight=1)
    async with cloud(limits=bounds) as (_, reg, url, _):
        async with connect(url.replace("http:", "ws:") + "/tunnel/connect") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "version": 2,
                        "agent_id": "deployment-17",
                        "token": "fake-tunnel-token",
                        "services": [{"name": "erp", "protocol": "mcp", "tool_filter": None}],
                    }
                )
            )
            await ws.recv()
            request = asyncio.create_task(reg.call_tool("deployment-17", "erp", "echo", {}))
            frame = json.loads(await ws.recv())
            assert frame["type"] == "http_request"
            assert json.loads(frame["body"])["method"] == "initialize"
            with pytest.raises(TunnelError, match="busy"):
                await reg.list_tools("deployment-17", "erp")
            await reg.close()
            with pytest.raises(TunnelError) as caught:
                await request
            assert caught.value.indeterminate
            assert reg.connections("deployment-17") == []


@pytest.mark.parametrize("wrapped", [False, True], ids=["native-agent", "disabled-wrapper"])
def test_cli_starts_without_tunnel(tmp_path, monkeypatch, wrapped):
    from ksadk.cli.runtime_bootstrap import create_runtime_web_app
    from ksadk.detection import FrameworkDetector, FrameworkType
    from ksadk.runtime.factory import create_runtime_adapter

    monkeypatch.delenv("KSADK_TUNNEL_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-disabled")
    monkeypatch.setenv("KSADK_TUNNEL_PEER_SECRET", "invalid")
    monkeypatch.setenv("KSADK_TUNNEL_PEER_PORT", "not-a-port")
    project = tmp_path
    marker = tmp_path / "imported"
    prefix = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    entry = (
        "from google.adk.agents import Agent as GoogleAgent\n"
        "root_agent = GoogleAgent(name='inventory')\n"
    )
    if wrapped:
        entry += "from ksadk import Agent\nroot_agent = Agent(root_agent, enable_tunnel=False)\n"
    (project / "agent.py").write_text(prefix + entry)
    (project / "agentengine.yaml").write_text(
        "framework: adk\nentry_point: agent.py\nagent_variable: root_agent\n"
    )
    detection = FrameworkDetector(str(project)).detect()
    assert detection.type == FrameworkType.ADK
    assert detection.entry_point == "agent.py"
    app = create_runtime_web_app(detection, project)
    context = app.state.runtime.launch_context
    assert not marker.exists()
    assert not context.services.tunnel_binding.resolved
    assert "/tunnel/connect" not in [getattr(route, "path", None) for route in app.routes]
    runner = create_runtime_adapter(context)._runner
    runner.load_agent()
    assert runner._agent.name == "inventory"
    assert marker.exists()
    assert runner._tunnel_binding is None
    assert context.services.tunnel_binding.peers is None


def test_web_starts_even_when_agent_dependency_is_missing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ksadk.cli.runtime_bootstrap import create_runtime_web_app
    from ksadk.detection import FrameworkDetector
    from ksadk.runtime.factory import create_runtime_adapter

    monkeypatch.setenv("AGENT_RUNTIME_ID", "runtime-unloaded")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fixture-token")
    monkeypatch.setenv("KSADK_TUNNEL_PEER_PORT", "not-a-port")
    (tmp_path / "agent.py").write_text(
        "import unavailable_ksadk_example_dependency\nfrom google.adk.agents import Agent\n"
        "root_agent = Agent(name='demo')\n"
    )
    (tmp_path / "agentengine.yaml").write_text(
        "framework: adk\nentry_point: agent.py\nagent_variable: root_agent\n"
    )
    detection = FrameworkDetector(str(tmp_path)).detect()
    app = create_runtime_web_app(detection, tmp_path)
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        adapter = create_runtime_adapter(app.state.runtime.launch_context)
        with pytest.raises(ImportError, match="unavailable_ksadk_example_dependency"):
            adapter._runner.load_agent()


@pytest.mark.parametrize("framework", ["adk", "langgraph"])
@pytest.mark.parametrize(("source", "active"), [
    ("import ksadk as sdk\nroot = sdk.Agent(graph, enable_tunnel=True)", True),
    ("from ksadk import Agent as wrap\nroot = wrap(graph, enable_tunnel=enabled)", True),
    ("from ksadk.tunnel import TunnelToolset", True),
    ("from ksadk import Agent\nroot = Agent(graph, enable_tunnel=False)", False),
    ("import ksadk as sdk\nroot = sdk.Agent(graph)", False),
    ("from ksadk import Agent", False),
])
def test_tunnel_deployment_respects_protocol_versions(tmp_path, framework, source, active):
    from ksadk.builders.code_builder import CodeBuilder
    from ksadk.builders.container_builder import ContainerBuilder

    detection = SimpleNamespace(type=SimpleNamespace(value=framework))
    (tmp_path / "agent.py").write_text(source)
    protocol = {"mcp>=1.26.0,<2.0.0", "websockets>=12.0,<16.0"}
    for user_pins in (
        "", "mcp>=1.1.0\nwebsockets", "mcp==1.26.0\nwebsockets==13.0",
        "mcp==1.1.0\nwebsockets==16.0",
    ):
        (tmp_path / "requirements.txt").write_text(user_pins)
        code = CodeBuilder(tmp_path)._build_requirements_list(detection)
        image = ContainerBuilder(tmp_path)._generate_requirements(detection, tmp_path).splitlines()
        for requirements in (code, image):
            assert set(user_pins.splitlines()) <= set(requirements)
            if active:
                assert protocol <= set(requirements)
            elif user_pins:
                assert protocol.isdisjoint(requirements)


def test_builder_preserves_tunnel_deployment_config(tmp_path, monkeypatch):
    from ksadk.builders.code_builder import CodeBuilder
    from ksadk.builders.container_builder import ContainerBuilder

    for cls in (CodeBuilder, ContainerBuilder):
        builder = cls(tmp_path)
        monkeypatch.setattr(builder, "_load_config", lambda: {})
        detection = SimpleNamespace(
            package_path="agent",
            type=SimpleNamespace(value="adk", name="ADK"),
            name="model-agent",
            entry_point="agent/agent.py",
            agent_variable="root_agent",
            runner_class="",
        )
        source = (
            builder._generate_entrypoint(detection)
            if cls is CodeBuilder
            else builder._generate_entrypoint(detection, "agent")
        )
        assert "RuntimeLaunchContext(" in source and "create_runtime_app(" in source


async def test_declared_api_calls_plain_http_from_connector_and_enforces_operation_allowlist():
    from fastapi import Header

    from ksadk.tunnel import APIOperation, LocalAPIService, TunnelToolset

    api = FastAPI()
    received = []

    @api.get("/stock")
    async def stock(sku: str, authorization: str = Header()):
        received.append((sku, authorization))
        return {"quantity": 17}

    async with serve(api) as api_url, cloud() as (_, registry, url, _):
        service = LocalAPIService(
            "erp",
            api_url,
            operations=(
                APIOperation(
                    name="lookup",
                    method="GET",
                    path="/stock",
                    query_params=("sku",),
                    input_schema={
                        "type": "object",
                        "properties": {"sku": {"type": "string"}},
                        "required": ["sku"],
                    },
                ),
            ),
            headers={"Authorization": "Bearer fake-internal"},
        )
        connector = TunnelConnector(
            url,
            agent_id="deployment-17",
            token="fake-tunnel-token",
            services=[service],
        )
        async with connected(connector):
            tools = await TunnelToolset("deployment-17", registry).get_tools()
            (tool,) = tools
            result = await tool.run_async(args={"sku": "item-15"}, tool_context=None)
            assert result["structuredContent"] == {"quantity": 17}
            assert received == [("item-15", "Bearer fake-internal")]
            with pytest.raises(TunnelError, match="forbidden"):
                await registry.call_tool("deployment-17", "erp", "hidden", {})
            with pytest.raises(TunnelError, match="invalid_request"):
                await tool.run_async(args={"sku": 123}, tool_context=None)
            assert len(received) == 1


async def test_api_rejection_is_a_tool_result_and_uncertain_calls_are_not_replayed():
    from http import HTTPStatus

    from fastapi.responses import JSONResponse, StreamingResponse

    from ksadk.tunnel import APIOperation, LocalAPIService, TunnelToolset
    from ksadk.tunnel.langgraph import TunnelTools

    api = FastAPI()
    received = []

    @api.post("/submit")
    async def submit(status: int):
        received.append(status)
        if status == 200:

            async def interrupted():
                yield b"partial"
                raise RuntimeError("fixture lost response")

            return StreamingResponse(interrupted())
        return JSONResponse({"details": "internal diagnostic"}, status_code=status)

    async with serve(api) as endpoint, cloud() as (_, registry, url, _):
        service = LocalAPIService(
            "erp",
            endpoint,
            operations=(
                APIOperation(
                    "submit", "POST", "/submit", {"type": "object"}, query_params=("status",)
                ),
            ),
        )
        client = TunnelConnector(url, token="fake-tunnel-token", services=[service])
        async with connected(client):
            (tool,) = await TunnelToolset("deployment-17", registry).get_tools()
            for status in (400, 404, 422):
                result = await tool.run_async(args={"status": status}, tool_context=None)
                assert result["isError"] and result["structuredContent"] == {"status_code": status}
                assert result["content"] == [
                    {"type": "text", "text": f"HTTP {status} {HTTPStatus(status).phrase}"}
                ]
            tools = TunnelTools("deployment-17", registry)
            (native,) = await tools.get_tools(await tools.discover())
            reply = await native.ainvoke(
                {"type": "tool_call", "id": "refused", "name": native.name, "args": {"status": 422}}
            )
            assert (
                reply.status == "error"
                and reply.artifact["structuredContent"]["status_code"] == 422
            )
            for status in (408, 499, 503, 200):
                with pytest.raises(TunnelError) as failure:
                    await tool.run_async(args={"status": status}, tool_context=None)
                assert failure.value.indeterminate
            assert not (await tool.run_async(args={"status": 201}, tool_context=None))["isError"]
            assert received == [400, 404, 422, 422, 408, 499, 503, 200, 201]


async def test_declared_root_uses_runtime_secret_and_connector_supplied_alias(
    tmp_path, monkeypatch
):
    from ksadk.cli.runtime_bootstrap import create_runtime_web_app
    from ksadk.detection import FrameworkDetector
    from ksadk.tunnel import TunnelToolset

    (tmp_path / "agent.py").write_text(
        "from ksadk import Agent\nfrom google.adk.agents import Agent as GoogleAgent\n"
        "native = GoogleAgent(name='inventory')\n"
        "root_agent = Agent(native, enable_tunnel=True, tunnel_token_env='INVENTORY_TOKEN')\n"
        "assert root_agent is native\n"
    )
    (tmp_path / "agentengine.yaml").write_text(
        "framework: adk\nentry_point: agent.py\nagent_variable: root_agent\n"
    )
    detection = FrameworkDetector(str(tmp_path)).detect()
    monkeypatch.delenv("INVENTORY_TOKEN", raising=False)
    missing_secret_app = create_runtime_web_app(detection, tmp_path)
    with pytest.raises(ValueError, match="INVENTORY_TOKEN"):
        missing_secret_app.state.runtime.launch_context.services.tunnel_binding.for_connector()
    monkeypatch.setenv("INVENTORY_TOKEN", "fixture-local-token")
    app = create_runtime_web_app(detection, tmp_path)
    binding = app.state.runtime.launch_context.services.tunnel_binding
    async with local_mcp() as (endpoint, calls, _, _), serve(app) as url:
        services = [LocalMCPService("new-alias", endpoint, tool_filter=("echo",))]
        with pytest.raises(TunnelError, match="unauthorized"):
            await TunnelConnector(url, token="wrong", services=services).run(reconnect=False)
        async with connected(TunnelConnector(url, token="fixture-local-token", services=services)):
            (tool,) = await TunnelToolset(
                binding.binding.policy.agent_id, binding.binding.registry
            ).get_tools()
            await tool.run_async(args={"text": "inventory"}, tool_context=None)
            assert calls == ["inventory"]


async def test_http_chunks_arrive_before_end_and_early_close_cancels_only_that_request():
    from ksadk.tunnel.http_tools import TunnelTransport

    async with cloud() as (_, registry, url, _):
        async with connect(url.replace("http:", "ws:") + "/tunnel/connect") as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "version": 2,
                        "token": "fake-tunnel-token",
                        "services": [{"name": "erp", "protocol": "mcp", "tool_filter": None}],
                    }
                )
            )
            assert json.loads(await ws.recv())["type"] == "registered"
            [(service, connection)] = registry.connections("deployment-17")
            # TunnelTransport 基于 httpx2（mcp 2.x），client 必须同库——
            # 混用会触发 httpx2 内部裸 assert，看不到根因。
            from ksadk._mcp_compat import httpx_module

            async with httpx_module().AsyncClient(
                transport=TunnelTransport(connection, service)
            ) as client:
                task = asyncio.create_task(
                    client.send(client.build_request("GET", "http://tunnel/"), stream=True)
                )
                request = json.loads(await ws.recv())
                rid = request["id"]
                assert request["type"] == "http_request" and request["method"] == "GET"
                await ws.send(
                    json.dumps(
                        {
                            "type": "http_response",
                            "id": rid,
                            "status": 200,
                            "headers": {"content-type": "text/event-stream"},
                        }
                    )
                )
                response = await asyncio.wait_for(task, 2)
                chunks = response.aiter_bytes()
                await ws.send(
                    json.dumps({"type": "http_chunk", "id": rid, "data": "data: 库存\n\n"})
                )
                assert await asyncio.wait_for(anext(chunks), 2) == "data: 库存\n\n".encode()
                await response.aclose()
                assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {
                    "type": "http_cancel",
                    "id": rid,
                }
                assert not connection.pending


def test_api_operation_binds_path_query_and_body_to_fixed_local_endpoint():
    from ksadk.tunnel import APIOperation, LocalAPIService
    from ksadk.tunnel.services import prepare_request

    service = LocalAPIService(
        "inventory",
        "http://inventory.invalid/v1",
        operations=(
            APIOperation(
                "update", "PATCH", "/items/{sku}", {"type": "object"}, query_params=("rev",)
            ),
        ),
    )
    frame = {
        "method": "POST",
        "path": "update",
        "query": "",
        "headers": {},
        "body": json.dumps({"sku": "item 1", "rev": 3, "quantity": 17}),
    }
    request = prepare_request(service, frame)
    assert request.method == "PATCH"
    assert str(request.url) == "http://inventory.invalid/v1/items/item%201?rev=3"
    assert json.loads(request.content) == {"quantity": 17}
    with pytest.raises(TunnelError, match="invalid_request"):
        prepare_request(service, {**frame, "body": json.dumps({"sku": ".."})})
    with pytest.raises(TunnelError, match="forbidden"):
        prepare_request(service, {**frame, "path": "undeclared"})


@pytest.mark.parametrize("call", [True, False], ids=["call", "list"])
async def test_direct_local_mcp_error_preserves_outcome(monkeypatch, call):
    registry = TunnelRegistry()
    registry.add(ConnectorConnection("agent", {"erp": {"protocol": "mcp"}}, None, TunnelLimits()))
    attempts = []

    async def operation(transport, descriptor, name, arguments):
        attempts.append(name)
        raise TunnelError("local_mcp_error")

    monkeypatch.setattr("ksadk.tunnel.http_tools.mcp_operation", operation)
    with pytest.raises(TunnelError) as caught:
        if call:
            await registry.call_tool("agent", "erp", "save", {})
        else:
            await registry.list_tools("agent", "erp")
    assert caught.value.code == "local_mcp_error"
    assert caught.value.indeterminate is call
    assert attempts == ["save" if call else None]
