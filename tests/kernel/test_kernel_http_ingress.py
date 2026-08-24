# -*- coding: utf-8 -*-
"""Kernel canonical HTTP ingress（/agent-kernel/v1/*）契约测试。

锁定三边一致路径（agentengine-gateway 转发、agentengine-server runtime
client、KsADK runtime）与 receipt 映射、health shape、env bootstrap。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from ksadk.kernel import ingress
from ksadk.kernel.contracts import AgentControlReceipt


def _command_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command_id": str(uuid.uuid4()),
        "idempotency_key": "idem-1",
        "tenant_id": "tenant-a",
        "agent_instance_id": "instance-a",
        "session_id": "sess-1",
        "command_type": "enqueue",
        "payload": {"content": "hello"},
        "source": {"kind": "studio", "ref": "ui"},
        "authorization_ref": "permit-1",
        "submitted_at": "2026-08-18T00:00:00Z",
    }


class StubKernel:
    def __init__(self) -> None:
        self.submits: list[Any] = []
        self.statuses: list[Any] = []

    async def submit(self, command: Any, *, permit: Any) -> AgentControlReceipt:
        self.submits.append((command, permit))
        return AgentControlReceipt(
            command_id=command.command_id,
            status="accepted",
            message_id=uuid.uuid4(),
            accepted_seq=3,
        )

    async def status(self, query: Any, *, permit: Any) -> Any:
        self.statuses.append(query)

        class _Snap:
            def model_dump_json(self) -> str:
                import json

                return json.dumps({"agent_instance_id": query.agent_instance_id})

        return _Snap()


def test_local_trusted_context_uses_runtime_agent_instance(monkeypatch):
    """Local web compatibility routes must target the worker's instance id."""
    monkeypatch.setenv("AGENT_INSTANCE_ID", "instance-from-runtime")
    trusted = ingress.trusted_context(source_kind="system", source_ref="test")
    assert trusted.agent_instance_id == "instance-from-runtime"
    assert trusted.permit.agent_instance_id == "instance-from-runtime"


def test_response_projector_resolves_completed_output_refs():
    from ksadk.server.routes.kernel_ingress import _new_responses_projector

    projector = _new_responses_projector()
    assert (
        projector(
            SimpleNamespace(
                event_type="item.updated",
                payload={
                    "item_id": "draft",
                    "update": {"text": "discarded commentary", "op": "append"},
                },
            )
        )
        is None
    )
    assert (
        projector(
            SimpleNamespace(
                event_type="item.completed",
                payload={
                    "item_id": "final",
                    "snapshot": {"parts": [{"text": "durable answer"}]},
                },
            )
        )
        is None
    )
    projected = projector(
        SimpleNamespace(
            event_type="run.completed",
            payload={"output_refs": [{"item_id": "final", "part_id": "text-0"}]},
        )
    )
    assert projected is not None
    assert projected[1]["output_text"] == "durable answer"


def test_response_projector_surfaces_durable_approval_as_responses_item():
    from ksadk.server.routes.kernel_ingress import _new_responses_projector

    projected = _new_responses_projector()(
        SimpleNamespace(
            event_type="interaction.requested",
            payload={
                "interaction_id": "approval-1",
                "kind": "approval",
                "request": {
                    "presentation": {
                        "title": "run_command",
                        "description": '{"arguments":{"command":"touch marker"}}',
                    }
                },
            },
        )
    )

    assert projected == (
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "item": {
                "id": "approval-1",
                "type": "mcp_approval_request",
                "name": "run_command",
                "arguments": '{"command": "touch marker"}',
            },
        },
    )


