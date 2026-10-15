from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from ksadk.compat.adk_compat import (
    BaseSessionService,
    Event,
    GetSessionConfig,
    InMemorySessionService,
    ListSessionsResponse,
    Session,
)
from ksadk.sessions.db_budget import DatabaseOperationDeadline, session_db_budget
from ksadk.sessions.resilience import is_session_backend_failure

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ADKEventCommitUnknown:
    """Fail-open live event whose durable COMMIT cannot be confirmed."""

    app_name: str
    user_id: str
    session_id: str
    event_id: str


class ResilientADKSessionService(BaseSessionService):
    """Run ADK sessions locally while mirroring them to durable storage."""

    _probe_interval_seconds: float = 30.0

    def __init__(self, primary: BaseSessionService) -> None:
        self.primary = primary
        self.live = InMemorySessionService()
        self._primary_sessions: dict[tuple[str, str, str], Session] = {}
        # A failed write has an unknown commit outcome. Never replay it or
        # hydrate stale durable state over the live session after recovery.
        self._dirty_sessions: set[tuple[str, str, str]] = set()
        self._deleted_sessions: set[tuple[str, str, str]] = set()
        self.unknown_event_commits: set[ADKEventCommitUnknown] = set()
        self._primary_enabled = True
        self._probe_task: asyncio.Task[None] | None = None

    @property
    def degraded(self) -> bool:
        return not self._primary_enabled

    def _degrade(self, exc: Exception) -> None:
        if not self._primary_enabled:
            return
        self._primary_enabled = False
        logger.error(
            "ADK session persistence degraded; using in-memory live session",
            extra={
                "session_backend_state": "degraded",
                "session_backend": type(self.primary).__name__,
                "session_backend_reason_code": "SESSION_STORE_UNREACHABLE",
            },
        )
        self._start_probe()

    def _handle_primary_failure(self, exc: Exception) -> bool:
        if not is_session_backend_failure(exc):
            return False
        self._degrade(exc)
        return True

    def _start_probe(self) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            return
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def _probe_loop(self) -> None:
        while not self._primary_enabled:
            await asyncio.sleep(self._probe_interval_seconds)
            if self._primary_enabled:
                break
            await self.refresh_persistence_capability()

    async def refresh_persistence_capability(self) -> bool:
        """Probe a degraded ADK database service without rebuilding the runner."""
        if self._primary_enabled:
            return True
        try:
            await self.primary.get_session(
                app_name="__ksadk_probe__",
                user_id="__ksadk_probe__",
                session_id="__ksadk_probe__",
            )
        except Exception:
            return False
        self._primary_enabled = True
        logger.info(
            "ADK session persistence recovered; durable backend re-enabled",
            extra={
                "session_backend_state": "recovered",
                "session_backend": type(self.primary).__name__,
                "session_backend_reason_code": "READY",
            },
        )
        return True

    @staticmethod
    def _key(app_name: str, user_id: str, session_id: str) -> tuple[str, str, str]:
        return app_name, user_id, session_id

    def _write_deadline(self) -> DatabaseOperationDeadline:
        return DatabaseOperationDeadline(
            getattr(self.primary, "db_budget", None) or session_db_budget()
        )

    async def reconcile_event_commit(
        self, *, app_name: str, user_id: str, session_id: str, event_id: str
    ) -> str:
        """Read back a stable ADK event ID after a write's outcome is unknown.

        Absence is still ``unknown`` because an in-flight commit may finish
        after this read. This method never replays an event or clears dirty
        local state.
        """
        try:
            durable = await self.primary.get_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
        except Exception:
            return "unknown"
        if durable and any(event.id == event_id for event in durable.events):
            self.unknown_event_commits.discard(
                ADKEventCommitUnknown(app_name, user_id, session_id, event_id)
            )
            return "committed"
        return "unknown"

    async def _hydrate(self, durable: Session) -> Session:
        key = self._key(durable.app_name, durable.user_id, durable.id)
        if key in self._dirty_sessions:
            existing = await self.live.get_session(
                app_name=durable.app_name, user_id=durable.user_id, session_id=durable.id
            )
            if existing is None:
                raise RuntimeError("Dirty ADK session has no live state")
            return existing
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
        existing_ids = {event.id for event in existing.events}
        for event in durable.events:
            if event.id not in existing_ids:
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
            key = self._key(app_name, user_id, session_id)
            existing = await self.live.get_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
            if key in self._dirty_sessions and existing is not None:
                return existing
            if self._primary_enabled and key not in self._dirty_sessions:
                try:
                    durable = await self.primary.get_session(
                        app_name=app_name,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    if durable is not None:
                        return await self._hydrate(durable)
                except Exception as exc:
                    if not self._handle_primary_failure(exc):
                        raise
            if existing is not None:
                return existing

        live = await self.live.create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            session_id=session_id,
        )
        key = self._key(app_name, user_id, live.id)
        self._deleted_sessions.discard(key)
        if self._primary_enabled and key not in self._dirty_sessions:
            try:
                durable = await self.primary.create_session(
                    app_name=app_name,
                    user_id=user_id,
                    state=state,
                    session_id=live.id,
                )
                self._primary_sessions[self._key(app_name, user_id, live.id)] = durable
            except Exception as exc:
                self._dirty_sessions.add(key)
                if not self._handle_primary_failure(exc):
                    raise
        else:
            self._dirty_sessions.add(key)
        return live

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        key = self._key(app_name, user_id, session_id)
        live = await self.live.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            config=config,
        )
        if key in self._deleted_sessions:
            return None
        if not self._primary_enabled or key in self._dirty_sessions:
            return live
        try:
            durable = await self.primary.get_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                config=config,
            )
            return await self._hydrate(durable) if durable is not None else live
        except Exception as exc:
            if not self._handle_primary_failure(exc):
                raise
            return live

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
                    key = self._key(session.app_name, session.user_id, session.id)
                    if key in self._dirty_sessions:
                        continue
                    await self._hydrate(session)
            except Exception as exc:
                if not self._handle_primary_failure(exc):
                    raise
        return await self.live.list_sessions(app_name=app_name, user_id=user_id)

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        key = self._key(app_name, user_id, session_id)
        await self.live.delete_session(app_name=app_name, user_id=user_id, session_id=session_id)
        self._primary_sessions.pop(key, None)
        self._dirty_sessions.add(key)
        self._deleted_sessions.add(key)
        if self._primary_enabled:
            try:
                await self.primary.delete_session(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                )
                self._dirty_sessions.discard(key)
                self._deleted_sessions.discard(key)
            except Exception as exc:
                if not self._handle_primary_failure(exc):
                    raise

    async def append_event(self, session: Session, event: Event) -> Event:
        stored = await self.live.append_event(session, event)
        deadline = self._write_deadline()
        key = self._key(session.app_name, session.user_id, session.id)
        if not self._primary_enabled or key in self._dirty_sessions:
            self._dirty_sessions.add(key)
            return stored
        durable_session = self._primary_sessions.get(key)
        try:
            if durable_session is None:
                durable_session = await deadline.run(
                    self.primary.get_session(
                        app_name=session.app_name,
                        user_id=session.user_id,
                        session_id=session.id,
                    )
                )
                if durable_session is None:
                    durable_session = await deadline.run(
                        self.primary.create_session(
                            app_name=session.app_name,
                            user_id=session.user_id,
                            state=session.state,
                            session_id=session.id,
                        )
                    )
                self._primary_sessions[key] = durable_session
            await deadline.run(self.primary.append_event(durable_session, event))
        except Exception as exc:
            if is_session_backend_failure(exc):
                try:
                    outcome = await deadline.run(
                        self.reconcile_event_commit(
                            app_name=session.app_name,
                            user_id=session.user_id,
                            session_id=session.id,
                            event_id=event.id,
                        )
                    )
                except Exception:
                    outcome = "unknown"
                if outcome == "committed":
                    # A lost acknowledgement is not a failed write. Reload the
                    # exact durable revision before the next append.
                    self._primary_sessions.pop(key, None)
                    return stored
                self.unknown_event_commits.add(ADKEventCommitUnknown(*key, event.id))
                logger.warning(
                    "ADK event commit outcome unknown; retaining dirty live state",
                    extra={"session_backend_reason_code": "ADK_EVENT_COMMIT_UNKNOWN"},
                )
            self._dirty_sessions.add(key)
            if not self._handle_primary_failure(exc):
                raise
        return stored

    async def flush(self) -> None:
        await self.live.flush()
        if self._primary_enabled:
            try:
                await self.primary.flush()
            except Exception as exc:
                if not self._handle_primary_failure(exc):
                    raise

    async def close(self) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
        close = getattr(self.primary, "close", None)
        if close is not None:
            await close()
