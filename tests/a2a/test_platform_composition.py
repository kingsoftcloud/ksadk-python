from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.requests import Request

from ksadk.a2a.platform import JWTGatewayIdentityVerifier, build_managed_a2a_bootstrap_from_env


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _request(token: str, *, path: str = "/a2a/jsonrpc") -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "query_string": b"",
            "headers": [
                (b"x-agentengine-a2a-assertion", token.encode("ascii")),
                (b"x-agentengine-route-target", b"ar-target"),
            ],
        },
        receive=receive,
    )


@pytest.mark.parametrize("binding_case", ["legacy", "complete", "partial", "invalid_operation", "empty_task"])
@pytest.mark.asyncio
async def test_gateway_assertion_requires_request_and_target_binding(binding_case) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    header = _b64(json.dumps({"alg": "RS256"}).encode("utf-8"))
    claims = {
        "iss": "agentengine-a2a-gateway",
        "aud": "a2a-runtime",
        "exp": time.time() + 30,
        "iat": time.time() - 1,
        "jti": "assertion-1",
        "account_id": "account-1",
        "tenant_id": "tenant-1",
        "caller_principal_id": "caller-1",
        "caller_runtime_id": "runtime-caller",
        "target_agent_id": "ar-target",
        "target_runtime_id": "runtime-target",
        "target_a2a_agent_id": "a2a-agent-target",
        "body_sha256": hashlib.sha256(b"{}").hexdigest(),
        "query_sha256": hashlib.sha256(b"").hexdigest(),
        "http_method": "POST",
        "path": "/a2a/jsonrpc",
    }
    if binding_case != "legacy":
        claims.update(platform_task_id="a2a-task-test", target_version_id="version-1", operation="send_message")
    if binding_case == "partial":
        del claims["target_version_id"]
    if binding_case == "invalid_operation":
        claims["operation"] = "enqueue"
    if binding_case == "empty_task":
        claims["platform_task_id"] = "a2a-task-"
    payload = _b64(json.dumps(claims).encode("utf-8"))
    signature = private.sign(
        f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    verifier = JWTGatewayIdentityVerifier(public)
    if binding_case in {"partial", "invalid_operation", "empty_task"}:
        with pytest.raises(PermissionError):
            await verifier.verify(_request(f"{header}.{payload}.{_b64(signature)}"))
        return
    identity = await verifier.verify(_request(f"{header}.{payload}.{_b64(signature)}"))
    assert identity.verified_request_body == b"{}"
    if binding_case == "complete":
        assert identity.task_binding.platform_task_id == "a2a-task-test"
        assert identity.task_binding.target_version_id == "version-1"
        assert identity.task_binding.operation == "send_message"
    else:
        assert identity.task_binding is None
    assert identity.target_runtime_id == "runtime-target"

    with pytest.raises(PermissionError, match="route"):
        await verifier.verify(
            _request(f"{header}.{payload}.{_b64(signature)}", path="/a2a/v1/tasks")
        )


def test_managed_composition_never_defaults_missing_locators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KSADK_A2A_DATABASE_DSN", raising=False)
    with pytest.raises(RuntimeError, match="KSADK_A2A_DATABASE_DSN"):
        build_managed_a2a_bootstrap_from_env()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("postgresql://user:pw@host/db", "postgresql+asyncpg://user:pw@host/db"),
        ("postgres://user:pw@host/db", "postgresql+asyncpg://user:pw@host/db"),
        ("postgresql+asyncpg://user:pw@host/db", "postgresql+asyncpg://user:pw@host/db"),
    ],
)
def test_managed_database_dsn_reuses_kernel_postgres_secret(monkeypatch, source, expected):
    from ksadk.a2a.platform import _managed_database_dsn

    monkeypatch.setenv("KSADK_A2A_DATABASE_DSN", source)
    assert _managed_database_dsn() == expected