@pytest.mark.asyncio
async def test_submit_command_creates_the_session_in_the_kernel_shared_log(monkeypatch):
    """A direct runtime ingress must not rely on a separate HTTP session service."""
    from ksadk.kernel.bootstrap import clear_agent_kernel_runtime, set_agent_kernel_runtime
    from ksadk.kernel.contracts import AgentControlCommand
    from ksadk.sessions.in_memory import InMemorySessionService

    shared_sessions = InMemorySessionService()
    stub = StubKernel()
    ingress.set_agent_kernel(stub)
    set_agent_kernel_runtime(
        SimpleNamespace(config=SimpleNamespace(session_service=shared_sessions))
    )
    try:
        command = AgentControlCommand.model_validate(_command_payload())
        receipt = await ingress.submit_command(command, permit=object())

        assert receipt.status == "accepted"
        session = await shared_sessions.get_session(command.session_id)
        assert session is not None
        assert session.agent_id == command.agent_instance_id
    finally:
        clear_agent_kernel_runtime()
        ingress.clear_agent_kernel()


@pytest.fixture
def app_with_kernel(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    stub = StubKernel()
    ingress.set_agent_kernel(stub)
    app = FastAPI()
    app.include_router(ingress.agent_kernel_router())
    client = TestClient(app)
    yield client, stub
    ingress.clear_agent_kernel()


@pytest.fixture
def app_without_kernel():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    ingress.clear_agent_kernel()
    app = FastAPI()
    app.include_router(ingress.agent_kernel_router())
    client = TestClient(app)
    yield client
    ingress.clear_agent_kernel()


# ---------------------------------------------------------------------------
# 路径常量：三边一致的 wire contract
# ---------------------------------------------------------------------------


def test_kernel_ingress_path_constants():
    assert ingress.KERNEL_INGRESS_BASE_PATH == "/agent-kernel/v1"
    assert ingress.KERNEL_INGRESS_SUBMIT_PATH == "/agent-kernel/v1/SubmitAgentControl"
    assert ingress.KERNEL_INGRESS_STATUS_PATH == "/agent-kernel/v1/GetAgentStatus"
    assert ingress.KERNEL_INGRESS_SESSION_EVENTS_PATH == "/agent-kernel/v1/SubscribeSessionEvents"
    assert ingress.KERNEL_INGRESS_HEALTH_PATH == "/agent-kernel/v1/health"


# ---------------------------------------------------------------------------
# SubmitAgentControl
# ---------------------------------------------------------------------------


def test_submit_agent_control_wrapped_command_and_permit(app_with_kernel):
    client, stub = app_with_kernel
    response = client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": _command_payload()},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["accepted_seq"] == 3
    assert response.headers["X-Ksadk-Control-Status"] == "accepted"
    assert len(stub.submits) == 1


def test_submit_agent_control_bare_command_body(app_with_kernel):
    """Gateway 原样转发公网 command body（无 permit wrapper）也必须可用。"""

    client, stub = app_with_kernel
    response = client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json=_command_payload(),
    )
    assert response.status_code == 202, response.text
    assert stub.submits[0][0].idempotency_key == "idem-1"


def test_submit_agent_control_invalid_command_is_400(app_with_kernel):
    client, _ = app_with_kernel
    response = client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json={"command": {"nope": 1}})
    assert response.status_code == 400


def test_kernel_routes_503_when_not_registered(app_without_kernel):
    client = app_without_kernel
    response = client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json={"command": _command_payload()})
    assert response.status_code == 503
    assert response.json()["error"]["Code"] == "kernel_not_enabled"


# ---------------------------------------------------------------------------
# health：Operator AgentKernelReady 读这个 shape
# ---------------------------------------------------------------------------


def test_kernel_health_shape(app_without_kernel, monkeypatch):
    from ksadk.kernel.contract_fingerprints import AGENT_KERNEL_V1_AGGREGATE_DIGEST

    client = app_without_kernel
    monkeypatch.setenv("AGENT_KERNEL_CONTRACT_DIGEST", "c" * 64)
    monkeypatch.setenv("AGENT_KERNEL_CAPABILITY_DIGEST", "p" * 64)
    response = client.get(ingress.KERNEL_INGRESS_HEALTH_PATH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "enabled",
        "ready",
        "store_driver",
        "contract_digest",
        "capability_digest",
        "runtime_identity",
    }
    assert body["contract_digest"] == AGENT_KERNEL_V1_AGGREGATE_DIGEST
    assert body["capability_digest"] == ""
    assert body["runtime_identity"]["ksadk_version"]


