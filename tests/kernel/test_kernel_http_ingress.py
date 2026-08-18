# -*- coding: utf-8 -*-
"""Kernel canonical HTTP ingress（/agent-kernel/v1/*）契约测试。

锁定三边一致路径（agentengine-gateway 转发、agentengine-server runtime
client、KsADK runtime）与 receipt 映射、health shape、env bootstrap。
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

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
    assert (
        ingress.KERNEL_INGRESS_SESSION_EVENTS_PATH
        == "/agent-kernel/v1/SubscribeSessionEvents"
    )
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
    response = client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH, json={"command": {"nope": 1}}
    )
    assert response.status_code == 400


def test_kernel_routes_503_when_not_registered(app_without_kernel):
    client = app_without_kernel
    response = client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH, json={"command": _command_payload()}
    )
    assert response.status_code == 503
    assert response.json()["error"]["Code"] == "kernel_not_enabled"


# ---------------------------------------------------------------------------
# health：Operator AgentKernelReady 读这个 shape
# ---------------------------------------------------------------------------


def test_kernel_health_shape(app_without_kernel, monkeypatch):
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
    }
    assert body["contract_digest"] == "c" * 64
    assert body["capability_digest"] == "p" * 64


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
