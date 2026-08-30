"""Canonical replay plus the temporary mixed-schema legacy read boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from ksadk.events.canonical import (
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    RunCompleted,
    RuntimeEvent,
)
from ksadk.events.canonical_store import RuntimeEventStore, session_event_to_runtime_event
from ksadk.events.content import ArtifactContent, TextContent
from ksadk.events.reducer import RunProjection, StreamReducer
from ksadk.events.v1_compat import (
    EventTypeV1,
    RuntimeEventV1,
    RuntimeEventV1Parser,
    RuntimeEventV1ProjectionContext,
    V1ProjectionContextRequiredError,
    project_to_v1,
)
from ksadk.sessions.base import Session, SessionEvent


class LegacyRunNotResumableError(RuntimeError):
    status_code = 409
    code = "legacy_run_not_resumable"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"legacy run {run_id!r} cannot resume as a canonical run")
        self.run_id = run_id


@dataclass(frozen=True)
class LegacySessionEventGroup:
    """One indivisible public delivery group sharing a canonical session seq."""

    seq: int
    events: tuple[dict[str, Any], ...]


class RuntimeEventV1ContextProvider(Protocol):
    """Supply protocol-specific v1 refs at the temporary legacy read boundary."""

    def __call__(
        self,
        session: Session,
        event: RuntimeEvent,
        projection: RunProjection,
    ) -> RuntimeEventV1ProjectionContext: ...


class _LegacySessionProjector:
    """Session-scoped reducer/parser state shared by hydrate and incremental tail."""

    def __init__(
        self,
        session: Session,
        context_provider: RuntimeEventV1ContextProvider | None,
    ) -> None:
        self.session = session
        self.context_provider = context_provider
        self.reducers: dict[str, StreamReducer] = {}
        self.parser = RuntimeEventV1Parser()

    def project(self, raw: SessionEvent) -> list[dict[str, Any]]:
        canonical = session_event_to_runtime_event(raw)
        if canonical is None:
            return [_legacy_raw_payload(raw)]
        reducer = self.reducers.setdefault(canonical.run_id, StreamReducer())
        reducer.apply(canonical)
        projection = reducer.snapshot()
        context = _legacy_projection_context(
            self.session,
            canonical,
            projection,
            provider=self.context_provider,
        )
        v1_events = project_to_v1(canonical, context=context)
        for event in v1_events:
            self.parser.feed(event)
        return _canonical_legacy_payloads(
            canonical,
            v1_events,
            projection=projection,
        )


async def replay_projection(
    store: RuntimeEventStore,
    session_id: str,
    *,
    run_id: str,
    through_seq: int | None = None,
    settle_open: bool = False,
) -> RunProjection:
    """Rebuild one run exclusively through the live ``StreamReducer.apply`` path.

    With ``settle_open`` the projection never exposes an unfinished stream to a
    cold reader: an open run at the read boundary is completed in-memory with
    the same deterministic outcomes :func:`ksadk.events.cold_recovery.settle_finding`
    would persist, so live readers (run still executing) and cold readers
    (process gone) both observe a conformant projection without consumers
    special-casing a dangling stream. The synthesized events are applied to the
    in-memory reducer only; persisting them is :func:`cold_recovery.recover_session`'s
    job.
    """

    reducer = StreamReducer()
    for event in await store.list(session_id, run_id=run_id):
        if through_seq is not None and event.seq > through_seq:
            break
        reducer.apply(event)
    if settle_open and (snapshot := reducer.snapshot()).status in (None, "running"):
        from ksadk.events.cold_recovery import OpenItem, RecoveryFinding, settle_finding

        finding = RecoveryFinding(
            run_id=run_id,
            scope_id=_root_scope_for(run_id),
            resumable=any(c.resumable for c in snapshot.continuations),
            continuation_id=(
                snapshot.continuations[-1].continuation_id
                if snapshot.continuations
                else None
            ),
            open_items=[
                OpenItem(
                    scope_id=item.scope_id,
                    item_id=item.item_id,
                    item_kind=item.item_kind,
                )
                for item in snapshot.items
                if item.status == "open"
            ],
            last_seq=snapshot.last_seq or 0,
        )
        # 冷读者默认不允许接管执行(resume 裁决属执行层),只做确定性结算。
        for event in settle_finding(
            finding, session_id, allow_resume=False, timestamp=0.0
        ):
            reducer.apply(event)
    return reducer.snapshot()


def _root_scope_for(run_id: str) -> str:
    return f"run:{run_id}"


async def list_legacy_session_events(
    store: RuntimeEventStore,
    session_id: str,
    *,
    session_service: Any | None = None,
    after_seq: int = 0,
    before_seq: int | None = None,
    limit: int | None = None,
    context_provider: RuntimeEventV1ContextProvider | None = None,
) -> list[dict[str, Any]]:
    """Merge historical rows and v2 read projections in one physical seq space.

    One canonical event may project to several public rows.  Rows with the same
    physical seq are selected as one atomic pagination/delivery group.
    """

    service = session_service or store.session_service
    session = await service.get_session_metadata(session_id)
    if session is None:
        return []
    raw_events = await service.get_events(session_id, before_seq_id=before_seq)
    raw_events.sort(key=lambda item: item.seq_id)
    projector = _LegacySessionProjector(session, context_provider)
    groups: list[tuple[int, list[dict[str, Any]]]] = []

    for raw in raw_events:
        projected = projector.project(raw)
        if projected:
            groups.append((raw.seq_id, projected))

    groups = [
        group
        for group in groups
        if group[0] > after_seq and (before_seq is None or group[0] < before_seq)
    ]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        selected: list[tuple[int, list[dict[str, Any]]]] = []
        selected_size = 0
        for group in reversed(groups):
            selected.append(group)
            selected_size += len(group[1])
            if selected_size >= limit:
                break
        groups = list(reversed(selected))
    return [payload for _seq, group in groups for payload in group]


async def subscribe_legacy_session_events(
    store: RuntimeEventStore,
    session_id: str,
    *,
    session_service: Any | None = None,
    after_seq: int = 0,
    poll_interval: float = 0.25,
    timeout: float = 5 * 60,
    context_provider: RuntimeEventV1ContextProvider | None = None,
) -> AsyncIterator[LegacySessionEventGroup]:
    """Yield whole projection groups and advance only after each group delivery."""

    service = session_service or store.session_service
    session = await service.get_session_metadata(session_id)
    if session is None:
        return
    cursor = int(after_seq or 0)
    projector = _LegacySessionProjector(session, context_provider)
    if cursor > 0:
        prefix = await service.get_events(session_id, before_seq_id=cursor + 1)
        prefix.sort(key=lambda item: item.seq_id)
        for raw in prefix:
            projector.project(raw)

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rows = await service.get_events(session_id, after_seq_id=cursor)
        rows.sort(key=lambda item: item.seq_id)
        for raw in rows:
            projected = projector.project(raw)
            if projected:
                yield LegacySessionEventGroup(seq=raw.seq_id, events=tuple(projected))
            cursor = raw.seq_id
        if asyncio.get_running_loop().time() >= deadline:
            return
        await asyncio.sleep(poll_interval)


async def ensure_canonical_resume_allowed(
    store: RuntimeEventStore,
    session_id: str,
    run_id: str,
    *,
    session_service: Any | None = None,
) -> None:
    """Reject v1-only run resume instead of silently producing an empty v2 run."""

    if await store.list(session_id, run_id=run_id):
        return
    service = session_service or store.session_service
    for event in await service.get_events_by_invocation_id(session_id, run_id):
        if session_event_to_runtime_event(event) is None:
            raise LegacyRunNotResumableError(run_id)


def _legacy_raw_payload(event: SessionEvent) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "EventId": event.id,
        "SessionId": event.session_id,
        "Author": event.author,
        "EventType": event.event_type,
        "Content": event.content,
        "Timestamp": event.timestamp,
        "SeqId": event.seq_id,
        "Metadata": event.metadata,
    }
    if event.invocation_id:
        payload["InvocationId"] = event.invocation_id
    if event.state_delta:
        payload["StateDelta"] = event.state_delta
    return payload


def _legacy_projection_context(
    session: Session,
    event: RuntimeEvent,
    projection: RunProjection,
    *,
    provider: RuntimeEventV1ContextProvider | None,
) -> RuntimeEventV1ProjectionContext:
    requirement = _protocol_context_requirement(event)
    if provider is None:
        if requirement is not None:
            raise V1ProjectionContextRequiredError(
                f"{requirement} requires a RuntimeEvent v1 context provider"
            )
        return RuntimeEventV1ProjectionContext.from_projection(
            projection,
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
        )

    context = provider(session, event, projection)
    if not isinstance(context, RuntimeEventV1ProjectionContext):
        raise TypeError("RuntimeEvent v1 context provider returned an invalid context")
    if (
        context.agent_id != session.agent_id
        or context.user_id != session.user_id
        or context.session_id != session.id
        or context.projection != projection
    ):
        raise V1ProjectionContextRequiredError(
            "RuntimeEvent v1 context provider must preserve session and current projection"
        )
    _validate_protocol_context(event, context)
    return context


def _protocol_context_requirement(event: RuntimeEvent) -> str | None:
    if event.source.framework == "a2a":
        return "A2A task projection"
    if isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted)):
        if event.item_kind == "artifact":
            return "artifact projection"
        if event.item_kind == "data" and event.source.protocol == "a2ui":
            return "A2UI surface projection"
    if (
        isinstance(event, (InteractionRequested, InteractionResolved))
        and event.source.protocol == "a2ui"
    ):
        return "A2UI interaction projection"
    return None


def _validate_protocol_context(
    event: RuntimeEvent,
    context: RuntimeEventV1ProjectionContext,
) -> None:
    if event.source.framework == "a2a":
        a2a_ref = context.a2a_tasks.get((event.run_id, event.scope_id))
        if a2a_ref is None or not a2a_ref.task_id.strip() or not a2a_ref.origin.strip():
            raise V1ProjectionContextRequiredError(
                "A2A task projection requires a context provider task ref"
            )
    if isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted)):
        if event.item_kind == "artifact":
            for part in _artifact_parts(event):
                context.artifact_version(event.scope_id, event.item_id, part.artifact_id)
        if event.item_kind == "data" and event.source.protocol == "a2ui":
            surface_ref = context.a2ui_surfaces.get((event.scope_id, event.item_id))
            if surface_ref is None or not surface_ref.surface_id.strip():
                raise V1ProjectionContextRequiredError(
                    "A2UI surface projection requires a context provider surface ref"
                )
    if (
        isinstance(event, (InteractionRequested, InteractionResolved))
        and event.source.protocol == "a2ui"
    ):
        interaction_ref = context.a2ui_interactions.get((event.scope_id, event.interaction_id))
        if interaction_ref is None or not interaction_ref.surface_id.strip():
            raise V1ProjectionContextRequiredError(
                "A2UI interaction projection requires a context provider interaction ref"
            )


def _artifact_parts(
    event: ItemStarted | ItemUpdated | ItemSnapshotReplaced | ItemCompleted,
) -> tuple[ArtifactContent, ...]:
    if isinstance(event, ItemStarted):
        parts = event.initial.parts if event.initial is not None else ()
    elif isinstance(event, ItemUpdated):
        parts = (event.update,)
    else:
        parts = event.snapshot.parts
    return tuple(part for part in parts if isinstance(part, ArtifactContent))


def _canonical_legacy_payloads(
    canonical: RuntimeEvent,
    events: tuple[RuntimeEventV1, ...],
    *,
    projection: RunProjection,
) -> list[dict[str, Any]]:
    runtime_identities = _projected_runtime_identities(canonical, events, projection=projection)
    return [
        _v1_to_session_payload(event, runtime_item=runtime_identities.get(index))
        for index, event in enumerate(events)
    ]


def _projected_runtime_identities(
    canonical: RuntimeEvent,
    events: tuple[RuntimeEventV1, ...],
    *,
    projection: RunProjection,
) -> dict[int, dict[str, Any]]:
    identities: dict[int, dict[str, Any]] = {}
    if isinstance(canonical, RunCompleted):
        text_indexes = [
            index
            for index, event in enumerate(events)
            if event.event_type in {EventTypeV1.TEXT_COMPLETED, EventTypeV1.REASONING_COMPLETED}
        ]
        selected_parts: list[tuple[Any, str]] = []
        for ref in canonical.output_refs:
            item = next(
                (
                    candidate
                    for candidate in projection.items
                    if candidate.scope_id == ref.scope_id and candidate.item_id == ref.item_id
                ),
                None,
            )
            if item is None:
                continue
            parts = [part for part in item.parts if isinstance(part, TextContent)]
            if ref.part_id is not None:
                parts = [part for part in parts if part.part_id == ref.part_id]
            selected_parts.extend((ref, part.part_id) for part in parts)
        for index, (ref, output_part_id) in zip(text_indexes, selected_parts):
            identities[index] = {
                "RunId": canonical.run_id,
                "ScopeId": ref.scope_id,
                "ItemId": ref.item_id,
                "PartId": output_part_id,
                "Operation": "replace",
                "SourceEventId": canonical.source.native_event_id or canonical.event_id,
            }
    for index, event in enumerate(events):
        if index in identities:
            continue
        item_id = event.payload.get("item_id")
        payload_part_id = event.payload.get("part_id")
        if item_id:
            identities[index] = {
                "RunId": canonical.run_id,
                "ScopeId": event.payload.get("scope_id"),
                "ItemId": item_id,
                "PartId": payload_part_id,
                "Operation": event.payload.get("operation"),
                "SourceEventId": event.payload.get("source_event_id") or canonical.event_id,
            }
    return identities


def _v1_to_session_payload(
    event: RuntimeEventV1, *, runtime_item: dict[str, Any] | None
) -> dict[str, Any]:
    content: dict[str, Any]
    if event.event_type in {EventTypeV1.TEXT_COMPLETED, EventTypeV1.TEXT_DELTA}:
        event_type = (
            "assistant_message"
            if event.event_type == EventTypeV1.TEXT_COMPLETED
            else "assistant_stream_delta"
        )
        content = {"role": "model", "parts": [{"text": event.payload.get("text", "")}]}
    elif event.event_type in {EventTypeV1.REASONING_COMPLETED, EventTypeV1.REASONING_DELTA}:
        event_type = "reasoning"
        content = {"role": "model", "parts": [{"text": event.payload.get("text", "")}]}
    elif event.event_type in {
        EventTypeV1.RUN_STARTED,
        EventTypeV1.RUN_PROGRESS,
        EventTypeV1.RUN_INTERRUPTED,
        EventTypeV1.RUN_COMPLETED,
        EventTypeV1.RUN_FAILED,
        EventTypeV1.RUN_CANCELED,
    }:
        event_type = "run_status"
        content = {"status": event.payload.get("status")}
    else:
        event_type = event.event_type
        content = dict(event.payload)
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "RuntimeEventV1": dict(event.payload),
    }
    if runtime_item is not None:
        metadata["RuntimeItem"] = runtime_item
    return {
        "EventId": event.event_id,
        "SessionId": event.session_id,
        "Author": event.agent_id,
        "EventType": event_type,
        "Content": content,
        "Timestamp": event.timestamp,
        "SeqId": event.seq_id,
        "InvocationId": event.invocation_id,
        "Metadata": metadata,
    }


__all__ = [
    "LegacyRunNotResumableError",
    "LegacySessionEventGroup",
    "RuntimeEventV1ContextProvider",
    "ensure_canonical_resume_allowed",
    "list_legacy_session_events",
    "replay_projection",
    "subscribe_legacy_session_events",
]
