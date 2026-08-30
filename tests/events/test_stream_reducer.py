"""Lifecycle and bounded-idempotency tests for the canonical StreamReducer."""

from __future__ import annotations

import pytest

from ksadk.events.canonical import (
    ErrorInfo,
    ItemCompleted,
    ItemKind,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunProgress,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import ContentSnapshot, JsonContent, TextContent
from ksadk.events.reducer import ProjectionPatch, StreamConformanceError, StreamReducer

SOURCE = SourceRef(framework="adk", native_run_id="native-run-1")


def _envelope(seq: int, event_id: str | None = None, *, scope_id: str = "scope-1") -> dict:
    return {
        "schema_version": 2,
        "event_id": event_id or f"event-{seq}",
        "seq": seq,
        "timestamp": float(seq),
        "run_id": "run-1",
        "run_seq": seq,
        "scope_id": scope_id,
        "source": SOURCE,
    }


def started(
    item_id: str,
    *,
    seq: int = 1,
    item_kind: ItemKind = "message",
    scope_id: str = "scope-1",
) -> ItemStarted:
    return ItemStarted(
        **_envelope(seq, scope_id=scope_id),
        item_id=item_id,
        item_kind=item_kind,
        phase="final_answer",
    )


def appended(
    item_id: str,
    text: str,
    *,
    seq: int = 2,
    item_kind: str = "message",
    scope_id: str = "scope-1",
    event_id: str | None = None,
) -> ItemUpdated:
    return ItemUpdated(
        **_envelope(seq, event_id, scope_id=scope_id),
        item_id=item_id,
        item_kind=item_kind,
        op="append",
        update=TextContent(part_id="text-0", text=text),
    )


def replaced(item_id: str, text: str, *, seq: int = 3) -> ItemUpdated:
    return ItemUpdated(
        **_envelope(seq),
        item_id=item_id,
        item_kind="message",
        op="replace",
        update=TextContent(part_id="text-0", text=text),
    )


def snapshot_replaced(
    item_id: str,
    parts: tuple[TextContent | JsonContent, ...],
    *,
    seq: int = 3,
    item_kind: str = "message",
    scope_id: str = "scope-1",
    event_id: str | None = None,
) -> ItemSnapshotReplaced:
    return ItemSnapshotReplaced(
        **_envelope(seq, event_id, scope_id=scope_id),
        item_id=item_id,
        item_kind=item_kind,
        snapshot=ContentSnapshot(parts=parts),
    )


def completed(
    item_id: str,
    text: str,
    *,
    seq: int = 3,
    scope_id: str = "scope-1",
) -> ItemCompleted:
    return ItemCompleted(
        **_envelope(seq, scope_id=scope_id),
        item_id=item_id,
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text=text),)),
    )


def test_completed_snapshot_replaces_provisional_text() -> None:
    reducer = StreamReducer()
    for event in (started("item-1"), appended("item-1", "hel"), completed("item-1", "hello")):
        reducer.apply(event)
    assert reducer.snapshot().items[0].parts[0].text == "hello"


def test_replace_overwrites_a_named_provisional_part() -> None:
    reducer = StreamReducer()
    for event in (started("item-1"), appended("item-1", "wrong"), replaced("item-1", "right")):
        reducer.apply(event)
    assert reducer.snapshot().items[0].parts[0].text == "right"


def test_snapshot_replace_atomically_removes_stale_parts_and_keeps_item_open() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1", seq=1))
    reducer.apply(appended("item-1", "stale", seq=2))
    reducer.apply(
        ItemUpdated(
            **_envelope(3),
            item_id="item-1",
            item_kind="message",
            op="replace",
            update=JsonContent(part_id="stale-json", value={"stale": True}),
        )
    )

    replacement = snapshot_replaced(
        "item-1",
        (
            JsonContent(part_id="meta", value={"version": 2}),
            TextContent(part_id="text-fresh", text="fresh"),
        ),
        seq=4,
    )
    patch = reducer.apply(replacement)

    item = reducer.snapshot().items[0]
    assert item.status == "open"
    assert [part.part_id for part in item.parts] == ["meta", "text-fresh"]
    assert patch.reconciled is True
    assert patch.mutation == replacement

    reducer.apply(
        ItemUpdated(
            **_envelope(5),
            item_id="item-1",
            item_kind="message",
            op="append",
            update=TextContent(part_id="text-fresh", text=" answer"),
        )
    )
    assert reducer.snapshot().items[0].parts == (
        JsonContent(part_id="meta", value={"version": 2}),
        TextContent(part_id="text-fresh", text="fresh answer"),
    )
    reducer.apply(
        ItemCompleted(
            **_envelope(6),
            item_id="item-1",
            item_kind="message",
            snapshot=ContentSnapshot(
                parts=(
                    JsonContent(part_id="meta", value={"version": 2}),
                    TextContent(part_id="text-fresh", text="fresh answer"),
                )
            ),
        )
    )
    assert reducer.snapshot().items[0].parts == (
        JsonContent(part_id="meta", value={"version": 2}),
        TextContent(part_id="text-fresh", text="fresh answer"),
    )