def test_managed_database_dsn_rejects_non_postgres_secret(monkeypatch):
    from ksadk.a2a.platform import _managed_database_dsn

    monkeypatch.setenv("KSADK_A2A_DATABASE_DSN", "sqlite:///tmp/a2a.db")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        _managed_database_dsn()


def test_projected_secret_accepts_kubernetes_atomic_writer_layout(tmp_path, monkeypatch):
    from ksadk.a2a.platform import _projected_file

    volume = tmp_path / "secret"
    volume.mkdir()
    version = volume / "..2026_09_14"
    version.mkdir()
    (version / "dsn").write_text("fake-dsn\n")
    (volume / "..data").symlink_to(version.name, target_is_directory=True)
    (volume / "dsn").symlink_to("..data/dsn")
    monkeypatch.setenv("TEST_PROJECTED_FILE", str(volume / "dsn"))
    assert _projected_file("TEST_PROJECTED_FILE") == "fake-dsn"


def test_projected_secret_rejects_link_escaping_volume(tmp_path, monkeypatch):
    from ksadk.a2a.platform import _projected_file

    outside = tmp_path / "outside"
    outside.write_text("must-not-read")
    volume = tmp_path / "secret"
    volume.mkdir()
    (volume / "dsn").symlink_to(outside)
    monkeypatch.setenv("TEST_PROJECTED_FILE", str(volume / "dsn"))
    with pytest.raises(RuntimeError, match="projected file"):
        _projected_file("TEST_PROJECTED_FILE")


def test_managed_http_verify_loads_projected_ca_without_enabling_proxy_env(tmp_path, monkeypatch):
    from ksadk.a2a import platform

    ca_file = tmp_path / "ca-bundle.pem"
    ca_file.write_text("fixture-ca")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    captured = {}

    def create_default_context(*, cadata):
        captured["cadata"] = cadata
        return "fixture-context"

    monkeypatch.setattr(platform.ssl, "create_default_context", create_default_context)
    assert platform._managed_http_verify() == "fixture-context"
    assert captured == {"cadata": "fixture-ca"}


