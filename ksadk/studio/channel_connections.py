"""Studio-owned outbound Channel connections; no Channel server is embedded."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ksadk.connector import InvokeRequest, create_channel_connector
from ksadk.studio.errors import StudioError

CHANNEL_ACTIONS = frozenset(
    {
        "ListChannels",
        "CreateChannel",
        "UpdateChannel",
        "DeleteChannel",
        "GetChannel",
        "GetConnectQr",
        "ListPairingRequests",
        "ApprovePairing",
        "RejectPairing",
        "ListBindings",
        "UpdateBinding",
        "DeleteBinding",
        "ListMessages",
        "CountMessages",
        "Takeover",
        "ReleaseTakeover",
        "GetActiveTakeover",
        "ListTakeovers",
    }
)


class ChannelConnectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    serverUrl: str = ""
    accountId: str = ""
    workspaceId: str = ""
    apiToken: SecretStr | None = None

    @field_validator("serverUrl")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlsplit(value)
        try:
            loopback = (
                parsed.hostname == "localhost"
                or ipaddress.ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(ord(c) < 33 for c in value)
            or not (parsed.scheme == "https" or parsed.scheme == "http" and loopback)
        ):
            raise ValueError("渠道服务须使用 HTTPS；本机调试可使用 loopback HTTP，地址不可包含凭证")
        # Validate the port now rather than while establishing a connection.
        _ = parsed.port
        return value

    @field_validator("accountId", "workspaceId")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError("渠道身份格式无效")
        return value


class ChannelAgentConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    token: SecretStr | None = Field(default=None, max_length=16384)


class StudioChannelConnections:
    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self._connections: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._connection_keys: dict[str, tuple[str, ...]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_users: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._startup: asyncio.Task | None = None
        self._closed = False
        self._error: str | None = None

    def _settings(self) -> ChannelConnectionSettings:
        config = self.studio.configuration
        stored = config.settings().get("channelConnection", {})
        defaults = {
            "serverUrl": config.resolve("AGENTENGINE_CHANNEL_URL")[0] or "",
            "accountId": config.resolve("KSYUN_ACCOUNT_ID")[0] or "",
            "workspaceId": config.resolve("KSADK_CHANNEL_WORKSPACE_ID")[0]
            or (
                "studio-"
                + hashlib.sha256(str(self.studio.workspace.root).encode()).hexdigest()[:24]
            ),
        }
        return ChannelConnectionSettings.model_validate({**defaults, **stored})

    def _agents(self) -> dict[str, dict[str, Any]]:
        return self.studio.configuration.settings().get("channelAgents", {})

    @staticmethod
    def _token_name(agent_id: str) -> str:
        return "KSADK_CHANNEL_AGENT_" + hashlib.sha256(agent_id.encode()).hexdigest().upper()

    def status(self) -> dict[str, Any]:
        settings = self._settings()
        agents = []
        for agent_id, value in self._agents().items():
            connection = self._connections.get(agent_id)
            state = (
                asdict(connection.status)
                if connection
                else {
                    "state": "disabled" if not value.get("enabled") else "disconnected",
                    "connected": False,
                    "active_tasks": 0,
                    "last_error": self._error,
                }
            )
            agents.append(
                {
                    "agentId": agent_id,
                    "enabled": bool(value.get("enabled")),
                    "hasToken": bool(
                        self.studio.configuration.resolve(self._token_name(agent_id))[0]
                    ),
                    **state,
                }
            )
        return {
            **settings.model_dump(exclude={"apiToken"}),
            "configured": bool(settings.serverUrl and settings.accountId),
            "hasApiToken": bool(self.studio.configuration.resolve("KSADK_CHANNEL_API_TOKEN")[0]),
            "agents": agents,
            "error": self._error,
        }

    def start_background(self) -> None:
        if self._closed or self._startup is not None:
            return
        self._startup = asyncio.create_task(self._restore())

    async def _restore(self) -> None:
        try:
            async with self._lock:
                await self._reconnect()
        except Exception:
            # Optional connection failures must never delay Studio's listener.
            self._error = "channel_configuration_invalid"

    async def configure(self, payload: ChannelConnectionSettings) -> dict[str, Any]:
        async with self._lock:
            if self._closed:
                raise StudioError("CHANNEL_CLOSED", "渠道连接已关闭", status_code=409)
            settings = payload.model_dump(exclude={"apiToken"})
            settings["workspaceId"] = payload.workspaceId or self._settings().workspaceId
            if payload.apiToken is not None:
                self.studio.configuration.put_secret(
                    "KSADK_CHANNEL_API_TOKEN", payload.apiToken.get_secret_value() or None
                )
            self.studio.configuration.update_settings({"channelConnection": settings})
            await self._reconnect()
        return self.status()

    async def configure_agent(
        self, agent_id: str, payload: ChannelAgentConnectionInput
    ) -> dict[str, Any]:
        # Only existing, explicitly selected local agents can receive remote calls.
        self.studio.drafts.get(agent_id)
        async with self._lock:
            if self._closed:
                raise StudioError("CHANNEL_CLOSED", "渠道连接已关闭", status_code=409)
            settings = self._settings()
            token = (
                payload.token.get_secret_value()
                if payload.token is not None
                else self.studio.configuration.resolve(self._token_name(agent_id))[0]
            )
            if payload.enabled and not (settings.serverUrl and settings.accountId and token):
                raise StudioError(
                    "CHANNEL_CONFIGURATION_REQUIRED",
                    "请先配置渠道服务和该 Agent 的连接凭证",
                    status_code=422,
                )
            if payload.token is not None:
                self.studio.configuration.put_secret(self._token_name(agent_id), token or None)
            agents = self._agents()
            agents[agent_id] = {"enabled": payload.enabled}
            self.studio.configuration.update_settings({"channelAgents": agents})
            await self._reconnect()
        return self.status()

    async def _stop_connection(self, agent_id: str) -> None:
        connection = self._connections.pop(agent_id, None)
        task = self._tasks.pop(agent_id, None)
        self._connection_keys.pop(agent_id, None)
        if connection is not None:
            await connection.stop()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _stop_connections(self) -> None:
        for agent_id in list(self._connections):
            await self._stop_connection(agent_id)

    async def _reconnect(self) -> None:
        self._error = None
        settings = self._settings()
        if self._closed or not settings.serverUrl or not settings.accountId:
            await self._stop_connections()
            return
        ws_url = (
            ("wss" if settings.serverUrl.startswith("https:") else "ws")
            + settings.serverUrl[settings.serverUrl.index(":") :]
            + "/agentengine/connector/v1"
        )
        desired = {}
        for agent_id, entry in self._agents().items():
            if not entry.get("enabled"):
                continue
            token = self.studio.configuration.resolve(self._token_name(agent_id))[0]
            if not token:
                self._error = "channel_token_missing"
                continue
            desired[agent_id] = (ws_url, settings.accountId, settings.workspaceId, token)
        for agent_id in list(self._connections):
            if (
                self._connection_keys.get(agent_id) != desired.get(agent_id)
                or self._tasks[agent_id].done()
            ):
                await self._stop_connection(agent_id)
        for agent_id, key in desired.items():
            if agent_id in self._connections:
                continue

            async def invoke(request: InvokeRequest, target: str = agent_id) -> str:
                return await self._invoke(target, settings, request)

            connection = create_channel_connector(
                url=ws_url,
                token=key[3],
                agent_id=agent_id,
                tenant_id=settings.accountId,
                workspace_id=settings.workspaceId,
                invoke=invoke,
            )
            self._connections[agent_id] = connection
            self._connection_keys[agent_id] = key
            self._tasks[agent_id] = asyncio.create_task(connection.start())

    async def _invoke(
        self, agent_id: str, settings: ChannelConnectionSettings, request: InvokeRequest
    ) -> str:
        if not request.session_id:
            raise ValueError("channel_session_required")
        # Stable IM sessions are isolated from manually created Studio sessions.
        identity = "\0".join(
            (settings.accountId, settings.workspaceId, agent_id, request.session_id)
        )
        session_id = "channel_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        self._session_users[session_id] = self._session_users.get(session_id, 0) + 1
        try:
            async with lock:
                build = await self.studio.ensure_current_build(agent_id)
                record = await self.studio.run_build(build.id, request.message, session_id)
                if str(record.status) not in {"completed", "RunStatus.COMPLETED"}:
                    raise RuntimeError("channel_agent_execution_failed")
                return str(record.output or "")
        finally:
            remaining = self._session_users.get(session_id, 1) - 1
            if remaining:
                self._session_users[session_id] = remaining
            else:
                self._session_users.pop(session_id, None)
                self._session_locks.pop(session_id, None)

    async def proxy(self, action: str, payload: dict[str, Any]) -> tuple[int, Any]:
        settings = self._settings()
        if not settings.serverUrl or not settings.accountId:
            raise StudioError(
                "CHANNEL_CONFIGURATION_REQUIRED", "请先配置渠道服务连接", status_code=503
            )
        headers = {"X-Ksc-Account-Id": settings.accountId}
        token = self.studio.configuration.resolve("KSADK_CHANNEL_API_TOKEN")[0]
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=15, follow_redirects=False
            ) as client:
                response = await client.post(
                    f"{settings.serverUrl}/agentengine/api/v1/{action}",
                    json=payload,
                    headers=headers,
                )
                return response.status_code, response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise StudioError(
                "CHANNEL_BACKEND_UNAVAILABLE", "渠道服务暂时无法连接", status_code=502
            ) from error

    async def close(self) -> None:
        if self._startup is not None and not self._startup.done():
            self._startup.cancel()
            await asyncio.gather(self._startup, return_exceptions=True)
        async with self._lock:
            self._closed = True
            await self._stop_connections()
            self._session_locks.clear()
            self._session_users.clear()
