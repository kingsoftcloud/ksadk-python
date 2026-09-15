"""Mixed v1/v2 session read-view and legacy resume contracts."""

from __future__ import annotations

import pytest

from ksadk.events.canonical import (
    InteractionRequested,
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemStarted,
    OutputRef,
    RunCompleted,
    RunStarted,
    SourceRef,
)
from ksadk.events.canonical_replay import (
    LegacyRunNotResumableError,
    RuntimeEventV1ContextProvider,
    ensure_canonical_resume_allowed,
    list_legacy_session_events,
    subscribe_legacy_session_events,
)
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.content import ArtifactContent, ContentSnapshot, DataContent, TextContent
from ksadk.events.reducer import RunProjection
from ksadk.events.v1_compat import (
    A2ATaskProjectionRef,
    A2UIInteractionProjectionRef,
    A2UISurfaceProjectionRef,
    RuntimeEventV1ProjectionContext,
)
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


def _base(event_id: str) -> dict:
    return {
        "schema_version": 2,
        "event_id": event_id,
        "seq": 0,
        "timestamp": 2.0,
        "run_id": "run-v2",
        "scope_id": "scope-v2",
        "source": SourceRef(framework="adk", native_event_id=event_id),
    }


def _event_text(event: dict) -> str:
    return "".join(
        str(part.get("text") or "")
        for part in (event.get("Content") or {}).get("parts") or []
        if isinstance(part, dict)
    )


def _assistant_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("EventType") == "assistant_message"]


@pytest.fixture
async def mixed_schema_session():
    service = InMemorySessionService()
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id="mixed")
    await service.append_event(
        "mixed",
        SessionEvent(
            id="legacy-answer",
            session_id="mixed",
            author="assistant",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "old answer"}]},
            invocation_id="run-v1-finished",
            metadata={"schema_version": 1},
        ),
    )
    await service.append_event(
        "mixed",
        SessionEvent(
            id="legacy-active",
            session_id="mixed",
            author="assistant",
            event_type="run_status",
            content={"status": "in_progress"},
            invocation_id="run-v1-active",
            metadata={"schema_version": 1, "ksadk_runtime_event": True},
        ),
    )
    store = RuntimeEventStore(service)
    events = (
        RunStarted(**_base("run-start"), status="running"),
        ItemStarted(
            **_base("item-start"),
            item_id="item-1",
            item_kind="message",
            phase="final_answer",
        ),
        ItemCompleted(
            **_base("item-complete"),
            item_id="item-1",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text="new answer"),)),
        ),
        RunCompleted(
            **_base("run-complete"),
            status="completed",
            output_refs=(OutputRef(scope_id="scope-v2", item_id="item-1", part_id="text-0"),),
        ),
    )
    for event in events:
        await store.append_one("mixed", event)
    return service, store


@pytest.mark.asyncio
async def test_legacy_view_keeps_old_run_and_projects_new_run_in_shared_seq_order(
    mixed_schema_session,
):
    service, store = mixed_schema_session
    events = await list_legacy_session_events(store, "mixed", session_service=service)

    assert [_event_text(event) for event in _assistant_events(events)] == [
        "old answer",
        "new answer",
    ]
    assert [event["SeqId"] for event in events] == sorted(event["SeqId"] for event in events)
    assert _assistant_events(events)[1]["Metadata"]["RuntimeItem"] == {
        "RunId": "run-v2",
        "ScopeId": "scope-v2",
        "ItemId": "item-1",
        "PartId": "text-0",
        "Operation": "replace",
        "SourceEventId": "run-complete",
    }


@pytest.mark.asyncio
async def test_one_canonical_seq_projects_as_an_atomic_legacy_delivery_group(
    mixed_schema_session,
):
    service, store = mixed_schema_session
    completed_seq = (await store.list("mixed", run_id="run-v2"))[-1].seq

    after = await list_legacy_session_events(
        store,
        "mixed",
        session_service=service,
        after_seq=completed_seq - 1,
        limit=1,
    )
    before = await list_legacy_session_events(
        store,
        "mixed",
        session_service=service,
        before_seq=completed_seq,
    )

    assert len(after) == 2
    assert {event["SeqId"] for event in after} == {completed_seq}
    assert [event["EventType"] for event in after] == ["assistant_message", "run_status"]
    assert all(event["SeqId"] < completed_seq for event in before)

    stream = subscribe_legacy_session_events(
        store,
        "mixed",
        session_service=service,
        after_seq=completed_seq - 1,
        timeout=0.1,
    )
    live_group = await anext(stream)
    await stream.aclose()
    assert live_group.seq == completed_seq
    assert len(live_group.events) == 2