# ---------------------------------------------------------------------------
# env bootstrap：AGENT_KERNEL_ENABLED=1 自动装配
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_disabled_is_noop(monkeypatch):
    monkeypatch.delenv("AGENT_KERNEL_ENABLED", raising=False)
    monkeypatch.delenv("KSADK_AGENT_KERNEL", raising=False)
    ingress.clear_agent_kernel()
    assert await ingress.bootstrap_agent_kernel_from_env() is None
    assert ingress.get_agent_kernel() is None


@pytest.mark.asyncio
async def test_bootstrap_enabled_registers_kernel(monkeypatch):
    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_STORE_DRIVER", "memory")
    ingress.clear_agent_kernel()
    kernel = await ingress.bootstrap_agent_kernel_from_env()
    try:
        assert kernel is not None
        assert ingress.get_agent_kernel() is kernel
        assert ingress.kernel_route_active()
    finally:
        ingress.clear_agent_kernel()


@pytest.mark.asyncio
async def test_bootstrap_enabled_requires_dsn_for_postgres(monkeypatch):
    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_STORE_DRIVER", "postgres")
    monkeypatch.delenv("AGENT_KERNEL_STORE_DSN", raising=False)
    ingress.clear_agent_kernel()
    with pytest.raises(RuntimeError):
        await ingress.bootstrap_agent_kernel_from_env()
    ingress.clear_agent_kernel()


@pytest.mark.asyncio
async def test_hosted_legacy_ingress_bootstrap_refuses_half_runtime(monkeypatch):
    """hosted 必须由完整 composition root 启动，不能只注册 ingress facade。"""

    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "hosted")
    ingress.clear_agent_kernel()
    with pytest.raises(RuntimeError, match="full production composition root"):
        await ingress.bootstrap_agent_kernel_from_env()
    assert ingress.get_agent_kernel() is None


@pytest.mark.asyncio
async def test_hosted_legacy_ingress_rejects_pre_registered_bare_kernel(monkeypatch):
    """已提前注册的 facade 同样不能绕过 hosted composition-root 约束。"""

    from ksadk.kernel.bootstrap import clear_agent_kernel_runtime

    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "hosted")
    clear_agent_kernel_runtime()
    ingress.set_agent_kernel(object())
    try:
        with pytest.raises(RuntimeError, match="bare kernel is not allowed"):
            await ingress.bootstrap_agent_kernel_from_env()
    finally:
        ingress.clear_agent_kernel()


@pytest.mark.asyncio
async def test_bootstrap_idempotent(monkeypatch):
    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_STORE_DRIVER", "memory")
    ingress.clear_agent_kernel()
    first = await ingress.bootstrap_agent_kernel_from_env()
    second = await ingress.bootstrap_agent_kernel_from_env()
    try:
        assert first is second
    finally:
        ingress.clear_agent_kernel()


def test_kernel_ingress_enabled_accepts_platform_env(monkeypatch):
    monkeypatch.delenv("KSADK_AGENT_KERNEL", raising=False)
    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    assert ingress.kernel_ingress_enabled()
    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "false")
    assert not ingress.kernel_ingress_enabled()


# ---------------------------------------------------------------------------
# GetAgentStatus / SubscribeSessionEvents：真实 in-process kernel 的 permit 绑定
#
# 预发回归：canonical ingress 的 status 用本地 trusted context 签 permit，
# 但拿 caller 自报的 query.authorization_ref 做绑定校验 -> authorization_ref_mismatch
# -> 恒 fail-closed，instance_state 永远 unavailable。必须用 StubKernel 之外
# 的真实 kernel 断言（StubKernel 不跑 verifier，掩盖了该 bug）。
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_with_real_kernel(monkeypatch):
    import httpx

    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_STORE_DRIVER", "memory")
    ingress.clear_agent_kernel()
    kernel = await ingress.bootstrap_agent_kernel_from_env()
    assert kernel is not None
    # canonical ingress 不建 session（server bootstrap / canary middleware 负责），
    # 测试里直接经 event store 的 session service 预建。
    await kernel._events.session_service.create_session(
        agent_id="local-agent", user_id="kernel-test", session_id="sess-1"
    )
    app = FastAPI()
    app.include_router(ingress.agent_kernel_router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime.test"
    ) as client:
        yield client, kernel
    ingress.clear_agent_kernel()


