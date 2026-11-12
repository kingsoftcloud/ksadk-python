"""Real local Runtime App/Kernel/LangGraph/SQLite and loopback callback tests.

IAM/MCP use an isolated HTTP fixture. No Server imports, cloud credentials,
PostgreSQL, workload environment or fixed-success signature verifier.
"""

import asyncio
import errno
import json
import os
import socket
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from langgraph.graph import END, START, StateGraph

from ksadk.agui.config import AGUIConfig
from ksadk.kernel import bootstrap, ingress
from ksadk.kernel.state import RunState
from ksadk.mcp_runtime.oauth.client import BrokerError
from ksadk.mcp_runtime.oauth.langgraph import call_mcp_tool
from ksadk.mcp_runtime.oauth.local.persistence import LocalVault
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.local_service import LocalSessionService
from ksadk.tools.gateway import ToolPolicy
from tests.agui.test_kernel_native_input import until
from tests.agui.test_runtime_adapter import _input
from tests.fixtures.local_oauth_provider import create_provider
from tests.interaction.test_native_confirmation_pipeline import State

DECLARATION = {
    "name": "research",
    "url": "https://fixture.example.test/mcp",
    "auth_mode": "oauth",
    "oauth": {"scopes": ["mcp:read"]},
    "tool_filter": ["read_snapshot"],
}


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    for key in (
        "KSADK_AGENT_KERNEL",
        "AGENT_KERNEL_ENABLED",
        "AGENT_CONTROL_JWKS_URL",
        "KSADK_CHECKPOINT_BACKEND",
        "KSADK_CHECKPOINT_PATH",
        "KSADK_DEPLOYMENT_MODE",
        "KSADK_LANGGRAPH_CHECKPOINT_DSN",
        "KSADK_CHECKPOINT_DSN",
        "KSADK_SESSION_DSN",
        "AGENT_INSTANCE_ID",
        "AGENTENGINE_MANAGED_RUNTIME",
        "KSADK_MCP_OAUTH_PROJECTION",
        "AGENT_RUNTIME_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "local")
    monkeypatch.delenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", raising=False)
    ui_dir = tmp_path / "project" / ".agentengine" / "ui"
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))
    monkeypatch.setenv("KSADK_SESSION_PATH", str(ui_dir / "sessions.sqlite"))
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "project").mkdir()


@pytest.fixture
def provider(monkeypatch):
    app = create_provider("https://fixture.example.test")

    def factory(credential):
        def client(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app), headers=headers, timeout=timeout, auth=auth
            )

        return client

    monkeypatch.setattr("ksadk.mcp_runtime.oauth.transport.http_client_factory", factory)
    return app


@asynccontextmanager
async def stack(
    tmp_path, provider, before, results, *, declarations=None, mode="local", local_oauth=True,
    agent_factory=None,
):
    project = tmp_path / "project"
    detection = SimpleNamespace(name="agent", type=SimpleNamespace(value="langgraph"))

    def create(_):
        def initialize(state):
            before.append(1)
            return {"context": {}}

        async def call(state):
            result = await call_mcp_tool(
                "research",
                "read_snapshot",
                {},
                request_key="lookup",
                policy=ToolPolicy(risk_level="low"),
            )
            results.append(result)
            return {"answer": "local MCP result received"}

        graph = StateGraph(State)
        graph.add_node("before", initialize)
        graph.add_node("lookup", call)
        graph.add_edge(START, "before")
        graph.add_edge("before", "lookup")
        graph.add_edge("lookup", END)
        runner = LangGraphRunner(detection, str(project))
        runner._agent = agent_factory() if agent_factory else graph.compile()
        return LangGraphRuntimeAdapter(runner)

    registry = RuntimeRegistry()
    registry.register("langgraph", create)
    executor = RuntimeExecutor(registry)
    service = LocalSessionService(project_dir=str(project))
    if await service.get_session("thread-1") is None:
        await service.create_session("agent", "user-1", "thread-1")
    launch = RuntimeLaunchContext(
        runtime_type="langgraph",
        project_dir=project,
        detection=detection,
        deployment_mode=mode,
        config={"mcp_servers": [DECLARATION] if declarations is None else declarations},
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch,
            agui=AGUIConfig(enabled=True, agent_name="agent"),
            session_service_provider=lambda: service,
            local_mcp_oauth=local_oauth,
        ),
        configure_runtime_app,
    )
    local = app.state.runtime.local_interactions
    if local and local.oauth:
        local.oauth.http = httpx.AsyncClient(transport=httpx.ASGITransport(provider))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=("127.0.0.1", 12345)),
            base_url="http://127.0.0.1:8000",
        ) as client:
            s = SimpleNamespace(
                app=app, client=client, local=local, service=service, response_task=None
            )
            try:
                yield s
            finally:
                if s.response_task and not s.response_task.done():
                    s.response_task.cancel()
                    from contextlib import suppress

                    with suppress(asyncio.CancelledError):
                        await s.response_task