@pytest.mark.asyncio
async def test_legacy_snapshot_only_hides_open_snapshot_replace_until_run_completed():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="snapshot-replace")
    store = RuntimeEventStore(service)
    events = (
        RunStarted(**_base("run-start-snapshot"), status="running"),
        ItemStarted(
            **_base("item-start-snapshot"),
            item_id="item-1",
            item_kind="message",
            phase="final_answer",
            initial=ContentSnapshot(parts=(TextContent(part_id="stale-part", text="stale"),)),
        ),
        ItemSnapshotReplaced(
            **_base("item-snapshot-replaced"),
            item_id="item-1",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="fresh-part", text="fresh"),)),
        ),
        ItemCompleted(
            **_base("item-complete-snapshot"),
            item_id="item-1",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="fresh-part", text="fresh"),)),
        ),
    )
    for event in events:
        await store.append_one("snapshot-replace", event)

    before_terminal = await list_legacy_session_events(
        store,
        "snapshot-replace",
        session_service=service,
    )
    assert _assistant_events(before_terminal) == []

    await store.append_one(
        "snapshot-replace",
        RunCompleted(
            **_base("run-complete-snapshot"),
            status="completed",
            output_refs=(
                OutputRef(
                    scope_id="scope-v2",
                    item_id="item-1",
                    part_id="fresh-part",
                ),
            ),
        ),
    )

    after_terminal = await list_legacy_session_events(
        store,
        "snapshot-replace",
        session_service=service,
    )
    assert [_event_text(event) for event in _assistant_events(after_terminal)] == ["fresh"]


@pytest.mark.asyncio
async def test_active_v1_run_cannot_resume_as_v2(mixed_schema_session):
    service, store = mixed_schema_session

    with pytest.raises(LegacyRunNotResumableError) as caught:
        await ensure_canonical_resume_allowed(
            store,
            "mixed",
            "run-v1-active",
            session_service=service,
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "legacy_run_not_resumable"


@pytest.mark.asyncio
async def test_v1_parser_state_is_not_shared_between_sessions():
    service = InMemorySessionService()
    store = RuntimeEventStore(service)
    for session_id, text in (("left", "left answer"), ("right", "right answer")):
        await service.create_session(agent_id="agent-1", user_id="user-1", session_id=session_id)
        await service.append_event(
            session_id,
            SessionEvent(
                id=f"legacy-{session_id}",
                author="assistant",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": text}]},
                invocation_id="same-run-id",
            ),
        )

    left = await list_legacy_session_events(store, "left", session_service=service)
    right = await list_legacy_session_events(store, "right", session_service=service)

    assert [_event_text(event) for event in _assistant_events(left)] == ["left answer"]
    assert [_event_text(event) for event in _assistant_events(right)] == ["right answer"]


@pytest.mark.asyncio
async def test_protocol_specific_legacy_projection_fails_closed_without_context_provider():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="protocols")
    store = RuntimeEventStore(service)
    artifact = ItemStarted(
        **_base("artifact-start"),
        item_id="artifact-item",
        item_kind="artifact",
        initial=ContentSnapshot(
            parts=(
                ArtifactContent(
                    part_id="artifact-part",
                    artifact_id="artifact-1",
                    name="report.md",
                ),
            )
        ),
    )
    await store.append_one("protocols", artifact)

    with pytest.raises(ValueError, match="context provider"):
        await list_legacy_session_events(store, "protocols")