def test_managed_http_verify_prefers_dedicated_control_plane_ca(tmp_path, monkeypatch):
    from ksadk.a2a import platform

    dedicated = tmp_path / "dedicated.pem"
    dedicated.write_text("dedicated-ca", encoding="utf-8")
    legacy = tmp_path / "legacy.pem"
    legacy.write_text("legacy-ca", encoding="utf-8")
    monkeypatch.setenv("KSADK_A2A_CONTROL_PLANE_CA_FILE", str(dedicated))
    monkeypatch.setenv("SSL_CERT_FILE", str(legacy))
    observed = {}

    def fake_context(*, cadata):
        observed["cadata"] = cadata
        return "fixture-context"

    monkeypatch.setattr(platform.ssl, "create_default_context", fake_context)

    assert platform._managed_http_verify() == "fixture-context"
    assert observed["cadata"] == "dedicated-ca"


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
async def test_outbound_only_platform_projection_needs_no_inbound_registration(
    tmp_path, monkeypatch, custom
):
    budgets = {"CONTROL": 25.0, "CONNECT": 30.0, "PREPARE": 30.0,
               "FIRST_EVENT": 300.0, "IDLE": 300.0, "TRANSPORT_READ": 300.0}
    if custom:
        budgets.update(CONTROL=13.0, CONNECT=17.0, PREPARE=19.0,
                       FIRST_EVENT=160.0, IDLE=170.0, TRANSPORT_READ=180.0)
    for phase, value in budgets.items():
        name = "KSADK_A2A_" + phase + "_SECONDS"
        if custom:
            monkeypatch.setenv(name, str(value))
        else:
            monkeypatch.delenv(name, raising=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    monkeypatch.setenv("KSADK_A2A_DATABASE_DSN", "postgresql+asyncpg://fixture@127.0.0.1:1/unused")
    gateway_key_path = tmp_path / "KSADK_A2A_GATEWAY_PUBLIC_KEY_FILE"
    gateway_key_path.write_text(public)
    monkeypatch.setenv("KSADK_A2A_GATEWAY_PUBLIC_KEY_FILE", str(gateway_key_path))
    for name, value in {
        "KSADK_A2A_ACCOUNT_ID": "account",
        "KSADK_A2A_TENANT_ID": "tenant",
        "KSADK_A2A_AGENT_ID": "ar-caller",
        "KSADK_A2A_RUNTIME_ID": "runtime",
        "KSADK_A2A_CONTROL_PLANE_URL": "https://control.example.test",
        "KSADK_A2A_INBOUND_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "AGENT_REGISTRATION_ID",
        "INTERNAL_BASE_URL",
        "AGENT_NAME",
        "AGENT_VERSION",
        "PUBLIC_TOOL_POLICY_FILE",
    ):
        monkeypatch.delenv("KSADK_A2A_" + name, raising=False)
    # Verify the CA context reaches the control-plane client, not the
    # independently trusted Gateway data-plane client.
    import ssl
    from ksadk.a2a import platform

    control_context = ssl.create_default_context()
    monkeypatch.setattr(platform, "_managed_http_verify", lambda: control_context)
    engine_options = {}
    create_engine = platform.create_async_engine

    def capture_engine(dsn, **options):
        engine_options.update(options)
        return create_engine(dsn, **options)

    monkeypatch.setattr(platform, "create_async_engine", capture_engine)
    bootstrap = build_managed_a2a_bootstrap_from_env()
    try:
        assert bootstrap.outbound_enabled and not bootstrap.inbound_enabled
        assert bootstrap.runtime_metadata.a2a_agent_id == ""
        assert bootstrap.invocation_checkpoint_store is not None
        assert engine_options["pool_pre_ping"] is True
        assert engine_options["connect_args"]["server_settings"] == {
            "tcp_keepalives_idle": "30",
            "tcp_keepalives_interval": "10",
            "tcp_keepalives_count": "3",
        }
        control_http = bootstrap._control_plane._client
        assert control_http is not None
        assert control_http is not bootstrap._hosted_http_client
        assert control_http._transport._pool._ssl_context is control_context
        assert bootstrap._hosted_http_client._transport._pool._ssl_context is not control_context
        assert control_http.trust_env is False
        assert control_http.timeout.read == budgets["CONTROL"]
        assert bootstrap._hosted_http_client.timeout.read == budgets["TRANSPORT_READ"]
        assert bootstrap._hosted_http_client.timeout.connect == budgets["CONNECT"]
        service = bootstrap.invocation_service_for_space(
            "a2a-space-00000000000040008000000000000001",
            event_store=object(), policy=object(),
        )
        assert service._timeouts.prepare_seconds == budgets["PREPARE"]
        assert service._timeouts.connect_seconds == budgets["CONNECT"]
        assert service._timeouts.first_event_seconds == budgets["FIRST_EVENT"]
        assert service._timeouts.idle_seconds == budgets["IDLE"]
        observed_limits = []

        async def observe_wait(awaitable, timeout):
            observed_limits.append(timeout)
            return await awaitable

        async def operation():
            return "received"

        service._clock = lambda: 100.0
        with monkeypatch.context() as patch:
            patch.setattr("asyncio.wait_for", observe_wait)
            for segment in ("first_event", "idle"):
                assert await service._timed(operation(), 175.0, segment) == "received"
        assert observed_limits == [75.0, 75.0]
    finally:
        for closer in bootstrap._managed_resource_closers:
            await closer()
    assert control_http.is_closed
    monkeypatch.setenv("KSADK_A2A_INBOUND_ENABLED", "invalid")
    with pytest.raises(RuntimeError, match="INBOUND_ENABLED"):
        build_managed_a2a_bootstrap_from_env()


def test_gateway_ca_is_explicit_and_preserves_public_roots(tmp_path, monkeypatch):
    from ksadk.a2a import platform
    import ssl

    monkeypatch.delenv("KSADK_A2A_GATEWAY_CA_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "unrelated.pem"))
    assert platform._gateway_http_verify() is True
    ca = tmp_path / "gateway.pem"
    ca.write_text("gateway-ca")
    monkeypatch.setenv("KSADK_A2A_GATEWAY_CA_FILE", str(ca))
    calls = []

    class Context:
        def load_verify_locations(self, *, cadata):
            calls.append(cadata)

    context = Context()

    def create_context(*, cafile):
        assert cafile == platform.certifi.where()
        return context

    monkeypatch.setattr(platform.ssl, "create_default_context", create_context)
    assert platform._gateway_http_verify() is context
    assert calls == ["gateway-ca"]

    def reject(*, cadata):
        raise ssl.SSLError("invalid")

    monkeypatch.setattr(context, "load_verify_locations", reject)
    with pytest.raises(RuntimeError, match="Gateway CA file"):
        platform._gateway_http_verify()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,accepted", [
    ({}, True),
    ({"caller_runtime_id": "forged-runtime"}, False),
    ({"caller_agent_id": "forged-agent"}, False),
    ({"permit_jti": "workload-permit"}, False),
    ({"caller_principal_type": "runtime"}, False),
    ({"authn_mode": "workload_permit"}, False),
    ({"schema": "a2a-forward-identity/v3"}, False),
    ({"schema": {}}, False),
    ({"schema": "a2a-forward-identity/v1"}, False),
    ({"key_id": ""}, False),
    *[({"sub_account_id": value}, False) for value in (None, "", " ", 1, "x" * 65)],
    ({"sub": "another-user"}, False),
    ({"platform_task_id": ""}, False),
    *[({field: value}, False) for field in ("exp", "iat")
      for value in (float("nan"), float("inf"), float("-inf"), True, False)],
    ({"iat": time.time() + 2, "exp": time.time() + 1}, False),
])
async def test_product_key_signed_identity_is_not_a_runtime(change, accepted):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    claims = {
        "schema": "a2a-forward-identity/v2", "iss": "agentengine-a2a-gateway",
        "aud": "a2a-runtime", "iat": time.time() - 1, "exp": time.time() + 30,
        "jti": "assertion-test", "sub": "user-1", "account_id": "account-1",
        "tenant_id": "sub-1", "caller_principal_type": "user", "caller_principal_id": "user-1",
        "authn_mode": "platform_api_key", "key_id": "key-1", "sub_account_id": "sub-1",
        "platform_task_id": "a2a-task-test", "target_version_id": "version-1",
        "operation": "send_message", "target_agent_id": "ar-target",
        "target_runtime_id": "runtime-target", "target_a2a_agent_id": "a2a-agent-123456781234423482341234567890ab",
        "body_sha256": hashlib.sha256(b"{}").hexdigest(),
        "query_sha256": hashlib.sha256(b"").hexdigest(),
        "http_method": "POST", "path": "/a2a/jsonrpc", **change,
    }
    header = _b64(json.dumps({"alg": "RS256"}).encode())
    payload = _b64(json.dumps(claims).encode())
    signature = private.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    request = _request(f"{header}.{payload}.{_b64(signature)}")
    verifier = JWTGatewayIdentityVerifier(public)
    if not accepted:
        with pytest.raises(PermissionError):
            await verifier.verify(request)
        return
    identity = await verifier.verify(request)
    identity.validate()
    assert identity.caller_principal_type == "user"
    assert identity.caller_principal_id == "user-1"
    assert identity.sub_account_id == "sub-1"
    assert identity.caller_runtime_id is None
    assert identity.authn_mode == "api_key"
    assert identity.task_binding.platform_task_id == "a2a-task-test"
