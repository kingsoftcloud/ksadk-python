import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ksadk.interaction.langgraph_interrupt import normalize_interrupt
from ksadk.mcp_runtime import MCPServerConfig, build_connection_params
from ksadk.mcp_runtime.oauth.client import BrokerError
from ksadk.mcp_runtime.oauth.contract import CAPABILITY
from ksadk.mcp_runtime.oauth.kernel import MCPAuthorizationExtension
from ksadk.mcp_runtime.oauth.langgraph import MARKER, call_mcp_tool, classify_mcp_error
from ksadk.mcp_runtime.oauth.security import sign, verify


def test_extension_contract_is_independent_and_consistent():
    import ksadk.mcp_runtime.oauth as module

    data = json.loads((Path(module.__file__).parent / "contract.json").read_text())
    assert (
        hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        == CAPABILITY["digest"]
    )
    assert data["ordinary_interaction_completion"] is False


def test_oauth_config_never_becomes_an_eager_anonymous_toolset():
    config = MCPServerConfig(
        name="oauth",
        url="https://mcp.example.test/mcp",
        auth_mode="oauth",
        oauth_config_id="configured",
    )
    with pytest.raises(ValueError, match="helper"):
        build_connection_params(config)
    with pytest.raises(ValueError):
        MCPServerConfig(
            name="oauth",
            url="https://mcp.example.test",
            auth_mode="oauth",
            api_key="not-a-real-key",
            oauth_config_id="configured",
        )


def test_oauth_interrupt_is_recognized_before_ordinary_approval():
    normalized = normalize_interrupt(
        {
            "id": "native-interrupt",
            "value": {
                "kind": "external_authorization",
                "schema": "mcp.authorization/v1",
                "wait_id": "wait",
            },
        }
    )
    assert normalized.schema[MARKER] == "wait"
    assert normalized.schema_source == CAPABILITY["schema"]
    with pytest.raises(ValueError):
        normalize_interrupt(
            {"id": "i", "value": {"kind": "external_authorization", "schema": "future"}}
        )


@pytest.mark.asyncio
async def test_unmanaged_or_unidentified_graph_cannot_start_authorization():
    with pytest.raises(BrokerError, match="MCP_OAUTH_RUNTIME_UNSUPPORTED"):
        await call_mcp_tool("fixture", "read", {}, request_key="stable")


def test_durability_is_not_silently_downgraded():
    with pytest.raises(ValueError, match="PostgreSQL"):
        MCPAuthorizationExtension(SimpleNamespace(), None, public_key=None)


@pytest.mark.parametrize(
    "status,challenge,expected",
    [
        (401, "", "REAUTHORIZATION_REQUIRED"),
        (403, "", "BUSINESS_PERMISSION_DENIED"),
        (403, 'Bearer error="insufficient_scope"', "INSUFFICIENT_SCOPE"),
        (503, "", "CONNECTION_FAILED"),
    ],
)
def test_mcp_failure_classification(status, challenge, expected):
    error = httpx.HTTPStatusError(
        "transport message may contain secrets",
        request=httpx.Request("POST", "https://mcp.example.test"),
        response=httpx.Response(status, headers={"WWW-Authenticate": challenge}),
    )
    assert classify_mcp_error(ExceptionGroup("group", [error])) == expected


def test_completion_signatures_cannot_be_reused_as_workload_credentials():
    key = Ed25519PrivateKey.generate()
    token = sign(
        {"body_digest": "digest", "sub": "runtime"},
        key,
        issuer="broker",
        audience="mcp-authorization-runtime",
    )
    assert (
        verify(token, key.public_key(), issuer="broker", audience="mcp-authorization-runtime")[
            "sub"
        ]
        == "runtime"
    )
    with pytest.raises(ValueError):
        verify(token, key.public_key(), issuer="broker", audience="agentengine-mcp-broker")
    with pytest.raises(ValueError):
        verify(
            token,
            Ed25519PrivateKey.generate().public_key(),
            issuer="broker",
            audience="mcp-authorization-runtime",
        )


