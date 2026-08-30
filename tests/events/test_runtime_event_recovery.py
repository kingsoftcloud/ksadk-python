"""Canonical persistence pipeline recovery contracts."""

from __future__ import annotations

import json

import pytest

from ksadk.events.canonical import (
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.canonical_replay import replay_projection
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.content import ContentSnapshot, TextContent
from ksadk.events.pipeline import (
    CanonicalEventPipeline,
    PipelineMetrics,
    _reconciliation_reason,
)
from ksadk.events.reducer import ProjectionPatch, StreamReducer
from ksadk.sessions.in_memory import InMemorySessionService


def _base(event_id: str, *, seq: int = 0, scope_id: str = "scope-1") -> dict:
    return {
        "schema_version": 2,
        "event_id": event_id,
        "seq": seq,
        "timestamp": 1.0,
        "run_id": "run-1",
        "run_seq": None,
        "scope_id": scope_id,
        "source": SourceRef(
            framework="adk",
            native_event_id=event_id,
            native_run_id="native-run-1",
        ),
    }


def _run_started() -> RunStarted:
    return RunStarted(**_base("run-start"), status="running")


def _item_started(item_id: str) -> ItemStarted:
    return ItemStarted(
        **_base(f"{item_id}-start"),
        item_id=item_id,
        item_kind="message",
        phase="final_answer",
    )


def _item_updated(item_id: str, text: str) -> ItemUpdated:
    return ItemUpdated(
        **_base(f"{item_id}-update"),
        item_id=item_id,
        item_kind="message",
        op="append",
        update=TextContent(part_id="text-0", text=text),
    )


def _item_completed(item_id: str, text: str) -> ItemCompleted:
    return ItemCompleted(
        **_base(f"{item_id}-complete"),
        item_id=item_id,
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text=text),)),
    )


def _item_snapshot_replaced(item_id: str, text: str) -> ItemSnapshotReplaced:
    return ItemSnapshotReplaced(
        **_base(f"{item_id}-snapshot-replaced"),
        item_id=item_id,
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="text-fresh", text=text),)),
    )


class _FailSecondRecoveryWrite(InMemorySessionService):
    def __init__(self):
        super().__init__()
        self.recovery_writes = 0
        self.failed = False
        self.full_scans = 0
        self.run_scans = 0

    async def append_event(self, session_id, event):
        runtime = (event.content or {}).get("runtime_event") or {}
        metadata = (runtime.get("source") or {}).get("metadata") or {}
        if metadata.get("recovery_for_event_id"):
            self.recovery_writes += 1
            if self.recovery_writes == 2 and not self.failed:
                self.failed = True
                raise ConnectionError("partial recovery write")
        return await super().append_event(session_id, event)

    async def get_events(self, *args, **kwargs):
        self.full_scans += 1
        return await super().get_events(*args, **kwargs)

    async def get_events_by_invocation_id(self, *args, **kwargs):
        self.run_scans += 1
        return await super().get_events_by_invocation_id(*args, **kwargs)


@pytest.fixture
async def pipeline_fixture():
    service = InMemorySessionService()
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    metrics = PipelineMetrics()
    published = []

    async def publish(_session_id, event):
        published.append(event)

    pipeline = CanonicalEventPipeline(
        store,
        session_id="session-1",
        publisher=publish,
        metrics=metrics,
    )
    return pipeline, store, metrics, published


@pytest.mark.asyncio
async def test_pipeline_live_full_and_cursor_replay_share_one_reducer(pipeline_fixture):
    pipeline, store, _metrics, _published = pipeline_fixture
    events = (
        _run_started(),
        _item_started("item-1"),
        _item_updated("item-1", "hel"),
        _item_completed("item-1", "hello"),
        RunCompleted(
            **_base("run-complete"),
            status="completed",
            output_refs=(OutputRef(scope_id="scope-1", item_id="item-1", part_id="text-0"),),
        ),
    )
    persisted = []
    for event in events:
        persisted.extend(await pipeline.ingest(event))

    full = await replay_projection(store, "session-1", run_id="run-1")
    cursor_pipeline = CanonicalEventPipeline(store, session_id="session-1")
    for event in await store.list("session-1", before_seq=3):
        cursor_pipeline.reducer.apply(event)
    for event in await store.list("session-1", after_seq=2):
        cursor_pipeline.reducer.apply(event)

    assert pipeline.reducer.snapshot() == full == cursor_pipeline.reducer.snapshot()
    assert [event.seq for event in persisted] == [1, 2, 3, 4, 5]

    terminal_replay = await pipeline.ingest(events[-1])
    assert terminal_replay[0].seq == 5
    assert len(await store.list("session-1")) == 5