@pytest.mark.asyncio
async def test_a2a_and_a2ui_legacy_projection_fail_closed_without_required_refs():
    service = InMemorySessionService()
    source_a2ui = SourceRef(framework="ksadk", native_event_id="a2ui-native", protocol="a2ui")
    cases = {
        "a2a": RunStarted(
            **{
                **_base("a2a-start"),
                "source": SourceRef(framework="a2a", native_event_id="a2a-native"),
            },
            status="running",
        ),
        "a2ui-surface": ItemStarted(
            **{**_base("surface-start"), "source": source_a2ui},
            item_id="surface-item",
            item_kind="data",
            initial=ContentSnapshot(parts=(DataContent(part_id="data-0", data={"type": "text"}),)),
        ),
        "a2ui-interaction": InteractionRequested(
            **{**_base("interaction"), "source": source_a2ui},
            interaction_id="interaction-1",
            interaction_kind="structured_input",
            request={
                "request_type": "structured_input",
                "prompt": "name",
                "schema": {"type": "string"},
            },
        ),
    }

    for name, protocol_event in cases.items():
        session_id = f"missing-{name}"
        await service.create_session("agent-1", "user-1", session_id=session_id)
        store = RuntimeEventStore(service)
        if not isinstance(protocol_event, RunStarted):
            await store.append_one(session_id, RunStarted(**_base(f"{name}-run"), status="running"))
        await store.append_one(session_id, protocol_event)

        with pytest.raises(ValueError, match="context provider"):
            await list_legacy_session_events(store, session_id)


@pytest.mark.asyncio
async def test_legacy_projection_rejects_context_provider_missing_protocol_ref():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="missing-ref")
    store = RuntimeEventStore(service)
    event = RunStarted(
        **{
            **_base("a2a-start"),
            "source": SourceRef(framework="a2a", native_event_id="a2a-native"),
        },
        status="running",
    )
    await store.append_one("missing-ref", event)

    def empty_provider(session, _event, projection):
        return RuntimeEventV1ProjectionContext.from_projection(
            projection,
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
        )

    with pytest.raises(ValueError, match="A2A task"):
        await list_legacy_session_events(
            store,
            "missing-ref",
            context_provider=empty_provider,
        )


@pytest.mark.asyncio
async def test_context_provider_preserves_artifact_a2a_and_a2ui_families():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="protocols")
    store = RuntimeEventStore(service)
    source_a2a = SourceRef(framework="a2a", native_event_id="a2a-native")
    source_a2ui = SourceRef(framework="ksadk", native_event_id="a2ui-native", protocol="a2ui")
    events = (
        RunStarted(**{**_base("a2a-start"), "source": source_a2a}, status="running"),
        ItemStarted(
            **{**_base("artifact-start"), "source": source_a2a},
            item_id="artifact-item",
            item_kind="artifact",
            initial=ContentSnapshot(
                parts=(
                    ArtifactContent(
                        part_id="artifact-part",
                        artifact_id="artifact-1",
                        name="report.md",
                    ),
                )
            ),
        ),
        ItemStarted(
            **{**_base("surface-start"), "source": source_a2ui},
            item_id="surface-item",
            item_kind="data",
            initial=ContentSnapshot(parts=(DataContent(part_id="data-0", data={"type": "text"}),)),
        ),
        InteractionRequested(
            **{**_base("interaction"), "source": source_a2ui},
            interaction_id="interaction-1",
            interaction_kind="structured_input",
            request={
                "request_type": "structured_input",
                "prompt": "name",
                "schema": {"type": "string"},
            },
        ),
    )
    for event in events:
        await store.append_one("protocols", event)

    def provider(session, event, projection: RunProjection):
        return RuntimeEventV1ProjectionContext.from_projection(
            projection,
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            a2a_tasks={(event.run_id, event.scope_id): A2ATaskProjectionRef("task-1", "remote")},
            a2ui_surfaces={("scope-v2", "surface-item"): A2UISurfaceProjectionRef("surface-1")},
            a2ui_interactions={
                ("scope-v2", "interaction-1"): A2UIInteractionProjectionRef("surface-1")
            },
            artifact_versions={
                ("scope-v2", "artifact-item", "artifact-1"): 7,
            },
        )

    typed_provider: RuntimeEventV1ContextProvider = provider
    projected = await list_legacy_session_events(
        store, "protocols", context_provider=typed_provider
    )

    assert [event["EventType"] for event in projected] == [
        "a2a.task.created",
        "a2a.task.artifact",
        "a2ui.surface.begin",
        "a2ui.interaction",
    ]
    artifact = projected[1]["Content"]["artifact"]
    assert artifact["version"] == 7


