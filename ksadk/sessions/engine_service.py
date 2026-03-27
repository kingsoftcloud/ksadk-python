from __future__ import annotations

import os
from typing import Optional

import httpx

from ksadk.sessions.base import BaseSessionService, Session, SessionEvent, SessionState


class EngineSessionService(BaseSessionService):
    def __init__(
        self,
        endpoint: Optional[str] = None,
        token: Optional[str] = None,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 10.0,
    ):
        self.endpoint = (endpoint or os.getenv("AGENTENGINE_SESSION_ENDPOINT", "")).rstrip("/")
        self.token = token or os.getenv("AGENTENGINE_SESSION_TOKEN", "")
        self._transport = transport
        self._timeout = timeout

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        payload = {
            "agent_id": agent_id,
            "user_id": user_id,
            "session_id": session_id,
        }
        data = await self._request("POST", "/conversations/sessions", json=payload)
        return Session.from_dict(data)

    async def get_session(self, session_id: str) -> Optional[Session]:
        response = await self._request(
            "GET",
            f"/conversations/sessions/{session_id}",
            allow_404=True,
        )
        return Session.from_dict(response) if response else None

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> list[Session]:
        params = {"agent_id": agent_id}
        if user_id is not None:
            params["user_id"] = user_id
        data = await self._request("GET", "/conversations/sessions", params=params)
        return [Session.from_dict(item) for item in data]

    async def delete_session(self, session_id: str) -> bool:
        data = await self._request(
            "DELETE",
            f"/conversations/sessions/{session_id}",
            allow_404=True,
        )
        return bool(data and data.get("deleted"))

    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        payload = event.to_dict()
        payload.pop("session_id", None)
        data = await self._request(
            "POST",
            f"/conversations/sessions/{session_id}/events",
            json=payload,
        )
        return SessionEvent.from_dict(data, session_id=session_id)

    async def get_events(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        params = {"limit": limit} if limit is not None else None
        data = await self._request(
            "GET",
            f"/conversations/sessions/{session_id}/events",
            params=params,
        )
        return [SessionEvent.from_dict(item, session_id=session_id) for item in data]

    async def get_state(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str = "session",
    ) -> Optional[SessionState]:
        params = {"agent_id": agent_id}
        if user_id:
            params["user_id"] = user_id
        if session_id:
            params["session_id"] = session_id
        data = await self._request(
            "GET",
            f"/conversations/states/{scope}",
            params=params,
            allow_404=True,
        )
        return SessionState.from_dict(data) if data else None

    async def update_state(
        self,
        *,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict,
    ) -> SessionState:
        payload = {
            "agent_id": agent_id,
            "user_id": user_id,
            "session_id": session_id,
            "state_delta": state_delta,
        }
        data = await self._request("PUT", f"/conversations/states/{scope}", json=payload)
        return SessionState.from_dict(data)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        allow_404: bool = False,
    ):
        async with httpx.AsyncClient(
            base_url=self.endpoint,
            headers=self._headers(),
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            response = await client.request(method, path, params=params, json=json)
        if allow_404 and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers
