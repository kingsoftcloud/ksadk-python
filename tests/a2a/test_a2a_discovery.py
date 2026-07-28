"""A2ASpaceClient 动态发现 + egress + 调用 测试 (goal-06,-k discovery)。

- mock A2AControlPlane 提供 hosted/external Agent(§5.6 统一模型)。
- hosted 调用经 goal-05 A2AProtocolServer(ASGI in-process)roundtrip。
- external public 调用受 egress 约束:关→``A2A_PUBLIC_EGRESS_DISABLED``;开→通。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from a2a.types import Message, Part, Role, StreamResponse, Task, TaskState, TaskStatus
from fastapi import FastAPI

from ksadk.a2a import (
    A2AConfig,
    A2AControlPlane,
    A2AExternalTransport,
    A2ARoute,
    A2ARouteInterface,
    A2ARuntimeTaskAdapter,
    A2ASpaceClient,
    A2ATarget,
    CredentialInjection,
    DiscoveredAgent,
    PreparedA2AOperation,
    RemoteTaskReference,
    SpaceAgentPage,
    add_a2a_protocol_routes,
    build_agent_card,
)
from ksadk.a2a.control_plane import ENV_A2A_CONTROL_PLANE_URL
from ksadk.a2a.external_transport import A2ATransportLease
from ksadk.a2a.space_client import (
    ENV_A2A_ENABLE_PUBLIC_EGRESS,
    ENV_A2A_SPACE_IDS,
    ERR_PUBLIC_EGRESS_DISABLED,
)
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

SPACE_ID = "a2a-space-00000000000040008000000000000011"
SPACE_ID_2 = "a2a-space-00000000000040008000000000000021"
HOSTED_AGENT_ID = "a2a-agent-00000000000040008000000000000012"
EXTERNAL_AGENT_ID = "a2a-agent-00000000000040008000000000000013"
VPC_AGENT_ID = "a2a-agent-00000000000040008000000000000014"
TASK_ID = "a2a-task-00000000000040008000000000000015"


class _EchoRunner:
    async def invoke(self, input_data):
        return {"output": f"echo:{input_data['input']}"}

    async def stream(self, input_data):
        yield {"delta": "echo:", "type": "text"}
        yield {"delta": str(input_data["input"]), "type": "text"}
        yield {"output": f"echo:{input_data['input']}", "type": "final"}


def _echo_app(dsn: str) -> FastAPI:
    app = FastAPI()
    runner = _EchoRunner()
    add_a2a_protocol_routes(
        app,
        runner,
        A2AConfig(
            enabled=True,
            base_url="http://testserver",
            agent_name="echo-agent",
            skills=["echo"],
            task_store_dsn=dsn,
            create_table=True,
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner, runtime_type="test"), runtime_type="test"
        ),
    )
    return app


def _agent(agent_id: str, source: str, name: str = "echo-agent") -> DiscoveredAgent:
    return DiscoveredAgent(
        agent_id=agent_id,
        version_id=f"a2a-version-{agent_id.removeprefix('a2a-agent-')}",
        source=source,
        agent_card=build_agent_card(name=name, base_url="http://testserver", skills=["echo"]),
        card_sha256="",
        route_kind="hosted_gateway" if source == "hosted" else "external_public",
    )


class _MockDiscoveryBackend(A2AControlPlane):
    def __init__(self, agents):
        self._agents = list(agents)
        self.calls: list[dict] = []
        self.prepare_calls: list[dict] = []
        self.bind_calls: list[dict] = []
        self.append_calls: list[dict] = []
        self.resolve_calls: list[dict] = []
        self.credential_handles: dict[str, str | None] = {}
        self.credential_injection = CredentialInjection()
        self.credential_injections: dict[str, CredentialInjection] = {}
        self.task_operation_calls: list[dict] = []
        self.platform_task_ids: list[str] = []
        self.append_error: Exception | None = None

    async def list_space_agents(self, space_id, *, prompt=None, skill_id=None, **kwargs):
        self.calls.append({"space_id": space_id, "prompt": prompt, "skill": skill_id})
        # §5.5:7 月 Prompt 仅做名称/描述/skills 受控关键词匹配;mock 简单过滤。
        agents = self._agents
        if skill_id:
            agents = [a for a in agents if any(s.id == skill_id for s in a.agent_card.skills)]
        return SpaceAgentPage(agents=agents, etag="etag-1")

    async def prepare_call(self, **kwargs):
        self.prepare_calls.append(kwargs)
        agent = next(a for a in self._agents if a.agent_id == kwargs["target_agent_id"])
        return PreparedA2AOperation(
            platform_task_id=(self.platform_task_ids.pop(0) if self.platform_task_ids else TASK_ID),
            target=A2ATarget(agent.agent_id, agent.version_id, agent.card_sha256),
            route=A2ARoute(
                kind=agent.route_kind,
                interface=A2ARouteInterface(
                    url="http://testserver/a2a/jsonrpc",
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                ),
            ),
            call_permit="permit-1",
            call_permit_expires_at="2026-07-27T10:05:00Z",
            credential_handle=(
                self.credential_handles.get(agent.agent_id, "credential-1")
                if agent.source == "external"
                else None
            ),
        )

    async def prepare_task_operation(self, **kwargs):
        self.task_operation_calls.append(kwargs)
        agent = self._agents[0]
        binding = self.bind_calls[-1]
        return PreparedA2AOperation(
            platform_task_id=kwargs["platform_task_id"],
            target=A2ATarget(agent.agent_id, agent.version_id, agent.card_sha256),
            route=A2ARoute(
                kind=agent.route_kind,
                interface=A2ARouteInterface(
                    url="http://testserver/a2a/jsonrpc",
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                ),
            ),
            remote_task=RemoteTaskReference(
                remote_task_id=binding["remote_task_id"],
                remote_context_id=binding["remote_context_id"],
            ),
            call_permit="permit-task-operation",
            call_permit_expires_at="2026-07-27T10:05:00Z",
            credential_handle=("credential-1" if agent.source == "external" else None),
        )

    async def bind_remote_task(self, **kwargs):
        existing = next(
            (
                item
                for item in self.bind_calls
                if item["platform_task_id"] == kwargs["platform_task_id"]
            ),
            None,
        )
        if existing is not None:
            if (
                existing["remote_task_id"],
                existing["remote_context_id"],
            ) != (
                kwargs["remote_task_id"],
                kwargs["remote_context_id"],
            ):
                raise RuntimeError("A2A_REMOTE_BINDING_CONFLICT")
            return {"A2ATaskId": kwargs["platform_task_id"], "AlreadyBound": True}
        self.bind_calls.append(kwargs)
        return {"A2ATaskId": kwargs["platform_task_id"], "AlreadyBound": False}

    async def append_task_events(self, **kwargs):
        if self.append_error is not None:
            raise self.append_error
        self.append_calls.append(kwargs)
        return {"AcceptedCount": len(kwargs["events"]), "DuplicateCount": 0}

    async def resolve_credential(self, **kwargs):
        self.resolve_calls.append(kwargs)
        handle = kwargs.get("credential_handle")
        return self.credential_injections.get(handle, self.credential_injection)

    def gateway_token(self):
        return "gateway-token"


class _StaticExternalTransport(A2AExternalTransport):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.routes: list[tuple[str, str]] = []

    @asynccontextmanager
    async def open_for_route(self, route, *, route_kind):  # noqa: ANN001, ANN201
        self.routes.append((route.url, route_kind))
        yield A2ATransportLease(
            httpx_client=self.client,
            effective_interface=route,
            route_kind=route_kind,
            policy_revision="test",
        )


def _client_for_app(app: FastAPI, agents, *, egress: bool) -> A2ASpaceClient:
    """构造 SpaceClient,其 httpx_client 指到 echo app(经 ASGI)。"""
    transport = httpx.ASGITransport(app=app)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return A2ASpaceClient(
        SPACE_ID,
        _MockDiscoveryBackend(agents),
        egress_enabled=egress,
        httpx_client=httpx_client,
        external_transport=_StaticExternalTransport(httpx_client),
    )


def test_from_env_requires_space_selection(monkeypatch):
    monkeypatch.delenv(ENV_A2A_SPACE_IDS, raising=False)
    with pytest.raises(ValueError, match=ENV_A2A_SPACE_IDS):
        A2ASpaceClient.from_env()


def test_from_env_requires_explicit_selection_for_multiple_spaces(monkeypatch):
    monkeypatch.setenv(ENV_A2A_SPACE_IDS, f'["{SPACE_ID}", "{SPACE_ID_2}"]')

    with pytest.raises(ValueError, match="pass space_id explicitly"):
        A2ASpaceClient.from_env(backend=_MockDiscoveryBackend([]))

    client = A2ASpaceClient.from_env(
        space_id=SPACE_ID_2,
        backend=_MockDiscoveryBackend([]),
    )
    assert client._space_id == SPACE_ID_2


@pytest.mark.parametrize(
    "raw_space_ids",
    ["not-json", "[]", f'["{SPACE_ID}", "{SPACE_ID}"]'],
)
def test_from_env_rejects_invalid_space_id_lists(monkeypatch, raw_space_ids):
    monkeypatch.setenv(ENV_A2A_SPACE_IDS, raw_space_ids)
    with pytest.raises(ValueError, match=ENV_A2A_SPACE_IDS):
        A2ASpaceClient.from_env(backend=_MockDiscoveryBackend([]))


@pytest.mark.asyncio
async def test_constructor_rejects_raw_external_http_client():
    http = httpx.AsyncClient()
    try:
        with pytest.raises(TypeError, match="A2AExternalTransport"):
            A2ASpaceClient(
                SPACE_ID,
                _MockDiscoveryBackend([]),
                external_transport=http,  # type: ignore[arg-type]
            )
    finally:
        await http.aclose()


def test_from_env_builds_with_kop_backend(monkeypatch):
    monkeypatch.setenv(ENV_A2A_SPACE_IDS, f'["{SPACE_ID}"]')
    monkeypatch.setenv(ENV_A2A_CONTROL_PLANE_URL, "http://kop")
    monkeypatch.setenv(ENV_A2A_ENABLE_PUBLIC_EGRESS, "true")
    client = A2ASpaceClient.from_env()
    assert client._space_id == SPACE_ID
    assert client._egress_enabled is True


@pytest.mark.asyncio
async def test_discovery_returns_unified_hosted_external(tmp_path):
    agents = [
        _agent(HOSTED_AGENT_ID, "hosted"),
        _agent(EXTERNAL_AGENT_ID, "external"),
    ]
    client = _client_for_app(_echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db"), agents, egress=True)
    discovered = await client.discover()
    assert {a.agent_id for a in discovered} == {
        HOSTED_AGENT_ID,
        EXTERNAL_AGENT_ID,
    }
    assert {a.agent_id: a.source for a in discovered} == {
        HOSTED_AGENT_ID: "hosted",
        EXTERNAL_AGENT_ID: "external",
    }
    # prompt/skill 透传到 backend(§5.5 受控关键词匹配由服务端做)
    await client.discover(prompt="天气", skill="echo")
    assert client._backend.calls[-1] == {
        "space_id": SPACE_ID,
        "prompt": "天气",
        "skill": "echo",
    }


@pytest.mark.asyncio
async def test_send_message_to_hosted_via_discovery(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    seen_headers: list[dict[str, str]] = []

    @app.middleware("http")
    async def capture_headers(request, call_next):  # noqa: ANN001, ANN202
        if request.url.path.startswith("/a2a/"):
            seen_headers.append(dict(request.headers))
        return await call_next(request)

    client = _client_for_app(app, [_agent(HOSTED_AGENT_ID, "hosted")], egress=False)
    await client.discover()
    task = await client.send_message(HOSTED_AGENT_ID, "ping", return_immediately=True)
    assert task is not None and task.id == TASK_ID
    assert client._backend.prepare_calls[0]["target_agent_id"] == HOSTED_AGENT_ID
    assert client._backend.prepare_calls[0]["space_id"] == SPACE_ID
    assert client._backend.bind_calls[0]["platform_task_id"] == TASK_ID
    assert client._backend.append_calls
    assert seen_headers[-1]["authorization"] == "Bearer gateway-token"
    assert seen_headers[-1]["x-agentengine-a2a-permit"] == "permit-1"


@pytest.mark.asyncio
async def test_get_task_recovers_remote_task_reference_from_platform_task_id(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/recover.db")
    client = _client_for_app(app, [_agent(HOSTED_AGENT_ID, "hosted")], egress=False)
    await client.discover()
    created = await client.send_message(HOSTED_AGENT_ID, "ping", return_immediately=True)

    recovered = await client.get_task(created.id)

    assert recovered.id == TASK_ID
    assert recovered.remote_task is not None
    assert client._backend.task_operation_calls[-1] == {
        "platform_task_id": TASK_ID,
        "operation": "get_task",
    }


class _FakeTaskClient:
    def __init__(self) -> None:
        self.sent = []
        self.send_responses = []
        self.subscribed = []
        self.canceled = []
        self.subscribe_responses = [
            Task(
                id="remote-task-1",
                context_id="remote-context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        ]
        self.cancel_response = Task(
            id="remote-task-1",
            context_id="remote-context-1",
            status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
        )
        self.get_response = Task(
            id="remote-task-1",
            context_id="remote-context-1",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        )

    async def send_message(self, request, *, context):  # noqa: ANN001, ANN201
        self.sent.append((request, context))
        for response in self.send_responses:
            yield response

    async def subscribe(self, request, *, context):  # noqa: ANN001, ANN201
        self.subscribed.append((request, context))
        for response in self.subscribe_responses:
            yield response

    async def cancel_task(self, request, *, context):  # noqa: ANN001, ANN201
        self.canceled.append((request, context))
        return self.cancel_response

    async def get_task(self, request, *, context):  # noqa: ANN001, ANN201
        return self.get_response

    async def close(self) -> None:
        return None


def _task_operation_client(monkeypatch):  # noqa: ANN001, ANN201
    agent = _agent(HOSTED_AGENT_ID, "hosted")
    backend = _MockDiscoveryBackend([agent])
    backend.bind_calls.append(
        {
            "platform_task_id": TASK_ID,
            "remote_task_id": "remote-task-1",
            "remote_context_id": "remote-context-1",
        }
    )
    wire_client = _FakeTaskClient()

    async def create_fake_client(**kwargs):  # noqa: ANN003, ANN202
        return wire_client

    monkeypatch.setattr("ksadk.a2a.space_client.create_client", create_fake_client)
    return A2ASpaceClient(SPACE_ID, backend), backend, wire_client


@pytest.mark.asyncio
async def test_continue_task_uses_operation_permit_and_remote_task_reference(monkeypatch):
    client, backend, wire_client = _task_operation_client(monkeypatch)

    continued = await client.continue_task(TASK_ID, "more", return_immediately=True)

    assert continued.id == TASK_ID
    assert backend.task_operation_calls[-1]["operation"] == "send_message"
    request, context = wire_client.sent[-1]
    assert request.message.task_id == "remote-task-1"
    assert request.message.context_id == "remote-context-1"
    assert context.service_parameters["X-AgentEngine-A2A-Permit"] == "permit-task-operation"


@pytest.mark.asyncio
async def test_continue_task_rejects_changed_remote_task(monkeypatch):
    client, backend, wire_client = _task_operation_client(monkeypatch)
    wire_client.send_responses.append(
        StreamResponse(
            task=Task(
                id="remote-task-2",
                context_id="remote-context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        )
    )

    with pytest.raises(RuntimeError, match="A2A_REMOTE_BINDING_CONFLICT"):
        await client.continue_task(TASK_ID, "more", return_immediately=True)

    assert backend.bind_calls[-1]["remote_task_id"] == "remote-task-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "cancel", "subscribe"])
async def test_task_operations_reject_changed_remote_reference(monkeypatch, operation):
    client, backend, wire_client = _task_operation_client(monkeypatch)
    changed = Task(
        id="remote-task-2",
        context_id="remote-context-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    wire_client.get_response = changed
    wire_client.cancel_response = changed
    wire_client.subscribe_responses = [changed]

    with pytest.raises(RuntimeError, match="A2A_REMOTE_BINDING_CONFLICT"):
        if operation == "get":
            await client.get_task(TASK_ID)
        elif operation == "cancel":
            await client.cancel(TASK_ID)
        else:
            _ = [item async for item in client.subscribe(TASK_ID)]

    assert backend.append_calls == []


@pytest.mark.parametrize(
    ("message", "error"),
    [
        (Message(role=Role.ROLE_AGENT, parts=[Part(text="x")]), "role must be user"),
        (Message(role=Role.ROLE_USER), "parts must contain 1-64"),
        (
            Message(role=Role.ROLE_USER, parts=[Part(text="x")] * 65),
            "parts must contain 1-64",
        ),
        (
            Message(
                role=Role.ROLE_USER,
                parts=[Part(text="x")],
                message_id="m" * 129,
            ),
            "message_id must contain 1-128",
        ),
    ],
)
def test_message_contract_is_validated_before_prepare(message, error):
    client = A2ASpaceClient(SPACE_ID, _MockDiscoveryBackend([]))

    with pytest.raises(ValueError, match=error):
        client._normalize_initial_message(message)


def test_message_contract_rejects_payload_over_one_mib() -> None:
    client = A2ASpaceClient(SPACE_ID, _MockDiscoveryBackend([]))

    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        client._normalize_initial_message("x" * (1024 * 1024))


@pytest.mark.asyncio
async def test_direct_message_completes_without_remote_task_binding(monkeypatch):
    agent = _agent(HOSTED_AGENT_ID, "hosted")
    backend = _MockDiscoveryBackend([agent])
    wire_client = _FakeTaskClient()
    wire_client.send_responses.append(
        StreamResponse(
            message=Message(
                role=Role.ROLE_AGENT,
                parts=[Part(text="done")],
                message_id="message-direct-1",
                context_id="remote-context-1",
            )
        )
    )

    async def create_fake_client(**kwargs):  # noqa: ANN003, ANN202
        return wire_client

    monkeypatch.setattr("ksadk.a2a.space_client.create_client", create_fake_client)
    client = A2ASpaceClient(SPACE_ID, backend)
    await client.discover()

    result = await client.send_message(HOSTED_AGENT_ID, "ping")

    assert result.remote_task is None
    assert backend.bind_calls == []
    assert [event["EventKind"] for event in backend.append_calls[0]["events"]] == [
        "message",
        "status",
    ]


@pytest.mark.asyncio
async def test_cancel_uses_operation_permit_and_remote_task_id(monkeypatch):
    client, backend, wire_client = _task_operation_client(monkeypatch)

    canceled = await client.cancel(TASK_ID, idempotency_token="idem-cancel-1")

    assert canceled.id == TASK_ID
    assert backend.task_operation_calls[-1] == {
        "platform_task_id": TASK_ID,
        "operation": "cancel_task",
        "idempotency_token": "idem-cancel-1",
    }
    assert wire_client.canceled[-1][0].id == "remote-task-1"
    assert (
        wire_client.canceled[-1][1].service_parameters["X-AgentEngine-A2A-Permit"]
        == "permit-task-operation"
    )


@pytest.mark.asyncio
async def test_subscribe_variants_prepare_operation_and_use_remote_task_id(monkeypatch):
    client, backend, wire_client = _task_operation_client(monkeypatch)
    wire_items = [item async for item in client.subscribe(TASK_ID)]

    event_client, event_backend, event_wire_client = _task_operation_client(monkeypatch)
    runtime_events = [event async for event in event_client.subscribe_events(TASK_ID)]

    assert len(wire_items) == 1
    assert runtime_events
    assert backend.task_operation_calls[-1]["operation"] == "subscribe_to_task"
    assert event_backend.task_operation_calls[-1]["operation"] == "subscribe_to_task"
    assert wire_client.subscribed[-1][0].id == "remote-task-1"
    assert event_wire_client.subscribed[-1][0].id == "remote-task-1"
    assert (
        wire_client.subscribed[-1][1].service_parameters["X-AgentEngine-A2A-Permit"]
        == "permit-task-operation"
    )
    assert (
        event_wire_client.subscribed[-1][1].service_parameters["X-AgentEngine-A2A-Permit"]
        == "permit-task-operation"
    )


@pytest.mark.asyncio
async def test_external_blocked_when_egress_disabled(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _client_for_app(app, [_agent(EXTERNAL_AGENT_ID, "external")], egress=False)
    await client.discover()
    with pytest.raises(PermissionError, match=ERR_PUBLIC_EGRESS_DISABLED):
        await client.send_message(EXTERNAL_AGENT_ID, "ping")


@pytest.mark.asyncio
async def test_external_fails_closed_without_guarded_transport():
    agent = _agent(EXTERNAL_AGENT_ID, "external")
    client = A2ASpaceClient(
        SPACE_ID,
        _MockDiscoveryBackend([agent]),
        egress_enabled=True,
    )
    await client.discover()

    with pytest.raises(RuntimeError, match="A2A_EGRESS_TRANSPORT_REQUIRED"):
        await client.send_message(agent.agent_id, "ping")
    assert client._backend.resolve_calls == []


@pytest.mark.asyncio
async def test_external_allowed_when_egress_enabled(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    client = _client_for_app(app, [_agent(EXTERNAL_AGENT_ID, "external")], egress=True)
    await client.discover()
    task = await client.send_message(EXTERNAL_AGENT_ID, "ping", return_immediately=True)
    assert task is not None and task.id == TASK_ID
    assert client._backend.resolve_calls[0] == {
        "platform_task_id": TASK_ID,
        "credential_handle": "credential-1",
        "call_permit": "permit-1",
    }


@pytest.mark.asyncio
async def test_external_vpc_does_not_require_public_egress(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/vpc.db")
    agent = _agent(VPC_AGENT_ID, "external")
    agent.route_kind = "external_vpc"
    client = _client_for_app(app, [agent], egress=False)
    await client.discover()

    task = await client.send_message(agent.agent_id, "ping", return_immediately=True)

    assert task.id == TASK_ID
    assert client._backend.resolve_calls


@pytest.mark.asyncio
async def test_external_broker_injection_is_request_scoped(tmp_path):
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/injection.db")
    seen: list[dict[str, str]] = []

    @app.middleware("http")
    async def capture_injection(request, call_next):  # noqa: ANN001, ANN202
        if request.url.path.startswith("/a2a/"):
            seen.append(
                {
                    "api_key": request.query_params.get("api_key", ""),
                    "cookie": request.headers.get("cookie", ""),
                    "authorization": request.headers.get("authorization", ""),
                }
            )
        return await call_next(request)

    agent = _agent(EXTERNAL_AGENT_ID, "external")
    backend = _MockDiscoveryBackend([agent])
    backend.credential_injection = CredentialInjection(
        headers={"Authorization": "Bearer external-token"},
        query={"api_key": "query-secret"},
        cookies={"session": "cookie-secret"},
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    client = A2ASpaceClient(
        SPACE_ID,
        backend,
        egress_enabled=True,
        httpx_client=http,
        external_transport=_StaticExternalTransport(http),
    )
    try:
        await client.discover()
        await client.send_message(agent.agent_id, "ping", return_immediately=True)
    finally:
        await http.aclose()

    assert seen[-1] == {
        "api_key": "query-secret",
        "cookie": "session=cookie-secret",
        "authorization": "Bearer external-token",
    }
