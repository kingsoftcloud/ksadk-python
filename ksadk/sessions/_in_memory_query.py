"""Filter, order, and page in-memory session events."""

from __future__ import annotations

import copy

from ksadk.sessions.base import SessionEvent, SessionEventQuery


class _InMemoryQueryMixin:
    async def _query_events(
        self, query: SessionEventQuery, *, count_only: bool
    ) -> list[SessionEvent] | int:
        async with self._lock:
            selected_ids = (
                list(dict.fromkeys(query.session_ids))
                if query.session_ids is not None
                else list(self._sessions)
            )
            allowed_types = set(query.event_types or [])
            allowed_checkpoint_ids = set(query.checkpoint_ids or [])
            events = [
                event
                for session_id in selected_ids
                for session in [self._sessions.get(session_id)]
                if session is not None
                and (query.agent_id is None or session.agent_id == query.agent_id)
                for event in session.events
                if (query.after_seq_id is None or event.seq_id > query.after_seq_id)
                and (query.before_seq_id is None or event.seq_id < query.before_seq_id)
                and (not allowed_types or event.event_type in allowed_types)
                and (
                    not query.receipt_only or (
                        event.event_type == "tool_result"
                        and isinstance((event.metadata or {}).get("tool_receipt"), dict)
                        and bool(str((event.metadata or {}).get("tool_receipt", {}).get("tool_name")
                                     or (event.metadata or {}).get("tool_name") or "").strip())
                    )
                )
                and (query.invocation_id is None or event.invocation_id == query.invocation_id)
                and (
                    query.run_id is None
                    or str((event.metadata or {}).get("run_id") or "") == query.run_id
                    or str(((event.metadata or {}).get("tool_receipt") or {}).get("run_id") or "") == query.run_id
                    or (
                        event.event_type == "continuation.created"
                        and str(
                            ((event.content or {}).get("runtime_event") or {}).get(
                                "run_id", ""
                            )
                            or ""
                        )
                        == query.run_id
                    )
                )
                and (
                    query.checkpoint_id is None
                    or str((event.metadata or {}).get("checkpoint_id") or "")
                    == query.checkpoint_id
                    or str(((event.metadata or {}).get("tool_receipt") or {}).get("checkpoint_id") or "")
                    == query.checkpoint_id
                    or (
                        event.event_type == "continuation.created"
                        and str(
                            ((event.content or {}).get("runtime_event") or {}).get(
                                "continuation_id", ""
                            )
                            or ""
                        )
                        == query.checkpoint_id
                    )
                )
                and (
                    not allowed_checkpoint_ids
                    or str((event.metadata or {}).get("checkpoint_id") or "")
                    in allowed_checkpoint_ids
                    or (
                        event.event_type == "continuation.created"
                        and str(
                            ((event.content or {}).get("runtime_event") or {}).get(
                                "continuation_id", ""
                            )
                            or ""
                        )
                        in allowed_checkpoint_ids
                    )
                )
            ]
            if count_only:
                return len(events)
            if query.order_by_seq:
                events.sort(key=lambda event: (event.session_id, event.seq_id, event.id))
            else:
                events.sort(
                    key=lambda event: (
                        event.timestamp,
                        event.session_id,
                        event.seq_id,
                        event.id,
                    )
                )
            if query.from_start:
                return copy.deepcopy(events[query.offset : query.offset + query.limit])
            end = max(len(events) - query.offset, 0)
            return copy.deepcopy(events[max(end - query.limit, 0) : end])
