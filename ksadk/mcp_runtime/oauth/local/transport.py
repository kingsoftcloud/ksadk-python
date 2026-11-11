"""Public OAuth egress: DNS checked and pinned at each TCP connection."""

import asyncio
import ipaddress
import socket
import ssl

import anyio
import httpcore
import httpx


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, delegate=None):
        self.delegate = delegate or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            records = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=timeout or 10,
            )
            addresses = list(dict.fromkeys(str(record[4][0]) for record in records))
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise httpcore.ConnectError("OAUTH_NON_PUBLIC_ENDPOINT")
            # Dial this address, not the hostname a second time. httpcore keeps
            # the original hostname for certificate verification and TLS SNI.
            return await self.delegate.connect_tcp(
                addresses[0],
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        except (ValueError, OSError, TimeoutError):
            raise httpcore.ConnectError("OAUTH_DNS_FAILED") from None

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("OAUTH_NON_PUBLIC_ENDPOINT")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class PublicOAuthTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False, retries=0)
        # Same pinned httpx/httpcore direct-pool integration as SDK A2A egress.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=20,
            max_keepalive_connections=0,
            network_backend=PublicNetworkBackend(),
            retries=0,
        )

    async def handle_async_request(self, request):
        if request.url.scheme != "https" or request.url.userinfo:
            raise httpx.ConnectError("OAUTH_UNTRUSTED_ENDPOINT")
        response = await super().handle_async_request(request)
        try:
            if 300 <= response.status_code < 400:
                raise httpx.ConnectError("OAUTH_REDIRECT_NOT_ALLOWED")
            body = bytearray()
            with anyio.fail_after(20):
                async for chunk in response.stream:
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise httpx.ReadError("OAUTH_RESPONSE_TOO_LARGE")
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(body),
                extensions=response.extensions,
            )
        except TimeoutError:
            raise httpx.ReadTimeout("OAUTH_RESPONSE_TIMEOUT") from None
        finally:
            await response.aclose()
