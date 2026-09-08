from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any, Optional, cast

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
        self._checkpoint_partition: ContextVar[
            tuple[frozenset[str], frozenset[str]] | None
        ] = ContextVar(f"checkpoint_partition_{id(self)}", default=None)
        self._probe_task: asyncio.Task[None] | None = None

    @property
    def degraded(self) -> bool:
        return not self._primary_enabled

    # This service is intentionally live-first and writes two independently
    # sequenced stores.  Even when both children can atomically bind a local
    # seq, the wrapper cannot guarantee one shared physical seq/fact across
    # both writes, so it inherits BaseSessionService's empty canonical storage
    # capabilities and RuntimeEventStore fails closed before either write.

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

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        ok, durable = await self._call_primary("get_session_metadata", session_id)
        if ok and durable is not None:
            return cast(Session, durable)
        return await self.fallback.get_session_metadata(session_id)

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
            return cast(list[Session], durable_sessions or [])
        return await self.fallback.list_sessions(agent_id, user_id, offset, limit)

    async def count_sessions(self, agent_id: str, user_id: Optional[str] = None) -> int:
        ok, total = await self._call_primary("count_sessions", agent_id, user_id)
        if ok:
            return int(total or 0)
        return await self.fallback.count_sessions(agent_id, user_id)

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

    async def get_event_by_id(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        if await self.fallback.get_session_metadata(session_id) is None:
            await self.get_session(session_id)
        return await self.fallback.get_event_by_id(session_id, event_id)

    async def get_events_by_invocation_id(
        self,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        # ResilientSessionService is explicitly live-first: hydrate any durable
        # prefix, then read the indexed in-memory authority used by get_events.
        await self.get_session(session_id)
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
        result: dict[str, Session] = {}
        ok, durable = await self._call_primary("get_sessions_by_ids", session_ids)
        if ok:
            result.update({session.id: session for session in durable or []})
        live = await self.fallback.get_sessions_by_ids(session_ids)
        result.update({session.id: session for session in live})
        return [result[session_id] for session_id in session_ids if session_id in result]

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        result: dict[str, Session] = {}
        ok, durable = await self._call_primary("list_session_metadata", agent_id, user_id)
        if ok:
            result.update({session.id: session for session in durable or []})
        live = await self.fallback.list_session_metadata(agent_id, user_id)
        result.update({session.id: session for session in live})
        sessions = list(result.values())
        sessions.sort(key=lambda item: (item.updated_at, item.created_at, item.id), reverse=True)
        return sessions

    async def _partition_query_session_ids(
        self, session_ids: list[str] | None, agent_id: str | None
    ) -> tuple[list[str], list[str]]:
        if session_ids is None:
            sessions = await self.list_session_metadata(agent_id)
            ids = [session.id for session in sessions]
        else:
            ids = list(dict.fromkeys(session_ids))
        dirty = set(self._dirty_session_ids)
        durable_ids: set[str] = set()
        if ids:
            ok, durable_metadata = await self._call_primary("get_sessions_by_ids", ids)
            if ok:
                durable_ids = {session.id for session in durable_metadata or []}
        live_metadata = await self.fallback.get_sessions_by_ids(ids)
        live_ids = {session.id for session in live_metadata}
        clean_ids = [
            session_id
            for session_id in ids
            if session_id not in dirty and session_id in durable_ids
        ]
        fallback_ids = [
            session_id for session_id in ids if session_id in dirty or session_id in live_ids
        ]
        return clean_ids, fallback_ids

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        clean_ids, live_ids = await self._partition_query_session_ids(
            query.session_ids, query.agent_id
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
                return await self.fallback.query_events(query)
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
        # Deduplicate by physical event id because a hydrated clean session may
        # be present in both children during recovery.
        events = await self.query_events(
            SessionEventQuery(
                **{
                    **query.__dict__,
                    "offset": 0,
                    "limit": 2**31 - 1,
                    "from_start": True,
                }
            )
        )
        return len(events)

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        if session_id not in self._dirty_session_ids:
            ok, stats = await self._call_primary(
                "get_checkpoint_lookup_stats", session_id, run_id, checkpoint_id
            )
            if ok:
                return cast(dict[str, object], stats)
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
            return await self.fallback.scan_checkpoint_events(query)
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
                return await self.fallback.scan_checkpoint_events(query)
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
        return await self.fallback.get_events_for_agent(agent_id, user_id, offset, limit)

    async def count_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        ok, total = await self._call_primary("count_events_for_agent", agent_id, user_id)
        if ok:
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