@pytest.mark.asyncio
async def test_snapshot_replace_live_and_replay_remove_stale_parts_then_continue(
    pipeline_fixture,
):
    pipeline, store, metrics, _published = pipeline_fixture
    events = (
        _run_started(),
        _item_started("item-1"),
        _item_updated("item-1", "stale"),
        _item_snapshot_replaced("item-1", "fresh"),
        ItemUpdated(
            **_base("item-1-append-fresh"),
            item_id="item-1",
            item_kind="message",
            op="append",
            update=TextContent(part_id="text-fresh", text=" answer"),
        ),
        ItemCompleted(
            **_base("item-1-complete-fresh"),
            item_id="item-1",
            item_kind="message",
            snapshot=ContentSnapshot(
                parts=(TextContent(part_id="text-fresh", text="fresh answer"),)
            ),
        ),
        RunCompleted(
            **_base("run-complete-fresh"),
            status="completed",
            output_refs=(OutputRef(scope_id="scope-1", item_id="item-1", part_id="text-fresh"),),
        ),
    )

    for event in events:
        await pipeline.ingest(event)

    live = pipeline.reducer.snapshot()
    replayed = await replay_projection(store, "session-1", run_id="run-1")
    assert replayed == live
    assert live.status == "completed"
    assert live.items[0].parts == (TextContent(part_id="text-fresh", text="fresh answer"),)
    assert all(part.part_id != "text-0" for part in live.items[0].parts)
    assert (
        metrics.value(
            "stream_projection_reconciled_total",
            source="adk",
            reason="authoritative_snapshot_replace",
        )
        == 1
    )
    assert (
        metrics.value(
            "stream_projection_reconciled_total",
            source="adk",
            reason="completed_snapshot_mismatch",
        )
        == 0
    )


@pytest.mark.asyncio
async def test_fatal_conformance_closes_open_items_then_fails_run_deterministically(
    pipeline_fixture,
):
    pipeline, store, metrics, published = pipeline_fixture
    await pipeline.ingest(_run_started())
    await pipeline.ingest(_item_started("item-b"))
    await pipeline.ingest(_item_started("item-a"))
    invalid = RunCompleted(
        **_base("invalid-complete"),
        status="completed",
        output_refs=(OutputRef(scope_id="scope-1", item_id="item-a"),),
    )

    recovered = await pipeline.ingest(invalid)
    replayed = await pipeline.ingest(invalid)

    assert [event.event_type for event in recovered] == [
        "item.failed",
        "item.failed",
        "run.failed",
    ]
    assert [event.item_id for event in recovered[:-1]] == ["item-a", "item-b"]
    assert [event.event_id for event in replayed] == [event.event_id for event in recovered]
    assert len(await store.list("session-1")) == 6
    # The duplicate offending input is an explicit delivery retry, so the
    # already-durable recovery group is republished as one complete group.
    assert len(published) == 9
    assert (
        metrics.value(
            "stream_conformance_error_total",
            source="adk",
            reason="run_completed_with_open_items",
        )
        == 1
    )
    projection = await replay_projection(store, "session-1", run_id="run-1")
    assert projection.status == "failed"
    assert all(item.status == "failed" for item in projection.items)

    restarted = CanonicalEventPipeline(store, session_id="session-1")
    assert [event.event_id for event in await restarted.ingest(invalid)] == [
        event.event_id for event in recovered
    ]
    assert len(await store.list("session-1")) == 6
    assert restarted.reducer.snapshot().status == "failed"


@pytest.mark.asyncio
async def test_completed_snapshot_mismatch_reconciles_without_failing(pipeline_fixture):
    pipeline, _store, metrics, _published = pipeline_fixture
    for event in (
        _run_started(),
        _item_started("item-1"),
        _item_updated("item-1", "wrong"),
        _item_completed("item-1", "right"),
    ):
        await pipeline.ingest(event)

    projection = pipeline.reducer.snapshot()
    assert projection.status == "running"
    assert projection.items[0].parts[0].text == "right"
    assert (
        metrics.value(
            "stream_projection_reconciled_total",
            source="adk",
            reason="completed_snapshot_mismatch",
        )
        == 1
    )