async def ask(s, *, thread_id="thread-1", request_id="request-1"):
    response = await s.client.post(
        "/agentengine/api/v1/GetAgentUiBootstrap", json={"AgentId": "agent"}
    )
    assert response.status_code == 200
    assert response.json()["Data"]["Capabilities"]["McpAuthorization"]
    s.response_task = asyncio.create_task(
        s.client.post(
            "/agentengine/agui",
            json={**_input(run_id=request_id).model_dump(mode="json", by_alias=True), "threadId": thread_id},
        )
    )
    async def has_wait():
        broker = s.local.oauth.broker
        if s.response_task.done():
            response = s.response_task.result()
            assert broker and broker.data["waits"], response.text
        return broker is not None and any(w["session_id"] == thread_id for w in broker.data["waits"].values())

    await until(has_wait)
    broker = s.local.oauth.broker
    wait = next(w for w in broker.data["waits"].values() if w["session_id"] == thread_id)

    async def ready():
        return (await broker.get(wait["wait_id"]))["status"] == "awaiting_authorization"

    await until(ready)
    return await broker.get(wait["wait_id"])


async def action(s, name, wait, **kwargs):
    response = await s.client.post(
        "/agentengine/api/v1/" + name,
        json={
            "WaitId": wait["wait_id"],
            "ExpectedRevision": wait["revision"],
            "AgentId": "agent",
            "SessionId": "thread-1",
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["Data"]


async def consent(s, provider, wait, *, decision="allow"):
    opened = await action(s, "OpenMcpAuthorization", wait)
    assert opened["wait_id"] == wait["wait_id"] and opened["schema"] == "mcp.authorization/v1"
    async with httpx.AsyncClient() as browser:
        response = await browser.get(opened["authorization_url"])
    assert response.status_code == 303, response.text
    params = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(provider), base_url="https://fixture.example.test"
    ) as iam:
        response = await iam.post(
            "/consent", data={**params, "user": "alice", "decision": decision}
        )
    assert response.status_code == 303, response.text
    async with httpx.AsyncClient() as browser:
        callback = await browser.get(response.headers["location"])
    assert callback.status_code == 200, callback.text
    return response.headers["location"]


async def completed(s, wait):
    async def check():
        run = await s.local.runtime.kernel_store.load_run(wait["run_id"])
        return run.state is RunState.COMPLETED

    await until(check)
    async def acknowledged():
        return (await s.local.oauth.broker.get(wait["wait_id"]))["status"] == "resumed"
    await until(acknowledged)


@pytest.mark.parametrize("lookup", [False, True])
async def test_code_only_platform_client_ref_is_rejected_before_discovery_or_persistence(
    local_env, tmp_path, provider, monkeypatch, lookup,
):
    from langgraph.types import interrupt

    def graph():
        def wait(_state):
            return {"answer": interrupt({"kind": "structured_input", "schema": {"type": "string"}})}

        builder = StateGraph(State)
        builder.add_node("wait", wait)
        builder.add_edge(START, "wait")
        builder.add_edge("wait", END)
        return builder.compile()

    async with stack(tmp_path, provider, [], [], declarations=[], agent_factory=graph) as s:
        response = await s.client.post(
            "/agentengine/agui", json=_input().model_dump(mode="json", by_alias=True),
        )
        assert response.status_code == 200
        (record,) = await s.local.runtime.kernel_store.list_pending_interactions("local", "thread-1")
        await s.local.oauth.activate()
        broker = s.local.oauth.broker
        resolve = AsyncMock(wraps=broker.protocol.resolve_connection)
        monkeypatch.setattr(broker.protocol, "resolve_connection", resolve)
        save = Mock(wraps=broker.vault.save)
        monkeypatch.setattr(broker.vault, "save", save)
        original = deepcopy(broker.data)
        assert not any(original[key] for key in ("configs", "clients", "grants", "waits", "transactions"))
        declaration = {**DECLARATION, "oauth": {**DECLARATION["oauth"], "client_ref": "platform-client"}}

        with pytest.raises(BrokerError, match="^LOCAL_OAUTH_PLATFORM_CLIENT_REF_UNSUPPORTED$"):
            await broker.ensure(
                record.run_id, "research", "code-only", declaration=declaration, lookup=lookup,
            )

        resolve.assert_not_awaited()
        save.assert_not_called()
        assert provider.state.stats["registrations"] == 0
        assert broker.data == original


@pytest.mark.parametrize("lookup", [False, True])
async def test_existing_wait_replays_frozen_config_after_platform_client_ref_edit(
    local_env, tmp_path, provider, monkeypatch, lookup,
):
    async with stack(tmp_path, provider, [], []) as s:
        wait = await ask(s)
        broker = s.local.oauth.broker
        frozen_config = deepcopy(broker.data["configs"][wait["config_id"]])
        frozen_wait = deepcopy(broker.data["waits"][wait["wait_id"]])
        broker.declarations["research"] = {
            **DECLARATION, "oauth": {**DECLARATION["oauth"], "client_ref": "platform-client"},
        }
        resolve = AsyncMock(wraps=broker.protocol.resolve_connection)
        monkeypatch.setattr(broker.protocol, "resolve_connection", resolve)

        replay = await broker.ensure(wait["run_id"], "research", "lookup", lookup=lookup)

        assert replay == ({"status": "resume_wait"} if lookup else wait)
        resolve.assert_not_awaited()
        assert broker.data["configs"][wait["config_id"]] == frozen_config
        assert broker.data["waits"][wait["wait_id"]] == frozen_wait
        assert provider.state.stats["registrations"] == 0


async def test_node_declares_local_connection_only_when_invoked(local_env, tmp_path, provider):
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.graph import MessagesState
    from langgraph.prebuilt import ToolNode

    from ksadk.mcp_runtime.oauth.tools import McpOAuthTools

    def graph():
        def cloud():
            return McpOAuthTools({"research": {"url": DECLARATION["url"], "scopes": ["mcp:read"]}})
        async def model(state):
            tools = await cloud().get_tools(state)
            outputs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
            if len(outputs) == 2:
                return {"messages": [AIMessage(content="done")]}
            name = "connect_research" if not outputs else "research__read_snapshot"
            assert name in {t.name for t in tools}
            return {"messages": [AIMessage(content="", tool_calls=[{"name": name, "id": name, "args": {}}])]}
        async def execute(state, config):
            return await ToolNode(await cloud().get_tools(state)).ainvoke(state, config=config)
        builder = StateGraph(MessagesState)
        builder.add_node("model", model)
        builder.add_node("tools", execute)
        builder.add_edge(START, "model")
        builder.add_conditional_edges("model", lambda state: "tools" if state["messages"][-1].tool_calls else END)
        builder.add_edge("tools", "model")
        return builder.compile()

    async with stack(tmp_path, provider, [], [], declarations=[], agent_factory=graph) as s:
        assert not s.local.oauth.directory.exists()
        wait = await ask(s)
    async with stack(tmp_path, provider, [], [], declarations=[], agent_factory=graph) as s:
        await s.local.ensure()
        await consent(s, provider, wait)
        await completed(s, wait)
        assert provider.state.stats["calls"] == 1
        assert provider.state.stats["registrations"] == 1
        assert len(s.local.oauth.broker.data["waits"]) == 1

        # A worker that dies after dispatch must not send the business request twice.
        from ksadk.mcp_runtime.oauth.client import BrokerError, MCPToolOutcomeUnknown

        extension = await s.local.oauth.activate()
        key = (wait["run_id"], "business-call", "call-digest")
        assert await extension.claim_dispatch(*key) is None
        with pytest.raises(MCPToolOutcomeUnknown):
            await extension.claim_dispatch(*key)
        with pytest.raises(BrokerError, match="MCP_TOOL_REPLAY_MISMATCH"):
            await extension.claim_dispatch(*key[:2], "other-digest")
        await extension.finish_dispatch(*key, {"content": ["cached result"]})
        assert await extension.claim_dispatch(*key) == {"content": ["cached result"]}


async def test_restored_tool_with_a_lost_reply_cannot_be_redispatched(local_env, tmp_path, provider):
    from langchain_core.messages import AIMessage

    from ksadk.mcp_runtime.oauth.client import MCPToolOutcomeUnknown
    from ksadk.mcp_runtime.oauth.tools import McpOAuthState, McpOAuthTools
    from tests.test_mcp_oauth_agent import ScriptedModel, ai_call
    from tests.test_mcp_oauth_native_toolnode import native_graph

    # The model already knows the tool. Authorization happens in the business
    # call itself, and a retryable ToolMessage would prompt a second call ID.
    tool = {"name": "read_snapshot", "description": "Read a snapshot", "inputSchema": {"type": "object"}}
    model = ScriptedModel(responses=[
        ai_call("research__read_snapshot", "original"),
        ai_call("research__read_snapshot", "retry"),
        AIMessage(content="done"),
    ])

    def graph():
        return native_graph(
            model, McpOAuthTools(["research"]), schema=McpOAuthState,
            before=lambda _: {"mcp_catalogs": {"research": [tool]}},
        )

    async with stack(tmp_path, provider, [], [], agent_factory=graph) as s:
        wait = await ask(s)
    async with stack(tmp_path, provider, [], [], agent_factory=graph) as s:
        await s.local.ensure()
        provider.state.failure = "lost_tool_reply"
        await consent(s, provider, wait)

        async def outcome_unknown():
            snapshot = await s.local.oauth.broker.get(wait["wait_id"])
            run = await s.local.runtime.kernel_store.load_run(wait["run_id"])
            return snapshot["status"] == "resume_unknown" and run.state is RunState.WAITING

        await until(outcome_unknown)
        assert provider.state.stats["calls"] == provider.state.stats["exchanges"] == 1
        assert len(model._bound_tools) == 1
        extension = s.local.oauth.broker.extension
        async with extension.pool.acquire() as connection:
            dispatch, = await connection.fetch(
                "SELECT request_key,call_digest,result FROM mcp_tool_dispatches WHERE run_id=$1", wait["run_id"],
            )
        assert dispatch["request_key"] == "original" and dispatch["result"] is None
        with pytest.raises(MCPToolOutcomeUnknown):
            await extension.claim_dispatch(wait["run_id"], "original", dispatch["call_digest"])

    async with stack(tmp_path, provider, [], [], agent_factory=graph) as s:
        current = await action(s, "GetMcpAuthorizationWait", wait)
        assert current["status"] == "resume_unknown" and current["allowed_actions"] == ["query", "cancel"]
        assert provider.state.stats["calls"] == 1 and len(model._bound_tools) == 1


async def test_agent_factory_real_kernel_callback_and_cold_restore(local_env, tmp_path, provider):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from ksadk.mcp_runtime.oauth import create_mcp_agent

    class Model(BaseChatModel):
        @property
        def _llm_type(self):
            return "fixture-only"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            outputs = [m for m in messages if isinstance(m, ToolMessage)]
            if len(outputs) < 2:
                name = "connect_research" if not outputs else "research__read_snapshot"
                message = AIMessage(content="", tool_calls=[{
                    "name": name, "id": name, "args": {},
                }])
            else:
                message = AIMessage(content="factory received MCP result")
            return ChatResult(generations=[ChatGeneration(message=message)])

    def factory():
        return create_mcp_agent(
            Model(), mcp_servers=["research"], read_only_tools={"research": ["read_snapshot"]}
        )

    async with stack(tmp_path, provider, [], [], agent_factory=factory) as s:
        wait = await ask(s)
        original_target = s.local.oauth.broker.data["waits"][wait["wait_id"]]["runtime_ref"]
    async with stack(tmp_path, provider, [], [], agent_factory=factory) as s:
        await s.local.ensure()
        await consent(s, provider, wait)
        await completed(s, wait)
        waits = list(s.local.oauth.broker.data["waits"].values())
        assert len(waits) == 1  # The actual call uses the grant without registering another wait.
        assert {w["run_id"] for w in waits} == {wait["run_id"]}
        assert waits[0]["runtime_ref"] == original_target
        assert provider.state.stats["calls"] == provider.state.stats["exchanges"] == 1
        secret = next(iter(s.local.oauth.broker.data["grants"].values()))["tokens"]["access_token"]
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes(), path


async def test_local_one_question_authorization_restores_same_run_and_encrypts_secrets(
    local_env, tmp_path, provider
):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        assert before == [1] and not results
        callback = await consent(s, provider, wait)
        await completed(s, wait)
        assert before == [1] and len(results) == 1
        assert provider.state.stats["calls"] == provider.state.stats["exchanges"] == 1
        # Live AG-UI must carry the durable OAuth card before any page refresh.
        response = await asyncio.wait_for(s.response_task, 3)
        frames = [
            json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        snapshots = [
            frame["value"] for frame in frames
            if frame.get("type") == "CUSTOM"
            and frame.get("name") == "ksadk.session_event"
            and frame.get("value", {}).get("event_type") == "mcp.authorization.requested"
        ]
        assert len(snapshots) == 1
        assert snapshots[0]["run_id"] == wait["run_id"]
        assert snapshots[0]["payload"]["wait_id"] == wait["wait_id"]
        async with httpx.AsyncClient() as browser:
            assert (await browser.get(callback)).status_code == 200
        assert provider.state.stats["calls"] == provider.state.stats["exchanges"] == 1
        assert len(s.local.oauth.broker.data["waits"]) == 1
        secret = next(iter(s.local.oauth.broker.data["grants"].values()))["tokens"][
            "access_token"
        ].encode()
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert secret not in path.read_bytes(), path
        assert (tmp_path / "project/.agentengine/mcp-oauth/key").stat().st_mode & 0o777 == 0o600
    assert bootstrap.get_agent_kernel_runtime() is None and ingress.get_agent_kernel() is None


async def test_restart_before_callback_preserves_run_checkpoint_and_original_configuration(
    local_env, tmp_path, provider
):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        opened = await action(s, "OpenMcpAuthorization", wait)
        async with httpx.AsyncClient() as browser:
            redirect = (await browser.get(opened["authorization_url"])).headers["location"]
        runtime_ref = s.local.oauth.broker.data["waits"][wait["wait_id"]]["runtime_ref"].copy()
    changed = {**DECLARATION, "url": "https://changed.example.test/mcp"}
    async with stack(tmp_path, provider, before, results, declarations=[changed]) as s:
        await s.local.ensure()
        params = {k: v[0] for k, v in parse_qs(urlsplit(redirect).query).items()}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(provider), base_url="https://fixture.example.test"
        ) as iam:
            response = await iam.post(
                "/consent", data={**params, "user": "alice", "decision": "allow"}
            )
        async with httpx.AsyncClient() as browser:
            assert (await browser.get(response.headers["location"])).status_code == 200
        await completed(s, wait)
        assert before == [1] and len(results) == 1
        assert s.local.oauth.broker.data["waits"][wait["wait_id"]]["runtime_ref"] == runtime_ref


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
async def test_cancel_or_expiry_blocks_late_callback(local_env, tmp_path, provider, terminal):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        await action(s, "OpenMcpAuthorization", wait)
        broker = s.local.oauth.broker
        if terminal == "cancelled":
            wait = await broker.get(wait["wait_id"])
            await action(s, "CancelMcpAuthorizationWait", wait)
        else:
            broker.clock = lambda: wait["deadline"] + 1
        state = next(iter(broker.data["transactions"].values()))
        params = {k: v[0] for k, v in parse_qs(urlsplit(state["url"]).query).items()}
        result = await broker.callback(
            state=params["state"], code="late-unused-code", issuer="https://fixture.example.test"
        )
        assert result["status"] == terminal
        await broker.get(wait["wait_id"])
        assert not results and provider.state.stats["exchanges"] == 0


async def test_two_waiting_sessions_share_one_local_authorization(local_env, tmp_path, provider):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        first = await ask(s)
        first_stream = s.response_task
        await s.service.create_session("agent", "user-1", "thread-2")
        second = await ask(s, thread_id="thread-2", request_id="request-2")
        assert first["run_id"] != second["run_id"]
        assert second["status"] == "awaiting_authorization"

        await consent(s, provider, first)
        await asyncio.gather(completed(s, first), completed(s, second))
        await asyncio.gather(first_stream, s.response_task)
        assert before == [1, 1] and len(results) == 2
        assert provider.state.stats["calls"] == 2 and provider.state.stats["exchanges"] == 1


async def test_denial_retry_refresh_and_cross_session_rejection(local_env, tmp_path, provider):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        await consent(s, provider, wait, decision="deny")
        broker = s.local.oauth.broker
        denied = await broker.get(wait["wait_id"])
        assert denied["status"] == "authorization_failed" and denied["error"] == "USER_DENIED"
        assert denied["deadline"] == wait["deadline"]
        wrong = await s.client.post(
            "/agentengine/api/v1/GetMcpAuthorizationWait",
            json={"WaitId": wait["wait_id"], "SessionId": "another"},
        )
        assert wrong.status_code == 409
        attack = await s.client.post(
            "/agentengine/api/v1/OpenMcpAuthorization",
            headers={"Origin": "https://attacker.example"},
            json={"WaitId": wait["wait_id"]},
        )
        assert (
            attack.status_code == 409
            and attack.json()["ReasonCode"] == "LOCAL_OAUTH_ORIGIN_REJECTED"
        )
        with pytest.raises(BrokerError, match="INVALID_OAUTH_STATE"):
            await broker.callback(state="forged", code="forged")
        await consent(s, provider, denied)
        await completed(s, wait)
        grant = broker.data["grants"][wait["config_id"]]
        old = grant["tokens"]["access_token"]
        grant["expires"] = 0
        credential = await broker.credential(wait["wait_id"])
        assert credential["access_token"] != old and provider.state.stats["refreshes"] == 1


@pytest.mark.parametrize("mode", ["ksadk_managed_cloud", "external_managed"])
async def test_hosted_modes_never_attach_local_oauth(local_env, tmp_path, provider, mode):
    async with stack(tmp_path, provider, [], [], mode=mode) as s:
        assert s.local.oauth is None
        assert not any("McpAuthorization" in getattr(route, "path", "") for route in s.app.routes)
        assert not (tmp_path / "project/.agentengine/mcp-oauth").exists()


@pytest.mark.parametrize(
    "env",
    [
        "AGENTENGINE_MANAGED_RUNTIME",
        "AGENT_CONTROL_JWKS_URL",
        "KSADK_MCP_OAUTH_PROJECTION",
        "AGENT_RUNTIME_ID",
    ],
)
async def test_platform_markers_do_not_enable_local_vault(
    local_env, tmp_path, provider, monkeypatch, env
):
    monkeypatch.setenv(env, "1")
    async with stack(tmp_path, provider, [], []) as s:
        assert s.local is None or s.local.oauth is None
        assert not (tmp_path / "project/.agentengine/mcp-oauth").exists()


@pytest.mark.parametrize("declarations", [
    [],
    [{"name": "search", "auth_mode": "api_key", "url": "https://fixture.example.test/key-mcp"}],
    [DECLARATION],
])
async def test_plain_chat_does_not_open_oauth_vault_or_listener(local_env, tmp_path, provider, declarations):
    from langchain_core.messages import AIMessage

    def plain_chat():
        graph = StateGraph(State)
        graph.add_node("reply", lambda state: {"messages": [AIMessage(content="plain chat works")]})
        graph.add_edge(START, "reply")
        graph.add_edge("reply", END)
        return graph.compile()

    async with stack(tmp_path, provider, [], [], declarations=declarations, agent_factory=plain_chat) as s:
        response = await s.client.post("/agentengine/agui", json=_input().model_dump(mode="json", by_alias=True))
        assert response.status_code == 200 and "plain chat works" in response.text
        assert s.local.oauth.broker is None and s.local.oauth.socket is None
        assert not s.local.oauth.directory.exists()


@pytest.mark.parametrize("value,action", [
    ("Enter a value", "submit"),
    ({"kind": "approval"}, "approve"),
    ({"type": "legacy_review", "tool_name": "write", "arguments": {}}, "approve"),
    ({"kind": "legacy_review", "action_requests": [{"name": "write", "args": {}}]}, "reject"),
])
async def test_ordinary_native_wait_finishes_without_activating_oauth(
    local_env, tmp_path, provider, value, action
):
    from langgraph.types import interrupt

    before, consumed = [], []

    def factory():
        def initialize(_state):
            before.append(1)
            return {}

        def wait(_state):
            consumed.append(interrupt(value))
            return {"answer": "Finished"}

        graph = StateGraph(State)
        graph.add_node("before", initialize)
        graph.add_node("wait", wait)
        graph.add_edge(START, "before")
        graph.add_edge("before", "wait")
        graph.add_edge("wait", END)
        return graph.compile()

    async with stack(tmp_path, provider, [], [], declarations=[], agent_factory=factory) as s:
        response = await s.client.post(
            "/agentengine/agui", json=_input().model_dump(mode="json", by_alias=True)
        )
        assert response.status_code == 200
        runtime = s.local.runtime
        (record,) = await runtime.kernel_store.list_pending_interactions("local", "thread-1")
        assert record.kind == ("structured_input" if action == "submit" else "approval")
        answer = "ordinary answer" if action == "submit" else {"decision": action}
        response = await s.client.post("/agentengine/api/v1/SubmitInteraction", json={
            "AgentId": "agent", "SessionId": "thread-1", "RunId": record.run_id,
            "InteractionId": record.interaction_id, "ExpectedRevision": record.revision,
            "InteractionAction": action, "Response": answer,
            "IdempotencyKey": "ordinary-native-resume",
        })
        assert response.status_code == 202

        async def completed():
            return (await runtime.kernel_store.load_run(record.run_id)).state is RunState.COMPLETED

        await until(completed)
        assert (await runtime.kernel_store.get(record.interaction_id)).status == "resolved"
        assert before == [1] and consumed == [answer]
        assert s.local.oauth.broker is None and s.local.oauth.socket is None
        assert not (tmp_path / "project/.agentengine/mcp-oauth").exists()


async def test_default_composition_never_enables_local_oauth_from_yaml(
    local_env, tmp_path, provider
):
    async with stack(tmp_path, provider, [], [], local_oauth=False) as s:
        assert s.local is None or s.local.oauth is None
        assert not (tmp_path / "project/.agentengine/mcp-oauth").exists()


async def test_explicit_checkpoint_opt_out_is_not_overridden(
    local_env, tmp_path, provider, monkeypatch
):
    monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", "0")
    from langchain_core.messages import AIMessage
    from langgraph.graph import MessagesState

    def plain_chat():
        graph = StateGraph(MessagesState)
        graph.add_node("chat", lambda state: {"messages": [AIMessage(content="plain chat works")]})
        graph.add_edge(START, "chat")
        graph.add_edge("chat", END)
        return graph.compile()

    async with stack(tmp_path, provider, [], [], agent_factory=plain_chat) as s:
        assert await s.local.ensure() is None
        response = await s.client.post(
            "/agentengine/agui", json=_input(run_id="no-saver").model_dump(mode="json", by_alias=True),
        )
        assert response.status_code == 200 and "plain chat works" in response.text
        assert "mcp.authorization.requested" not in response.text
        assert s.local.oauth.broker is None


async def test_public_submit_and_wrong_completion_signature_cannot_resume(
    local_env, tmp_path, provider
):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        extension = s.local.runtime.worker._mcp_authorization
        row = await extension.row(wait["wait_id"])
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction",
            json={
                "AgentId": "agent",
                "UserId": "user-1",
                "SessionId": "thread-1",
                "RunId": wait["run_id"],
                "InteractionId": row["interaction_id"],
                "ExpectedRevision": 1,
                "InteractionAction": "submit",
                "Response": True,
                "IdempotencyKey": "forged-browser-completion",
            },
        )
        # Admission is queued; the worker rejects it without resolving the wait.
        assert response.status_code == 202

        async def rejected():
            facts = await s.local.runtime.session_events.read("thread-1", 0, 200)
            return any(f.event_type == "control.command_rejected" for f in facts)

        await until(rejected)
        with pytest.raises(ValueError, match="INVALID_SERVICE_CREDENTIAL"):
            await extension.accept({"wait_id": wait["wait_id"]}, "forged.signature.value")
        assert before == [1] and not results


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("permission", "BUSINESS_PERMISSION_DENIED"),
        ("empty", "TOOLS_EMPTY"),
        ("disconnect", "CONNECTION_FAILED"),
    ],
)
async def test_connection_retry_preserves_credential_and_task(
    local_env, tmp_path, provider, failure, expected
):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        provider.state.failure = failure
        await consent(s, provider, wait)
        broker = s.local.oauth.broker

        async def failed():
            return (await broker.get(wait["wait_id"]))["status"] == "connection_failed"

        await until(failed)
        snapshot = await broker.get(wait["wait_id"])
        assert snapshot["error"] == expected
        provider.state.failure = None
        await action(s, "RetryMcpConnection", snapshot)
        await completed(s, wait)
        assert provider.state.stats["exchanges"] == 1 and provider.state.stats["calls"] == 1
        assert before == [1] and len(results) == 1


