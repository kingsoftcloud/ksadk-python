"""Bounded HTTP transport for the Teams server and outbound execution nodes."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import TeamsError

Headers = Callable[[str, str, str], Awaitable[dict[str, str]]]


class TeamsHTTPClient:
    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        headers: Headers | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed = urlsplit(base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TeamsError("teams_url_invalid", "团队服务地址不能包含凭据或查询参数", status=422)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise TeamsError("teams_https_required", "远程团队服务必须使用 HTTPS", status=422)
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.headers = headers
        self.node_token: str | None = None
        self.http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(35, connect=10),
            trust_env=False,
        )

    async def request_headers(self, method: str, path: str, body: str = ""):
        result = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.access_token:
            result["Authorization"] = "Bearer " + self.access_token
        if self.node_token:
            result["X-Teams-Node-Token"] = self.node_token
        if self.headers:
            result.update(await self.headers(method, self.base_url + path, body))
        return result

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Teams request requires a relative absolute path")
        content = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else ""
        )
        try:
            response = await self.http.request(
                method,
                self.base_url + path,
                content=content,
                headers=await self.request_headers(method, path, content),
            )
        except httpx.RequestError as error:
            raise TeamsError(
                "teams_transport_unavailable",
                "团队服务暂时无法连接，原操作状态需要核对",
                status=503,
            ) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise TeamsError(
                "teams_response_invalid", "团队服务返回了无法识别的响应", status=502
            ) from error
        if response.is_error or response.is_redirect:
            failure = payload.get("error") or payload.get("detail") or {}
            if not isinstance(failure, dict):
                failure = {}
            raise TeamsError(
                str(failure.get("code") or "teams_request_failed"),
                str(failure.get("message") or "团队请求未完成，请查看连接状态"),
                status=response.status_code,
            )
        return payload

    async def close(self):
        await self.http.aclose()
