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

TARGET_AGENT_ID = "ar-target"
TARGET_RUNTIME_ID = "runtime-target"
TARGET_A2A_AGENT_ID = "a2a-agent-00000000000040008000000000000041"


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


def _identity(**overrides: str | None) -> A2AIngressIdentity:
    values: dict[str, str | None] = {
        "account_id": "account-real",
        "tenant_id": "tenant-real",
        "caller_principal_type": "user",
        "caller_principal_id": "caller-real",
        "target_agent_id": TARGET_AGENT_ID,
        "target_runtime_id": TARGET_RUNTIME_ID,
        "target_a2a_agent_id": TARGET_A2A_AGENT_ID,
        "authn_mode": "api_key",
        "caller_runtime_id": None,
    }
    values.update(overrides)
    return A2AIngressIdentity(**values)  # type: ignore[arg-type]


def test_production_owner_context_rejects_forged_headers_without_verified_state() -> None:
    builder = A2AOwnerContextBuilder(
        identity_resolver=A2ATrustedIdentityResolver(),
        allow_unverified_identity=False,
    )

    with pytest.raises(PermissionError, match="verified Gateway identity"):
        builder.build(_request())


def test_production_owner_context_uses_only_verified_gateway_identity() -> None:
    identity = _identity()
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


@pytest.mark.parametrize(
    ("expected", "identity_values"),
    [
        ({"expected_account_id": "account-current"}, {"account_id": "account-other"}),
        ({"expected_tenant_id": "tenant-current"}, {"tenant_id": "tenant-other"}),
        ({"expected_target_agent_id": "ar-current"}, {"target_agent_id": "ar-other"}),
        ({"expected_target_runtime_id": "runtime-current"}, {"target_runtime_id": "runtime-other"}),
        (
            {"expected_target_a2a_agent_id": "a2a-agent-00000000000040008000000000000042"},
            {"target_a2a_agent_id": "a2a-agent-00000000000040008000000000000043"},
        ),
        ({}, {"authn_mode": "gateway_service"}),
    ],
)
def test_gateway_identity_middleware_rejects_any_scope_or_auth_mode_mismatch(
    expected: dict[str, str], identity_values: dict[str, str]
) -> None:
    app = FastAPI()

    @app.post("/a2a/jsonrpc")
    async def a2a_handler():
        return {"ok": True}

    async def verify(request):  # noqa: ANN001, ANN202
        return _identity(**identity_values)

    app.add_middleware(
        A2AGatewayIdentityMiddleware,
        verifier=CallableGatewayIdentityVerifier(verify),
        **expected,
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
        return _identity()

    app.add_middleware(
        A2AGatewayIdentityMiddleware,
        verifier=CallableGatewayIdentityVerifier(verify),
        expected_account_id="account-real",
        expected_tenant_id="tenant-real",
        expected_target_agent_id=TARGET_AGENT_ID,
        expected_target_runtime_id=TARGET_RUNTIME_ID,
        expected_target_a2a_agent_id=TARGET_A2A_AGENT_ID,
    )

    with TestClient(app) as client:
        response = client.post("/a2a/jsonrpc", json={})

    assert response.status_code == 200
    assert response.json() == {"principal": "caller-real"}


@pytest.mark.parametrize(
    "identity_values",
    [
        {"caller_principal_type": "anonymous", "caller_principal_id": "anonymous"},
        {"caller_principal_type": "runtime", "authn_mode": "workload_permit"},
        {"caller_runtime_id": "runtime-for-user"},
    ],
)
def test_gateway_identity_middleware_rejects_invalid_caller_identity(
    identity_values: dict[str, str],
) -> None:
    app = FastAPI()

    @app.post("/a2a/jsonrpc")
    async def a2a_handler():
        return {"ok": True}

    async def verify(request):  # noqa: ANN001, ANN202
        return _identity(**identity_values)

    app.add_middleware(
        A2AGatewayIdentityMiddleware,
        verifier=CallableGatewayIdentityVerifier(verify),
        expected_account_id="account-real",
        expected_tenant_id="tenant-real",
        expected_target_agent_id=TARGET_AGENT_ID,
        expected_target_runtime_id=TARGET_RUNTIME_ID,
        expected_target_a2a_agent_id=TARGET_A2A_AGENT_ID,
    )

    with TestClient(app) as client:
        response = client.post("/a2a/jsonrpc", json={})

    assert response.status_code == 401


def test_gateway_identity_middleware_fails_closed_on_verifier_errors() -> None:
    app = FastAPI()

    @app.post("/a2a/jsonrpc")
    async def a2a_handler():
        return {"ok": True}

    async def verify(request):  # noqa: ANN001, ANN202
        raise RuntimeError("token verifier unavailable")

    app.add_middleware(
        A2AGatewayIdentityMiddleware,
        verifier=CallableGatewayIdentityVerifier(verify),
    )

    with TestClient(app) as client:
        response = client.post("/a2a/jsonrpc", json={})

    assert response.status_code == 401


def test_account_scoped_empty_tenant_requires_an_exact_empty_target() -> None:
    identity = _identity(tenant_id="")
    resolver = A2ATrustedIdentityResolver(
        expected_account_id="account-real",
        expected_tenant_id="",
        expected_target_agent_id=TARGET_AGENT_ID,
        expected_target_runtime_id=TARGET_RUNTIME_ID,
        expected_target_a2a_agent_id=TARGET_A2A_AGENT_ID,
    )

    assert resolver.resolve(_request(state={"a2a_identity": identity})) is identity