async def test_exchange_failure_does_not_claim_success(local_env, tmp_path, provider):
    async with stack(tmp_path, provider, [], []) as s:
        wait = await ask(s)
        provider.state.failure = "token"
        await consent(s, provider, wait)
        snapshot = await s.local.oauth.broker.get(wait["wait_id"])
        assert snapshot["status"] == "authorization_failed"
        assert snapshot["error"] == "TOKEN_EXCHANGE_FAILED"
        assert not s.local.oauth.broker.data["grants"]


def test_vault_owner_lock_missing_key_and_wrong_key_fail_closed(tmp_path):
    vault = LocalVault(tmp_path)
    vault.data["private"] = "test-secret-only"
    vault.save()
    with pytest.raises(ValueError, match="ALREADY_RUNNING"):
        LocalVault(tmp_path)
    vault.close()
    key = tmp_path / "key"
    key.unlink()
    with pytest.raises(ValueError, match="KEY_MISSING"):
        LocalVault(tmp_path)


async def test_occupied_callback_port_releases_vault_and_retries_same_port(local_env, tmp_path, provider):
    async with stack(tmp_path, provider, [], []) as s:
        await s.local.ensure()
        oauth = s.local.oauth
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            port = holder.getsockname()[1]
            vault = LocalVault(oauth.directory)
            try:
                vault.data["port"] = port
                vault.save()
            finally:
                vault.close()
            with pytest.raises(BrokerError, match="LOCAL_OAUTH_CALLBACK_PORT_IN_USE"):
                await oauth.activate()
            assert oauth.vault is None and oauth.socket is None
        await oauth.activate()
        assert oauth.socket.getsockname() == ("127.0.0.1", port)