def test_unknown_reconciled_event_type_has_no_misleading_metric_fallback() -> None:
    event = RunProgress(
        **_base("future-reconciled-event"),
        status="running",
        progress=0.5,
    )

    with pytest.raises(RuntimeError, match="no declared metric semantics"):
        _reconciliation_reason(event)


@pytest.mark.asyncio
async def test_unknown_reconciled_reason_is_rejected_before_persist_or_live_apply() -> None:
    class _FutureReconciledReducer(StreamReducer):
        def apply(self, event: RuntimeEvent) -> ProjectionPatch:
            patch = super().apply(event)
            if isinstance(event, RunProgress):
                return patch.model_copy(update={"reconciled": True})
            return patch

    service = InMemorySessionService()
    await service.create_session(
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-future-reconciled",
    )
    store = RuntimeEventStore(service)
    pipeline = CanonicalEventPipeline(
        store,
        session_id="session-future-reconciled",
        reducer=_FutureReconciledReducer(),
    )
    await pipeline.ingest(_run_started())
    before = pipeline.reducer.snapshot()
    future = RunProgress(
        **_base("future-reconciled-progress"),
        status="running",
        progress=0.5,
    )

    with pytest.raises(RuntimeError, match="no declared metric semantics"):
        await pipeline.ingest(future)

    assert pipeline.reducer.snapshot() == before
    assert [event.event_id for event in await store.list("session-future-reconciled")] == [
        "run-start"
    ]


@pytest.mark.asyncio
async def test_isolated_delta_is_rejected_and_run_failure_replays(pipeline_fixture):
    pipeline, store, _metrics, _published = pipeline_fixture
    await pipeline.ingest(_run_started())

    recovered = await pipeline.ingest(_item_updated("missing", "orphan"))

    assert [event.event_type for event in recovered] == ["run.failed"]
    assert recovered[0].source.metadata["recovery_plan_owner_event_id"] == recovered[0].event_id
    assert "recovery_plan" in recovered[0].source.metadata
    assert all(event.event_id != "missing-update" for event in await store.list("session-1"))
    assert (await replay_projection(store, "session-1", run_id="run-1")).status == "failed"


@pytest.mark.asyncio
async def test_update_after_complete_preserves_snapshot_and_fails_run(pipeline_fixture):
    pipeline, store, _metrics, _published = pipeline_fixture
    for event in (_run_started(), _item_started("item-1"), _item_completed("item-1", "done")):
        await pipeline.ingest(event)

    recovery = await pipeline.ingest(_item_updated("item-1", "late"))

    assert [event.event_type for event in recovery] == ["run.failed"]
    projection = await replay_projection(store, "session-1", run_id="run-1")
    assert projection.status == "failed"
    assert projection.items[0].status == "completed"
    assert projection.items[0].parts[0].text == "done"


@pytest.mark.asyncio
async def test_incompatible_kind_closes_open_item_before_run_failure(pipeline_fixture):
    pipeline, store, _metrics, _published = pipeline_fixture
    await pipeline.ingest(_run_started())
    await pipeline.ingest(_item_started("item-1"))
    invalid = ItemUpdated(
        **_base("wrong-kind"),
        item_id="item-1",
        item_kind="reasoning",
        op="append",
        update=TextContent(part_id="text-0", text="bad"),
    )

    recovery = await pipeline.ingest(invalid)

    assert [event.event_type for event in recovery] == ["item.failed", "run.failed"]
    projection = await replay_projection(store, "session-1", run_id="run-1")
    assert projection.status == "failed"
    assert projection.items[0].status == "failed"
    assert projection.items[0].item_kind == "message"


