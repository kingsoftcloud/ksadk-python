import asyncio
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from ksadk.mcp_runtime.oauth.transport import MCPNetworkBackend, MCPTransport


@pytest.mark.asyncio
async def test_dns_is_pinned_and_mixed_private_results_are_rejected(monkeypatch):
    delegate = AsyncMock()
    loop = asyncio.get_running_loop()
    resolve = AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    backend = MCPNetworkBackend(delegate=delegate)
    await backend.connect_tcp("example.com", 443)
    assert delegate.connect_tcp.call_args.args == ("8.8.8.8", 443)
    resolve.return_value.append((2, 1, 6, "", ("169.254.169.254", 443)))
    delegate.reset_mock()
    with pytest.raises(httpcore.ConnectError, match="NON_PUBLIC"):
        await backend.connect_tcp("example.com", 443)
    delegate.connect_tcp.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_token_transport_rejects_redirect_and_target_change(monkeypatch):
    handler = AsyncMock(
        return_value=httpx.Response(302, headers={"Location": "https://other.example/mcp"})
    )
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handler)
    async with MCPTransport("https://example.com/mcp") as transport:
        with pytest.raises(httpx.ConnectError, match="ENDPOINT_CHANGED"):
            await transport.handle_async_request(httpx.Request("POST", "https://other.example/mcp"))
        handler.assert_not_awaited()
        with pytest.raises(httpx.ConnectError, match="REDIRECT_NOT_ALLOWED"):
            await transport.handle_async_request(httpx.Request("POST", "https://example.com/mcp"))


@pytest.mark.asyncio
async def test_loopback_is_an_explicit_broker_lab_exception():
    with pytest.raises(httpx.ConnectError, match="UNTRUSTED"):
        MCPTransport("http://127.0.0.1/mcp")
    async with MCPTransport("http://127.0.0.1/mcp", allow_loopback=True):
        pass
    with pytest.raises(httpx.ConnectError, match="UNTRUSTED"):
        MCPTransport("http://private.example/mcp", allow_loopback=True)
