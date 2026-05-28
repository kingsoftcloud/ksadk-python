"""Thin OpenClaw gateway client for Dashboard short-link sessions."""

from __future__ import annotations

import asyncio
import json
import platform
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from ksadk.api import AgentEngineClient
from ksadk.version import VERSION as KSADK_VERSION


DEFAULT_CLIENT_NAME = "openclaw-control-ui"
DEFAULT_CLIENT_MODE = "webchat"
DEFAULT_SCOPES = ("operator.admin",)


class OpenClawGatewayError(RuntimeError):
    """Gateway connection or protocol error."""


class OpenClawGatewayRequestError(OpenClawGatewayError):
    """Structured gateway RPC error."""

    def __init__(self, message: str, *, code: Any = None, details: Any = None):
        self.code = code
        self.details = details
        super().__init__(message)


@dataclass
class DashboardAccessInfo:
    agent_id: str
    agent_name: Optional[str]
    access_url: str
    ws_url: str
    cookie_header: str
    origin: str
    link_id: Optional[str] = None
    expires_at: Optional[str] = None


def derive_gateway_ws_url(access_url: str) -> str:
    """Derive the router websocket URL from a Dashboard short-link URL."""
    parsed = urlsplit((access_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise OpenClawGatewayError("Dashboard access URL is invalid")

    marker = "/s/"
    path = parsed.path or "/"
    marker_idx = path.find(marker)
    base_path = path[:marker_idx] if marker_idx >= 0 else path
    if not base_path:
        base_path = "/"
    if not base_path.endswith("/"):
        base_path = f"{base_path}/"

    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((ws_scheme, parsed.netloc, base_path, "", ""))


class OpenClawGatewayClient:
    """Minimal cookie-session websocket client for a managed OpenClaw gateway."""

    def __init__(
        self,
        *,
        region: str,
        agent_id: str,
        agent_name: Optional[str] = None,
        client_name: str = DEFAULT_CLIENT_NAME,
        client_mode: str = DEFAULT_CLIENT_MODE,
    ):
        self.region = region
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.client_name = client_name
        self.client_mode = client_mode
        self.instance_id = str(uuid.uuid4())
        self.session = requests.Session()
        self._ws: Any = None
        self.connection_info: Optional[DashboardAccessInfo] = None
        self.hello: Optional[dict[str, Any]] = None
        self.challenge_nonce: Optional[str] = None

    async def __aenter__(self) -> "OpenClawGatewayClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        await self.close()
        return False

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self.session.close()

    @property
    def methods(self) -> list[str]:
        features = (self.hello or {}).get("features") or {}
        methods = features.get("methods")
        if isinstance(methods, list):
            return [str(item) for item in methods]
        return []

    def has_method(self, method: str) -> bool:
        return method in set(self.methods)

    async def build_access_info(
        self,
        *,
        path: str = "/",
        expires_seconds: Optional[int] = None,
        link_type: str = "private",
        force_new: bool = False,
    ) -> DashboardAccessInfo:
        async with AgentEngineClient(region=self.region) as client:
            link = await client.create_dashboard_access_link(
                agent_id=self.agent_id,
                name=self.agent_name,
                link_type=link_type,
                path=path,
                expires_seconds=expires_seconds,
                force_new=force_new,
            )

        access_url = str(link.get("access_url") or "").strip()
        if not access_url:
            raise OpenClawGatewayError("CreateDashboardAccessLink returned an empty access URL")

        response = await asyncio.to_thread(
            self.session.get,
            access_url,
            allow_redirects=True,
            timeout=30,
        )
        if response.status_code >= 400:
            raise OpenClawGatewayError(
                f"Dashboard short-link bootstrap failed: HTTP {response.status_code}"
            )

        cookie_header = self._build_cookie_header()
        if not cookie_header:
            raise OpenClawGatewayError("Dashboard short-link bootstrap did not produce a session cookie")

        parsed = urlsplit(access_url)
        self.connection_info = DashboardAccessInfo(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            access_url=access_url,
            ws_url=derive_gateway_ws_url(access_url),
            cookie_header=cookie_header,
            origin=f"{parsed.scheme}://{parsed.netloc}",
            link_id=str(link.get("link_id") or "").strip() or None,
            expires_at=str(link.get("expires_at") or "").strip() or None,
        )
        return self.connection_info

    async def connect(
        self,
        *,
        path: str = "/",
        expires_seconds: Optional[int] = None,
        link_type: str = "private",
    ) -> dict[str, Any]:
        info = self.connection_info or await self.build_access_info(
            path=path,
            expires_seconds=expires_seconds,
            link_type=link_type,
        )

        headers = {
            "Cookie": info.cookie_header,
            "User-Agent": f"ksadk/{KSADK_VERSION}",
            "Origin": info.origin,
        }
        self._ws = await self._connect_ws(info.ws_url, headers)
        self.challenge_nonce = await self._wait_for_connect_challenge()
        self.hello = await self.request(
            "connect",
            {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {
                    "id": self.client_name,
                    "displayName": "ksadk openclaw channel",
                    "version": KSADK_VERSION,
                    "platform": platform.system().lower() or "unknown",
                    "mode": self.client_mode,
                    "instanceId": self.instance_id,
                },
                "caps": [],
                "role": "operator",
                "scopes": list(DEFAULT_SCOPES),
            },
            timeout_ms=10_000,
        )
        return self.hello

    async def channels_status(self, *, probe: bool = False, timeout_ms: Optional[int] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"probe": bool(probe)}
        if timeout_ms is not None:
            params["timeoutMs"] = int(timeout_ms)
        return await self.request("channels.status", params)

    async def config_get(self) -> dict[str, Any]:
        return await self.request("config.get", {})

    async def config_apply(
        self,
        *,
        config: dict[str, Any],
        base_hash: str,
        note: Optional[str] = None,
        session_key: Optional[str] = None,
        restart_delay_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "raw": json.dumps(config, ensure_ascii=False, indent=2),
            "baseHash": base_hash,
        }
        if note:
            params["note"] = note
        if session_key:
            params["sessionKey"] = session_key
        if restart_delay_ms is not None:
            params["restartDelayMs"] = int(restart_delay_ms)
        return await self.request("config.apply", params, timeout_ms=20_000)

    async def web_login_start(
        self,
        *,
        force: bool = False,
        timeout_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"force": bool(force)}
        if timeout_ms is not None:
            params["timeoutMs"] = int(timeout_ms)
        return await self.request("web.login.start", params, timeout_ms=(timeout_ms or 30_000) + 5_000)

    async def web_login_wait(
        self,
        *,
        account_id: Optional[str] = None,
        session_key: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        effective_account_id = account_id or session_key
        if effective_account_id:
            params["accountId"] = effective_account_id
        if timeout_ms is not None:
            params["timeoutMs"] = int(timeout_ms)
        return await self.request("web.login.wait", params, timeout_ms=(timeout_ms or 120_000) + 5_000)

    async def request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise OpenClawGatewayError("Gateway websocket is not connected")

        request_id = uuid.uuid4().hex
        payload = {
            "type": "req",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        await self._ws.send(json.dumps(payload, ensure_ascii=False))
        return await self._wait_for_response(request_id, timeout_ms=timeout_ms)

    async def wait_for_disconnect(self, *, timeout_ms: int = 5_000) -> bool:
        """Wait briefly for the current websocket to close, typically after config-triggered restart."""
        if self._ws is None:
            return True

        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await self._recv_json(timeout=remaining)
            except OpenClawGatewayError as exc:
                return "timed out" not in str(exc).lower()

    def _build_cookie_header(self) -> str:
        cookies = self.session.cookies.get_dict()
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    async def _connect_ws(self, ws_url: str, headers: dict[str, str]):
        try:
            import websockets
        except Exception as exc:  # pragma: no cover - exercised only when dependency missing
            raise OpenClawGatewayError(
                "Missing dependency `websockets`; reinstall ksadk with channel support enabled"
            ) from exc

        connect_kwargs = {
            "open_timeout": 10,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 50 * 1024 * 1024,
        }
        try:
            return await websockets.connect(ws_url, additional_headers=headers, **connect_kwargs)
        except TypeError:
            return await websockets.connect(ws_url, extra_headers=headers, **connect_kwargs)

    async def _wait_for_connect_challenge(self, *, timeout_ms: int = 10_000) -> str:
        deadline = timeout_ms / 1000
        while True:
            frame = await self._recv_json(timeout=deadline)
            if frame.get("type") != "event":
                continue
            if frame.get("event") != "connect.challenge":
                continue
            payload = frame.get("payload") or {}
            nonce = str(payload.get("nonce") or "").strip()
            if nonce:
                return nonce
            raise OpenClawGatewayError("Gateway connect challenge missing nonce")

    async def _wait_for_response(self, request_id: str, *, timeout_ms: int) -> dict[str, Any]:
        deadline = timeout_ms / 1000
        while True:
            frame = await self._recv_json(timeout=deadline)
            if frame.get("type") != "res":
                continue
            if frame.get("id") != request_id:
                continue
            if frame.get("ok"):
                payload = frame.get("payload")
                if isinstance(payload, dict):
                    return payload
                return {"value": payload}
            error = frame.get("error") or {}
            raise OpenClawGatewayRequestError(
                str(error.get("message") or "Gateway request failed"),
                code=error.get("code"),
                details=error.get("details"),
            )

    async def _recv_json(self, *, timeout: float) -> dict[str, Any]:
        if self._ws is None:
            raise OpenClawGatewayError("Gateway websocket is not connected")

        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise OpenClawGatewayError("Gateway websocket timed out") from exc
        except Exception as exc:
            raise OpenClawGatewayError(f"Gateway websocket receive failed: {exc}") from exc

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise OpenClawGatewayError("Gateway websocket returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise OpenClawGatewayError("Gateway websocket returned an unexpected frame")
        return parsed