def test_identical_snapshot_replace_is_applied_without_reconciliation() -> None:
    reducer = StreamReducer()
    initial = ContentSnapshot(parts=(TextContent(part_id="text-0", text="same"),))
    reducer.apply(
        ItemStarted(
            **_envelope(1),
            item_id="item-1",
            item_kind="message",
            phase="final_answer",
            initial=initial,
        )
    )

    patch = reducer.apply(snapshot_replaced("item-1", initial.parts, seq=2))

    assert patch.applied is True
    assert patch.reconciled is False
    assert reducer.snapshot().items[0].status == "open"


@pytest.mark.parametrize(
    ("prepare", "event", "code"),
    [
        (
            (),
            snapshot_replaced("missing", (TextContent(part_id="text-0", text="x"),), seq=1),
            "item_not_started",
        ),
        (
            (started("item-1", seq=1),),
            snapshot_replaced(
                "item-1",
                (TextContent(part_id="text-0", text="x"),),
                seq=2,
                item_kind="reasoning",
            ),
            "incompatible_item_kind",
        ),
        (
            (started("item-1", seq=1), completed("item-1", "done", seq=2)),
            snapshot_replaced("item-1", (TextContent(part_id="text-0", text="late"),), seq=3),
            "item_already_closed",
        ),
    ],
)
def test_snapshot_replace_requires_the_same_open_item(prepare, event, code) -> None:
    reducer = StreamReducer()
    for mutation in prepare:
        reducer.apply(mutation)
    before = reducer.snapshot()

    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(event)

    assert caught.value.code == code
    assert reducer.snapshot() == before


def test_snapshot_replace_rejects_duplicate_part_ids_atomically() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1"))
    before = reducer.snapshot()

    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(
            snapshot_replaced(
                "item-1",
                (
                    TextContent(part_id="duplicate", text="left"),
                    TextContent(part_id="duplicate", text="right"),
                ),
                seq=2,
            )
        )

    assert caught.value.code == "duplicate_part_id"
    assert reducer.snapshot() == before


def test_snapshot_replace_keeps_duplicate_and_event_id_rules() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1"))
    event = snapshot_replaced(
        "item-1",
        (TextContent(part_id="text-0", text="first"),),
        seq=2,
        event_id="snapshot-event",
    )
    assert reducer.apply(event).applied is True
    assert reducer.apply(event).applied is False

    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(
            snapshot_replaced(
                "item-1",
                (TextContent(part_id="text-0", text="conflict"),),
                seq=3,
                event_id="snapshot-event",
            )
        )
    assert caught.value.code == "conflicting_event_id"


def test_identical_text_in_distinct_items_is_not_deduplicated() -> None:
    reducer = StreamReducer()
    events = (
        started("item-1", seq=1),
        completed("item-1", "same", seq=2),
        started("item-2", seq=3),
        completed("item-2", "same", seq=4),
    )
    for event in events:
        reducer.apply(event)
    assert [item.parts[0].text for item in reducer.snapshot().items] == ["same", "same"]


def test_same_item_id_in_distinct_scopes_is_not_merged() -> None:
    reducer = StreamReducer()
    for event in (
        started("item-1", seq=1, scope_id="scope-a"),
        completed("item-1", "left", seq=2, scope_id="scope-a"),
        started("item-1", seq=3, scope_id="scope-b"),
        completed("item-1", "right", seq=4, scope_id="scope-b"),
    ):
        reducer.apply(event)
    assert [item.parts[0].text for item in reducer.snapshot().items] == ["left", "right"]


