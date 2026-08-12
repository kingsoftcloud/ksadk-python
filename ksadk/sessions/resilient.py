from __future__ import annotations

import asyncio
import copy
import logging
from contextlib import aclosing
from contextvars import ContextVar
from enum import Enum
from typing import Any, AsyncIterator, Optional, cast

from ksadk.sessions.base import (
    BaseSessionService,
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    SessionState,
)
from ksadk.sessions.errors import CheckpointScanRestartRequired
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.resilience import is_session_backend_failure

logger = logging.getLogger(__name__)


class _PrimaryCallStatus(Enum):
    AVAILABLE_RESULT = "available_result"
    BACKEND_FAILURE = "backend_failure"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"


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
        self._dirty_session_ids: set[str] = set()
        self._probe_task: asyncio.Task[None] | None = None
        self._checkpoint_partition: ContextVar[
            tuple[frozenset[str], frozenset[str]] | None
        ] = ContextVar(f"checkpoint_partition_{id(self)}", default=None)

    @property
    def degraded(self) -> bool:
        return not self._primary_enabled

    async def _call_primary(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> tuple[_PrimaryCallStatus, Any]:
        if not self._primary_enabled:
            return _PrimaryCallStatus.BACKEND_FAILURE, None
        try:
            method = getattr(self.primary, method_name)
            return _PrimaryCallStatus.AVAILABLE_RESULT, await method(*args, **kwargs)
        except NotImplementedError as exc:
            return _PrimaryCallStatus.CAPABILITY_UNSUPPORTED, exc
        except Exception as exc:
            if not is_session_backend_failure(exc):
                raise
            self._disable_primary(exc)
            return _PrimaryCallStatus.BACKEND_FAILURE, None

    @staticmethod
    def _raise_if_capability_unsupported(status: _PrimaryCallStatus, result: Any) -> None:
        if status is _PrimaryCallStatus.CAPABILITY_UNSUPPORTED:
            raise cast(NotImplementedError, result)

    def _disable_primary(self, exc: Exception) -> None:
        if not self._primary_enabled:
            return
        self._primary_enabled = False
        chain: list[BaseException] = []
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(current)
            current = current.__cause__ or current.__context__
        reason_code = (
            "DEPENDENCY_MISSING"
            if any(isinstance(item, (ModuleNotFoundError, ImportError)) for item in chain)
            else "SESSION_STORE_UNREACHABLE"
        )
        logger.error(
            "KSADK session persistence degraded; using in-memory live session",
            extra={
                "session_backend_state": "degraded",
                "session_backend": type(self.primary).__name__,
                "session_backend_reason_code": reason_code,
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
            await self.refresh_persistence_capability()

    async def refresh_persistence_capability(self) -> bool:
        """Probe a degraded primary immediately for app capability refresh."""
        if self._primary_enabled:
            return True
        try:
            await self.primary.get_session("__ksadk_probe__")
        except Exception:
            return False
        self._primary_enabled = True
        logger.info(
            "KSADK session persistence recovered; durable backend re-enabled",
            extra={
                "session_backend_state": "recovered",
                "session_backend": type(self.primary).__name__,
                "session_backend_reason_code": "READY",
            },
        )
        return True

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
            status, durable = await self._call_primary("get_session", session_id)
            if status is _PrimaryCallStatus.AVAILABLE_RESULT and durable is not None:
                return await self._hydrate(durable)
            existing = await self.fallback.get_session(session_id)
            if existing is not None:
                return existing

        live = await self.fallback.create_session(agent_id, user_id, session_id=session_id)
        status, durable = await self._call_primary(
            "create_session",
            agent_id,
            user_id,
            session_id=live.id,
        )
        if status is _PrimaryCallStatus.AVAILABLE_RESULT and durable is not None:
            self._primary_session_ids.add(durable.id)
            return await self._hydrate(durable)
        return live

    async def get_session(self, session_id: str) -> Optional[Session]:
        live = await self.fallback.get_session(session_id)
        status, durable = await self._call_primary("get_session", session_id)
        if status is _PrimaryCallStatus.AVAILABLE_RESULT and durable is not None:
            return await self._hydrate(durable)
        return live

    async def list_sessions(
        self,
        agent_id: Optional[str],
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        status, durable_sessions = await self._call_primary(
            "list_sessions",
            agent_id,
            user_id,
            None,
            None,
        )
        if status is _PrimaryCallStatus.AVAILABLE_RESULT:
            for session in durable_sessions or []:
                await self._hydrate(session)
            # Preserve the durable backend's ordering before applying pagination.
            # Hydrating into the in-memory fallback assigns fresh timestamps, so
            # reading the page back from that store would reorder equal-age
            # sessions and previously applied the requested page twice.
            live_sessions = await self.fallback.list_sessions(agent_id, user_id)
            live_by_id = {session.id: session for session in live_sessions}
            durable_ids = {session.id for session in durable_sessions or []}
            sessions = [
                live_by_id.get(session.id, session)
                if session.id in self._dirty_session_ids
                else session
                for session in durable_sessions or []
            ]
            sessions.extend(session for session in live_sessions if session.id not in durable_ids)
            sessions.sort(
                key=lambda item: (item.updated_at, item.created_at, item.id), reverse=True
            )
            start = offset or 0
            end = None if limit is None else start + limit
            return sessions[start:end]
        return cast(list[Session], await self.fallback.list_sessions(agent_id, user_id, offset, limit))

    async def count_sessions(self, agent_id: str, user_id: Optional[str] = None) -> int:
        sessions = await self.list_sessions(agent_id, user_id)
        return len(sessions)

    async def delete_session(self, session_id: str) -> bool:
        deleted = await self.fallback.delete_session(session_id)
        status, durable_deleted = await self._call_primary("delete_session", session_id)
        if status is _PrimaryCallStatus.AVAILABLE_RESULT:
            self._primary_session_ids.discard(session_id)
        return (
            deleted or bool(durable_deleted)
            if status is _PrimaryCallStatus.AVAILABLE_RESULT
            else deleted
        )

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
            status, durable = await self._call_primary("get_session", session_id)
            if status is not _PrimaryCallStatus.AVAILABLE_RESULT:
                return
            if durable is None:
                live = await self.fallback.get_session(session_id)
                if live is None:
                    return
                status, durable = await self._call_primary(
                    "create_session",
                    live.agent_id,
                    live.user_id,
                    session_id=live.id,
                )
                if status is not _PrimaryCallStatus.AVAILABLE_RESULT or durable is None:
                    return
            self._primary_session_ids.add(session_id)

    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        if await self.fallback.get_session(session_id) is None:
            await self.get_session(session_id)
        if event.event_type == "run_checkpoint":
            checkpoint_event = copy.deepcopy(event)
            await self._ensure_primary_session(session_id)
            status, _ = await self._call_primary(
                "append_event", session_id, checkpoint_event
            )
            if status is not _PrimaryCallStatus.AVAILABLE_RESULT:
                checkpoint_event.metadata.update(
                    {
                        "is_resumable": False,
                        "resume_status": "disabled",
                        "durable": False,
                        "resume_disabled_reason": (
                            "Checkpoint was not written to durable persistence"
                        ),
                    }
                )
                self._dirty_session_ids.add(session_id)
            return await self.fallback.append_event(session_id, checkpoint_event)
        live = await self.fallback.append_event(session_id, event)
        await self._ensure_primary_session(session_id)
        status, _ = await self._call_primary("append_event", session_id, event)
        if status is not _PrimaryCallStatus.AVAILABLE_RESULT:
            self._dirty_session_ids.add(session_id)
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

    async def get_sessions_by_ids(self, session_ids: list[str]) -> list[Session]:
        status, durable = await self._call_primary("get_sessions_by_ids", session_ids)
        self._raise_if_capability_unsupported(status, durable)
        if status is _PrimaryCallStatus.BACKEND_FAILURE:
            return await self.fallback.get_sessions_by_ids(session_ids)
        durable_by_id = {session.id: session for session in durable or []}
        live_ids = [
            session_id
            for session_id in session_ids
            if session_id in self._dirty_session_ids or session_id not in durable_by_id
        ]
        live = await self.fallback.get_sessions_by_ids(live_ids)
        live_by_id = {session.id: session for session in live}
        # A degraded-era session, or a session with unsynchronised writes, must
        # remain visible through its live metadata even after primary recovery.
        return [
            live_by_id[session_id]
            if session_id in live_by_id and (
                session_id in self._dirty_session_ids or session_id not in durable_by_id
            )
            else durable_by_id[session_id]
            for session_id in session_ids
            if session_id in live_by_id or session_id in durable_by_id
        ]

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        sessions = await self.get_sessions_by_ids([session_id])
        return sessions[0] if sessions else None

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        clean_ids, live_ids = await self._partition_batch_session_ids(None, agent_id)
        primary: list[Session] = []
        if clean_ids:
            status, result = await self._call_primary("get_sessions_by_ids", clean_ids)
            self._raise_if_capability_unsupported(status, result)
            if status is _PrimaryCallStatus.AVAILABLE_RESULT:
                primary = cast(list[Session], result or [])
            else:
                live_ids = [session.id for session in await self.fallback.list_session_metadata(agent_id)]
        live = await self.fallback.get_sessions_by_ids(live_ids) if live_ids else []
        return [session for session in [*primary, *live] if user_id is None or session.user_id == user_id]

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        clean_ids, live_ids = await self._partition_batch_session_ids(query.session_ids, query.agent_id)
        window = query.offset + query.limit
        clean = SessionEventQuery(**{**query.__dict__, "session_ids": clean_ids, "offset": 0, "limit": window})
        live = SessionEventQuery(**{**query.__dict__, "session_ids": live_ids, "offset": 0, "limit": window})
        durable_events: list[SessionEvent] = []
        if clean_ids:
            status, durable_events = await self._call_primary("query_events", clean)
            self._raise_if_capability_unsupported(status, durable_events)
            if status is _PrimaryCallStatus.BACKEND_FAILURE:
                return await self.fallback.query_events(query)
        live_events = await self.fallback.query_events(live) if live_ids else []
        merged = list(durable_events or []) + list(live_events)
        if query.order_by_seq:
            merged.sort(key=lambda event: (event.session_id, event.seq_id, event.id))
        else:
            merged.sort(
                key=lambda event: (
                    event.timestamp,
                    event.session_id,
                    event.seq_id,
                    event.id,
                )
            )
        if query.from_start:
            return merged[query.offset : query.offset + query.limit]
        end = max(len(merged) - query.offset, 0)
        return merged[max(end - query.limit, 0) : end]

    async def count_event_query(self, query: SessionEventQuery) -> int:
        clean_ids, live_ids = await self._partition_batch_session_ids(query.session_ids, query.agent_id)
        clean = SessionEventQuery(**{**query.__dict__, "session_ids": clean_ids})
        live = SessionEventQuery(**{**query.__dict__, "session_ids": live_ids})
        durable_count = 0
        if clean_ids:
            status, durable_count = await self._call_primary("count_event_query", clean)
            self._raise_if_capability_unsupported(status, durable_count)
            if status is _PrimaryCallStatus.BACKEND_FAILURE:
                return await self.fallback.count_event_query(query)
        live_count = await self.fallback.count_event_query(live) if live_ids else 0
        return int(durable_count or 0) + int(live_count or 0)

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        clean_ids, live_ids = await self._partition_batch_session_ids([session_id], None)
        if live_ids:
            return await self.fallback.get_checkpoint_lookup_stats(session_id, run_id, checkpoint_id)
        if clean_ids:
            status, stats = await self._call_primary(
                "get_checkpoint_lookup_stats", session_id, run_id, checkpoint_id
            )
            self._raise_if_capability_unsupported(status, stats)
            if status is _PrimaryCallStatus.AVAILABLE_RESULT:
                return stats
        return await self.fallback.get_checkpoint_lookup_stats(session_id, run_id, checkpoint_id)

    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        clean_ids, live_ids = await self._partition_batch_session_ids(
            query.session_ids, query.agent_id
        )
        if clean_ids and not live_ids:
            clean_query = CheckpointEventQuery(
                **{**query.__dict__, "session_ids": clean_ids}
            )
            status, result = await self._call_primary(
                "scan_checkpoint_events", clean_query
            )
            self._raise_if_capability_unsupported(status, result)
            if status is _PrimaryCallStatus.AVAILABLE_RESULT:
                return cast(list[SessionEvent], result or [])
            return await self.fallback.scan_checkpoint_events(query)
        if live_ids and not clean_ids:
            return await self.fallback.scan_checkpoint_events(
                CheckpointEventQuery(**{**query.__dict__, "session_ids": live_ids})
            )
        backend_offsets = {"clean": 0, "live": 0}
        pages: dict[str, list[SessionEvent]] = {"clean": [], "live": []}
        indexes = {"clean": 0, "live": 0}
        exhausted = {"clean": not clean_ids, "live": not live_ids}

        async def load(kind: str) -> bool:
            ids = clean_ids if kind == "clean" else live_ids
            if exhausted[kind]:
                return True
            page_query = CheckpointEventQuery(
                **{**query.__dict__, "session_ids": ids, "offset": backend_offsets[kind], "limit": 50}
            )
            if kind == "clean":
                status, result = await self._call_primary("scan_checkpoint_events", page_query)
                self._raise_if_capability_unsupported(status, result)
                if status is _PrimaryCallStatus.BACKEND_FAILURE:
                    return False
                page = cast(list[SessionEvent], result or [])
            else:
                page = await self.fallback.scan_checkpoint_events(page_query)
            pages[kind] = page
            indexes[kind] = 0
            backend_offsets[kind] += len(page)
            exhausted[kind] = len(page) < 50
            return True

        if not await load("clean"):
            return await self.fallback.scan_checkpoint_events(query)
        await load("live")
        skipped = 0
        result: list[SessionEvent] = []
        while len(result) < query.limit:
            for kind in ("clean", "live"):
                if indexes[kind] >= len(pages[kind]) and not exhausted[kind]:
                    if not await load(kind):
                        return await self.fallback.scan_checkpoint_events(query)
            candidates = [
                (event.timestamp, event.session_id, event.seq_id, event.id, kind, event)
                for kind in ("clean", "live")
                for event in pages[kind][indexes[kind] : indexes[kind] + 1]
            ]
            if not candidates:
                break
            *_, kind, event = min(candidates)
            indexes[kind] += 1
            if skipped < query.offset:
                skipped += 1
            else:
                result.append(event)
        return result

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        clean_ids, live_ids = await self._partition_batch_session_ids(
            query.session_ids, query.agent_id
        )
        partition_token = self._checkpoint_partition.set(
            (frozenset(clean_ids), frozenset(live_ids))
        )
        partitioned_batches = self._iter_checkpoint_event_chunks_for_partition(
            query, clean_ids, live_ids
        )
        try:
            async with aclosing(partitioned_batches) as batches:
                async for batch in batches:
                    yield batch
        finally:
            self._checkpoint_partition.reset(partition_token)

    async def _iter_checkpoint_event_chunks_for_partition(
        self,
        query: CheckpointEventQuery,
        clean_ids: list[str],
        live_ids: list[str],
    ) -> AsyncIterator[list[SessionEvent]]:
        if not clean_ids:
            live_query = CheckpointEventQuery(
                **{**query.__dict__, "session_ids": live_ids}
            )
            async with aclosing(
                self.fallback.iter_checkpoint_event_chunks(live_query)
            ) as fallback_batches:
                async for batch in fallback_batches:
                    yield batch
            return

        if not live_ids:
            clean_query = CheckpointEventQuery(
                **{**query.__dict__, "session_ids": clean_ids}
            )
            clean_batches = self.primary.iter_checkpoint_event_chunks(
                clean_query
            ).__aiter__()
            try:
                async for batch in clean_batches:
                    yield batch
            except NotImplementedError:
                raise
            except Exception as exc:
                if not is_session_backend_failure(exc):
                    raise
                self._disable_primary(exc)
                raise CheckpointScanRestartRequired(
                    "primary failed during clean checkpoint scan"
                ) from exc
            finally:
                close_clean_batches = getattr(clean_batches, "aclose", None)
                if callable(close_clean_batches):
                    await close_clean_batches()
            return

        clean_query = CheckpointEventQuery(
            **{
                **query.__dict__,
                "session_ids": clean_ids,
                "offset": 0,
                "limit": 50,
            }
        )
        live_query = CheckpointEventQuery(
            **{
                **query.__dict__,
                "session_ids": live_ids,
                "offset": 0,
                "limit": 50,
            }
        )
        backend_iterators = {
            "clean": self.primary.iter_checkpoint_event_chunks(clean_query).__aiter__(),
            "live": self.fallback.iter_checkpoint_event_chunks(live_query).__aiter__(),
        }
        pages: dict[str, list[SessionEvent]] = {"clean": [], "live": []}
        indexes = {"clean": 0, "live": 0}
        exhausted = {"clean": False, "live": False}
        skipped = 0

        async def load(kind: str) -> bool:
            try:
                page = await anext(backend_iterators[kind])
            except StopAsyncIteration:
                page = []
            except NotImplementedError:
                raise
            except Exception as exc:
                if kind != "clean" or not is_session_backend_failure(exc):
                    raise
                self._disable_primary(exc)
                return False
            pages[kind] = page
            indexes[kind] = 0
            exhausted[kind] = len(page) < 50
            return True

        try:
            for kind in ("clean", "live"):
                if not await load(kind):
                    raise CheckpointScanRestartRequired(
                        "primary failed during mixed checkpoint scan"
                    )

            page: list[SessionEvent] = []
            while True:
                for kind in ("clean", "live"):
                    if indexes[kind] >= len(pages[kind]) and not exhausted[kind]:
                        if not await load(kind):
                            raise CheckpointScanRestartRequired(
                                "primary failed during mixed checkpoint scan"
                            )
                candidates = [
                    (event.timestamp, event.session_id, event.seq_id, event.id, kind, event)
                    for kind in ("clean", "live")
                    for event in pages[kind][indexes[kind] : indexes[kind] + 1]
                ]
                if not candidates:
                    if page:
                        yield page
                    return
                *_, kind, event = min(candidates)
                indexes[kind] += 1
                if skipped < query.offset:
                    skipped += 1
                    continue
                page.append(event)
                if len(page) == query.limit:
                    yield page
                    page = []
        finally:
            for backend_iterator in backend_iterators.values():
                close_iterator = getattr(backend_iterator, "aclose", None)
                if callable(close_iterator):
                    await close_iterator()

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        unique_keys = list(dict.fromkeys(keys))
        requested_session_ids = list(dict.fromkeys(key[0] for key in unique_keys))
        active_partition = self._checkpoint_partition.get()
        if active_partition is None:
            clean_ids, live_ids = await self._partition_batch_session_ids(
                requested_session_ids, None
            )
        else:
            clean_set, live_set = active_partition
            clean_ids = [
                session_id for session_id in requested_session_ids
                if session_id in clean_set
            ]
            live_ids = [
                session_id for session_id in requested_session_ids
                if session_id in live_set
            ]
        clean_set, live_set = set(clean_ids), set(live_ids)
        clean_keys = [key for key in unique_keys if key[0] in clean_set]
        live_keys = [key for key in unique_keys if key[0] in live_set]
        durable: dict[str, object] = {"audits": {}, "latest_seq_ids": {}}
        if clean_keys:
            status, value = await self._call_primary("get_checkpoint_stats", clean_keys)
            self._raise_if_capability_unsupported(status, value)
            if status is _PrimaryCallStatus.BACKEND_FAILURE:
                raise CheckpointScanRestartRequired(
                    "primary failed while reading checkpoint stats"
                )
            durable = cast(dict[str, object], value or durable)
        live = await self.fallback.get_checkpoint_stats(live_keys) if live_keys else {
            "audits": {}, "latest_seq_ids": {}
        }
        return {
            "audits": {**cast(dict, durable["audits"]), **cast(dict, live["audits"])},
            "latest_seq_ids": {
                **cast(dict, durable["latest_seq_ids"]),
                **cast(dict, live["latest_seq_ids"]),
            },
        }

    async def get_events_batch(
        self,
        session_ids: list[str] | None = None,
        *,
        agent_id: str | None = None,
        offset: int = 0,
        limit: int = 1000,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        event_types: list[str] | None = None,
        from_start: bool = False,
    ) -> list[SessionEvent]:
        clean_ids, live_ids = await self._partition_batch_session_ids(session_ids, agent_id)
        window = offset + limit
        durable_events: list[SessionEvent] = []
        if clean_ids:
            status, durable = await self._call_primary(
                "get_events_batch", clean_ids, agent_id=agent_id, offset=0, limit=window,
                after_seq_id=after_seq_id, before_seq_id=before_seq_id, event_types=event_types,
                from_start=from_start,
            )
            self._raise_if_capability_unsupported(status, durable)
            if status is _PrimaryCallStatus.BACKEND_FAILURE:
                return await self.fallback.get_events_batch(
                    session_ids, agent_id=agent_id, offset=offset, limit=limit,
                    after_seq_id=after_seq_id, before_seq_id=before_seq_id, event_types=event_types,
                    from_start=from_start,
                )
            durable_events = cast(list[SessionEvent], durable or [])
        live_events = await self.fallback.get_events_batch(
            live_ids, agent_id=agent_id, offset=0, limit=window,
            after_seq_id=after_seq_id, before_seq_id=before_seq_id, event_types=event_types,
            from_start=from_start,
        ) if live_ids else []
        merged = durable_events + live_events
        merged.sort(key=lambda event: (event.timestamp, event.session_id, event.seq_id, event.id))
        if from_start:
            return merged[offset : offset + limit]
        end = max(len(merged) - offset, 0)
        return merged[max(end - limit, 0) : end]

    async def count_events_batch(
        self,
        session_ids: list[str] | None = None,
        *,
        agent_id: str | None = None,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        event_types: list[str] | None = None,
    ) -> int:
        clean_ids, live_ids = await self._partition_batch_session_ids(session_ids, agent_id)
        durable_total = 0
        if clean_ids:
            status, durable = await self._call_primary(
                "count_events_batch", clean_ids, agent_id=agent_id,
                after_seq_id=after_seq_id, before_seq_id=before_seq_id, event_types=event_types,
            )
            self._raise_if_capability_unsupported(status, durable)
            if status is _PrimaryCallStatus.BACKEND_FAILURE:
                return await self.fallback.count_events_batch(
                    session_ids, agent_id=agent_id, after_seq_id=after_seq_id,
                    before_seq_id=before_seq_id, event_types=event_types,
                )
            durable_total = int(durable or 0)
        live_total = await self.fallback.count_events_batch(
            live_ids, agent_id=agent_id, after_seq_id=after_seq_id,
            before_seq_id=before_seq_id, event_types=event_types,
        ) if live_ids else 0
        return durable_total + live_total

    async def _partition_batch_session_ids(
        self, session_ids: list[str] | None, agent_id: str | None
    ) -> tuple[list[str], list[str]]:
        """Partition metadata only: durable-clean sessions vs dirty/live-only.

        This intentionally does not call ``get_session``/``_hydrate`` and thus
        cannot pull historical event arrays into the live fallback.
        """
        if session_ids is not None:
            requested = list(session_ids)
            status, primary_sessions = await self._call_primary("get_sessions_by_ids", requested)
            self._raise_if_capability_unsupported(status, primary_sessions)
            if status is _PrimaryCallStatus.BACKEND_FAILURE:
                return [], requested
            primary_ids = {session.id for session in primary_sessions}
            live_ids = [session_id for session_id in requested if session_id in self._dirty_session_ids or session_id not in primary_ids]
            return [session_id for session_id in requested if session_id in primary_ids and session_id not in self._dirty_session_ids], live_ids
        status, primary_sessions = await self._call_primary("list_session_metadata", agent_id, None)
        self._raise_if_capability_unsupported(status, primary_sessions)
        if status is _PrimaryCallStatus.BACKEND_FAILURE:
            return [], [session.id for session in await self.fallback.list_session_metadata(agent_id, user_id=None)]
        fallback_sessions = await self.fallback.list_session_metadata(agent_id, user_id=None)
        primary_ids = {session.id for session in primary_sessions}
        fallback_ids = {session.id for session in fallback_sessions}
        live_ids = sorted((fallback_ids - primary_ids) | (fallback_ids & self._dirty_session_ids))
        clean_ids = sorted(primary_ids - set(live_ids))
        return clean_ids, live_ids

    async def get_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        status, events = await self._call_primary(
            "get_events_for_agent", agent_id, user_id, offset, limit
        )
        self._raise_if_capability_unsupported(status, events)
        if status is _PrimaryCallStatus.AVAILABLE_RESULT:
            return cast(list[SessionEvent], events)
        return await self.fallback.get_events_for_agent(agent_id, user_id, offset, limit)

    async def count_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        status, total = await self._call_primary(
            "count_events_for_agent", agent_id, user_id
        )
        self._raise_if_capability_unsupported(status, total)
        if status is _PrimaryCallStatus.AVAILABLE_RESULT:
            return int(total or 0)
        return await self.fallback.count_events_for_agent(agent_id, user_id)

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
        status, durable = await self._call_primary(
            "get_state",
            agent_id,
            user_id,
            session_id,
            scope,
        )
        if status is _PrimaryCallStatus.AVAILABLE_RESULT and durable is not None:
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
