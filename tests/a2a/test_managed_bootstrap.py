from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient

from ksadk.a2a.bootstrap import AgentEngineA2ABootstrap, RuntimeA2AMetadata
from ksadk.a2a.context_store import SQLiteA2AContextStore
from ksadk.a2a.external_transport import (
    A2ATransportLease,
    CallableA2ARouteOpener,
    GuardedA2AExternalTransport,
)
from ksadk.a2a.identity import A2AIngressIdentity, CallableGatewayIdentityVerifier
from ksadk.a2a.task_event_outbox import SQLiteA2ATaskEventOutbox
from ksadk.a2a.task_store import build_a2a_task_store
from ksadk.harness import HarnessApp
from ksadk.server.app import _configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app

SPACE_A = "a2a-space-00000000000040008000000000000041"
SPACE_B = "a2a-space-00000000000040008000000000000042"


class _Runner:
    async def invoke(self, input_data):  # noqa: ANN001
        return {"output": input_data["input"]}


def test_managed_bootstrap_wires_runtime_lifecycle_and_shared_space_clients(tmp_path) -> None:
    external_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
    hosted_client = httpx.AsyncClient(trust_env=False)

    @asynccontextmanager
    async def open_route(route, route_kind):  # noqa: ANN001, ANN202
        yield A2ATransportLease(
            httpx_client=external_client,
            effective_interface=route,
            route_kind=route_kind,
            policy_revision="test",
        )

    async def verify(request):  # noqa: ANN001, ANN202
        if request.headers.get("x-gateway-mtls") != "verified":
            raise PermissionError("Gateway mTLS is required")
        return A2AIngressIdentity(
            account_id="account-a",
            tenant_id="tenant-a",
            caller_principal_type="user",
            caller_principal_id="caller-a",
            target_agent_id="ar-00000000000040008000000000000041",
        )

    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="managed-agent",
            version="1.0.0",
            skills=("echo",),
        ),
        task_store=build_a2a_task_store(
            dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.sqlite3",
        ),
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
        checkpoint_store=object(),
        gateway_identity_verifier=CallableGatewayIdentityVerifier(verify),
        external_transport=GuardedA2AExternalTransport(CallableA2ARouteOpener(open_route)),
        control_plane=object(),
        hosted_http_client=hosted_client,
        event_outbox=SQLiteA2ATaskEventOutbox(tmp_path / "events.sqlite3"),
    )
    app = create_runtime_app(
        RuntimeAppConfig(runner=_Runner(), a2a=bootstrap),  # type: ignore[arg-type]
        _configure_runtime_app,
    )

    try:
        first = bootstrap.client_for_space(SPACE_A)
        second = bootstrap.client_for_space(SPACE_B)
        assert first.event_dispatcher is second.event_dispatcher
        with TestClient(app) as client:
            assert app.state.runtime.a2a_server is not None
            assert client.post("/a2a/jsonrpc", json={}).status_code == 401
    finally:
        asyncio.run(hosted_client.aclose())
        asyncio.run(external_client.aclose())


def test_outbound_only_bootstrap_does_not_require_inbound_dependencies(tmp_path) -> None:
    external_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
    hosted_client = httpx.AsyncClient(trust_env=False)

    @asynccontextmanager
    async def open_route(route, route_kind):  # noqa: ANN001, ANN202
        yield A2ATransportLease(
            httpx_client=external_client,
            effective_interface=route,
            route_kind=route_kind,
            policy_revision="test",
        )

    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="outbound-only-agent",
            version="1.0.0",
            skills=("echo",),
        ),
        task_store=None,
        context_store=None,
        checkpoint_store=None,
        gateway_identity_verifier=None,
        external_transport=GuardedA2AExternalTransport(CallableA2ARouteOpener(open_route)),
        control_plane=object(),
        hosted_http_client=hosted_client,
        event_outbox=SQLiteA2ATaskEventOutbox(tmp_path / "events.sqlite3"),
        inbound_enabled=False,
    )
    app = create_runtime_app(
        RuntimeAppConfig(runner=_Runner(), a2a=bootstrap),  # type: ignore[arg-type]
        _configure_runtime_app,
    )

    try:
        assert bootstrap.client_for_space(SPACE_A).event_dispatcher is bootstrap.event_dispatcher
        with TestClient(app):
            assert app.state.a2a_bootstrap is bootstrap
            assert app.state.runtime.a2a_bootstrap is bootstrap
            assert app.state.runtime.a2a_server is None
    finally:
        asyncio.run(hosted_client.aclose())
        asyncio.run(external_client.aclose())


def test_harness_uses_the_managed_bootstrap_path(tmp_path) -> None:
    external_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
    hosted_client = httpx.AsyncClient(trust_env=False)

    @asynccontextmanager
    async def open_route(route, route_kind):  # noqa: ANN001, ANN202
        yield A2ATransportLease(
            httpx_client=external_client,
            effective_interface=route,
            route_kind=route_kind,
            policy_revision="test",
        )

    async def verify(request):  # noqa: ANN001, ANN202
        if request.headers.get("x-gateway-mtls") != "verified":
            raise PermissionError("Gateway mTLS is required")
        return A2AIngressIdentity(
            account_id="account-a",
            tenant_id="tenant-a",
            caller_principal_type="user",
            caller_principal_id="caller-a",
            target_agent_id="ar-00000000000040008000000000000041",
        )

    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="harness-agent",
            version="1.0.0",
            skills=("echo",),
        ),
        task_store=build_a2a_task_store(
            dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.sqlite3",
        ),
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
        checkpoint_store=object(),
        gateway_identity_verifier=CallableGatewayIdentityVerifier(verify),
        external_transport=GuardedA2AExternalTransport(CallableA2ARouteOpener(open_route)),
        control_plane=object(),
        hosted_http_client=hosted_client,
        event_outbox=SQLiteA2ATaskEventOutbox(tmp_path / "events.sqlite3"),
    )
    config_path = tmp_path / "harness.yaml"
    config_path.write_text("model: test-model\nprompt: test prompt\n", encoding="utf-8")
    harness = HarnessApp.from_yaml(config_path, a2a=bootstrap, workspace_root=tmp_path)

    try:
        with TestClient(harness.build_app()) as client:
            assert client.post("/a2a/jsonrpc", json={}).status_code == 401
            assert harness.build_app().state.runtime.a2a_server is bootstrap.server
    finally:
        asyncio.run(hosted_client.aclose())
        asyncio.run(external_client.aclose())