def test_append_before_start_is_a_structured_error() -> None:
    reducer = StreamReducer()
    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(appended("missing", "hello"))
    assert caught.value.code == "item_not_started"
    assert caught.value.source == "adk"
    assert caught.value.scope_id == "scope-1"
    assert caught.value.item_id == "missing"


def test_update_after_complete_is_rejected_without_changing_snapshot() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1"))
    reducer.apply(completed("item-1", "done", seq=2))
    with pytest.raises(StreamConformanceError, match="already closed") as caught:
        reducer.apply(appended("item-1", "again", seq=3))
    assert caught.value.code == "item_already_closed"
    assert reducer.snapshot().items[0].parts[0].text == "done"


def test_incompatible_item_kind_is_rejected() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1", item_kind="message"))
    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(appended("item-1", "x", seq=2, item_kind="reasoning"))
    assert caught.value.code == "incompatible_item_kind"


def test_run_completion_with_open_items_is_rejected() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1"))
    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(
            RunCompleted(
                **_envelope(2),
                status="completed",
                output_refs=(OutputRef(scope_id="scope-1", item_id="item-1"),),
            )
        )
    assert caught.value.code == "run_completed_with_open_items"
    assert caught.value.item_id == "item-1"


def test_exact_duplicate_event_id_and_seq_is_a_noop() -> None:
    reducer = StreamReducer()
    event = started("item-1")
    assert reducer.apply(event).applied is True
    assert reducer.apply(event).applied is False
    assert len(reducer.snapshot().items) == 1


def test_reused_event_id_with_conflicting_content_is_rejected() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1"))
    original = appended("item-1", "a", seq=2, event_id="same-event")
    conflict = appended("item-1", "b", seq=3, event_id="same-event")
    reducer.apply(original)
    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(conflict)
    assert caught.value.code == "conflicting_event_id"


def test_conflicting_reused_seq_is_rejected() -> None:
    reducer = StreamReducer()
    reducer.apply(started("item-1"))
    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(started("item-2"))
    assert caught.value.code == "conflicting_seq"


def test_run_output_ref_order_is_authoritative() -> None:
    reducer = StreamReducer()
    for event in (
        started("item-1", seq=1),
        completed("item-1", "first", seq=2),
        started("item-2", seq=3),
        completed("item-2", "second", seq=4),
    ):
        reducer.apply(event)
    refs = (
        OutputRef(scope_id="scope-1", item_id="item-2", part_id="text-0"),
        OutputRef(scope_id="scope-1", item_id="item-1", part_id="text-0"),
    )
    reducer.apply(RunCompleted(**_envelope(5), status="completed", output_refs=refs))
    assert reducer.snapshot().output_refs == refs


def test_recent_event_diagnostics_are_bounded_to_1024_entries() -> None:
    reducer = StreamReducer()
    for seq in range(1, 1101):
        reducer.apply(
            RunProgress(
                **_envelope(seq),
                status="running",
                progress=seq / 1100,
            )
        )
    assert reducer.recent_event_count == StreamReducer.RECENT_EVENT_LIMIT == 1024
    assert reducer.snapshot().last_seq == 1100


def test_patch_only_consumer_rebuilds_append_replace_and_completed_snapshot() -> None:
    reducer = StreamReducer()
    patches = [
        reducer.apply(started("item-1", seq=1)),
        reducer.apply(appended("item-1", "hel", seq=2)),
        reducer.apply(appended("item-1", "lo", seq=3)),
        reducer.apply(replaced("item-1", "hello!", seq=4)),
        reducer.apply(completed("item-1", "hello", seq=5)),
    ]

    text_by_part: dict[tuple[str, str, str], str] = {}
    seen_updates: list[tuple[str, str]] = []
    for patch in patches:
        mutation = patch.mutation
        assert mutation is not None
        if mutation.event_type == "item.updated":
            assert isinstance(mutation.update, TextContent)
            key = (mutation.scope_id, mutation.item_id, mutation.update.part_id)
            seen_updates.append((mutation.op, mutation.update.text))
            if mutation.op == "append":
                text_by_part[key] = text_by_part.get(key, "") + mutation.update.text
            else:
                text_by_part[key] = mutation.update.text
        elif mutation.event_type == "item.completed":
            for part in mutation.snapshot.parts:
                assert isinstance(part, TextContent)
                text_by_part[(mutation.scope_id, mutation.item_id, part.part_id)] = part.text

    assert seen_updates == [("append", "hel"), ("append", "lo"), ("replace", "hello!")]
    projected = reducer.snapshot().items[0]
    assert text_by_part[(projected.scope_id, projected.item_id, "text-0")] == (
        projected.parts[0].text
    )
    patch_only_reducer = StreamReducer()
    for patch in patches:
        assert patch.mutation is not None
        patch_only_reducer.apply(patch.mutation)
    assert patch_only_reducer.snapshot() == reducer.snapshot()
    assert ProjectionPatch.model_validate_json(patches[2].model_dump_json()) == patches[2]


