import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ksadk.mcp_runtime.oauth.client import BrokerClient, BrokerError
from ksadk.mcp_runtime.oauth.contract import CAPABILITY
from ksadk.mcp_runtime.oauth.kernel import MCPAuthorizationExtension
from ksadk.mcp_runtime.oauth.security import b64, canonical, digest


@pytest.mark.asyncio
async def test_platform_jwks_rotation_preserves_purpose_and_body_binding():
    keys = {name: Ed25519PrivateKey.generate() for name in ("old", "new")}
    jwks = SimpleNamespace(
        fetch_verification_keys=AsyncMock(
            return_value={
                name: b64(
                    key.public_key().public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw
                    )
                )
                for name, key in keys.items()
            }
        )
    )
    pool = MagicMock()
    connection = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = connection
    ext = MCPAuthorizationExtension(
        SimpleNamespace(_pool=pool, _assert_fence=True),
        None,
        issuer="existing-control",
        jwks_source=jwks,
    )
    command = {
        "schema": "mcp.authorization/v1",
        "operation": "resolve_external_authorization",
        "wait_id": "wait",
        "revision": 2,
        "instance_id": "instance",
        "run_id": "run",
        "session_id": "session",
    }
    ext.row = AsyncMock(return_value={**command})

    def permit(kid, **overrides):
        now = int(time.time())
        claims = {
            "iss": "existing-control",
            "aud": "mcp-authorization-runtime",
            "sub": "instance",
            "iat": now,
            "exp": now + 120,
            "body_digest": digest(command),
            **overrides,
        }
        body = (
            b64(canonical({"alg": "EdDSA", "typ": "JWT", "kid": kid}))
            + "."
            + b64(canonical(claims))
        )
        return body + "." + b64(keys[kid].sign(body.encode()))

    for kid in keys:
        assert await ext.accept(command, permit(kid)) == {"status": "accepted"}
    assert jwks.fetch_verification_keys.await_count == 2
    with pytest.raises(ValueError):
        await ext.accept(command, permit("new", aud="agentengine-mcp-broker"))
    with pytest.raises(BrokerError, match="INVALID_COMPLETION_PERMIT"):
        await ext.accept({**command, "run_id": "other"}, permit("new"))
    jwks.fetch_verification_keys.return_value.pop("old")
    with pytest.raises(BrokerError, match="INVALID_COMPLETION_PERMIT"):
        await ext.accept(command, permit("old"))


@pytest.mark.asyncio
async def test_workload_token_is_reread_after_atomic_file_rotation(tmp_path):
    path = tmp_path / "workload.jwt"
    path.write_text("test-old")
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=CAPABILITY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BrokerClient.from_token_file("https://platform.example", path, http=http)
        await client.capabilities()
        replacement = tmp_path / "next.jwt"
        replacement.write_text("test-new")
        replacement.replace(path)
        await client.capabilities()
    assert seen == ["Bearer test-old", "Bearer test-new"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,extra", [
    ("LookupMcpCredential", {}),
    ("EnsureMcpAuthorization", {}),
    ("RuntimeMcpOAuthIO", {"io_action": "claim"}),
])
async def test_name_only_binding_retries_with_project_yaml(tmp_path, operation, extra):
    import json

    (tmp_path / "agentengine.yaml").write_text(
        "mcp_servers:\n"
        "  - {name: unrelated, auth_mode: api_key}\n"
        "  - name: cloud\n"
        "    auth_mode: oauth\n"
        "    url: https://provider.example/mcp\n"
        "    oauth: {scopes: [read]}\n"
        "    tool_filter: [Search]\n"
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if "declaration" not in body:
            return httpx.Response(404, json={"ReasonCode": "MCP_CONFIGURATION_NOT_FOUND"})
        return httpx.Response(200, json={"status": "ready"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BrokerClient("https://broker.example", lambda: "fixture", http=http, project_dir=tmp_path)
        args = dict(task_id="original", config_id="cloud", **extra)
        assert await client.call(operation, **args) == {"status": "ready"}
        first, second = requests
        declaration = second.pop("declaration")
        assert first == second == args
        assert declaration["name"] == "cloud"
        assert declaration["tool_filter"] == ["Search"]
        assert declaration["oauth"]["scopes"] == ["read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["published", "explicit", "api_key", "denied", "still_missing"])
async def test_project_fallback_scope_and_retry_limit(tmp_path, mode):
    import json

    oauth = {"name": "cloud", "auth_mode": "oauth", "url": "https://provider.example/mcp",
             "oauth": {"scopes": ["read"]}}
    config = {"mcp_servers": [dict(oauth, auth_mode="api_key") if mode == "api_key" else oauth]}
    (tmp_path / "agentengine.yaml").write_text(json.dumps(config))
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if mode == "published":
            return httpx.Response(200, json={"status": "authorized"})
        return httpx.Response(403 if mode == "denied" else 404, json={
            "ReasonCode": "TASK_BINDING_MISMATCH" if mode == "denied" else "MCP_CONFIGURATION_NOT_FOUND"
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BrokerClient("https://broker.example", lambda: "fixture", http=http, project_dir=tmp_path)
        args = {"config_id": "cloud"}
        if mode == "explicit":
            args["declaration"] = oauth
        if mode == "published":
            assert await client.call("LookupMcpCredential", **args) == {"status": "authorized"}
        else:
            with pytest.raises(BrokerError):
                await client.call("LookupMcpCredential", **args)
        assert seen[0] == args
        assert len(seen) == (2 if mode == "still_missing" else 1)