def _status_query_payload(*, authorization_ref: str = "caller-self-declared-ref") -> dict:
    return {
        "schema_version": 1,
        "tenant_id": "local",
        "agent_instance_id": "local-agent",
        "session_id": "sess-1",
        "authorization_ref": authorization_ref,
    }


async def test_get_agent_status_returns_real_state_not_fail_closed(app_with_real_kernel):
    """status 必须返回真实 instance_state / inbox_depth，而不是恒 unavailable。"""
    from ksadk.kernel.store import ActivationLeaseRequest

    client, kernel = app_with_real_kernel

    # 真实路径提交一个 enqueue -> inbox_depth = 1
    submit = await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=_command_payload())
    assert submit.status_code == 202, submit.text

    # worker 持有 lease -> instance_state = ready（模拟 worker activation）
    await kernel._store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="local-agent",
            session_id="sess-1",
            activation_id="act-status-1",
            runtime_type="fake",
            bundle_digest="phase1-test",
            capability_digest="phase1-test",
            lease_ttl_seconds=60.0,
        )
    )

    response = await client.post(
        ingress.KERNEL_INGRESS_STATUS_PATH,
        json=_status_query_payload(authorization_ref="caller-self-declared-ref"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instance_state"] == "ready", body
    assert body["inbox_depth"] == 1, body
    assert body["activation_id"] == "act-status-1"


async def test_get_agent_status_without_lease_reports_degraded_not_unavailable(
    app_with_real_kernel,
):
    """无 lease 时是 degraded（真实状态），不得 fail-closed 成 unavailable。"""
    client, _ = app_with_real_kernel
    response = await client.post(ingress.KERNEL_INGRESS_STATUS_PATH, json=_status_query_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instance_state"] == "degraded", body
    assert body["inbox_depth"] == 0


# ---------------------------------------------------------------------------
# Task 4 Step 1: hosted 模式 fail-closed ingress。
#
# hosted authority（AGENT_KERNEL_AUTHORITY_MODE=hosted / JWKS 配置时推断）：
# - 缺 permit 一律 401，禁止进程内自签补发；
# - 本地自签 / 未知 key / 错 issuer / 过期 / session 绑定不符 / operation
#   越权 / nonce 重放一律 403（kernel 内 fail-closed，HTTP 层不得 200/400 放行）。
# 只有显式 AGENT_KERNEL_AUTHORITY_MODE=local 才允许本地授权（开发/灰度）。
# ---------------------------------------------------------------------------


@pytest.fixture
async def hosted_kernel_app(monkeypatch):
    import httpx

    from ksadk.kernel.control import AgentKernel
    from tests.kernel.control_harness import CLOCK_AT, kernel_stack

    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "hosted")
    stack = await kernel_stack()
    ingress.clear_agent_kernel()
    kernel = AgentKernel(stack.store, stack.events, stack.verifier, clock=lambda: CLOCK_AT)
    ingress.set_agent_kernel(kernel)
    app = FastAPI()
    app.include_router(ingress.agent_kernel_router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime.test"
    ) as client:
        yield client, stack
    ingress.clear_agent_kernel()


def _hosted_command_payload(*, session_id: str = "s1", command_type: str = "enqueue"):
    payload = _command_payload()
    payload.update(
        {
            "tenant_id": "tenant-1",
            "agent_instance_id": "agent-1",
            "session_id": session_id,
            "command_type": command_type,
        }
    )
    return payload


async def test_hosted_missing_permit_is_401(hosted_kernel_app):
    client, _ = hosted_kernel_app
    response = await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=_hosted_command_payload())
    assert response.status_code == 401, response.text


async def test_hosted_locally_signed_permit_is_403(hosted_kernel_app):
    """进程内 issuer（本地 authority）签的 permit 在 hosted 模式必须被拒。"""

    client, _ = hosted_kernel_app
    trusted = ingress.trusted_context(
        source_kind="system",
        source_ref="local",
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        session_id="s1",
        operations=("enqueue",),
    )
    payload = _hosted_command_payload()
    payload["authorization_ref"] = trusted.permit.permit_id
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": trusted.permit.model_dump(mode="json")},
    )
    assert response.status_code == 403, response.text


