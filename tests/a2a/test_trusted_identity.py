from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from ksadk.a2a.identity import (
    A2AGatewayIdentityMiddleware,
    A2AIngressIdentity,
    A2ATrustedIdentityResolver,
    CallableGatewayIdentityVerifier,
)
from ksadk.a2a.task_store import A2AOwnerContextBuilder


def _request(*, state: dict | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/a2a/jsonrpc",
            "headers": [
                (b"x-ksc-account-id", b"forged-account"),
                (b"x-auth-agent-id", b"forged-runtime"),
            ],
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1),
            "state": state or {},
        }
    )


def test_production_owner_context_rejects_forged_headers_without_verified_state() -> None:
    builder = A2AOwnerContextBuilder(
        identity_resolver=A2ATrustedIdentityResolver(),
        allow_unverified_identity=False,
    )

    with pytest.raises(PermissionError, match="verified Gateway identity"):
        builder.build(_request())


def test_production_owner_context_uses_only_verified_gateway_identity() -> None:
    identity = A2AIngressIdentity(
        account_id="account-real",
        tenant_id="tenant-real",
        caller_principal_type="user",
        caller_principal_id="caller-real",
        target_agent_id="ar-target",
    )
    builder = A2AOwnerContextBuilder(
        identity_resolver=A2ATrustedIdentityResolver(),
        allow_unverified_identity=False,
    )

    context = builder.build(_request(state={"a2a_identity": identity}))

    assert context.state["account_id"] == "account-real"
    assert context.state["tenant_id"] == "tenant-real"
    assert context.state["caller_principal_id"] == "caller-real"
    assert "runtime_id" not in context.state
    assert context.tenant == "account-real/tenant-real/user/caller-real"


def test_gateway_identity_middleware_rejects_a_forwarded_identity_for_another_agent() -> None:
    app = FastAPI()

    @app.post("/a2a/jsonrpc")
    async def a2a_handler():
        return {"ok": True}

    async def verify(request):  # noqa: ANN001, ANN202
        return A2AIngressIdentity(
            account_id="account-real",
            tenant_id="tenant-real",
            caller_principal_type="user",
            caller_principal_id="caller-real",
            target_agent_id="a2a-agent-other",
        )

    app.add_middleware(
        A2AGatewayIdentityMiddleware,
        verifier=CallableGatewayIdentityVerifier(verify),
        expected_target_agent_id="a2a-agent-current",
    )

    with TestClient(app) as client:
        response = client.post("/a2a/jsonrpc", json={})

    assert response.status_code == 401


def test_gateway_identity_middleware_exposes_only_verified_identity_to_the_handler() -> None:
    app = FastAPI()

    @app.post("/a2a/jsonrpc")
    async def a2a_handler(request: Request):
        identity = request.scope["state"]["a2a_identity"]
        return {"principal": identity.caller_principal_id}

    async def verify(request):  # noqa: ANN001, ANN202
        return A2AIngressIdentity(
            account_id="account-real",
            tenant_id="tenant-real",
            caller_principal_type="user",
            caller_principal_id="caller-real",
            target_agent_id="a2a-agent-current",
        )

    app.add_middleware(
        A2AGatewayIdentityMiddleware,
        verifier=CallableGatewayIdentityVerifier(verify),
        expected_target_agent_id="a2a-agent-current",
    )

    with TestClient(app) as client:
        response = client.post("/a2a/jsonrpc", json={})

    assert response.status_code == 200
    assert response.json() == {"principal": "caller-real"}