def test_windows_callback_uses_exclusive_bind_and_maps_winsock_conflict(monkeypatch):
    from ksadk.mcp_runtime.oauth.local.runtime import _bind_callback_socket

    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    listener = Mock()
    conflict = OSError(errno.EACCES, "fixture")
    conflict.winerror = 10013
    listener.bind.side_effect = conflict
    with pytest.raises(BrokerError, match="LOCAL_OAUTH_CALLBACK_PORT_IN_USE"):
        _bind_callback_socket(listener, 8100)
    listener.setsockopt.assert_called_once_with(socket.SOL_SOCKET, -5, 1)
    listener.bind.assert_called_once_with(("127.0.0.1", 8100))


async def test_listener_start_failure_releases_project_lock(local_env, tmp_path, provider, monkeypatch):
    async def fail_to_serve(server, *, sockets):
        raise OSError("fixture listener failure")

    async with stack(tmp_path, provider, [], []) as s:
        await s.local.ensure()
        oauth = s.local.oauth
        monkeypatch.setattr("uvicorn.Server.serve", fail_to_serve)
        with pytest.raises(OSError, match="fixture listener failure"):
            await oauth.activate()
        assert oauth.socket is None and oauth.broker is None and oauth.vault is None
        LocalVault(oauth.directory).close()