@pytest.mark.asyncio
async def test_recovery_persists_full_group_before_apply_and_republishes_after_failure():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    publish_calls: list[str] = []
    fail_once = False

    async def publish(_session_id, event):
        nonlocal fail_once
        publish_calls.append(event.event_id)
        if fail_once:
            fail_once = False
            raise ConnectionError("publisher unavailable")

    pipeline = CanonicalEventPipeline(store, session_id="session-1", publisher=publish)
    await pipeline.ingest(_run_started())
    await pipeline.ingest(_item_started("item-a"))
    await pipeline.ingest(_item_started("item-b"))
    invalid = RunCompleted(
        **_base("invalid-complete"),
        status="completed",
        output_refs=(OutputRef(scope_id="scope-1", item_id="item-a"),),
    )
    fail_once = True
    publish_calls.clear()

    with pytest.raises(ConnectionError, match="publisher unavailable"):
        await pipeline.ingest(invalid)

    durable = await store.list("session-1")
    recovery = durable[-3:]
    assert [event.event_type for event in recovery] == [
        "item.failed",
        "item.failed",
        "run.failed",
    ]
    assert pipeline.reducer.snapshot().status == "failed"

    replayed = await pipeline.ingest(invalid)
    assert [event.event_id for event in replayed] == [event.event_id for event in recovery]
    assert publish_calls == [recovery[0].event_id, *[event.event_id for event in recovery]]


@pytest.mark.asyncio
async def test_partial_recovery_storage_retry_completes_original_plan():
    service = _FailSecondRecoveryWrite()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    pipeline = CanonicalEventPipeline(store, session_id="session-1")
    await pipeline.ingest(_run_started())
    await pipeline.ingest(_item_started("item-a"))
    await pipeline.ingest(_item_started("item-b"))
    invalid = RunCompleted(
        **_base("invalid-complete"),
        status="completed",
        output_refs=(OutputRef(scope_id="scope-1", item_id="item-a"),),
    )

    with pytest.raises(ConnectionError, match="partial recovery write"):
        await pipeline.ingest(invalid)
    partial = [
        event
        for event in await store.list("session-1")
        if event.source.metadata.get("recovery_for_event_id") == invalid.event_id
    ]
    assert len(partial) == 1
    assert partial[0].source.metadata["recovery_plan_owner_event_id"] == partial[0].event_id
    assert "recovery_plan" in partial[0].source.metadata
    # A fresh process has already replayed the first durable item.failed and no
    # longer has the original two-open-item projection in memory.  The stored
    # plan must still reconstruct and complete the original recovery group.
    reducer = StreamReducer()
    for persisted in await store.list("session-1", run_id=invalid.run_id):
        reducer.apply(persisted)
    service.full_scans = service.run_scans = 0
    published: list[str] = []

    async def publish(_session_id, event):
        published.append(event.event_id)

    pipeline = CanonicalEventPipeline(
        store,
        session_id="session-1",
        reducer=reducer,
        publisher=publish,
    )
    recovered = await pipeline.ingest(invalid)
    assert [event.event_type for event in recovered] == [
        "item.failed",
        "item.failed",
        "run.failed",
    ]
    assert recovered[0].event_id == partial[0].event_id
    assert len({event.event_id for event in recovered}) == 3
    assert sum("recovery_plan" in event.source.metadata for event in recovered) == 1
    assert published == [event.event_id for event in recovered]
    assert service.full_scans == 0
    assert service.run_scans == 0


@pytest.mark.parametrize(
    "updates",
    (
        {"timestamp": 9.0},
        {"run_id": "other-run", "scope_id": "other-scope"},
    ),
    ids=("same-run-fact-drift", "cross-run-scope-drift"),
)
@pytest.mark.asyncio
async def test_partial_recovery_owner_rejects_fact_drift_before_second_group_write(
    updates,
):
    service = _FailSecondRecoveryWrite()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    pipeline = CanonicalEventPipeline(store, session_id="session-1")
    await pipeline.ingest(_run_started())
    await pipeline.ingest(_item_started("item-a"))
    await pipeline.ingest(_item_started("item-b"))
    invalid = RunCompleted(
        **_base("partial-collision"),
        status="completed",
        output_refs=(OutputRef(scope_id="scope-1", item_id="item-a"),),
    )
    with pytest.raises(ConnectionError, match="partial recovery write"):
        await pipeline.ingest(invalid)
    recovery_writes_before_retry = service.recovery_writes

    conflicting = invalid.model_copy(update=updates)
    restarted = CanonicalEventPipeline(store, session_id="session-1")
    with pytest.raises(ValueError, match="recovery collision"):
        await restarted.ingest(conflicting)

    assert service.recovery_writes == recovery_writes_before_retry
    durable_recovery = [
        event
        for event in await store.list("session-1")
        if event.source.metadata.get("recovery_for_event_id") == invalid.event_id
    ]
    assert len(durable_recovery) == 1


