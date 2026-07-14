from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session

logger = logging.getLogger(__name__)


class ResilientADKSessionService(BaseSessionService):
    """Run ADK sessions locally while mirroring them to durable storage."""

    def __init__(self, primary: BaseSessionService) -> None:
        self.primary = primary
        self.live = InMemorySessionService()
        self._primary_sessions: dict[tuple[str, str, str], Session] = {}
        self._primary_enabled = True

    @property
    def degraded(self) -> bool:
        return not self._primary_enabled

    def _degrade(self, exc: Exception) -> None:
        if not self._primary_enabled:
            return
        self._primary_enabled = False
        logger.error(
            "ADK session persistence degraded; using in-memory live session: %s",
            exc,
            extra={
                "session_backend_state": "degraded",
                "session_backend": type(self.primary).__name__,
            },
        )

    @staticmethod
    def _key(app_name: str, user_id: str, session_id: str) -> tuple[str, str, str]:
        return app_name, user_id, session_id

    async def _hydrate(self, durable: Session) -> Session:
        existing = await self.live.get_session(
            app_name=durable.app_name,
            user_id=durable.user_id,
            session_id=durable.id,
        )
        if existing is None:
            existing = await self.live.create_session(
                app_name=durable.app_name,
                user_id=durable.user_id,
                state=durable.state,
                session_id=durable.id,
            )
            for event in durable.events:
                await self.live.append_event(existing, event)
        self._primary_sessions[self._key(durable.app_name, durable.user_id, durable.id)] = durable
        hydrated = await self.live.get_session(
            app_name=durable.app_name,
            user_id=durable.user_id,
            session_id=durable.id,
        )
        if hydrated is None:
            raise RuntimeError(f"Failed to hydrate ADK live session {durable.id}")
        return hydrated

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        if session_id:
            existing = await self.live.get_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
            if existing is not None:
                return existing
            if self._primary_enabled:
                try:
                    durable = await self.primary.get_session(
                        app_name=app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    if durable is not None:
                        return await self._hydrate(durable)
                except Exception as exc:
                    self._degrade(exc)

        live = await self.live.create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )
        if self._primary_enabled:
            try:
                durable = await self.primary.create_session(
                    app_name=app_name,
                    user_id=user_id,
                    state=state,
                    session_id=live.id,
                )
                self._primary_sessions[self._key(app_name, user_id, live.id)] = durable
            except Exception as exc:
                self._degrade(exc)
        return live

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        live = await self.live.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            config=config,
        )
        if live is not None:
            return live
        if not self._primary_enabled:
            return None
        try:
            durable = await self.primary.get_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                config=config,
            )
            return await self._hydrate(durable) if durable is not None else None
        except Exception as exc:
            self._degrade(exc)
            return None

    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: Optional[str] = None,
    ) -> ListSessionsResponse:
        if self._primary_enabled:
            try:
                durable = await self.primary.list_sessions(app_name=app_name, user_id=user_id)
                for session in durable.sessions:
                    await self._hydrate(session)
            except Exception as exc:
                self._degrade(exc)
        return await self.live.list_sessions(app_name=app_name, user_id=user_id)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        await self.live.delete_session(app_name=app_name, user_id=user_id, session_id=session_id)
        self._primary_sessions.pop(self._key(app_name, user_id, session_id), None)
        if self._primary_enabled:
            try:
                await self.primary.delete_session(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                )
            except Exception as exc:
                self._degrade(exc)

    async def append_event(self, session: Session, event: Event) -> Event:
        stored = await self.live.append_event(session, event)
        if not self._primary_enabled:
            return stored
        key = self._key(session.app_name, session.user_id, session.id)
        durable_session = self._primary_sessions.get(key)
        try:
            if durable_session is None:
                durable_session = await self.primary.get_session(
                    app_name=session.app_name,
                    user_id=session.user_id,
                    session_id=session.id,
                )
                if durable_session is None:
                    durable_session = await self.primary.create_session(
                        app_name=session.app_name,
                        user_id=session.user_id,
                        state=session.state,
                        session_id=session.id,
                    )
                self._primary_sessions[key] = durable_session
            await self.primary.append_event(durable_session, event)
        except Exception as exc:
            self._degrade(exc)
        return stored

    async def flush(self) -> None:
        await self.live.flush()
        if self._primary_enabled:
            try:
                await self.primary.flush()
            except Exception as exc:
                self._degrade(exc)

    async def close(self) -> None:
        close = getattr(self.primary, "close", None)
        if close is not None:
            await close()