async def test_authorization_preserves_provider_query_and_rejects_duplicate_state(
    local_env, tmp_path, provider
):
    async with stack(tmp_path, provider, [], []) as s:
        wait = await ask(s)
        broker = s.local.oauth.broker
        config = broker.config(broker.data["waits"][wait["wait_id"]])
        metadata = (await broker.registration(config))["metadata"]
        endpoint = metadata["authorization_endpoint"]
        metadata["authorization_endpoint"] = endpoint + "?tenant=one%2Ftwo&region=east&region=west"
        opened = await action(s, "OpenMcpAuthorization", wait)
        async with httpx.AsyncClient() as browser:
            redirect = (await browser.get(opened["authorization_url"])).headers["location"]
        query = urlsplit(redirect).query
        assert query.startswith("tenant=one%2Ftwo&region=east&region=west&")
        assert len(parse_qs(query)["client_id"]) == len(parse_qs(query)["state"]) == 1
        await consent(s, provider, wait, decision="deny")
        denied = await broker.get(wait["wait_id"])
        metadata["authorization_endpoint"] = endpoint + "?%73tate=conflicting"
        with pytest.raises(BrokerError, match="OAUTH_CLIENT_INCOMPATIBLE"):
            await broker.action("RetryMcpAuthorization", wait["wait_id"], denied["revision"])
        assert (await broker.get(wait["wait_id"]))["error"] == "OAUTH_CLIENT_INCOMPATIBLE"


