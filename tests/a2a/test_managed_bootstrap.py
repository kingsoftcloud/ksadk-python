from __future__ import annotations

import asyncio

import httpx
import pytest
from a2a.server.tasks import TaskStore
from a2a.types import AgentSkill
from fastapi.testclient import TestClient

from ksadk.a2a.bootstrap import AgentEngineA2ABootstrap, RuntimeA2AMetadata
from ksadk.a2a.context_store import A2AContextIdentity, A2AContextStore, SQLiteA2AContextStore
from ksadk.a2a.external_transport import RuntimeLocalA2AExternalTransport
from ksadk.a2a.identity import (
    A2AIngressIdentity,
    CallableGatewayIdentityVerifier,
    CallableGatewayProbeVerifier,
)
from ksadk.a2a.resume_store import InMemoryA2AResumeStateStore
from ksadk.a2a.task_event_outbox import SQLiteA2ATaskEventOutbox
from ksadk.a2a.task_store import build_a2a_task_store
from ksadk.harness import HarnessApp
from ksadk.server.app import _configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app

SPACE_A = "a2a-space-00000000000040008000000000000041"
SPACE_B = "a2a-space-00000000000040008000000000000042"
A2A_AGENT_ID = "a2a-agent-00000000000040008000000000000041"
ECHO_SKILLS = (AgentSkill(id="echo", name="Echo", description="Echo", tags=["echo"]),)


class _Runner:
    async def invoke(self, input_data):  # noqa: ANN001
        return {"output": input_data["input"]}


class _ReadyTaskStore(TaskStore):
    def __init__(self, initialized: list[str]) -> None:
        self._initialized = initialized

    async def initialize(self) -> None:
        self._initialized.append("task_store")

    async def save(self, task, context):  # noqa: ANN001, ANN201
        return None

    async def get(self, task_id, context):  # noqa: ANN001, ANN201
        return None

    async def list(self, params, context):  # noqa: ANN001, ANN201
        raise NotImplementedError

    async def delete(self, task_id, context):  # noqa: ANN001, ANN201
        return None


class _ReadyContextStore(A2AContextStore):
    def __init__(self, initialized: list[str]) -> None:
        self._initialized = initialized

    async def initialize(self) -> None:
        self._initialized.append("context_store")

    async def resolve_or_create(
        self,
        identity: A2AContextIdentity,
        external_context_id: str,
        *,
        isolation_scope: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def get(
        self,
        identity: A2AContextIdentity,
        external_context_id: str,
        *,
        isolation_scope: str | None = None,
    ) -> str | None:
        raise NotImplementedError


class _ReadyCheckpointStore:
    def __init__(self, initialized: list[str]) -> None:
        self._initialized = initialized

    async def initialize(self) -> None:
        self._initialized.append("checkpoint_store")


class _ReadyResumeStateStore(InMemoryA2AResumeStateStore):
    def __init__(self, initialized: list[str]) -> None:
        super().__init__()
        self._initialized = initialized

    async def initialize(self) -> None:
        self._initialized.append("resume_state_store")


async def _allow_probe(request) -> None:  # noqa: ANN001, ANN202
    return None


class _NoopControlPlane:
    async def append_task_events(self, *, platform_task_id, events):  # noqa: ANN001, ANN201
        return {"ok": True}


@pytest.mark.asyncio
async def test_managed_bootstrap_requires_and_initializes_all_inbound_durable_dependencies(
    tmp_path,
) -> None:
    initialized: list[str] = []
    hosted_client = httpx.AsyncClient(trust_env=False)
    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            a2a_agent_id=A2A_AGENT_ID,
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="managed-agent",
            version="1.0.0",
            skills=ECHO_SKILLS,
        ),
        task_store=_ReadyTaskStore(initialized),
        context_store=_ReadyContextStore(initialized),
        checkpoint_store=_ReadyCheckpointStore(initialized),
        resume_state_store=_ReadyResumeStateStore(initialized),
        gateway_identity_verifier=CallableGatewayIdentityVerifier(lambda request: None),  # type: ignore[arg-type]
        gateway_probe_verifier=CallableGatewayProbeVerifier(_allow_probe),
        external_transport=RuntimeLocalA2AExternalTransport(),
        control_plane=_NoopControlPlane(),
        hosted_http_client=hosted_client,
        event_outbox=SQLiteA2ATaskEventOutbox(tmp_path / "events.sqlite3"),
    )
    try:
        await bootstrap.start()
        assert initialized == [
            "task_store",
            "context_store",
            "checkpoint_store",
            "resume_state_store",
        ]
    finally:
        await bootstrap.stop(flush_timeout_seconds=0)
        await hosted_client.aclose()