@pytest.mark.asyncio
async def test_recovery_rejects_same_offending_id_with_different_fact(pipeline_fixture):
    pipeline, _store, _metrics, _published = pipeline_fixture
    await pipeline.ingest(_run_started())
    await pipeline.ingest(_item_started("item-1"))
    invalid = RunCompleted(
        **_base("invalid-complete"),
        status="completed",
        output_refs=(OutputRef(scope_id="scope-1", item_id="item-1"),),
    )
    await pipeline.ingest(invalid)

    conflicts = (
        invalid.model_copy(update={"timestamp": 9.0}),
        invalid.model_copy(
            update={
                "source": invalid.source.model_copy(update={"native_cursor": "different-cursor"})
            }
        ),
        invalid.model_copy(
            update={"output_refs": (OutputRef(scope_id="scope-1", item_id="different-item"),)}
        ),
        invalid.model_copy(update={"run_id": "other-run", "scope_id": "other-scope"}),
    )
    for conflicting in conflicts:
        with pytest.raises(ValueError, match="recovery collision"):
            await pipeline.ingest(conflicting)


@pytest.mark.asyncio
async def test_normal_publish_failure_republishes_durable_event_on_source_retry():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    calls: list[str] = []
    fail_once = True

    async def publish(_session_id, event):
        nonlocal fail_once
        calls.append(event.event_id)
        if fail_once:
            fail_once = False
            raise ConnectionError("publisher unavailable")

    pipeline = CanonicalEventPipeline(store, session_id="session-1", publisher=publish)
    with pytest.raises(ConnectionError, match="publisher unavailable"):
        await pipeline.ingest(_run_started())

    (retried,) = await pipeline.ingest(_run_started())

    assert retried.seq == 1
    assert calls == ["run-start", "run-start"]
    assert len(await store.list("session-1")) == 1


@pytest.mark.asyncio
async def test_old_durable_event_retry_after_terminal_lru_eviction_is_not_reapplied():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    published: list[str] = []

    async def publish(_session_id, event):
        published.append(event.event_id)

    pipeline = CanonicalEventPipeline(store, session_id="session-1", publisher=publish)
    original = _run_started()
    await pipeline.ingest(original)
    for index in range(1025):
        await pipeline.ingest(
            RunProgress(
                **_base(f"progress-{index}"),
                status="running",
                progress=index / 1025,
            )
        )
    await pipeline.ingest(RunCompleted(**_base("run-complete"), status="completed", output_refs=()))
    published.clear()
    restarted = CanonicalEventPipeline(store, session_id="session-1", publisher=publish)

    (retried,) = await restarted.ingest(original)

    assert retried.seq == 1
    assert published == [original.event_id]
    assert restarted.reducer.snapshot().last_seq == 1027
    assert restarted.reducer.snapshot().status == "completed"


@pytest.mark.asyncio
async def test_recovery_plan_metadata_is_linear_and_owned_by_first_fact():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    pipeline = CanonicalEventPipeline(store, session_id="session-1")
    await pipeline.ingest(_run_started())
    for index in range(40):
        await pipeline.ingest(_item_started(f"item-{index:02d}"))
    invalid = RunCompleted(
        **_base("invalid-many"),
        status="completed",
        output_refs=(OutputRef(scope_id="scope-1", item_id="item-00"),),
    )

    recovered = await pipeline.ingest(invalid)
    metadata = [event.source.metadata for event in recovered]
    owners = [value for value in metadata if "recovery_plan" in value]
    owner_id = recovered[0].event_id

    assert len(recovered) == 41
    assert len(owners) == 1
    assert metadata[0]["recovery_plan_owner_event_id"] == owner_id
    assert all(value["recovery_plan_owner_event_id"] == owner_id for value in metadata)
    plan_size = len(json.dumps(owners[0]["recovery_plan"], sort_keys=True))
    total_metadata_size = sum(len(json.dumps(value, sort_keys=True)) for value in metadata)
    assert total_metadata_size < plan_size + len(recovered) * 600