async def test_hosted_wrong_issuer_permit_is_403(hosted_kernel_app):
    """非 server JWKS 内的第三方 key 签发（wrong issuer / 未知 key）一律 403。"""

    from tests.kernel.control_harness import PermitAuthority

    client, _ = hosted_kernel_app
    rogue = PermitAuthority(key_id="rogue-key")
    permit = rogue.permit(
        operations=("enqueue",),
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        session_id="s1",
    )
    payload = _hosted_command_payload()
    payload["authorization_ref"] = permit.permit_id
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": permit.model_dump(mode="json")},
    )
    assert response.status_code == 403, response.text


async def test_hosted_unknown_key_permit_is_403(hosted_kernel_app):
    client, stack = hosted_kernel_app
    permit = stack.permit("enqueue")
    forged = permit.model_copy(update={"key_id": "no-such-key"})
    payload = _hosted_command_payload()
    payload["authorization_ref"] = forged.permit_id
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": forged.model_dump(mode="json")},
    )
    assert response.status_code == 403, response.text


async def test_hosted_expired_permit_is_403(hosted_kernel_app):
    from tests.kernel.control_harness import EXPIRED_AT

    client, stack = hosted_kernel_app
    permit = stack.permit("enqueue", expires_at=EXPIRED_AT)
    payload = _hosted_command_payload()
    payload["authorization_ref"] = permit.permit_id
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": permit.model_dump(mode="json")},
    )
    assert response.status_code == 403, response.text


async def test_hosted_session_mismatch_permit_is_403(hosted_kernel_app):
    client, stack = hosted_kernel_app
    permit = stack.permit("enqueue", session_id="s2")
    payload = _hosted_command_payload(session_id="s1")
    payload["authorization_ref"] = permit.permit_id
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": permit.model_dump(mode="json")},
    )
    assert response.status_code == 403, response.text


async def test_hosted_operation_mismatch_permit_is_403(hosted_kernel_app):
    client, stack = hosted_kernel_app
    permit = stack.permit("get_status")
    payload = _hosted_command_payload()  # enqueue command
    payload["authorization_ref"] = permit.permit_id
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": permit.model_dump(mode="json")},
    )
    assert response.status_code == 403, response.text


async def test_hosted_replayed_nonce_is_403(hosted_kernel_app):
    """同 nonce 被另一条 command 复用（重放攻击）必须 403。

    完全相同的 command（同 command_id/idempotency_key）重放是网络重试语义
    （duplicate receipt）；攻击是 permit nonce 换绑新 command。
    """

    client, stack = hosted_kernel_app
    permit = stack.permit("enqueue")
    payload = _hosted_command_payload()
    payload["authorization_ref"] = permit.permit_id
    first = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": permit.model_dump(mode="json")},
    )
    assert first.status_code == 202, first.text
    replay_payload = _hosted_command_payload()
    replay_payload["idempotency_key"] = "idem-replay-attack"
    replay_payload["authorization_ref"] = permit.permit_id
    replay = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": replay_payload, "permit": permit.model_dump(mode="json")},
    )
    assert replay.status_code == 403, replay.text