def test_protocol_mirror_has_not_diverged_from_platform():
    configured = os.getenv("A2A_TEST_SERVER_REPO")
    server_root = (Path(configured) if configured
                   else Path(__file__).resolve().parents[2] / "agentengine-server")
    server = server_root / "app/services/mcp_oauth"
    if not server.exists():
        pytest.skip("Cross-repository parity requires Server checkout")
    local = Path(__file__).resolve().parents[1] / "ksadk/mcp_runtime/oauth/local"
    for name in ("protocol.py", "transport.py"):
        assert (local / name).read_bytes() == (server / name).read_bytes()


@pytest.mark.parametrize(("is_pod", "flag", "projected", "expected"), [
    (False, None, None, True), (True, None, "1", False),
    (True, "1", "0", False), (True, "1", "1", True),
])
def test_hosted_agui_needs_published_and_projected_kernel(monkeypatch, is_pod, flag, projected, expected):
    from ksadk.agui.config import hosted_agui_requested
    for key, value in [("AGENT_RUNTIME_ID", "pod" if is_pod else None),
                       ("KSADK_ENABLE_KERNEL", flag), ("AGENT_KERNEL_ENABLED", projected)]:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert hosted_agui_requested("langgraph") is expected
    assert hosted_agui_requested("codex")