@pytest.mark.asyncio
async def test_completed_recovery_retry_point_reads_terminal_without_scanning_run():
    class CountingService(InMemorySessionService):
        def __init__(self):
            super().__init__()
            self.full_scans = 0
            self.run_scans = 0
            self.point_lookups = 0

        async def get_events(self, *args, **kwargs):
            self.full_scans += 1
            return await super().get_events(*args, **kwargs)

        async def get_events_by_invocation_id(self, *args, **kwargs):
            self.run_scans += 1
            return await super().get_events_by_invocation_id(*args, **kwargs)

        async def get_event_by_id(self, *args, **kwargs):
            self.point_lookups += 1
            return await super().get_event_by_id(*args, **kwargs)

    service = CountingService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = RuntimeEventStore(service)
    pipeline = CanonicalEventPipeline(store, session_id="session-1")
    await pipeline.ingest(_run_started())
    await pipeline.ingest(_item_started("item-1"))
    invalid = RunCompleted(
        **_base("invalid-complete"),
        status="completed",
        output_refs=(OutputRef(scope_id="scope-1", item_id="item-1"),),
    )
    await pipeline.ingest(invalid)
    service.full_scans = service.run_scans = service.point_lookups = 0

    replayed = await pipeline.ingest(invalid)

    assert [event.event_type for event in replayed] == ["item.failed", "run.failed"]
    assert service.full_scans == 0
    assert service.run_scans == 0
    assert service.point_lookups >= 3


# ------------------------------------------------ Task 7: fenced typed emit


@pytest.mark.asyncio
async def test_emit_is_fenced_and_write_context_never_leaks_into_payload():
    from ksadk.events.canonical_store import RuntimeEventStore as TypedStore
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.errors import StaleFenceError

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="session-1")
    fences = {"act-1": 1, "act-2": 2}

    async def validator(_envelope, guard):
        if fences.get(guard.activation_id) != guard.fencing_token:
            raise StaleFenceError("stale activation write guard")

    generic = SessionServiceEventStore(service, fence_validator=validator)
    typed = TypedStore(generic, session_id="session-1")
    published = []

    async def publish(_sid, event):
        published.append(event)

    pipeline = CanonicalEventPipeline(typed, session_id="session-1", publisher=publish)
    old_guard = ActivationWriteGuard(activation_id="act-1", fencing_token=1)
    new_guard = ActivationWriteGuard(activation_id="act-2", fencing_token=2)

    await pipeline.emit(_run_started(), write_context=old_guard)

    # takeover：旧 owner 的写入被 fence 拒绝，且没有落库。
    fences["act-1"] = 0  # 旧 activation 被 takeover
    with pytest.raises(StaleFenceError):
        await pipeline.emit(
            RunProgress(**_base("stale-delta"), status="running", progress=0.5),
            write_context=old_guard,
        )
    assert [event.event_id for event in await typed.list("session-1")] == ["run-start"]

    await pipeline.emit(
        RunProgress(**_base("fresh-delta"), status="running", progress=0.6),
        write_context=new_guard,
    )
    persisted = await typed.list("session-1")
    assert [event.event_id for event in persisted] == ["run-start", "fresh-delta"]
    # WriteContext 不进 RuntimeEvent payload / envelope projection / 物理行。
    for event in persisted:
        dump = event.model_dump()
        assert "activation_id" not in dump and "fencing_token" not in dump
    for envelope in await generic.read("session-1", 0, 10):
        assert "activation_id" not in envelope.payload
        assert "fencing_token" not in envelope.payload
    for row in await service.get_events("session-1"):
        assert "fencing_token" not in json.dumps(row.content)
    assert [event.event_id for event in published] == ["run-start", "fresh-delta"]


@pytest.mark.asyncio
async def test_emit_requires_typed_guard_and_typed_store():
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="session-1")
    legacy = RuntimeEventStore(service)
    pipeline = CanonicalEventPipeline(legacy, session_id="session-1")

    from ksadk.kernel.contracts import ActivationWriteGuard

    guard = ActivationWriteGuard(activation_id="act-1", fencing_token=1)
    with pytest.raises(TypeError, match="WriteContext"):
        await pipeline.emit(_run_started(), write_context="not-a-guard")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="SessionEventStore-backed"):
        await pipeline.emit(_run_started(), write_context=guard)