async def test_hosted_valid_server_permit_is_accepted(hosted_kernel_app):
    """fail-closed 不是全封：server JWKS 内合法 permit 正常 accepted。"""

    client, stack = hosted_kernel_app
    permit = stack.permit("enqueue")
    payload = _hosted_command_payload()
    payload["authorization_ref"] = permit.permit_id
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={"command": payload, "permit": permit.model_dump(mode="json")},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "accepted"


async def test_hosted_status_and_subscription_never_self_sign(hosted_kernel_app):
    """read path 也必须由 Server permit 约束，不能借 status/SSE 绕过 RBAC。"""

    client, _ = hosted_kernel_app
    query = {
        "schema_version": 1,
        "tenant_id": "tenant-1",
        "agent_instance_id": "agent-1",
        "session_id": "s1",
        "authorization_ref": "caller-forged",
    }
    status = await client.post(ingress.KERNEL_INGRESS_STATUS_PATH, json=query)
    assert status.status_code == 401, status.text

    events = await client.get(
        ingress.KERNEL_INGRESS_SESSION_EVENTS_PATH,
        params={"tenant_id": "tenant-1", "agent_instance_id": "agent-1", "session_id": "s1"},
    )
    assert events.status_code == 401, events.text


async def test_hosted_subscription_requires_explicit_resource_identity(hosted_kernel_app):
    """hosted SSE 不得把缺失的 tenant/instance 降级成 local identity。"""

    client, stack = hosted_kernel_app
    permit = stack.permit("subscribe_events")
    response = await client.get(
        ingress.KERNEL_INGRESS_SESSION_EVENTS_PATH,
        params={"session_id": "s1", "timeout": 0.01},
        headers={"X-Agent-Control-Permit": permit.model_dump_json()},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["Code"] == "missing_resource_identity"


@pytest.mark.asyncio
async def test_remote_jwks_parses_server_standard_keys_list(monkeypatch):
    """Server JWKS 是标准 ``{keys: [{kid, x}]}``，不能误当旧 dict 形状。"""

    import httpx

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, str]]]:
            return {
                "keys": [
                    {
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "kid": "server-key-current",
                        "x": "server-public-current",
                    },
                    {
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "kid": "server-key-rotating",
                        "x": "server-public-rotating",
                    },
                ]
            }

    class _AsyncClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, _url: str) -> _Response:
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    source = ingress._remote_jwks_source("https://server.internal/agent-control/jwks")
    assert await source.fetch_verification_keys() == {
        "server-key-current": "server-public-current",
        "server-key-rotating": "server-public-rotating",
    }


@pytest.mark.asyncio
async def test_hosted_env_verifier_never_merges_local_public_key(monkeypatch):
    """hosted verifier 只信 Server JWKS；本地 issuer 即使同进程也必须未知。"""

    from ksadk.kernel.authorization import InvalidPermitError
    from tests.kernel.control_harness import CLOCK_AT, PermitAuthority, command

    server = PermitAuthority(key_id="server-signing-key")
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "hosted")
    monkeypatch.setenv("AGENT_CONTROL_JWKS_URL", "https://server.internal/jwks")
    monkeypatch.setattr(ingress, "_remote_jwks_source", lambda _url: server.jwks())
    verifier = ingress._env_permit_verifier()

    server_permit = server.permit()
    await verifier.verify(
        server_permit,
        command(authorization_ref=server_permit.permit_id),
        "enqueue",
        CLOCK_AT,
    )

    local = ingress.InProcessPermitIssuer()
    local_permit = local.issue(
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        session_id="s1",
        operations=("enqueue",),
        now=CLOCK_AT,
    )
    with pytest.raises(InvalidPermitError, match="unknown_signing_key"):
        await verifier.verify(
            local_permit,
            command(authorization_ref=local_permit.permit_id),
            "enqueue",
            CLOCK_AT,
        )


