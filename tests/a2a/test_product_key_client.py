import httpx
import pytest

from ksadk.a2a.product_key import (
    A2AProductKeyConfigurationError,
    _ProductKeyAuth,
    product_key_http_client,
)


@pytest.mark.asyncio
async def test_config_priority_and_no_ambient_keys(monkeypatch):
    monkeypatch.setenv("KSADK_A2A_API_KEY", "fake-env-key")
    async with product_key_http_client(
        "https://gateway.example", api_key="fake-explicit-key"
    ) as client:
        assert not client.follow_redirects
        assert client._trust_env is False
        flow = client.auth.auth_flow(httpx.Request("POST", "https://gateway.example/a2a/jsonrpc"))
        assert next(flow).headers["authorization"] == "Bearer fake-explicit-key"
        assert "fake-explicit-key" not in repr(client.auth)
    with pytest.raises(A2AProductKeyConfigurationError):
        product_key_http_client("https://gateway.example", api_key="")
    monkeypatch.delenv("KSADK_A2A_API_KEY")
    monkeypatch.setenv("KSADK_MCP_API_KEY", "fake-mcp-key")
    monkeypatch.setenv("KSADK_AI_SEARCH_API_KEY", "fake-search-key")
    with pytest.raises(A2AProductKeyConfigurationError):
        product_key_http_client("https://gateway.example")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/a2a/jsonrpc",
        "http://gateway.example/a2a/jsonrpc",
        "https://gateway.example/control",
        "https://gateway.example:8443/a2a/jsonrpc",
    ],
)
async def test_credential_never_sent_to_other_destination(url):
    requests = []
    async with httpx.AsyncClient(
        auth=_ProductKeyAuth("https://gateway.example", "fake-key"),
        transport=httpx.MockTransport(lambda r: requests.append(r)),
    ) as client:
        with pytest.raises(A2AProductKeyConfigurationError):
            await client.post(url)
    assert not requests


@pytest.mark.asyncio
async def test_redirect_is_rejected_even_when_follow_enabled():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/a2a/jsonrpc"})

    async with product_key_http_client("https://gateway.example", api_key="fake-key") as client:
        await client._transport.aclose()
        client._transport = httpx.MockTransport(handler)
        client.follow_redirects = True
        with pytest.raises(A2AProductKeyConfigurationError) as error:
            await client.post("https://gateway.example/a2a/jsonrpc")
        assert "fake-key" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("key", ["", "bad key", "bad\nkey", "bad,key", "非ASCII", "a" * 4097])
def test_invalid_key_errors_are_redacted(key):
    with pytest.raises(A2AProductKeyConfigurationError) as error:
        _ProductKeyAuth("https://gateway.example", key)
    assert str(error.value) == "an explicit A2A product API Key is required"


@pytest.mark.parametrize(
    "header", ["Authorization", "Cookie", "X-API-Key", "X-AgentEngine-A2A-Permit"]
)
def test_conflicting_auth_never_overrides_product_key(header):
    auth = _ProductKeyAuth("https://gateway.example", "fake-key")
    flow = auth.auth_flow(
        httpx.Request("POST", "https://gateway.example/a2a/jsonrpc", headers={header: "other"})
    )
    with pytest.raises(A2AProductKeyConfigurationError):
        next(flow)


@pytest.mark.asyncio
async def test_official_a2a_client_uses_scoped_product_key(monkeypatch):
    from a2a.client import ClientConfig, create_client
    from a2a.types import GetTaskRequest

    from ksadk.a2a.card import build_agent_card

    monkeypatch.setenv("KSADK_A2A_API_KEY", "fake-env-key")
    requests = []

    def handler(request):
        import json

        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "id": "remote",
                    "contextId": "ctx",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                },
            },
        )

    async with product_key_http_client("https://gateway.example") as http:
        await http._transport.aclose()
        http._transport = httpx.MockTransport(handler)
        card = build_agent_card(name="example", base_url="https://gateway.example", skills=["echo"])
        client = await create_client(agent=card, client_config=ClientConfig(httpx_client=http))
        try:
            task = await client.get_task(GetTaskRequest(id="remote"))
            assert task.id == "remote"
            assert requests[0].headers["authorization"] == "Bearer fake-env-key"
        finally:
            await client.close()
