"""Read-side primary/fallback partitioning for resilient sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Optional, cast

from ksadk.sessions.base import CheckpointEventQuery, Session, SessionEvent, SessionEventQuery
from ksadk.sessions.errors import CheckpointScanRestartRequired, HistoryReloadRequired
from ksadk.sessions.resilience import is_session_backend_failure


class _ResilientReadMixin:
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
                "get_events_by_invocation_id",
                session_id,
                invocation_id,
                after_seq_id=after_seq_id,
                before_seq_id=before_seq_id,
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

    async def get_session_header_with_watermark(
        self, session_id: str
    ) -> tuple[Session | None, int]:
        if session_id in self._deleted_session_ids:
            return None, 0
        if session_id not in self._dirty_session_ids and callable(
            getattr(self.primary, "get_session_header_with_watermark", None)
        ):
            ok, value = await self._call_primary("get_session_header_with_watermark", session_id)
            if not ok:
                self._clean_read_unavailable()
            return value
        return await self.get_session_header(session_id), await self.get_max_seq_id(session_id)

    async def get_message_event_window(
        self,
        session_id: str,
        *,
        limit: int,
        from_start: bool = False,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        byte_limit: int = 64 * 1024,
    ) -> list[SessionEvent]:
        if session_id in self._deleted_session_ids:
            return []
        if session_id not in self._dirty_session_ids and callable(
            getattr(self.primary, "get_message_event_window", None)
        ):
            ok, value = await self._call_primary(
                "get_message_event_window",
                session_id,
                limit=limit,
                from_start=from_start,
                after_seq_id=after_seq_id,
                before_seq_id=before_seq_id,
                byte_limit=byte_limit,
            )
            if not ok:
                self._clean_read_unavailable()
            return cast(list[SessionEvent], value)
        if from_start:
            return await self.query_events(
                SessionEventQuery(
                    session_ids=[session_id],
                    from_start=True,
                    order_by_seq=True,
                    limit=limit,
                    after_seq_id=after_seq_id,
                    before_seq_id=before_seq_id,
                )
            )
        return await self.get_events(
            session_id,
            limit=limit,
            after_seq_id=after_seq_id,
            before_seq_id=before_seq_id,
        )

    async def has_events(
        self,
        session_id: str,
        *,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
    ) -> bool:
        if session_id in self._deleted_session_ids:
            return False
        if session_id in self._dirty_session_ids:
            return await self.fallback.has_events(
                session_id,
                after_seq_id=after_seq_id,
                before_seq_id=before_seq_id,
            )
        if not callable(getattr(self.primary, "has_events", None)):
            return await super().has_events(
                session_id,
                after_seq_id=after_seq_id,
                before_seq_id=before_seq_id,
            )
        ok, value = await self._call_primary(
            "has_events",
            session_id,
            after_seq_id=after_seq_id,
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
        result.update(
            {
                session.id: session
                for session in live
                if not ok or session.id in self._dirty_session_ids
            }
        )
        if not ok and any(sid not in self._dirty_session_ids for sid in session_ids):
            self._clean_read_unavailable()
        return [
            result[session_id]
            for session_id in session_ids
            if session_id in result and session_id not in self._deleted_session_ids
        ]

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
        result.update(
            {
                session.id: session
                for session in live
                if not ok or session.id in self._dirty_session_ids
            }
        )
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
            ids = [
                sid for sid in dict.fromkeys(session_ids) if sid not in self._deleted_session_ids
            ]
        dirty = set(self._dirty_session_ids)
        durable_ids: set[str] = set()
        if ids:
            ok, durable_metadata = await self._call_primary("get_session_ids_by_ids", ids, agent_id)
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
            session_id
            for session_id in ids
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
        return await self.fallback.get_checkpoint_lookup_stats(session_id, run_id, checkpoint_id)

    async def scan_checkpoint_events(self, query: CheckpointEventQuery) -> list[SessionEvent]:
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
        self,
        session_id: str,
        *,
        page_size: int = 256,
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
        token = self._checkpoint_partition.set((frozenset(clean_ids), frozenset(live_ids)))
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

    async def get_checkpoint_stats(self, keys: list[tuple[str, str, str]]) -> dict[str, object]:
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
                raise CheckpointScanRestartRequired("primary failed while reading checkpoint stats")
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