async def test_hosted_status_accepts_server_permit(hosted_kernel_app):
    client, stack = hosted_kernel_app
    permit = stack.permit("get_status")
    query = {
        "schema_version": 1,
        "tenant_id": "tenant-1",
        "agent_instance_id": "agent-1",
        "session_id": "s1",
        "authorization_ref": permit.permit_id,
    }
    response = await client.post(
        ingress.KERNEL_INGRESS_STATUS_PATH,
        json={"query": query, "permit": permit.model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text


async def test_local_authority_mode_explicitly_allows_self_signed_permit(monkeypatch):
    """AGENT_KERNEL_AUTHORITY_MODE=local 显式声明时才允许本地授权（开发模式）。"""
    import httpx

    from ksadk.kernel.control import AgentKernel
    from tests.kernel.control_harness import kernel_stack

    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "local")
    stack = await kernel_stack()
    ingress.clear_agent_kernel()
    # local 模式 verifier 认本地 issuer 公钥（合并 JWKS 语义保持）。
    from ksadk.kernel.ingress import InProcessPermitIssuer

    issuer = InProcessPermitIssuer()
    verifier = issuer.verifier()
    kernel = AgentKernel(stack.store, stack.events, verifier)
    ingress.set_agent_kernel(kernel)
    app = FastAPI()
    app.include_router(ingress.agent_kernel_router())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runtime.test"
        ) as client:
            trusted = ingress.trusted_context(
                source_kind="system",
                source_ref="local-dev",
                tenant_id="tenant-1",
                agent_instance_id="agent-1",
                session_id="s1",
                operations=("enqueue",),
                issuer=issuer,
            )
            payload = _hosted_command_payload()
            payload["authorization_ref"] = trusted.permit.permit_id
            response = await client.post(
                ingress.KERNEL_INGRESS_SUBMIT_PATH,
                json={"command": payload},
            )
            # 本地自签 permit 直接附带也被接受。
            response = await client.post(
                ingress.KERNEL_INGRESS_SUBMIT_PATH,
                json={
                    "command": payload,
                    "permit": trusted.permit.model_dump(mode="json"),
                },
            )
            assert response.status_code == 202, response.text
    finally:
        ingress.clear_agent_kernel()


async def test_subscribe_session_events_stops_when_client_disconnects(app_with_real_kernel):
    """客户端断开后 SSE 必须及时收口，而不是继续轮询到 5 分钟超时。"""
    import asyncio
    import time

    client, kernel = app_with_real_kernel
    await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=_command_payload())

    started = time.monotonic()

    async def consume_and_disconnect() -> None:
        async with client.stream(
            "GET",
            ingress.KERNEL_INGRESS_SESSION_EVENTS_PATH,
            params={"session_id": "sess-1", "after_seq": 0, "timeout": 2},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("id:"):
                    break
        # 客户端主动断开（取消仍在推送的订阅请求）。
        await response.aclose()

    # 断开后订阅协程必须随请求取消而终止（不再阻塞到 5 分钟 timeout）。
    await asyncio.wait_for(consume_and_disconnect(), timeout=10)

    # store 级别：should_stop 回调触发后订阅立即收口。
    stop = [False]

    async def should_stop() -> bool:
        return stop[0]

    async def consume_store() -> int:
        count = 0
        async for _ in kernel._events.subscribe("sess-1", 0, should_stop=should_stop):
            count += 1
            stop[0] = True
        return count

    count = await asyncio.wait_for(consume_store(), timeout=10)
    assert count >= 1
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"subscription should stop promptly, took {elapsed:.1f}s"


async def test_subscribe_session_events_streams_with_local_permit(app_with_real_kernel):
    """subscribe 的本地 permit 绑定自洽：SSE 正常产出事件而非 fail-closed。"""
    client, _ = app_with_real_kernel
    submit = await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=_command_payload())
    assert submit.status_code == 202, submit.text

    collected: list[str] = []
    async with client.stream(
        "GET",
        ingress.KERNEL_INGRESS_SESSION_EVENTS_PATH,
        params={"session_id": "sess-1", "after_seq": 0, "timeout": 2},
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("id:") or line.startswith("data:"):
                collected.append(line)
                if len(collected) >= 2:
                    break
    assert collected, "SSE 必须产出事件"
    assert any(line.startswith("id: 1") for line in collected), collected