def test_managed_bootstrap_rejects_uninitializable_inbound_dependencies(tmp_path) -> None:
    hosted_client = httpx.AsyncClient(trust_env=False)
    try:
        with pytest.raises(TypeError, match="checkpoint_store must implement async initialize"):
            AgentEngineA2ABootstrap.from_platform(
                runtime_metadata=RuntimeA2AMetadata(
                    account_id="account-a",
                    tenant_id="tenant-a",
                    agent_id="ar-00000000000040008000000000000041",
                    a2a_agent_id=A2A_AGENT_ID,
                    runtime_id="runtime-a",
                    internal_base_url="https://runtime.internal",
                    name="managed-agent",
                    version="1.0.0",
                    skills=ECHO_SKILLS,
                ),
                task_store=_ReadyTaskStore([]),
                context_store=_ReadyContextStore([]),
                checkpoint_store=object(),
                resume_state_store=_ReadyResumeStateStore([]),
                gateway_identity_verifier=CallableGatewayIdentityVerifier(lambda request: None),  # type: ignore[arg-type]
                gateway_probe_verifier=CallableGatewayProbeVerifier(_allow_probe),
                external_transport=RuntimeLocalA2AExternalTransport(),
                control_plane=_NoopControlPlane(),
                hosted_http_client=hosted_client,
                event_outbox=SQLiteA2ATaskEventOutbox(tmp_path / "events.sqlite3"),
            )
    finally:
        asyncio.run(hosted_client.aclose())


def test_managed_bootstrap_rejects_an_arbitrary_external_transport(tmp_path) -> None:
    hosted_client = httpx.AsyncClient(trust_env=False)
    try:
        with pytest.raises(TypeError, match="requires RuntimeLocalA2AExternalTransport"):
            AgentEngineA2ABootstrap.from_platform(
                runtime_metadata=RuntimeA2AMetadata(
                    account_id="account-a",
                    tenant_id="tenant-a",
                    agent_id="ar-00000000000040008000000000000041",
                    a2a_agent_id=A2A_AGENT_ID,
                    runtime_id="runtime-a",
                    internal_base_url="https://runtime.internal",
                    name="managed-agent",
                    version="1.0.0",
                    skills=ECHO_SKILLS,
                ),
                task_store=None,
                context_store=None,
                checkpoint_store=None,
                resume_state_store=None,
                gateway_identity_verifier=None,
                gateway_probe_verifier=None,
                external_transport=object(),  # type: ignore[arg-type]
                control_plane=_NoopControlPlane(),
                hosted_http_client=hosted_client,
                event_outbox=SQLiteA2ATaskEventOutbox(tmp_path / "events.sqlite3"),
                inbound_enabled=False,
            )
    finally:
        asyncio.run(hosted_client.aclose())


@pytest.mark.parametrize(
    "base_url",
    [
        "https://runtime.internal/path",
        "https://user:password@runtime.internal",
        "https://runtime.internal?query=value",
        "https://runtime.internal#fragment",
        "https://runtime.internal/",
        "https://runtime.internal:",
        "https://runtime.internal:0",
    ],
)
def test_runtime_metadata_requires_a_strict_http_origin(base_url: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP\\(S\\) origin"):
        RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            a2a_agent_id=A2A_AGENT_ID,
            runtime_id="runtime-a",
            internal_base_url=base_url,
            name="managed-agent",
            version="1.0.0",
            skills=ECHO_SKILLS,
        ).validate()


@pytest.mark.parametrize(
    "skills",
    [
        ("echo",),
        tuple(
            AgentSkill(id=f"skill-{index}", name="Skill", description="Skill")
            for index in range(101)
        ),
    ],
)
def test_runtime_metadata_requires_bounded_standard_skills(skills) -> None:  # noqa: ANN001
    with pytest.raises(ValueError, match="skills must contain at most 100 AgentSkill"):
        RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            a2a_agent_id=A2A_AGENT_ID,
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="managed-agent",
            version="1.0.0",
            skills=skills,
        ).validate()