def test_patch_preserves_typed_usage_and_failure_payloads() -> None:
    reducer = StreamReducer()
    usage_patch = reducer.apply(
        UsageReported(
            **_envelope(1),
            input_tokens=10,
            output_tokens=4,
            total_tokens=14,
            cached_tokens=2,
            reasoning_tokens=1,
        )
    )
    failure = ErrorInfo(
        code="source_failed",
        message="source failed",
        source="adk",
        scope_id="scope-1",
    )
    failure_patch = reducer.apply(RunFailed(**_envelope(2), status="failed", error=failure))

    assert usage_patch.mutation is not None
    assert usage_patch.mutation.event_type == "usage.reported"
    assert usage_patch.mutation.total_tokens == 14
    assert usage_patch.mutation.cached_tokens == 2
    assert failure_patch.mutation is not None
    assert failure_patch.mutation.event_type == "run.failed"
    assert failure_patch.mutation.error == failure
    patch_only_reducer = StreamReducer()
    patch_only_reducer.apply(usage_patch.mutation)
    patch_only_reducer.apply(failure_patch.mutation)
    assert patch_only_reducer.snapshot() == reducer.snapshot()


def test_terminal_run_rejects_progress_without_mutating_snapshot() -> None:
    reducer = StreamReducer()
    reducer.apply(RunCompleted(**_envelope(1), status="completed", output_refs=()))
    before = reducer.snapshot()

    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(RunProgress(**_envelope(2), status="running", progress=0.9))

    assert caught.value.code == "run_already_terminal"
    assert reducer.snapshot() == before


def test_terminal_run_rejects_item_start_without_mutating_snapshot() -> None:
    reducer = StreamReducer()
    reducer.apply(
        RunFailed(
            **_envelope(1),
            status="failed",
            error=ErrorInfo(code="failed", source="adk", scope_id="scope-1"),
        )
    )
    before = reducer.snapshot()

    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(started("late-item", seq=2))

    assert caught.value.code == "run_already_terminal"
    assert reducer.snapshot() == before


def test_terminal_run_rejects_another_terminal_without_mutating_snapshot() -> None:
    reducer = StreamReducer()
    reducer.apply(RunCanceled(**_envelope(1), status="canceled", reason="user_requested"))
    before = reducer.snapshot()

    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(RunCompleted(**_envelope(2), status="completed", output_refs=()))

    assert caught.value.code == "run_already_terminal"
    assert reducer.snapshot() == before


def test_terminal_run_allows_only_exact_recent_idempotent_replay() -> None:
    reducer = StreamReducer()
    terminal = RunCompleted(**_envelope(2), status="completed", output_refs=())
    reducer.apply(terminal)
    before = reducer.snapshot()

    replay_patch = reducer.apply(terminal)

    assert replay_patch.applied is False
    assert replay_patch.mutation is None
    assert reducer.snapshot() == before


def test_terminal_run_rejects_non_recent_stale_mutation() -> None:
    reducer = StreamReducer()
    reducer.apply(RunCompleted(**_envelope(2), status="completed", output_refs=()))
    before = reducer.snapshot()

    with pytest.raises(StreamConformanceError) as caught:
        reducer.apply(
            RunProgress(
                **_envelope(1, event_id="unseen-stale-event"),
                status="running",
                progress=0.1,
            )
        )

    assert caught.value.code == "run_already_terminal"
    assert reducer.snapshot() == before