@pytest.mark.asyncio
async def test_ui_proxy_requires_trusted_identity_and_cannot_return_credentials():
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from ksadk.mcp_runtime.oauth.http import install_public_routes
    from ksadk.runtime_context import PlatformIdentityContext
    from ksadk.server.invocation_identity import resolve_trusted_invocation_identity

    app = FastAPI()
    client = SimpleNamespace(call=AsyncMock(return_value={"wait_id": "wait", "status": "pending"}))
    install_public_routes(app, SimpleNamespace(client=client))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        url = "/agentengine/api/v1/GetMcpAuthorizationWait"
        assert (await http.post(url, json={"WaitId": "wait"})).status_code == 403
        assert client.call.await_count == 0
        app.dependency_overrides[resolve_trusted_invocation_identity] = lambda: (
            PlatformIdentityContext.from_payload(
                {
                    "identity_namespace": "fixture",
                    "tenant_id": "tenant",
                    "subject_type": "user",
                    "subject_id": "alice",
                }
            )
        )
        response = await http.post(
            url, json={"WaitId": "wait", "AgentId": "agent", "SessionId": "session"}
        )
        assert response.json()["Data"]["wait_id"] == "wait"
        assert client.call.call_args.kwargs["agent_id"] == "agent"
        assert len(client.call.call_args.kwargs["subject_digest"]) == 64
        assert (
            await http.post(url, json={"WaitId": "wait", "subject_id": "bob"})
        ).status_code == 422
        assert (
            await http.post("/agentengine/api/v1/ResolveMcpCredential", json={"WaitId": "wait"})
        ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", ["resume", "submit_interaction"])
async def test_public_commands_cannot_consume_oauth_wait(command_type):
    from unittest.mock import AsyncMock

    from ksadk.kernel.errors import InvalidCommandError
    from ksadk.kernel.worker_controls import WorkerControlMixin

    active = SimpleNamespace(run_id="run")
    worker = WorkerControlMixin()
    worker._store = SimpleNamespace(find_active_run=AsyncMock(return_value=active))
    worker._executions = {}
    worker._mcp_authorization = SimpleNamespace(
        pending=AsyncMock(return_value=[{"wait_id": "wait", "interaction_id": "interrupt"}])
    )
    command = SimpleNamespace(
        command_type=command_type,
        agent_instance_id="instance",
        session_id="session",
        payload={"run_id": "run", "interaction_id": "interrupt"},
    )
    with pytest.raises(InvalidCommandError):
        await worker._control_active_run(command, SimpleNamespace(fencing_token=1))


@pytest.mark.asyncio
async def test_cloud_studio_stream_keeps_oauth_capability_outside_messages():
    from unittest.mock import AsyncMock, Mock

    from ksadk.api.client import AgentEngineClient
    from ksadk.studio.cloud import CloudDeploymentService, DirectAgentEngineCloudDeploymentGateway
    from ksadk.studio.cloud_shared_web import CloudSharedWebBridge

    extensions = {"mcp.authorization": CAPABILITY}
    client = object.__new__(AgentEngineClient)
    client._open_chat_stream = Mock(return_value="opened-stream")
    gateway = object.__new__(DirectAgentEngineCloudDeploymentGateway)
    gateway.stream_client = client
    gateway._chat_agent_id = lambda target: "agent"
    service = object.__new__(CloudDeploymentService)
    service.gateway = gateway
    service._chat_target = AsyncMock(return_value=SimpleNamespace())
    result = await CloudSharedWebBridge(service).open_run_stream(
        "agent",
        {
            "SessionId": "session",
            "Messages": [{"role": "user", "content": "read"}],
            "Extensions": extensions,
            "InvocationId": "original-request",
        },
    )
    assert result == "opened-stream"
    body = client._open_chat_stream.call_args.args[0]
    assert body["Extensions"] == extensions
    assert body["SessionId"] == "session" and body["InvocationId"] == "original-request"
    assert "mcp.authorization" not in json.dumps(body["Messages"])

async def test_concurrent_first_schema_initialization_on_two_workers():
    import asyncio
    import os
    import uuid
    import asyncpg

    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires isolated PostgreSQL")
    schema = "oauth_review_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        async with asyncpg.create_pool(dsn, min_size=2, max_size=2,
                server_settings={"search_path": schema}) as pool:
            store = SimpleNamespace(_pool=pool, _assert_fence=True)
            workers = [MCPAuthorizationExtension(store, None) for _ in range(2)]
            await asyncio.gather(*(worker.ensure_schema() for worker in workers))
            assert all(w._schema_ready for w in workers)
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT COUNT(*) FROM mcp_authorization_waits") == 0
    finally:
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()