@pytest.mark.asyncio
async def test_legacy_subscribe_uses_context_provider_for_hydrate_and_tail():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="protocol-subscribe")
    store = RuntimeEventStore(service)
    source = SourceRef(framework="a2a", native_event_id="a2a-native")
    started = await store.append_one(
        "protocol-subscribe",
        RunStarted(**{**_base("a2a-start"), "source": source}, status="running"),
    )
    await store.append_one(
        "protocol-subscribe",
        ItemStarted(
            **{**_base("artifact-start"), "source": source},
            item_id="artifact-item",
            item_kind="artifact",
            initial=ContentSnapshot(
                parts=(
                    ArtifactContent(
                        part_id="artifact-part",
                        artifact_id="artifact-1",
                        name="report.md",
                    ),
                )
            ),
        ),
    )
    provider_calls: list[str] = []

    def provider(session, event, projection):
        provider_calls.append(event.event_id)
        return RuntimeEventV1ProjectionContext.from_projection(
            projection,
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            a2a_tasks={(event.run_id, event.scope_id): A2ATaskProjectionRef("task-1", "remote")},
            artifact_versions={(event.scope_id, "artifact-item", "artifact-1"): 7},
        )

    stream = subscribe_legacy_session_events(
        store,
        "protocol-subscribe",
        after_seq=started.seq,
        poll_interval=0.01,
        timeout=0.1,
        context_provider=provider,
    )
    group = await anext(stream)
    await stream.aclose()

    assert [event["EventType"] for event in group.events] == ["a2a.task.artifact"]
    assert provider_calls == ["a2a-start", "artifact-start"]


@pytest.mark.asyncio
async def test_legacy_subscribe_hydrates_once_then_polls_incrementally(mixed_schema_session):
    service, store = mixed_schema_session
    calls: list[tuple[int | None, int | None]] = []
    original = service.get_events

    async def counting_get_events(session_id, *args, **kwargs):
        calls.append((kwargs.get("after_seq_id"), kwargs.get("before_seq_id")))
        return await original(session_id, *args, **kwargs)

    service.get_events = counting_get_events
    completed_seq = (await store.list("mixed", run_id="run-v2"))[-1].seq
    calls.clear()
    stream = subscribe_legacy_session_events(
        store,
        "mixed",
        after_seq=completed_seq - 1,
        poll_interval=0.01,
        timeout=0.1,
    )

    group = await anext(stream)
    await stream.aclose()

    assert group.seq == completed_seq
    assert calls[0] == (None, completed_seq)
    assert calls[1] == (completed_seq - 1, None)
    assert all(after is not None or before is not None for after, before in calls)


@pytest.mark.asyncio
async def test_before_seq_is_applied_physically_before_protocol_projection():
    class CountingService(InMemorySessionService):
        def __init__(self):
            super().__init__()
            self.calls: list[tuple[int | None, int | None]] = []

        async def get_events(self, *args, **kwargs):
            self.calls.append((kwargs.get("after_seq_id"), kwargs.get("before_seq_id")))
            return await super().get_events(*args, **kwargs)

    service = CountingService()
    await service.create_session("agent-1", "user-1", session_id="before-protocol")
    store = RuntimeEventStore(service)
    started = await store.append_one(
        "before-protocol", RunStarted(**_base("normal-start"), status="running")
    )
    await store.append_one(
        "before-protocol",
        RunCompleted(
            **{
                **_base("future-a2a"),
                "source": SourceRef(framework="a2a", native_event_id="future-a2a"),
            },
            status="completed",
            output_refs=(),
        ),
    )
    service.calls.clear()

    events = await list_legacy_session_events(
        store,
        "before-protocol",
        session_service=service,
        before_seq=started.seq + 1,
    )

    assert [event["EventType"] for event in events] == ["run_status"]
    assert service.calls == [(None, started.seq + 1)]


@pytest.mark.asyncio
async def test_generic_metadata_named_protocol_does_not_enable_a2ui_projection():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="generic-data")
    store = RuntimeEventStore(service)
    await store.append_one("generic-data", RunStarted(**_base("run-start"), status="running"))
    await store.append_one(
        "generic-data",
        ItemStarted(
            **{
                **_base("generic-data"),
                "source": SourceRef(
                    framework="ksadk",
                    native_event_id="generic-data",
                    metadata={"protocol": "a2ui"},
                ),
            },
            item_id="generic-item",
            item_kind="data",
            initial=ContentSnapshot(parts=(DataContent(part_id="data-0", data={"x": 1}),)),
        ),
    )

    projected = await list_legacy_session_events(store, "generic-data")

    assert [event["EventType"] for event in projected] == ["run_status"]