def test_managed_bootstrap_wires_runtime_lifecycle_and_shared_space_clients(tmp_path) -> None:
    hosted_client = httpx.AsyncClient(trust_env=False)

    async def verify(request):  # noqa: ANN001, ANN202
        verified = request.headers.get("x-gateway-mtls")
        if verified not in {"verified", "cross-account"}:
            raise PermissionError("Gateway mTLS is required")
        return A2AIngressIdentity(
            account_id="account-other" if verified == "cross-account" else "account-a",
            tenant_id="tenant-a",
            caller_principal_type="user",
            caller_principal_id="caller-a",
            target_agent_id="ar-00000000000040008000000000000041",
            target_runtime_id="runtime-a",
            target_a2a_agent_id=A2A_AGENT_ID,
            authn_mode="api_key",
        )

    async def verify_probe(request):  # noqa: ANN001, ANN202
        if request.headers.get("x-gateway-mtls") != "verified":
            raise PermissionError("Gateway mTLS is required for Runtime AgentCard probes")

    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            a2a_agent_id=A2A_AGENT_ID,
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="managed-agent",
            version="1.0.0",
            skills=ECHO_SKILLS,
        ),
        task_store=build_a2a_task_store(
            dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.sqlite3",
        ),
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
        checkpoint_store=_ReadyCheckpointStore([]),
        resume_state_store=_ReadyResumeStateStore([]),
        gateway_identity_verifier=CallableGatewayIdentityVerifier(verify),
        gateway_probe_verifier=CallableGatewayProbeVerifier(verify_probe),
        external_transport=RuntimeLocalA2AExternalTransport(),
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
            assert client.get("/.well-known/agent-card.json").status_code == 401
            assert (
                client.get(
                    "/.well-known/agent-card.json",
                    headers={"x-gateway-mtls": "verified"},
                ).status_code
                == 200
            )
            assert client.post("/a2a/jsonrpc", json={}).status_code == 401
            assert (
                client.post(
                    "/a2a/jsonrpc",
                    headers={"x-gateway-mtls": "cross-account"},
                    json={},
                ).status_code
                == 401
            )
    finally:
        asyncio.run(hosted_client.aclose())


def test_outbound_only_bootstrap_does_not_require_inbound_dependencies(tmp_path) -> None:
    hosted_client = httpx.AsyncClient(trust_env=False)

    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            a2a_agent_id=A2A_AGENT_ID,
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="outbound-only-agent",
            version="1.0.0",
            skills=ECHO_SKILLS,
        ),
        task_store=None,
        context_store=None,
        checkpoint_store=None,
        resume_state_store=None,
        gateway_identity_verifier=None,
        gateway_probe_verifier=None,
        external_transport=None,
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


@pytest.mark.asyncio
async def test_inbound_only_bootstrap_does_not_require_outbound_dependencies() -> None:
    initialized: list[str] = []
    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            a2a_agent_id=A2A_AGENT_ID,
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="inbound-only-agent",
            version="1.0.0",
            skills=ECHO_SKILLS,
        ),
        task_store=_ReadyTaskStore(initialized),
        context_store=_ReadyContextStore(initialized),
        checkpoint_store=_ReadyCheckpointStore(initialized),
        resume_state_store=_ReadyResumeStateStore(initialized),
        gateway_identity_verifier=CallableGatewayIdentityVerifier(lambda request: None),  # type: ignore[arg-type]
        gateway_probe_verifier=CallableGatewayProbeVerifier(_allow_probe),
        external_transport=None,
        control_plane=None,
        hosted_http_client=None,
        event_outbox=None,
        outbound_enabled=False,
    )
    try:
        assert bootstrap.public_egress_enabled is False
        await bootstrap.start()
        assert initialized == [
            "task_store",
            "context_store",
            "checkpoint_store",
            "resume_state_store",
        ]
        with pytest.raises(RuntimeError, match="outbound client is disabled"):
            bootstrap.client_for_space(SPACE_A)
    finally:
        await bootstrap.stop(flush_timeout_seconds=0)


def test_harness_uses_the_managed_bootstrap_path(tmp_path) -> None:
    hosted_client = httpx.AsyncClient(trust_env=False)

    async def verify(request):  # noqa: ANN001, ANN202
        if request.headers.get("x-gateway-mtls") != "verified":
            raise PermissionError("Gateway mTLS is required")
        return A2AIngressIdentity(
            account_id="account-a",
            tenant_id="tenant-a",
            caller_principal_type="user",
            caller_principal_id="caller-a",
            target_agent_id="ar-00000000000040008000000000000041",
            target_runtime_id="runtime-a",
            target_a2a_agent_id=A2A_AGENT_ID,
            authn_mode="api_key",
        )

    bootstrap = AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=RuntimeA2AMetadata(
            account_id="account-a",
            tenant_id="tenant-a",
            agent_id="ar-00000000000040008000000000000041",
            a2a_agent_id=A2A_AGENT_ID,
            runtime_id="runtime-a",
            internal_base_url="https://runtime.internal",
            name="harness-agent",
            version="1.0.0",
            skills=ECHO_SKILLS,
        ),
        task_store=build_a2a_task_store(
            dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.sqlite3",
        ),
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
        checkpoint_store=_ReadyCheckpointStore([]),
        resume_state_store=_ReadyResumeStateStore([]),
        gateway_identity_verifier=CallableGatewayIdentityVerifier(verify),
        gateway_probe_verifier=CallableGatewayProbeVerifier(_allow_probe),
        external_transport=RuntimeLocalA2AExternalTransport(),
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
