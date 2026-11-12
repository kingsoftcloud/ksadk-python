import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ksadk.mcp_runtime.oauth.client import BrokerClient
from ksadk.mcp_runtime.oauth.contract import CAPABILITY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["lost_reply", "MCP_CONTROL_UNAVAILABLE", "MCP_CONTROL_TIMEOUT",
                "MCP_CONTROL_RATE_LIMITED", "MCP_CONTROL_LIMITER_UNAVAILABLE"]
)
async def test_retries_completion_without_repeating_provider_exchange(failure, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("ksadk.mcp_runtime.oauth.client.asyncio.sleep", sleep)
    counts = {"execute": 0, "complete": 0, "claims": 0}
    deliveries = []

    async def broker(req):
        if req.url.path.endswith("GetMcpAuthorizationCapabilities"):
            return httpx.Response(200, json=CAPABILITY, headers={"X-MCP-Provider-IO": "runtime/v1"})
        body = json.loads(req.content)
        if body["io_action"] == "claim":
            counts["claims"] += 1
            return httpx.Response(
                200,
                json={
                    "schema": "mcp.runtime-io/v1",
                    "status": "execute" if counts["claims"] == 1 else "ready",
                    "task_id": "run",
                    "kind": "exchange",
                    "operation_id": "operation",
                    "claim": "test-claim",
                    "payload": {},
                },
            )
        counts["complete"] += 1
        deliveries.append(body)
        assert body["result"] == {"access_token": "fixture-only"}
        if counts["complete"] == 1:
            if failure == "lost_reply":
                raise httpx.ReadTimeout("completion committed, reply lost")
            status = {"MCP_CONTROL_UNAVAILABLE": 502, "MCP_CONTROL_TIMEOUT": 504,
                      "MCP_CONTROL_RATE_LIMITED": 429, "MCP_CONTROL_LIMITER_UNAVAILABLE": 503}[failure]
            return httpx.Response(status, json={"ReasonCode": failure},
                                  headers={"Retry-After": "60"} if status == 429 else {})
        return httpx.Response(200, json={"status": "completed"})

    class Executor:
        async def execute(self, operation):
            counts["execute"] += 1
            return {"access_token": "fixture-only"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(broker)) as http:
        client = BrokerClient("https://broker.test", lambda: "fixture-workload", http=http)
        client._runtime_executor = Executor()
        await client.prepare("run", "cloud", subject_digest="a" * 64)
    assert counts == {"execute": 1, "complete": 2, "claims": 2}
    assert deliveries[0] == deliveries[1]
    sleep.assert_awaited_once_with(60 if failure == "MCP_CONTROL_RATE_LIMITED" else 0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refresh", "exchange"])
async def test_reported_token_failure_rechecks_grant_only_for_refresh(kind):
    from ksadk.mcp_runtime.oauth.client import BrokerError
    from ksadk.mcp_runtime.oauth.kernel import MCPAuthorizationExtension
    from ksadk.mcp_runtime.oauth.local.protocol import OAuthError

    operation = dict(
        schema="mcp.runtime-io/v1",
        status="execute",
        task_id="run",
        kind=kind,
        operation_id="op",
        claim="test-claim",
    )
    accepted = False
    lookups = 0

    async def broker(req):
        nonlocal accepted, lookups
        if req.url.path.endswith("/LookupMcpCredential"):
            lookups += 1
            return httpx.Response(
                200, json={"status": "authorization_required" if accepted else "refresh_required"}
            )
        body = json.loads(req.content)
        if body["io_action"] == "claim":
            ready = {"schema": "mcp.runtime-io/v1", "status": "ready"}
            return httpx.Response(200, json=ready if accepted else operation)
        assert body["io_action"] == "complete" and body["error"] == "TOKEN_EXCHANGE_FAILED"
        accepted = True
        return httpx.Response(200, json={"schema": "mcp.runtime-io/v1", "status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(broker)) as http:
        client = BrokerClient("https://broker.test", lambda: "fixture", http=http)
        client._provider_io = True
        executor = AsyncMock(side_effect=OAuthError("TOKEN_EXCHANGE_FAILED"))
        client._runtime_executor = SimpleNamespace(execute=executor)
        if kind == "refresh":
            ext = MCPAuthorizationExtension(SimpleNamespace(_pool=None, _assert_fence=None), client)
            ext._tool_binding = AsyncMock(return_value={})
            result = await ext.lookup_credential("run", "cloud", "tool-call")
            assert result["status"] == "authorization_required" and lookups == 2
        else:
            with pytest.raises(BrokerError, match="TOKEN_EXCHANGE_FAILED"):
                await client.get("wait")
            assert lookups == 0
        executor.assert_awaited_once()
        assert accepted


@pytest.mark.asyncio
async def test_existing_local_broker_does_not_invoke_runtime_io():
    calls = []

    async def broker(req):
        calls.append(req.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            json=CAPABILITY
            if calls[-1] == "GetMcpAuthorizationCapabilities"
            else {"status": "pending"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(broker)) as http:
        client = BrokerClient("https://broker.test", lambda: "fixture-workload", http=http)
        await client.prepare("run", "cloud")
        await client.get("wait")
    assert calls == ["GetMcpAuthorizationCapabilities", "GetMcpAuthorizationWait"]


@pytest.mark.asyncio
async def test_executor_discovers_registers_exchanges_refreshes_and_revokes_over_http():
    from dataclasses import asdict, replace
    from urllib.parse import parse_qs

    from ksadk.mcp_runtime.oauth.local.protocol import MCPConnection
    from ksadk.mcp_runtime.oauth.runtime_io import RuntimeOAuthExecutor

    config = MCPConnection(
        id="cloud",
        resource="https://mcp.test/mcp",
        issuer="https://mcp.test/mcp",
        redirect_uri="https://gateway.test/callback",
        scopes=("mcp:read",),
    )
    metadata = {
        "issuer": "https://iam.test",
        "authorization_endpoint": "https://iam.test/authorize",
        "token_endpoint": "https://iam.test/token",
        "registration_endpoint": "https://iam.test/register",
        "revocation_endpoint": "https://iam.test/revoke",
        "code_challenge_methods_supported": ["S256"],
    }
    calls = []

    async def provider(req):
        calls.append((req.method, str(req.url)))
        if req.url.path == "/mcp":
            return httpx.Response(401)
        if req.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={"resource": config.resource, "authorization_servers": ["https://iam.test"]},
            )
        if req.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=metadata)
        if req.url.path == "/register":
            body = __import__("json").loads(req.content)
            assert body["redirect_uris"] == [config.redirect_uri]
            return httpx.Response(
                201, json={"client_id": "fixture-client", "redirect_uris": body["redirect_uris"]}
            )
        body = parse_qs(req.content.decode())
        if req.url.path == "/token":
            assert body["client_id"] == ["fixture-client"]
            if body["grant_type"] == ["authorization_code"]:
                assert body["code_verifier"] == ["fixture-verifier"]
                assert body["redirect_uri"] == [config.redirect_uri]
            else:
                assert body["grant_type"] == ["refresh_token"]
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-token",
                    "refresh_token": "fixture-refresh",
                    "token_type": "Bearer",
                    "scope": "mcp:read",
                    "expires_in": 300,
                },
            )
        assert req.url.path == "/revoke"
        assert body["token_type_hint"][0] in {"access_token", "refresh_token"}
        return httpx.Response(200)

    executor = RuntimeOAuthExecutor(transport=httpx.MockTransport(provider))

    async def run(kind, payload):
        return await executor.execute(
            {"schema": "mcp.runtime-io/v1", "kind": kind, "payload": payload}
        )

    prepared = await run("prepare", {"config": asdict(config)})
    config = replace(config, issuer="https://iam.test", authorization_server="https://iam.test")
    base = {
        "config": asdict(config),
        "metadata": prepared["metadata"],
        "client": prepared["client"],
    }
    token = await run(
        "exchange",
        {
            **base,
            "data": {
                "grant_type": "authorization_code",
                "code": "fixture-code",
                "code_verifier": "fixture-verifier",
                "redirect_uri": config.redirect_uri,
            },
        },
    )
    await run(
        "refresh",
        {**base, "data": {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}},
    )
    await run("revoke", {**base, "tokens": token})
    assert sum(url.endswith("/token") for _, url in calls) == 2
    assert sum(url.endswith("/revoke") for _, url in calls) == 2


@pytest.mark.asyncio
async def test_executor_does_not_retry_ambiguous_token_exchange():
    from dataclasses import asdict

    from ksadk.mcp_runtime.oauth.local.protocol import MCPConnection, OAuthError
    from ksadk.mcp_runtime.oauth.runtime_io import RuntimeOAuthExecutor

    count = 0

    async def provider(req):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("provider may have consumed the code")

    config = MCPConnection(
        id="cloud",
        resource="https://mcp.test/mcp",
        issuer="https://iam.test",
        redirect_uri="https://gateway.test/callback",
    )
    executor = RuntimeOAuthExecutor(transport=httpx.MockTransport(provider))
    with pytest.raises(OAuthError, match="TOKEN_EXCHANGE_OUTCOME_UNKNOWN"):
        await executor.execute(
            {
                "schema": "mcp.runtime-io/v1",
                "kind": "exchange",
                "payload": {
                    "config": asdict(config),
                    "client": {"client_id": "fixture"},
                    "metadata": {
                        "issuer": config.issuer,
                        "authorization_endpoint": config.issuer + "/authorize",
                        "token_endpoint": config.issuer + "/token",
                        "code_challenge_methods_supported": ["S256"],
                    },
                    "data": {"grant_type": "authorization_code", "code": "fixture"},
                },
            }
        )
    assert count == 1


@pytest.mark.parametrize("status,headers,reason", [
    (429, {"Retry-After": "300"}, "MCP_CONTROL_RATE_LIMITED"),
    (403, {}, "BROKER_REJECTED"),
])
async def test_completion_stops_on_expired_budget_or_permanent_response(status, headers, reason, monkeypatch):
    from ksadk.mcp_runtime.oauth.client import BrokerError

    sends = []
    async def broker(req):
        body = json.loads(req.content)
        if body["io_action"] == "claim":
            return httpx.Response(200, json=dict(schema="mcp.runtime-io/v1", status="execute",
                task_id="run", kind="exchange", operation_id="op", claim="fixture"))
        sends.append(body)
        return httpx.Response(status, text="upstream", headers=headers)

    sleep = AsyncMock()
    monkeypatch.setattr("ksadk.mcp_runtime.oauth.client.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broker)) as http:
        client = BrokerClient("https://broker.test", lambda: "fixture", http=http)
        client._provider_io = True
        client._runtime_executor = AsyncMock()
        client._runtime_executor.execute.return_value = {"access_token": "fixture-only"}
        with pytest.raises(BrokerError, match=reason):
            await client.prepare("run", "cloud")
        client._runtime_executor.execute.assert_awaited_once()
    assert len(sends) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_hosted_iam_registers_one_callback_for_distinct_agents_and_resources():
    from ksadk.mcp_runtime.oauth.runtime_io import RuntimeOAuthExecutor

    iam = "https://iam.api.ksyun.com"
    registrations = []
    resources = {"mcp-one.example", "mcp-two.example"}

    def provider(request):
        host, path = request.url.host, request.url.path
        if host in resources:
            if path == "/mcp":
                return httpx.Response(401)
            return httpx.Response(200, json={"resource": f"https://{host}/mcp",
                    "authorization_servers": [f"{iam}/id/{host.split('.')[0]}"]})
        assert host == "iam.api.ksyun.com"
        if path.startswith("/.well-known/"):
            return httpx.Response(200, json={
                "issuer": iam, "code_challenge_methods_supported": ["S256"],
                "authorization_endpoint": iam + "/oauth/authorize", "token_endpoint": iam + "/oauth/token",
                "registration_endpoint": iam + "/oauth/register/id/" + path.rsplit("/", 1)[-1],
            })
        assert path.startswith("/oauth/register/id/")
        redirects = json.loads(request.content)["redirect_uris"]
        registrations.append(redirects)
        return httpx.Response(201, json={"client_id": "official", "redirect_uris": redirects})

    executor = RuntimeOAuthExecutor(transport=httpx.MockTransport(provider))
    for resource in sorted(resources):
        config_id = resource.split(".")[0]
        config = {"id": config_id, "resource": f"https://{resource}/mcp", "issuer": f"https://{resource}/mcp",
                  "redirect_uri": f"https://gateway.example/agentengine/oauth/mcp/callback/{config_id}"}
        result = await executor.execute({"schema": "mcp.runtime-io/v1", "kind": "prepare", "payload": {"config": config}})
        assert result["client"]["redirect_uris"] == registrations[-1]
    assert registrations[0] == registrations[1]
    assert len(registrations[0][0].rsplit("/", 1)[-1]) == 64
