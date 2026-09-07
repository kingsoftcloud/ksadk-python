"""Same-origin transport for the official Core client under the Studio shell.

This forwards bytes to the one supervised Core. It implements no DSH services,
plugin protocol, settings store, or module loader.
"""
from __future__ import annotations

import asyncio
import hmac
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, ProxyHandler, build_opener
from urllib.parse import urlsplit
from urllib.error import URLError

import httpx
from fastapi import FastAPI, Request, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from ksadk.studio.errors import StudioError
from ksadk.plugins.host import PluginHostError

_HOP_HEADERS = {"host", "connection", "transfer-encoding", "content-length"}


def register_dsh_application(app: FastAPI, studio, *, session_secret: str, security_enabled: bool):
    def authorized(cookies):
        token = cookies.get("agentkit_studio_session", "")
        return not security_enabled or hmac.compare_digest(token, session_secret)

    @app.api_route("/studio-core/", methods=["GET"])
    async def application(request: Request):
        if not authorized(request.cookies):
            raise StudioError("LOCAL_SESSION_REQUIRED", "请从 Studio 启动链接进入", status_code=401)
        try:
            lease = await studio.dsh_capabilities.connector_lease()
        except PluginHostError:
            raise StudioError("DSH_CORE_UNAVAILABLE", "插件服务暂时不可用，请返回 Studio 重试", status_code=503) from None
        def bootstrap():
            # urllib does not log the process-token URL at INFO like httpx.
            jar = CookieJar()
            opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))
            with opener.open(lease.browser_url(), timeout=30) as response:
                return response.read(), tuple(jar)

        try:
            body, cookies = await asyncio.to_thread(bootstrap)
        except (URLError, OSError):
            # The upstream bootstrap URL contains a process token. Do not
            # allow urllib's URL-bearing exception into API responses/logs.
            raise StudioError("DSH_CORE_UNAVAILABLE", "插件服务连接失败，请返回 Studio 重试", status_code=503) from None
        body = body.replace(b'<base href="/">', b'<base href="/studio-core/">')
        result = Response(body, media_type="text/html", headers={"Cache-Control": "no-store"})
        for cookie in cookies:
            result.set_cookie(cookie.name, cookie.value, path=cookie.path, httponly=True, samesite="strict")
        return result

    # Registered last: the Studio API/static routes retain ownership of their paths.
    @app.api_route("/{core_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def core_http(core_path: str, request: Request):
        if not authorized(request.cookies):
            raise StudioError("LOCAL_SESSION_REQUIRED", "缺少有效的 Studio 本地会话", status_code=401)
        # Retired Studio chat URLs must not start Core or depend on its availability.
        if core_path.rstrip("/") == "chat" or core_path.startswith(("api/v1/", "v1/")):
            raise StudioError("NOT_FOUND", "接口不存在", status_code=404)
        # Browser mutations must originate in this document; never treat a
        # cross-port loopback origin as trusted just because it is local.
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise StudioError("LOCAL_ORIGIN_FORBIDDEN", "插件请求来源无效", status_code=403)
        lease = await studio.dsh_capabilities.application_lease()
        origin = lease.endpoint.rsplit("/", 1)[0]
        path = request.url.path.removeprefix("/studio-core")
        if request.url.query:
            path += "?" + request.url.query
        client = httpx.AsyncClient(trust_env=False, timeout=None)
        headers = {key: value for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS}
        # Core checks origin against its own listening socket.
        if "origin" in headers:
            headers["origin"] = origin
        try:
            upstream = await client.send(client.build_request(request.method, origin + path, headers=headers, content=request.stream()), stream=True)
        except BaseException:
            await client.aclose()
            raise

        async def close():
            await upstream.aclose()
            await client.aclose()

        result = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(close))
        result.raw_headers = [(key, value) for key, value in upstream.headers.raw if key.decode().lower() not in _HOP_HEADERS]
        return result

    @app.websocket("/{core_path:path}")
    async def core_socket(websocket: WebSocket, core_path: str):
        # The SDK supports websockets 12 through 15; the legacy client is
        # available throughout this range (the asyncio client starts at 13).
        from websockets.legacy.client import connect

        origin = websocket.headers.get("origin")
        expected = f"http://{websocket.headers.get('host')}"
        if not authorized(websocket.cookies) or origin != expected:
            await websocket.close(code=1008)
            return
        lease = await studio.dsh_capabilities.application_lease()
        endpoint = urlsplit(lease.endpoint)
        path = websocket.url.path.removeprefix("/studio-core") + ("?" + websocket.url.query if websocket.url.query else "")
        protocols = websocket.scope.get("subprotocols", [])
        async with connect(
            f"ws://127.0.0.1:{endpoint.port}{path}",
            origin=f"http://127.0.0.1:{endpoint.port}",
            extra_headers={"Cookie": websocket.headers.get("cookie", "")},
            subprotocols=protocols or None, max_size=16 * 1024 * 1024,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def to_core():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(message.get("text") if message.get("text") is not None else message["bytes"])

            async def to_browser():
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            tasks = [asyncio.create_task(to_core()), asyncio.create_task(to_browser())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
