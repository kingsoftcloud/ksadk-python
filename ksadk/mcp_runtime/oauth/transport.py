"""Bearer-token MCP egress with DNS pinning and an immutable origin."""

import asyncio
import ipaddress
import socket
import ssl

import httpcore
import httpx


class MCPNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, allow_loopback=False, delegate=None):
        self.allow_loopback = allow_loopback
        self.delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=timeout or 10,
        )
        addresses = list(dict.fromkeys(str(record[4][0]) for record in records))
        if not addresses or any(
            not (
                ipaddress.ip_address(ip).is_global
                or self.allow_loopback
                and ipaddress.ip_address(ip).is_loopback
            )
            for ip in addresses
        ):
            raise httpcore.ConnectError("MCP_NON_PUBLIC_ENDPOINT")
        return await self.delegate.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("MCP_NON_PUBLIC_ENDPOINT")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class MCPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, resource, *, allow_loopback=False):
        self.resource = httpx.URL(resource)
        local = allow_loopback and self.resource.host in {"localhost", "127.0.0.1", "::1"}
        if (
            (self.resource.scheme != "https" and not (local and self.resource.scheme == "http"))
            or self.resource.userinfo
            or self.resource.fragment
        ):
            raise httpx.ConnectError("MCP_UNTRUSTED_ENDPOINT")
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=4,
            max_keepalive_connections=0,
            retries=0,
            network_backend=MCPNetworkBackend(allow_loopback=local),
        )

    async def handle_async_request(self, request):
        if request.url != self.resource:
            raise httpx.ConnectError("MCP_ENDPOINT_CHANGED")
        response = await super().handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise httpx.ConnectError("MCP_REDIRECT_NOT_ALLOWED")
        # MCP owns streaming read limits; never buffer the long-lived SSE body.
        return response


def http_client_factory(credential):
    resource = credential["resource"]
    # Only the authenticated Broker may enable loopback in the local lab.
    allow_loopback = credential.get("allow_loopback") is True

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=False,
            trust_env=False,
            transport=MCPTransport(resource, allow_loopback=allow_loopback),
        )

    return factory