async def test_restart_rebuilds_oauth_index_without_a_second_card(local_env, tmp_path, provider):
    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        runtime = s.local.oauth.runtime
        async def first_card_saved():
            events = await runtime.session_events.read("thread-1", 0, 200)
            return any(e.event_type == "mcp.authorization.requested" for e in events)
        await until(first_card_saved)
        extension = s.local.oauth.broker.extension
        # Leave the committed native binding and its published event. The new
        # instance must rebuild its index and reconcile the existing fact.
        async with extension.pool.acquire() as conn:
            await conn.execute("DELETE FROM mcp_authorization_waits WHERE wait_id=$1", wait["wait_id"])
    async with stack(tmp_path, provider, before, results) as s:
        await s.local.ensure()
        runtime = s.local.oauth.runtime
        async def index_restored():
            row = await s.local.oauth.broker.extension.row(wait["wait_id"])
            return row and row["snapshot"].get("status") == "awaiting_authorization"
        await until(index_restored)
        await consent(s, provider, wait)
        await completed(s, wait)
        events = await runtime.session_events.read("thread-1", 0, 200)
        requested = [e for e in events if e.event_type == "mcp.authorization.requested"]
        assert len(requested) == 1 and requested[0].run_id == wait["run_id"]
        assert before == [1] and provider.state.stats["calls"] == 1


