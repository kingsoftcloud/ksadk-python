from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator
from contextvars import Context, ContextVar
from typing import Any, Optional, cast

from ksadk.sessions.base import (
    BaseSessionService,
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    SessionState,
    ToolReceiptClaim,
)
from ksadk.sessions.db_budget import DatabaseOperationDeadline, session_db_budget
from ksadk.sessions.errors import (
    CheckpointScanRestartRequired,
    HistoryReloadRequired,
    SessionBackendUnavailable,
)
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.resilience import is_session_backend_failure, is_session_connection_failure

logger = logging.getLogger(__name__)


class ResilientSessionService(BaseSessionService):
    """Keep live agent sessions available when durable persistence is unavailable.

    Clean sessions read and write through the durable backend. A session with
    confirmed process-local writes stays live-authoritative after recovery.
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
        self._hydrate_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._primary_session_lock = asyncio.Lock()
        self._primary_session_ids: set[str] = set()
        self._hydrated_session_ids: set[str] = set()
        self._live_generation: dict[str, int] = {}
        self._dirty_session_ids: set[str] = set()
        self._unsafe_live_session_ids: set[str] = set()
        self._dirty_state_keys: set[tuple[str, str, str, str]] = set()
        self._hydrated_state_keys: set[tuple[str, str, str, str]] = set()
        self._deleted_session_ids: set[str] = set()
        self._checkpoint_partition: ContextVar[
            tuple[frozenset[str], frozenset[str]] | None
        ] = ContextVar(f"checkpoint_partition_{id(self)}", default=None)
        self._probe_task: asyncio.Task[None] | None = None

    @property
    def canonical_event_service(self) -> BaseSessionService:
        """Return the single durable authority for canonical runtime facts.

        Canonical event sequences cannot
        fail over to the independently sequenced fallback without splitting one
        logical log, so RuntimeEventStore binds directly to the primary service.
        """

        return self.primary

    async def ensure_canonical_session_header(
        self, agent_id: str, user_id: str, session_id: str
    ) -> Session:
        """Preserve deletion and recovery fencing without hydrating a transcript."""
        if session_id in self._deleted_session_ids:
            raise SessionBackendUnavailable("Session deletion is pending durable recovery")
        if not self._primary_enabled:
            self._clean_read_unavailable()
        ensure = getattr(self.primary, "ensure_session_header", None)
        if callable(ensure):
            header = await ensure(agent_id, user_id, session_id)
        else:
            header = await self.primary.create_session(agent_id, user_id, session_id)
        if session_id in self._deleted_session_ids:
            raise SessionBackendUnavailable("Session deletion is pending durable recovery")
        return header

    @property
    def degraded(self) -> bool:
        return not self._primary_enabled

    def persistence_degraded(self, session_id: str | None = None) -> bool:
        """Whether a read may contain process-local facts absent from durable storage."""
        return not self._primary_enabled or (
            bool(self._dirty_session_ids or self._dirty_state_keys or self._deleted_session_ids)
            if session_id is None
            else session_id in self._dirty_session_ids
        )

    def _mark_dirty(self, session_id: str | None) -> None:
        if session_id:
            if session_id in self._unsafe_live_session_ids:
                self._clean_read_unavailable()
            self._dirty_session_ids.add(session_id)

    def _has_complete_live_session(self, session_id: str) -> bool:
        return (
            session_id in self._hydrated_session_ids
            and session_id not in self._unsafe_live_session_ids
            and session_id not in self._deleted_session_ids
        )

    @staticmethod
    def _clean_read_unavailable() -> None:
        raise SessionBackendUnavailable(
            "Durable session history is unavailable; clean history cannot be inferred from a partial live cache"
        )

    # The fallback can acquire an independent sequence when persistence fails,
    # so the wrapper itself does not claim canonical storage capabilities.

    async def _call_primary(self, method_name: str, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
        if not self._primary_enabled:
            return False, None
        try:
            method = getattr(self.primary, method_name)
            return True, await method(*args, **kwargs)
        except Exception as exc:
            readonly = (getattr(self.primary, "_retry_safe_reads", False)
                        and (method_name == "has_events" or
                             method_name.startswith(("get_", "list_", "query_", "count_"))))
            if readonly and isinstance(exc, TimeoutError):
                raise  # Request/SQL capacity timeout does not change the source.
            if readonly and is_session_connection_failure(exc):
                # A stale idle socket must not disable every clean session for
                # the entire probe interval. Only pure reads may reacquire once;
                # the caller's original deadline still owns the retry.
                try:
                    async with asyncio.timeout(2.0):
                        return True, await method(*args, **kwargs)
                except TimeoutError:
                    raise
                except Exception as retry_error:
                    exc = retry_error
            if not is_session_backend_failure(exc):
                raise exc
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
        # The recovery task outlives a request. Do not inherit request byte
        # budgets, checkpoint snapshot connections or identity ContextVars.
        self._probe_task = Context().run(asyncio.create_task, self._probe_loop())

    async def _probe_loop(self) -> None:
        while not self._primary_enabled:
            await asyncio.sleep(self._probe_interval_seconds)
            if self._primary_enabled:
                break
            try:
                probe = (self.primary.get_session_header if getattr(self.primary, "_retry_safe_reads", False)
                         else self.primary.get_session)
                async with asyncio.timeout(2.0):
                    await probe("__ksadk_probe__")
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

    async def _hydrate(self, session: Session, expected_generation: int) -> Optional[Session]:
        lock = self._hydrate_locks.setdefault(session.id, asyncio.Lock())
        async with lock:
            if session.id in self._deleted_session_ids:
                return None
            if session.id in self._dirty_session_ids:
                live = await self.fallback.get_session(session.id)
                if live is not None:
                    return live
            if self._live_generation.get(session.id, 0) != expected_generation:
                # A local mutation completed after the durable snapshot was
                # fetched but before this lock. Refresh under the same lock so
                # an older state snapshot cannot undo that mutation.
                ok, fresh = await self._call_primary("get_session", session.id)
                if not ok:
                    if session.id in self._hydrated_session_ids:
                        return await self.fallback.get_session(session.id)
                    self._clean_read_unavailable()
                if fresh is None:
                    return None
                session = cast(Session, fresh)
            self._primary_session_ids.add(session.id)
            existing = await self.fallback.get_session_header(session.id)
            if existing is not None and (
                existing.agent_id != session.agent_id
                or existing.user_id != session.user_id
                or existing.created_at != session.created_at
            ):
                # A different process deleted and recreated this ID. Never
                # graft its facts onto the old owner's recovery mirror.
                self._hydrated_session_ids.discard(session.id)
                self._unsafe_live_session_ids.add(session.id)
                await self.fallback.delete_session(session.id)
                existing = None
            if existing is None:
                await self.fallback.create_session(
                    session.agent_id,
                    session.user_id,
                    session_id=session.id,
                )
            await self.fallback.align_session_created_at(session.id, session.created_at)
            existing_ids = await self.fallback.get_session_event_ids(session.id)
            for index, event in enumerate(sorted(session.events, key=lambda item: item.seq_id)):
                if event.id not in existing_ids:
                    await self.fallback.append_event(session.id, event)
                if (index + 1) % 128 == 0:
                    # Uncontended in-memory awaits do not yield. Let history
                    # reads and driver cancellation run during a cold replay;
                    # the per-session hydrate lock still protects the merge.
                    await asyncio.sleep(0)
            await self.fallback.update_session_metadata(
                session.id,
                title=session.title,
                title_source=session.title_source,
                summary=session.summary,
                first_prompt=session.first_prompt,
                last_prompt=session.last_prompt,
            )
            current = await self.fallback.get_session_metadata(session.id)
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
            # The clean durable row defines the incarnation. The fallback
            # mirror may have been created a moment earlier with another clock.
            hydrated.created_at = session.created_at
            self._unsafe_live_session_ids.discard(session.id)
            self._hydrated_session_ids.add(session.id)
            return hydrated

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        if session_id in self._deleted_session_ids:
            raise SessionBackendUnavailable("Session deletion is pending durable recovery")
        if session_id:
            if session_id in self._dirty_session_ids:
                existing = await self.fallback.get_session(session_id)
                if existing is not None:
                    return existing
            generation = self._live_generation.get(session_id, 0)
            ok, durable = await self._call_primary("get_session", session_id)
            if ok and durable is not None:
                hydrated = await self._hydrate(durable, generation)
                if hydrated is not None:
                    return hydrated
            existing = await self.fallback.get_session(session_id)
            if existing is not None:
                if session_id in self._unsafe_live_session_ids:
                    self._clean_read_unavailable()
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
            # create_session may have returned an existing row from a racing
            # instance. Its metadata projection does not prove the event log
            # is empty, so establish a complete live mirror before marking it
            # safe for degraded writes.
            generation = self._live_generation.get(durable.id, 0)
            ok, complete = await self._call_primary("get_session", durable.id)
            if not ok or complete is None:
                self._clean_read_unavailable()
            hydrated = await self._hydrate(complete, generation)
            if hydrated is None:
                self._clean_read_unavailable()
            return hydrated
        self._mark_dirty(live.id)
        return live

    async def get_session(self, session_id: str) -> Optional[Session]:
        if session_id in self._deleted_session_ids:
            return None
        if session_id in self._dirty_session_ids:
            return await self.fallback.get_session(session_id)
        if not self._primary_enabled:
            if self._has_complete_live_session(session_id):
                return await self.fallback.get_session(session_id)
            self._clean_read_unavailable()
        generation = self._live_generation.get(session_id, 0)
        ok, durable = await self._call_primary("get_session", session_id)
        if ok and durable is not None:
            return await self._hydrate(durable, generation)
        if not ok:
            if self._has_complete_live_session(session_id):
                return await self.fallback.get_session(session_id)
            self._clean_read_unavailable()
        return None

    async def get_adk_session_snapshot(
        self, session_id: str, expected_session: Session | None = None
    ) -> Optional[Session]:
        if session_id in self._deleted_session_ids:
            raise HistoryReloadRequired("Session changed during legacy read")
        live = session_id in self._dirty_session_ids or not self._primary_enabled
        if (not self._primary_enabled and session_id not in self._dirty_session_ids
                and not self._has_complete_live_session(session_id)):
            self._clean_read_unavailable()
        backend = self.fallback if live else self.primary
        snapshot = getattr(backend, "get_adk_session_snapshot", None)
        if callable(snapshot):
            session = await snapshot(session_id, expected_session)
        else:
            session = await backend.get_session(session_id)
            if expected_session is not None and (
                session is None or session.agent_id != expected_session.agent_id
                or session.user_id != expected_session.user_id
                or session.created_at != expected_session.created_at
            ):
                raise HistoryReloadRequired("Session changed during legacy read")
        if (session_id in self._deleted_session_ids
                or (session_id in self._dirty_session_ids or not self._primary_enabled) != live):
            raise HistoryReloadRequired("Session source changed during legacy read")
        return session

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        if session_id in self._deleted_session_ids:
            return None
        if session_id in self._dirty_session_ids:
            return await self.fallback.get_session_metadata(session_id)
        if not self._primary_enabled:
            if self._has_complete_live_session(session_id):
                return await self.fallback.get_session_metadata(session_id)
            self._clean_read_unavailable()
        ok, durable = await self._call_primary("get_session_metadata", session_id)
        if ok and durable is not None:
            return cast(Session, durable)
        if not ok:
            if self._has_complete_live_session(session_id):
                return await self.fallback.get_session_metadata(session_id)
            self._clean_read_unavailable()
        return None

    async def get_session_header(self, session_id: str) -> Optional[Session]:
        if session_id in self._deleted_session_ids:
            return None
        if session_id in self._dirty_session_ids:
            return await self.fallback.get_session_header(session_id)
        if not self._primary_enabled:
            if self._has_complete_live_session(session_id):
                return await self.fallback.get_session_header(session_id)
            self._clean_read_unavailable()
        method = "get_session_header" if hasattr(self.primary, "get_session_header") else "get_session_metadata"
        ok, header = await self._call_primary(method, session_id)
        if not ok:
            if self._has_complete_live_session(session_id):
                return await self.fallback.get_session_header(session_id)
            self._clean_read_unavailable()
        return cast(Optional[Session], header)

    async def get_session_summary_events(self, session_id: str) -> list[SessionEvent]:
        if session_id in self._deleted_session_ids:
            return []
        if session_id in self._dirty_session_ids:
            return await self.fallback.get_session_summary_events(session_id)
        if not self._primary_enabled:
            if self._has_complete_live_session(session_id):
                return await self.fallback.get_session_summary_events(session_id)
            self._clean_read_unavailable()
        ok, events = await self._call_primary("get_session_summary_events", session_id)
        if ok:
            return cast(list[SessionEvent], events)
        if self._has_complete_live_session(session_id):
            return await self.fallback.get_session_summary_events(session_id)
        self._clean_read_unavailable()

    async def get_max_seq_id(self, session_id: str) -> int:
        if session_id in self._dirty_session_ids:
            return await self.fallback.get_max_seq_id(session_id)
        if not self._primary_enabled:
            self._clean_read_unavailable()
        ok, value = await self._call_primary("get_max_seq_id", session_id)
        if not ok:
            self._clean_read_unavailable()
        return int(value or 0)

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        dirty = bool(self._dirty_session_ids or self._deleted_session_ids)
        bounded = limit is not None
        # Fetch enough durable rows to replace every locally changed/deleted
        # row ahead of the requested page, without loading the whole agent.
        durable_limit = (
            (offset or 0) + limit + len(self._dirty_session_ids)
            + len(self._deleted_session_ids)
            if dirty and bounded else limit
        )
        ok, durable_sessions = await self._call_primary(
            "list_sessions",
            agent_id,
            user_id,
            0 if dirty and bounded else (None if dirty else offset),
            durable_limit,
        )
        if ok and not dirty:
            return [s for s in cast(list[Session], durable_sessions or [])
                    if s.id not in self._deleted_session_ids]
        if not ok:
            self._clean_read_unavailable()
        # Dirty rows may have an older durable counterpart. Merge metadata only.
        durable = {s.id: s for s in (durable_sessions or [])} if ok else {}
        if bounded and ok:
            live = [session for session in await asyncio.gather(*(
                self.fallback.get_session_metadata(session_id)
                for session_id in self._dirty_session_ids
            )) if session is not None]
        else:
            live = await self.fallback.list_session_metadata(agent_id, user_id)
        durable.update({s.id: s for s in live
                        if s.agent_id == agent_id
                        and (user_id is None or s.user_id == user_id)
                        and (not ok or s.id in self._dirty_session_ids)})
        ordered = sorted((s for s in durable.values() if s.id not in self._deleted_session_ids),
                         key=lambda s: (s.updated_at, s.created_at, s.id), reverse=True)
        start = offset or 0
        return ordered[start:None if limit is None else start + limit]

    async def count_sessions(self, agent_id: str, user_id: Optional[str] = None) -> int:
        ok, total = await self._call_primary("count_sessions", agent_id, user_id)
        if ok and not (self._dirty_session_ids or self._deleted_session_ids):
            return int(total or 0)
        if not ok:
            self._clean_read_unavailable()
        count = int(total or 0)
        for session_id in self._dirty_session_ids | self._deleted_session_ids:
            ok, durable = await self._call_primary("get_session_header", session_id)
            if not ok:
                self._clean_read_unavailable()
            live = (await self.fallback.get_session_header(session_id)
                    if session_id not in self._deleted_session_ids else None)
            durable_in_scope = (durable is not None and durable.agent_id == agent_id
                                and (user_id is None or durable.user_id == user_id))
            live_in_scope = (live is not None and live.agent_id == agent_id
                             and (user_id is None or live.user_id == user_id))
            count += int(live_in_scope) - int(durable_in_scope)
        return count

    async def delete_session(self, session_id: str) -> bool:
        budget = getattr(self.primary, "db_budget", session_db_budget())
        deadline = DatabaseOperationDeadline(budget)
        lock = self._hydrate_locks.setdefault(session_id, asyncio.Lock())
        await asyncio.wait_for(
            lock.acquire(), timeout=min(budget.lock_seconds, deadline.remaining_seconds()),
        )
        try:
            deleted = await deadline.run(self._delete_session_locked(session_id))
            self._live_generation[session_id] = self._live_generation.get(session_id, 0) + 1
            return deleted
        finally:
            lock.release()

    async def _delete_session_locked(self, session_id: str) -> bool:
        deleted = await self.fallback.delete_session(session_id)
        try:
            ok, durable_deleted = await self._call_primary("delete_session", session_id)
        except BaseException:
            # Cancellation after the local delete cannot let a recovered
            # durable row reappear in this process.
            self._deleted_session_ids.add(session_id)
            raise
        if ok:
            self._primary_session_ids.discard(session_id)
            self._dirty_session_ids.discard(session_id)
            self._hydrated_session_ids.discard(session_id)
            self._unsafe_live_session_ids.discard(session_id)
            self._deleted_session_ids.discard(session_id)
        else:
            self._deleted_session_ids.add(session_id)
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
        budget = getattr(self.primary, "db_budget", session_db_budget())
        deadline = DatabaseOperationDeadline(budget)
        lock = self._hydrate_locks.setdefault(session_id, asyncio.Lock())
        await asyncio.wait_for(
            lock.acquire(), timeout=min(budget.lock_seconds, deadline.remaining_seconds()),
        )
        try:
            updated = await deadline.run(self._update_session_metadata_locked(
                session_id, title=title, title_source=title_source,
                summary=summary, first_prompt=first_prompt, last_prompt=last_prompt,
            ))
            self._live_generation[session_id] = self._live_generation.get(session_id, 0) + 1
            return updated
        finally:
            lock.release()

    async def _update_session_metadata_locked(
        self, session_id: str, *, title: Optional[str] = None,
        title_source: Optional[str] = None, summary: Optional[str] = None,
        first_prompt: Optional[str] = None, last_prompt: Optional[str] = None,
    ) -> Session:
        kwargs = dict(title=title, title_source=title_source, summary=summary,
                      first_prompt=first_prompt, last_prompt=last_prompt)
        if session_id not in self._dirty_session_ids:
            ok, durable = await self._call_primary("update_session_metadata", session_id, **kwargs)
            if ok:
                if session_id in self._hydrated_session_ids:
                    await self.fallback.update_session_metadata(session_id, **kwargs)
                return cast(Session, durable)
            if session_id not in self._hydrated_session_ids or session_id in self._unsafe_live_session_ids:
                self._clean_read_unavailable()
            self._mark_dirty(session_id)
        return await self.fallback.update_session_metadata(session_id, **kwargs)

    async def _ensure_primary_session(self, session_id: str) -> None:
        """Create the session in PG if it only exists in memory (degraded-era)."""
        if not self._primary_enabled or session_id in self._primary_session_ids:
            return
        async with self._primary_session_lock:
            if not self._primary_enabled or session_id in self._primary_session_ids:
                return
            ok, durable = await self._call_primary("get_session_metadata", session_id)
            if not ok:
                return
            if durable is None:
                live = await self.fallback.get_session_metadata(session_id)
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
        return await self._append_event_entry(session_id, event, None)

    async def append_event_if_session_matches(
        self, session_id: str, event: SessionEvent, expected_session: Session
    ) -> SessionEvent:
        """Use normal recovery fencing with an incarnation check at each write."""
        return await self._append_event_entry(session_id, event, expected_session)

    async def _append_event_entry(
        self, session_id: str, event: SessionEvent, expected_session: Session | None
    ) -> SessionEvent:
        budget = getattr(self.primary, "db_budget", session_db_budget())
        deadline = DatabaseOperationDeadline(budget)
        lock = self._hydrate_locks.setdefault(session_id, asyncio.Lock())
        await asyncio.wait_for(
            lock.acquire(), timeout=min(budget.lock_seconds, deadline.remaining_seconds()),
        )
        clean_write = session_id not in self._dirty_session_ids
        try:
            persisted = await deadline.run(
                self._append_event_locked(session_id, event, deadline, expected_session)
            )
            self._live_generation[session_id] = self._live_generation.get(session_id, 0) + 1
            return persisted
        except (TimeoutError, asyncio.CancelledError, SessionBackendUnavailable) as error:
            if clean_write:
                # The primary may have committed before its acknowledgement or
                # stable-ID readback was interrupted. Fence the old mirror and
                # any hydration that captured a snapshot before this write.
                self._unsafe_live_session_ids.add(session_id)
                self._hydrated_session_ids.discard(session_id)
                self._hydrated_state_keys.difference_update(
                    key for key in tuple(self._hydrated_state_keys) if key[3] == session_id
                )
                self._live_generation[session_id] = self._live_generation.get(session_id, 0) + 1
            if isinstance(error, TimeoutError):
                raise SessionBackendUnavailable(
                    "Session event commit outcome is unknown; stable ID confirmation required"
                ) from error
            raise
        finally:
            lock.release()

    async def _append_event_locked(
        self, session_id: str, event: SessionEvent, deadline: DatabaseOperationDeadline,
        expected_session: Session | None = None,
    ) -> SessionEvent:
        if expected_session is not None and session_id in self._deleted_session_ids:
            raise HistoryReloadRequired("Session changed during legacy import")

        async def append_on(backend: BaseSessionService) -> SessionEvent:
            if expected_session is None:
                return await backend.append_event(session_id, event)
            guarded = getattr(backend, "append_event_if_session_matches", None)
            if callable(guarded):
                return await guarded(session_id, event, expected_session)
            current = await backend.get_session_metadata(session_id)
            if (current is None or current.agent_id != expected_session.agent_id
                    or current.user_id != expected_session.user_id
                    or current.created_at != expected_session.created_at):
                raise HistoryReloadRequired("Session changed during legacy import")
            return await backend.append_event(session_id, event)

        if session_id in self._dirty_session_ids:
            return await append_on(self.fallback)
        if not self._primary_enabled:
            if not self._has_complete_live_session(session_id):
                self._clean_read_unavailable()
            self._mark_dirty(session_id)
            return await append_on(self.fallback)
        if expected_session is None:
            ok, durable = await self._call_primary("append_event", session_id, event)
        else:
            try:
                durable = await append_on(self.primary)
                ok = True
            except Exception as exc:
                if not is_session_backend_failure(exc):
                    raise
                self._disable_primary(exc)
                ok, durable = False, None
        if not ok:
            # The commit may have succeeded before the transport failed. A
            # stable event ID is the only safe confirmation; never replay the
            # append or its upstream tool/LLM side effect automatically.
            try:
                if expected_session is not None:
                    guarded_read = getattr(
                        self.primary, "get_event_by_id_if_session_matches", None
                    )
                    if callable(guarded_read):
                        committed = await deadline.run(
                            guarded_read(session_id, event.id, expected_session)
                        )
                    else:
                        current = await deadline.run(
                            self.primary.get_session_metadata(session_id)
                        )
                        if (current is None or current.agent_id != expected_session.agent_id
                                or current.user_id != expected_session.user_id
                                or current.created_at != expected_session.created_at):
                            raise HistoryReloadRequired("Session changed during legacy import")
                        committed = await deadline.run(
                            self.primary.get_event_by_id(session_id, event.id)
                        )
                else:
                    committed = await deadline.run(
                        self.primary.get_event_by_id(session_id, event.id)
                    )
            except HistoryReloadRequired:
                raise
            except Exception as error:
                raise SessionBackendUnavailable(
                    "Session event commit outcome is unknown; stable ID confirmation failed"
                ) from error
            if committed is not None:
                self._primary_enabled = True
                if self._probe_task is not None and not self._probe_task.done():
                    self._probe_task.cancel()
                await self._mirror_persisted_event(session_id, event, committed)
                return cast(SessionEvent, committed)
            if session_id in self._unsafe_live_session_ids or session_id not in self._hydrated_session_ids:
                self._clean_read_unavailable()
            self._mark_dirty(session_id)
            return await append_on(self.fallback)
        persisted = cast(SessionEvent, durable)
        await self._mirror_persisted_event(session_id, event, persisted)
        return persisted

    async def _mirror_persisted_event(
        self, session_id: str, event: SessionEvent, persisted: SessionEvent,
    ) -> None:
        if session_id in self._hydrated_session_ids:
            expected_seq = await self.fallback.count_events(session_id) + 1
        else:
            expected_seq = None
        if expected_seq == persisted.seq_id:
            if await self.fallback.get_event_by_id(session_id, event.id) is None:
                await self.fallback.append_event(session_id, event)
        elif expected_seq is not None:
            self._unsafe_live_session_ids.add(session_id)

    async def claim_tool_receipt(self, session_id: str, **kwargs: Any) -> ToolReceiptClaim:
        # Falling back to the process-local live store here would re-open the
        # cross-process duplicate window this claim closes.  Fail closed while
        # the durable primary is unavailable instead.
        if (not self._primary_enabled or session_id in self._dirty_session_ids
                or session_id in self._deleted_session_ids):
            raise SessionBackendUnavailable("durable tool receipt store unavailable")
        ok, result = await self._call_primary("claim_tool_receipt", session_id, **kwargs)
        if not ok:
            raise SessionBackendUnavailable("durable tool receipt store unavailable")
        return cast(ToolReceiptClaim, result)

    async def settle_tool_receipt(self, session_id: str, **kwargs: Any) -> ToolReceiptClaim:
        if (not self._primary_enabled or session_id in self._dirty_session_ids
                or session_id in self._deleted_session_ids):
            raise SessionBackendUnavailable("durable tool receipt store unavailable")
        ok, result = await self._call_primary("settle_tool_receipt", session_id, **kwargs)
        if not ok:
            raise SessionBackendUnavailable("durable tool receipt store unavailable")
        return cast(ToolReceiptClaim, result)

    async def get_tool_receipt_claim(
        self, session_id: str, *, idempotency_key: str
    ) -> ToolReceiptClaim | None:
        if (not self._primary_enabled or session_id in self._dirty_session_ids
                or session_id in self._deleted_session_ids):
            raise SessionBackendUnavailable("durable tool receipt store unavailable")
        ok, result = await self._call_primary(
            "get_tool_receipt_claim", session_id, idempotency_key=idempotency_key
        )
        if not ok:
            raise SessionBackendUnavailable("durable tool receipt store unavailable")
        return cast(ToolReceiptClaim | None, result)

    async def get_event_by_id(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        if session_id not in self._dirty_session_ids:
            ok, value = await self._call_primary("get_event_by_id", session_id, event_id)
            if ok:
                return cast(Optional[SessionEvent], value)
            self._clean_read_unavailable()
        return await self.fallback.get_event_by_id(session_id, event_id)

    async def get_events_by_invocation_id(
        self,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        if session_id not in self._dirty_session_ids:
            ok, value = await self._call_primary(
                "get_events_by_invocation_id", session_id, invocation_id,
                after_seq_id=after_seq_id, before_seq_id=before_seq_id,
            )
            if ok:
                return cast(list[SessionEvent], value)
            self._clean_read_unavailable()
        return await self.fallback.get_events_by_invocation_id(
            session_id,
            invocation_id,
            after_seq_id=after_seq_id,
            before_seq_id=before_seq_id,
        )

    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        if session_id not in self._dirty_session_ids:
            ok, value = await self._call_primary(
                "get_events", session_id, offset, limit, after_seq_id, before_seq_id
            )
            if ok:
                return cast(list[SessionEvent], value)
            self._clean_read_unavailable()
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

    async def get_session_header_with_watermark(self, session_id: str) -> tuple[Session | None, int]:
        if session_id in self._deleted_session_ids:
            return None, 0
        if (session_id not in self._dirty_session_ids
                and callable(getattr(self.primary, 'get_session_header_with_watermark', None))):
            ok, value = await self._call_primary('get_session_header_with_watermark', session_id)
            if not ok:
                self._clean_read_unavailable()
            return value
        return await self.get_session_header(session_id), await self.get_max_seq_id(session_id)

    async def get_message_event_window(
        self, session_id: str, *, limit: int, from_start: bool = False,
        after_seq_id: int | None = None, before_seq_id: int | None = None,
        byte_limit: int = 64 * 1024,
    ) -> list[SessionEvent]:
        if session_id in self._deleted_session_ids:
            return []
        if (session_id not in self._dirty_session_ids
                and callable(getattr(self.primary, 'get_message_event_window', None))):
            ok, value = await self._call_primary(
                'get_message_event_window', session_id, limit=limit,
                from_start=from_start, after_seq_id=after_seq_id,
                before_seq_id=before_seq_id, byte_limit=byte_limit,
            )
            if not ok:
                self._clean_read_unavailable()
            return cast(list[SessionEvent], value)
        if from_start:
            return await self.query_events(SessionEventQuery(
                session_ids=[session_id], from_start=True, order_by_seq=True,
                limit=limit, after_seq_id=after_seq_id, before_seq_id=before_seq_id,
            ))
        return await self.get_events(
            session_id, limit=limit, after_seq_id=after_seq_id, before_seq_id=before_seq_id,
        )

    async def has_events(
        self, session_id: str, *, after_seq_id: int | None = None,
        before_seq_id: int | None = None,
    ) -> bool:
        if session_id in self._deleted_session_ids:
            return False
        if session_id in self._dirty_session_ids:
            return await self.fallback.has_events(
                session_id, after_seq_id=after_seq_id, before_seq_id=before_seq_id,
            )
        if not callable(getattr(self.primary, "has_events", None)):
            return await super().has_events(
                session_id, after_seq_id=after_seq_id, before_seq_id=before_seq_id,
            )
        ok, value = await self._call_primary(
            "has_events", session_id, after_seq_id=after_seq_id,
            before_seq_id=before_seq_id,
        )
        if not ok:
            self._clean_read_unavailable()
        return bool(value)

    async def count_events(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        if session_id not in self._dirty_session_ids:
            ok, value = await self._call_primary(
                "count_events", session_id, after_seq_id, before_seq_id
            )
            if ok:
                return int(value or 0)
            self._clean_read_unavailable()
        return cast(
            int,
            await self.fallback.count_events(session_id, after_seq_id, before_seq_id),
        )

    async def get_sessions_by_ids(self, session_ids: list[str]) -> list[Session]:
        result: dict[str, Session] = {}
        ok, durable = await self._call_primary("get_sessions_by_ids", session_ids)
        if ok:
            result.update({session.id: session for session in durable or []})
        live = await self.fallback.get_sessions_by_ids(session_ids)
        result.update({session.id: session for session in live
                       if not ok or session.id in self._dirty_session_ids})
        if not ok and any(sid not in self._dirty_session_ids for sid in session_ids):
            self._clean_read_unavailable()
        return [result[session_id] for session_id in session_ids
                if session_id in result and session_id not in self._deleted_session_ids]

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        result: dict[str, Session] = {}
        ok, durable = await self._call_primary("list_session_metadata", agent_id, user_id)
        if ok:
            result.update({session.id: session for session in durable or []})
        else:
            self._clean_read_unavailable()
        live = await self.fallback.list_session_metadata(agent_id, user_id)
        result.update({session.id: session for session in live
                       if not ok or session.id in self._dirty_session_ids})
        sessions = [s for s in result.values() if s.id not in self._deleted_session_ids]
        sessions.sort(key=lambda item: (item.updated_at, item.created_at, item.id), reverse=True)
        return sessions

    async def _partition_query_session_ids(
        self, session_ids: list[str] | None, agent_id: str | None
    ) -> tuple[list[str], list[str]]:
        if session_ids is None:
            ok, durable_listing = await self._call_primary("list_session_ids", agent_id)
            if not ok:
                self._clean_read_unavailable()
            live_listing = await self.fallback.list_session_ids(agent_id)
            ids = list(dict.fromkeys([*(durable_listing or []), *live_listing]))
        else:
            ids = [sid for sid in dict.fromkeys(session_ids)
                   if sid not in self._deleted_session_ids]
        dirty = set(self._dirty_session_ids)
        durable_ids: set[str] = set()
        if ids:
            ok, durable_metadata = await self._call_primary(
                "get_session_ids_by_ids", ids, agent_id
            )
            if ok:
                durable_ids = set(durable_metadata or [])
            elif any(sid not in dirty for sid in ids):
                self._clean_read_unavailable()
        live_ids = set(await self.fallback.get_session_ids_by_ids(ids, agent_id))
        clean_ids = [
            session_id
            for session_id in ids
            if session_id not in dirty and session_id in durable_ids
        ]
        fallback_ids = [
            session_id for session_id in ids
            if session_id in dirty or (session_id in live_ids and session_id not in durable_ids)
        ]
        return clean_ids, fallback_ids

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        clean_ids, live_ids = await self._partition_query_session_ids(
            query.session_ids, query.agent_id
        )
        # There is no cross-source merge to perform for a single partition.
        # Preserve the storage LIMIT/OFFSET instead of transferring every row
        # preceding a deep page and slicing it in Python.
        if clean_ids and not live_ids:
            ok, result = await self._call_primary(
                "query_events",
                SessionEventQuery(**{**query.__dict__, "session_ids": clean_ids}),
            )
            if not ok:
                self._clean_read_unavailable()
            return cast(list[SessionEvent], result or [])
        if live_ids and not clean_ids:
            return await self.fallback.query_events(
                SessionEventQuery(**{**query.__dict__, "session_ids": live_ids})
            )
        window = query.offset + query.limit
        durable_events: list[SessionEvent] = []
        if clean_ids:
            ok, result = await self._call_primary(
                "query_events",
                SessionEventQuery(
                    **{**query.__dict__, "session_ids": clean_ids, "offset": 0, "limit": window}
                ),
            )
            if ok:
                durable_events = cast(list[SessionEvent], result or [])
            else:
                self._clean_read_unavailable()
        live_events = (
            await self.fallback.query_events(
                SessionEventQuery(
                    **{**query.__dict__, "session_ids": live_ids, "offset": 0, "limit": window}
                )
            )
            if live_ids
            else []
        )
        merged_by_id = {event.id: event for event in [*durable_events, *live_events]}
        merged = list(merged_by_id.values())
        if query.order_by_seq:
            merged.sort(key=lambda event: (event.session_id, event.seq_id, event.id))
        else:
            merged.sort(
                key=lambda event: (event.timestamp, event.session_id, event.seq_id, event.id)
            )
        if query.from_start:
            return merged[query.offset : query.offset + query.limit]
        end = max(len(merged) - query.offset, 0)
        return merged[max(end - query.limit, 0) : end]

    async def count_event_query(self, query: SessionEventQuery) -> int:
        clean_ids, live_ids = await self._partition_query_session_ids(
            query.session_ids, query.agent_id
        )
        total = 0
        if clean_ids:
            ok, value = await self._call_primary(
                "count_event_query",
                SessionEventQuery(**{**query.__dict__, "session_ids": clean_ids}),
            )
            if not ok:
                self._clean_read_unavailable()
            total += int(value or 0)
        if live_ids:
            total += await self.fallback.count_event_query(
                SessionEventQuery(**{**query.__dict__, "session_ids": live_ids})
            )
        return total

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        if session_id not in self._dirty_session_ids:
            ok, stats = await self._call_primary(
                "get_checkpoint_lookup_stats", session_id, run_id, checkpoint_id
            )
            if ok:
                return cast(dict[str, object], stats)
            self._clean_read_unavailable()
        return await self.fallback.get_checkpoint_lookup_stats(
            session_id, run_id, checkpoint_id
        )

    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        clean_ids, live_ids = await self._partition_query_session_ids(
            query.session_ids, query.agent_id
        )
        if clean_ids and not live_ids:
            ok, result = await self._call_primary(
                "scan_checkpoint_events",
                CheckpointEventQuery(**{**query.__dict__, "session_ids": clean_ids}),
            )
            if ok:
                return cast(list[SessionEvent], result or [])
            self._clean_read_unavailable()
        if live_ids and not clean_ids:
            return await self.fallback.scan_checkpoint_events(
                CheckpointEventQuery(**{**query.__dict__, "session_ids": live_ids})
            )
        window = query.offset + query.limit
        durable: list[SessionEvent] = []
        if clean_ids:
            ok, result = await self._call_primary(
                "scan_checkpoint_events",
                CheckpointEventQuery(
                    **{
                        **query.__dict__,
                        "session_ids": clean_ids,
                        "offset": 0,
                        "limit": min(window, 50),
                    }
                ),
            )
            if ok:
                durable = cast(list[SessionEvent], result or [])
            else:
                self._clean_read_unavailable()
        live = (
            await self.fallback.scan_checkpoint_events(
                CheckpointEventQuery(
                    **{
                        **query.__dict__,
                        "session_ids": live_ids,
                        "offset": 0,
                        "limit": min(window, 50),
                    }
                )
            )
            if live_ids
            else []
        )
        merged = list({event.id: event for event in [*durable, *live]}.values())
        merged.sort(key=lambda event: (event.timestamp, event.session_id, event.seq_id, event.id))
        return merged[query.offset : query.offset + query.limit]

    async def iter_resume_related_event_pages(
        self, session_id: str, *, page_size: int = 256,
        expected_session: Session | None = None,
    ) -> AsyncIterator[list[SessionEvent]]:
        def source() -> str:
            if session_id in self._deleted_session_ids:
                raise HistoryReloadRequired("Checkpoint session was deleted during lookup")
            if session_id in self._dirty_session_ids:
                return "live"
            if not self._primary_enabled:
                if self._has_complete_live_session(session_id):
                    return "live"
                self._clean_read_unavailable()
            return "clean"

        fixed_source = source()
        backend = self.fallback if fixed_source == "live" else self.primary
        pages = backend.iter_resume_related_event_pages(
            session_id, page_size=page_size, expected_session=expected_session
        ).__aiter__()
        try:
            async for page in pages:
                if source() != fixed_source:
                    raise HistoryReloadRequired(
                        "Checkpoint source changed during lookup; retry the request"
                    )
                yield page
            if source() != fixed_source:
                raise HistoryReloadRequired(
                    "Checkpoint source changed during lookup; retry the request"
                )
        except Exception as exc:
            if fixed_source == "clean" and is_session_backend_failure(exc):
                self._disable_primary(exc)
                raise HistoryReloadRequired(
                    "Durable checkpoint source changed during lookup; retry the request"
                ) from exc
            raise
        finally:
            close = getattr(pages, "aclose", None)
            if callable(close):
                await close()

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        clean_ids, live_ids = await self._partition_query_session_ids(
            query.session_ids, query.agent_id
        )
        token = self._checkpoint_partition.set(
            (frozenset(clean_ids), frozenset(live_ids))
        )
        batches = self._iter_checkpoint_event_chunks_partitioned(
            query, clean_ids, live_ids
        ).__aiter__()
        try:
            async for batch in batches:
                yield batch
        finally:
            close = getattr(batches, "aclose", None)
            if callable(close):
                await close()
            self._checkpoint_partition.reset(token)

    async def _iter_checkpoint_event_chunks_partitioned(
        self,
        query: CheckpointEventQuery,
        clean_ids: list[str],
        live_ids: list[str],
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        if not clean_ids:
            batches = self.fallback.iter_checkpoint_event_chunks(
                CheckpointEventQuery(**{**query.__dict__, "session_ids": live_ids})
            ).__aiter__()
            try:
                async for batch in batches:
                    yield batch
            finally:
                close = getattr(batches, "aclose", None)
                if callable(close):
                    await close()
            return
        if not live_ids:
            batches = self.primary.iter_checkpoint_event_chunks(
                CheckpointEventQuery(**{**query.__dict__, "session_ids": clean_ids})
            ).__aiter__()
            try:
                async for batch in batches:
                    yield batch
            except Exception as exc:
                if not is_session_backend_failure(exc):
                    raise
                self._disable_primary(exc)
                raise CheckpointScanRestartRequired(
                    "primary failed during clean checkpoint scan"
                ) from exc
            finally:
                close = getattr(batches, "aclose", None)
                if callable(close):
                    await close()
            return

        iterators = {
            "clean": self.primary.iter_checkpoint_event_chunks(
                CheckpointEventQuery(
                    **{**query.__dict__, "session_ids": clean_ids, "offset": 0, "limit": 50}
                )
            ).__aiter__(),
            "live": self.fallback.iter_checkpoint_event_chunks(
                CheckpointEventQuery(
                    **{**query.__dict__, "session_ids": live_ids, "offset": 0, "limit": 50}
                )
            ).__aiter__(),
        }
        pages: dict[str, list[SessionEvent]] = {"clean": [], "live": []}
        indexes = {"clean": 0, "live": 0}
        exhausted = {"clean": False, "live": False}
        skipped = 0

        async def load(kind: str) -> None:
            try:
                pages[kind] = await anext(iterators[kind])
            except StopAsyncIteration:
                pages[kind] = []
                exhausted[kind] = True
            except Exception as exc:
                if kind != "clean" or not is_session_backend_failure(exc):
                    raise
                self._disable_primary(exc)
                raise CheckpointScanRestartRequired(
                    "primary failed during mixed checkpoint scan"
                ) from exc
            indexes[kind] = 0
            if len(pages[kind]) < 50:
                exhausted[kind] = True

        try:
            await load("clean")
            await load("live")
            output: list[SessionEvent] = []
            while True:
                for kind in ("clean", "live"):
                    if indexes[kind] >= len(pages[kind]) and not exhausted[kind]:
                        await load(kind)
                candidates = [
                    (event.timestamp, event.session_id, event.seq_id, event.id, kind, event)
                    for kind in ("clean", "live")
                    for event in pages[kind][indexes[kind] : indexes[kind] + 1]
                ]
                if not candidates:
                    if output:
                        yield output
                    break
                *_, kind, event = min(candidates)
                indexes[kind] += 1
                if skipped < query.offset:
                    skipped += 1
                    continue
                output.append(event)
                if len(output) == query.limit:
                    yield output
                    output = []
        finally:
            for iterator in iterators.values():
                close = getattr(iterator, "aclose", None)
                if callable(close):
                    await close()

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        unique_keys = list(dict.fromkeys(keys))
        active = self._checkpoint_partition.get()
        if active is None:
            clean_ids, live_ids = await self._partition_query_session_ids(
                list(dict.fromkeys(key[0] for key in unique_keys)), None
            )
            clean_set, live_set = set(clean_ids), set(live_ids)
        else:
            clean_set, live_set = map(set, active)
        clean_keys = [key for key in unique_keys if key[0] in clean_set]
        live_keys = [key for key in unique_keys if key[0] in live_set]
        durable: dict[str, object] = {"audits": {}, "latest_seq_ids": {}}
        if clean_keys:
            ok, value = await self._call_primary("get_checkpoint_stats", clean_keys)
            if not ok:
                raise CheckpointScanRestartRequired(
                    "primary failed while reading checkpoint stats"
                )
            durable = cast(dict[str, object], value or durable)
        live = (
            await self.fallback.get_checkpoint_stats(live_keys)
            if live_keys
            else {"audits": {}, "latest_seq_ids": {}}
        )
        return {
            "audits": {
                **cast(dict, durable["audits"]),
                **cast(dict, live["audits"]),
            },
            "latest_seq_ids": {
                **cast(dict, durable["latest_seq_ids"]),
                **cast(dict, live["latest_seq_ids"]),
            },
        }

    async def get_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        ok, events = await self._call_primary(
            "get_events_for_agent", agent_id, user_id, offset, limit
        )
        if ok:
            return cast(list[SessionEvent], events)
        self._clean_read_unavailable()
        return await self.fallback.get_events_for_agent(agent_id, user_id, offset, limit)

    async def count_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        ok, total = await self._call_primary("count_events_for_agent", agent_id, user_id)
        if ok:
            return int(total or 0)
        self._clean_read_unavailable()
        return await self.fallback.count_events_for_agent(agent_id, user_id)

    async def get_state(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str = "session",
    ) -> Optional[SessionState]:
        if session_id:
            lock = self._hydrate_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                return await self._get_state_locked(agent_id, user_id, session_id, scope)
        return await self._get_state_locked(agent_id, user_id, session_id, scope)

    async def _get_state_locked(
        self, agent_id: str, user_id: Optional[str], session_id: Optional[str],
        scope: str,
    ) -> Optional[SessionState]:
        state_key = (scope, agent_id, user_id or "", session_id or "")
        unsafe_live = session_id in self._unsafe_live_session_ids
        if not unsafe_live and (
            state_key in self._dirty_state_keys or session_id in self._dirty_session_ids
        ):
            return await self.fallback.get_state(agent_id, user_id, session_id, scope)
        safe_live = not unsafe_live and (
            state_key in self._hydrated_state_keys or (
                scope == "session" and bool(session_id)
                and self._has_complete_live_session(session_id)
            )
        )
        if not self._primary_enabled:
            if safe_live:
                return await self.fallback.get_state(agent_id, user_id, session_id, scope)
            self._clean_read_unavailable()
        ok, durable = await self._call_primary(
            "get_state",
            agent_id,
            user_id,
            session_id,
            scope,
        )
        if ok and durable is not None:
            await self.fallback.mirror_state_snapshot(cast(SessionState, durable))
            self._hydrated_state_keys.add(state_key)
            return cast(SessionState, durable)
        if not ok:
            if safe_live:
                return await self.fallback.get_state(agent_id, user_id, session_id, scope)
            self._clean_read_unavailable()
        return None

    async def update_state(
        self,
        *,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict[str, Any],
    ) -> SessionState:
        budget = getattr(self.primary, "db_budget", session_db_budget())
        deadline = DatabaseOperationDeadline(budget)
        if session_id:
            lock = self._hydrate_locks.setdefault(session_id, asyncio.Lock())
            await asyncio.wait_for(
                lock.acquire(), timeout=min(budget.lock_seconds, deadline.remaining_seconds()),
            )
            try:
                updated = await deadline.run(self._update_state_locked(
                    agent_id=agent_id, user_id=user_id, session_id=session_id,
                    scope=scope, state_delta=state_delta,
                ))
                self._live_generation[session_id] = self._live_generation.get(session_id, 0) + 1
                return updated
            finally:
                lock.release()
        return await deadline.run(self._update_state_locked(
            agent_id=agent_id, user_id=user_id, session_id=session_id,
            scope=scope, state_delta=state_delta,
        ))

    async def _update_state_locked(
        self, *, agent_id: str, user_id: Optional[str], session_id: Optional[str],
        scope: str, state_delta: dict[str, Any],
    ) -> SessionState:
        kwargs = dict(agent_id=agent_id, user_id=user_id, session_id=session_id,
                      scope=scope, state_delta=state_delta)
        state_key = (scope, agent_id, user_id or "", session_id or "")
        if session_id not in self._dirty_session_ids and state_key not in self._dirty_state_keys:
            ok, durable = await self._call_primary("update_state", **kwargs)
            if ok:
                await self.fallback.mirror_state_snapshot(cast(SessionState, durable))
                self._hydrated_state_keys.add(state_key)
                return cast(SessionState, durable)
            if state_key not in self._hydrated_state_keys and not (
                scope == "session" and bool(session_id)
                and self._has_complete_live_session(session_id)
            ):
                self._clean_read_unavailable()
            self._mark_dirty(session_id)
            self._dirty_state_keys.add(state_key)
        if session_id in self._unsafe_live_session_ids:
            self._clean_read_unavailable()
        return await self.fallback.update_state(**kwargs)

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
