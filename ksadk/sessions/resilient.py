from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast

from ksadk.sessions.base import BaseSessionService, Session, SessionEvent, SessionState
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.resilience import is_session_backend_failure

logger = logging.getLogger(__name__)


class ResilientSessionService(BaseSessionService):
    """Keep live agent sessions available when durable persistence is unavailable.

    The in-memory service is authoritative for the lifetime of this process. The
    configured durable service is used as a read-through source and a best-effort
    write-through sink. After its first failure it stays disabled until a
    background probe confirms the durable backend is reachable again, at which
    point it is re-enabled and an INFO log is emitted.
    """

    _probe_interval_seconds: float = 30.0

    def __init__(
        self,
        primary: BaseSessionService,
        fallback: InMemorySessionService | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback or InMemorySessionService()
        self._primary_enabled = True
        self._hydrate_lock = asyncio.Lock()
        self._primary_session_lock = asyncio.Lock()
        self._primary_session_ids: set[str] = set()
        self._probe_task: asyncio.Task[None] | None = None

    @property
    def degraded(self) -> bool:
        return not self._primary_enabled

    async def _call_primary(self, method_name: str, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
        if not self._primary_enabled:
            return False, None
        try:
            method = getattr(self.primary, method_name)
            return True, await method(*args, **kwargs)
        except Exception as exc:
            if not is_session_backend_failure(exc):
                raise
            self._disable_primary(exc)
            return False, None

    def _disable_primary(self, exc: Exception) -> None:
        if not self._primary_enabled:
            return
        self._primary_enabled = False
        logger.error(
            "KSADK session persistence degraded; using in-memory live session: %s",
            exc,
            extra={
                "session_backend_state": "degraded",
                "session_backend": type(self.primary).__name__,
            },
        )
        self._start_probe()

    def _start_probe(self) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            return
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def _probe_loop(self) -> None:
        while not self._primary_enabled:
            await asyncio.sleep(self._probe_interval_seconds)
            if self._primary_enabled:
                break
            try:
                await self.primary.get_session("__ksadk_probe__")
            except Exception:
                continue
            self._primary_enabled = True
            logger.info(
                "KSADK session persistence recovered; durable backend re-enabled",
                extra={
                    "session_backend_state": "recovered",
                    "session_backend": type(self.primary).__name__,
                },
            )

    async def _hydrate(self, session: Session) -> Session:
        async with self._hydrate_lock:
            self._primary_session_ids.add(session.id)
            existing = await self.fallback.get_session(session.id)
            if existing is None:
                await self.fallback.create_session(
                    session.agent_id,
                    session.user_id,
                    session_id=session.id,
                )
            existing_events = await self.fallback.get_events(session.id)
            existing_ids = {event.id for event in existing_events}
            for event in sorted(session.events, key=lambda item: item.seq_id):
                if event.id not in existing_ids:
                    await self.fallback.append_event(session.id, event)
            await self.fallback.update_session_metadata(
                session.id,
                title=session.title,
                title_source=session.title_source,
                summary=session.summary,
                first_prompt=session.first_prompt,
                last_prompt=session.last_prompt,
            )
            current = await self.fallback.get_session(session.id)
            if current is not None and session.state != current.state:
                await self.fallback.update_state(
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    scope="session",
                    state_delta=session.state,
                )
            hydrated = await self.fallback.get_session(session.id)
            if hydrated is None:
                raise RuntimeError(f"Failed to hydrate live session {session.id}")
            return hydrated

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        if session_id:
            ok, durable = await self._call_primary("get_session", session_id)
            if ok and durable is not None:
                return await self._hydrate(durable)
            existing = await self.fallback.get_session(session_id)
            if existing is not None:
                return existing

        live = await self.fallback.create_session(agent_id, user_id, session_id=session_id)
        ok, durable = await self._call_primary(
            "create_session",
            agent_id,
            user_id,
            session_id=live.id,
        )
        if ok and durable is not None:
            self._primary_session_ids.add(durable.id)
            return await self._hydrate(durable)
        return live

    async def get_session(self, session_id: str) -> Optional[Session]:
        live = await self.fallback.get_session(session_id)
        ok, durable = await self._call_primary("get_session", session_id)
        if ok and durable is not None:
            return await self._hydrate(durable)
        return live

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        ok, durable_sessions = await self._call_primary(
            "list_sessions",
            agent_id,
            user_id,
            offset,
            limit,
        )
        if ok:
            for session in durable_sessions or []:
                await self._hydrate(session)
        return cast(
            list[Session],
            await self.fallback.list_sessions(agent_id, user_id, offset, limit),
        )

    async def count_sessions(self, agent_id: str, user_id: Optional[str] = None) -> int:
        sessions = await self.list_sessions(agent_id, user_id)
        return len(sessions)

    async def delete_session(self, session_id: str) -> bool:
        deleted = await self.fallback.delete_session(session_id)
        ok, durable_deleted = await self._call_primary("delete_session", session_id)
        if ok:
            self._primary_session_ids.discard(session_id)
        return deleted or bool(durable_deleted) if ok else deleted

    async def update_session_metadata(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        title_source: Optional[str] = None,
        summary: Optional[str] = None,
        first_prompt: Optional[str] = None,
        last_prompt: Optional[str] = None,
    ) -> Session:
        if await self.fallback.get_session(session_id) is None:
            await self.get_session(session_id)
        live = await self.fallback.update_session_metadata(
            session_id,
            title=title,
            title_source=title_source,
            summary=summary,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
        )
        await self._ensure_primary_session(session_id)
        await self._call_primary(
            "update_session_metadata",
            session_id,
            title=title,
            title_source=title_source,
            summary=summary,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
        )
        return live

    async def _ensure_primary_session(self, session_id: str) -> None:
        """Create the session in PG if it only exists in memory (degraded-era)."""
        if not self._primary_enabled or session_id in self._primary_session_ids:
            return
        async with self._primary_session_lock:
            if not self._primary_enabled or session_id in self._primary_session_ids:
                return
            ok, durable = await self._call_primary("get_session", session_id)
            if not ok:
                return
            if durable is None:
                live = await self.fallback.get_session(session_id)
                if live is None:
                    return
                ok, durable = await self._call_primary(
                    "create_session",
                    live.agent_id,
                    live.user_id,
                    session_id=live.id,
                )
                if not ok or durable is None:
                    return
            self._primary_session_ids.add(session_id)

    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        if await self.fallback.get_session(session_id) is None:
            await self.get_session(session_id)
        live = await self.fallback.append_event(session_id, event)
        await self._ensure_primary_session(session_id)
        await self._call_primary("append_event", session_id, event)
        return live

    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        await self.get_session(session_id)
        return cast(
            list[SessionEvent],
            await self.fallback.get_events(
                session_id,
                offset,
                limit,
                after_seq_id,
                before_seq_id,
            ),
        )

    async def count_events(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        await self.get_session(session_id)
        return cast(
            int,
            await self.fallback.count_events(session_id, after_seq_id, before_seq_id),
        )

    async def get_state(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str = "session",
    ) -> Optional[SessionState]:
        if session_id:
            await self.get_session(session_id)
        live = await self.fallback.get_state(agent_id, user_id, session_id, scope)
        if live is not None or not self._primary_enabled:
            return live
        ok, durable = await self._call_primary(
            "get_state",
            agent_id,
            user_id,
            session_id,
            scope,
        )
        if ok and durable is not None:
            return await self.fallback.update_state(
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                scope=scope,
                state_delta=durable.state,
            )
        return live

    async def update_state(
        self,
        *,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict[str, Any],
    ) -> SessionState:
        if session_id and await self.fallback.get_session(session_id) is None:
            await self.get_session(session_id)
        live = await self.fallback.update_state(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            scope=scope,
            state_delta=state_delta,
        )
        if session_id:
            await self._ensure_primary_session(session_id)
        await self._call_primary(
            "update_state",
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            scope=scope,
            state_delta=state_delta,
        )
        return live

    async def aclose(self) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
        for service in (self.primary, self.fallback):
            close = getattr(service, "aclose", None)
            if close is not None:
                await close()