async def test_plain_two_turns_keep_history_once_and_reset_graph_state(local_env, tmp_path, provider):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import MessagesState

    class Chat(MessagesState):
        visits: int

    observed = []

    def respond(state):
        count = state.get("visits", 0) + 1
        observed.append((count, [m.content for m in state["messages"] if isinstance(m, HumanMessage)]))
        return {"messages": [AIMessage(content="answer")], "visits": count}

    graph = StateGraph(Chat)
    graph.add_node("reply", respond)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    async with stack(tmp_path, provider, [], [], declarations=[], agent_factory=graph.compile) as s:
        await s.client.post("/agentengine/api/v1/GetAgentUiBootstrap", json={"AgentId": "agent"})
        for n in (1, 2):
            body = _input(run_id=f"run-{n}").model_dump(mode="json", by_alias=True)
            body["messages"] = [{"role": "user", "id": f"msg-{n}", "content": f"turn-{n}"}]
            result = await s.client.post("/agentengine/agui", json=body)
            assert result.status_code == 200 and "RUN_FINISHED" in result.text
        assert observed == [(1, ["turn-1"]), (1, ["turn-1", "turn-2"])]
        assert s.local.oauth.broker is None and not s.local.oauth.directory.exists()


async def test_local_directory_retry_after_refresh_resumes_original_tool_once(
    local_env, tmp_path, provider, monkeypatch,
):
    from ksadk.mcp_runtime.oauth import langgraph as tools

    before, results = [], []
    async with stack(tmp_path, provider, before, results) as s:
        wait = await ask(s)
        broker = s.local.oauth.broker
        original = tools.connect
        rejected = []

        @asynccontextmanager
        async def directory(credential):
            if not rejected:
                rejected.append(credential["grant_version"])
                grant = broker.data["grants"][wait["config_id"]]
                grant["expires"] = 0
                fresh = await broker.credential(wait["wait_id"])
                assert fresh["grant_version"] > credential["grant_version"]
                raise BrokerError("REAUTHORIZATION_REQUIRED")
            async with original(credential) as connection:
                yield connection

        monkeypatch.setattr(tools, "connect", directory)
        await consent(s, provider, wait)
        await completed(s, wait)
        assert rejected == [1] and len(broker.data["waits"]) == 1
        assert before == [1] and len(results) == 1
        assert provider.state.stats["exchanges"] == provider.state.stats["refreshes"] == provider.state.stats["calls"] == 1
