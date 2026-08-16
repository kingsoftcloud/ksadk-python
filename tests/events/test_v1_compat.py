"""Read-only RuntimeEvent v1 compatibility projection contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import jsonschema  # type: ignore[import-untyped]
import pytest

from ksadk.events.canonical import (
    ApprovalRequest,
    ApprovalResponse,
    ContextCompactionCompleted,
    ContextCompactionStarted,
    ContinuationCreated,
    ContinuationResumed,
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
    ItemKind,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    SourceRef,
    StructuredInputRequest,
    StructuredInputResponse,
    UsageReported,
)
from ksadk.events.content import (
    ArtifactContent,
    ContentSnapshot,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.reducer import ItemProjection, RunProjection
from ksadk.events.v1_compat import (
    ALL_V1_EVENT_TYPES,
    A2ATaskProjectionRef,
    A2UIInteractionProjectionRef,
    A2UISurfaceProjectionRef,
    RuntimeEventV1,
    RuntimeEventV1Parser,
    RuntimeEventV1ProjectionContext,
    V1ProjectionContextRequiredError,
    project_to_v1,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "runtime_event_v1.json"
SCHEMA_PATH = (
    Path(__file__).parent.parent.parent
    / "ksadk_runtime_common"
    / "schemas"
    / "runtime_event_v1.json"
)

SOURCE = SourceRef(
    framework="adk",
    native_event_id="native-event",
    native_run_id="native-run",
)
A2UI_SOURCE = SourceRef(
    framework="ksadk",
    protocol="a2ui",
    native_event_id="a2ui-native-event",
)


def _envelope(
    seq: int,
    *,
    event_id: str | None = None,
    scope_id: str = "scope-1",
    source: SourceRef = SOURCE,
) -> dict:
    return {
        "schema_version": 2,
        "event_id": event_id or f"event-{seq}",
        "seq": seq,
        "timestamp": float(seq),
        "run_id": "run-1",
        "run_seq": seq,
        "scope_id": scope_id,
        "source": source,
    }


def _updated(
    text: str,
    *,
    seq: int = 1,
    item_id: str = "item-1",
    op: Literal["append", "replace"] = "append",
    item_kind: ItemKind = "message",
    source: SourceRef = SOURCE,
) -> ItemUpdated:
    return ItemUpdated(
        **_envelope(seq, source=source),
        item_id=item_id,
        item_kind=item_kind,
        op=op,
        update=TextContent(part_id="part-0", text=text),
    )


def _completed(
    text: str,
    *,
    seq: int = 2,
    item_id: str = "item-1",
    item_kind: ItemKind = "message",
    scope_id: str = "scope-1",
    source: SourceRef = SOURCE,
) -> ItemCompleted:
    return ItemCompleted(
        **_envelope(seq, scope_id=scope_id, source=source),
        item_id=item_id,
        item_kind=item_kind,
        snapshot=ContentSnapshot(parts=(TextContent(part_id="part-0", text=text),)),
    )


def _snapshot_replaced(
    text: str,
    *,
    seq: int = 2,
    item_id: str = "item-1",
    item_kind: ItemKind = "message",
    source: SourceRef = SOURCE,
) -> ItemSnapshotReplaced:
    return ItemSnapshotReplaced(
        **_envelope(seq, source=source),
        item_id=item_id,
        item_kind=item_kind,
        snapshot=ContentSnapshot(parts=(TextContent(part_id="part-0", text=text),)),
    )


def _message_projection(
    scope_id: str,
    item_id: str,
    text: str = "",
    *,
    phase: Literal["commentary", "final_answer"] = "final_answer",
) -> ItemProjection:
    return ItemProjection(
        scope_id=scope_id,
        item_id=item_id,
        item_kind="message",
        phase=phase,
        status="completed",
        parts=(TextContent(part_id="part-0", text=text),),
    )


BASE_PROJECTION = RunProjection(
    run_id="run-1",
    status="completed",
    items=(
        _message_projection("scope-1", "item-1", "hello"),
        _message_projection("scope-1", "item-left", "same"),
        _message_projection("scope-1", "item-right", "same"),
        _message_projection("scope-left", "item-1", "left"),
        _message_projection("scope-right", "item-1", "right"),
        ItemProjection(
            scope_id="scope-1",
            item_id="item-multi",
            item_kind="message",
            phase="final_answer",
            status="completed",
            parts=(
                TextContent(part_id="part-a", text="A"),
                TextContent(part_id="part-b", text="B"),
            ),
        ),
        ItemProjection(
            scope_id="scope-1",
            item_id="call-item",
            item_kind="tool_call",
            status="completed",
            parts=(
                ToolCallContent(
                    part_id="call-part",
                    call_id="call-1",
                    name="search",
                    arguments={"q": "x"},
                ),
            ),
        ),
        ItemProjection(
            scope_id="scope-1",
            item_id="artifact-item",
            item_kind="artifact",
            status="completed",
            parts=(
                ArtifactContent(
                    part_id="artifact-part",
                    artifact_id="artifact-1",
                    name="report.md",
                ),
            ),
        ),
    ),
)


def _context(
    projection: RunProjection | None = BASE_PROJECTION,
    **kwargs,
) -> RuntimeEventV1ProjectionContext:
    kwargs.setdefault(
        "artifact_versions",
        {("scope-1", "artifact-item", "artifact-1"): 1},
    )
    return RuntimeEventV1ProjectionContext.from_projection(
        projection,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        **kwargs,
    )


V1_CONTEXT = _context()


def test_snapshot_only_defers_content_until_run_output_refs_are_authoritative() -> None:
    output_refs = (OutputRef(scope_id="scope-1", item_id="item-1", part_id="part-0"),)
    projection = BASE_PROJECTION.model_copy(update={"output_refs": output_refs})
    context = _context(projection)
    completed = RunCompleted(
        **_envelope(3),
        status="completed",
        output_refs=output_refs,
    )

    assert project_to_v1(_updated("hel"), mode="snapshot_only", context=context) == ()
    assert (
        project_to_v1(_snapshot_replaced("hello", seq=2), mode="snapshot_only", context=context)
        == ()
    )
    assert project_to_v1(_completed("hello"), mode="snapshot_only", context=context) == ()
    projected = project_to_v1(completed, mode="snapshot_only", context=context)

    assert [(event.event_type, event.payload) for event in projected] == [
        ("text.completed", {"text": "hello"}),
        (
            "run.completed",
            {
                "status": "completed",
                "output_refs": [{"scope_id": "scope-1", "item_id": "item-1", "part_id": "part-0"}],
                "scope_id": "scope-1",
                "source_event_id": "native-event",
            },
        ),
    ]


def test_undeclared_mode_defaults_to_snapshot_only() -> None:
    assert project_to_v1(_updated("hello"), context=V1_CONTEXT) == ()
    assert project_to_v1(_completed("hello"), context=V1_CONTEXT) == ()


def test_snapshot_only_multiple_llm_items_emits_only_selected_final_output() -> None:
    intermediate = _message_projection("scope-1", "intermediate", "intermediate")
    final = _message_projection("scope-1", "final", "final")
    output_refs = (OutputRef(scope_id="scope-1", item_id="final", part_id="part-0"),)
    projection = RunProjection(
        run_id="run-1",
        status="completed",
        items=(intermediate, final),
        output_refs=output_refs,
    )
    context = _context(projection)
    completed = RunCompleted(
        **_envelope(30),
        status="completed",
        output_refs=output_refs,
    )

    assert (
        project_to_v1(
            _completed("intermediate", seq=28, item_id="intermediate"),
            context=context,
        )
        == ()
    )
    assert (
        project_to_v1(
            _completed("final", seq=29, item_id="final"),
            context=context,
        )
        == ()
    )
    projected = project_to_v1(completed, context=context)
    parser = RuntimeEventV1Parser()
    for event in projected:
        parser.feed(event)

    assert [event.event_type for event in projected] == ["text.completed", "run.completed"]
    assert parser.transcript()["items"][0]["text"] == "final"


def test_snapshot_only_preserves_output_ref_and_part_order() -> None:
    first = ItemProjection(
        scope_id="scope-1",
        item_id="first",
        item_kind="message",
        phase="final_answer",
        status="completed",
        parts=(
            TextContent(part_id="part-a", text="A"),
            TextContent(part_id="part-b", text="B"),
        ),
    )
    second = _message_projection("scope-1", "second", "second")
    output_refs = (
        OutputRef(scope_id="scope-1", item_id="second", part_id="part-0"),
        OutputRef(scope_id="scope-1", item_id="first", part_id="part-b"),
    )
    projection = RunProjection(
        run_id="run-1",
        status="completed",
        items=(first, second),
        output_refs=output_refs,
    )
    completed = RunCompleted(**_envelope(35), status="completed", output_refs=output_refs)

    projected = project_to_v1(completed, context=_context(projection))

    assert [event.payload.get("text") for event in projected] == [
        "second",
        "B",
        None,
    ]


def test_snapshot_only_run_completion_requires_reducer_projection() -> None:
    missing_projection = _context(None)
    completed = RunCompleted(**_envelope(3), status="completed", output_refs=())

    with pytest.raises(V1ProjectionContextRequiredError, match="RunProjection"):
        project_to_v1(completed, context=missing_projection)


def test_snapshot_only_rejects_projection_from_another_run_before_output() -> None:
    output_refs = (OutputRef(scope_id="scope-1", item_id="item-1"),)
    wrong_run = BASE_PROJECTION.model_copy(
        update={"run_id": "run-other", "output_refs": output_refs}
    )
    completed = RunCompleted(**_envelope(36), status="completed", output_refs=output_refs)

    with pytest.raises(V1ProjectionContextRequiredError, match="run_id"):
        project_to_v1(completed, context=_context(wrong_run))


def test_snapshot_only_requires_completed_terminal_projection() -> None:
    output_refs = (OutputRef(scope_id="scope-1", item_id="item-1"),)
    running = BASE_PROJECTION.model_copy(update={"status": "running", "output_refs": output_refs})
    completed = RunCompleted(**_envelope(37), status="completed", output_refs=output_refs)

    with pytest.raises(V1ProjectionContextRequiredError, match="status.*completed"):
        project_to_v1(completed, context=_context(running))


def test_identity_replace_completed_overwrites_instead_of_appending() -> None:
    parser = RuntimeEventV1Parser()
    events = (
        *project_to_v1(_updated("hel", seq=1), mode="identity_replace", context=V1_CONTEXT),
        *project_to_v1(_completed("hello", seq=2), mode="identity_replace", context=V1_CONTEXT),
    )

    for event in events:
        parser.feed(event)

    assert parser.transcript()["items"][0]["text"] == "hello"
    assert [event.payload["operation"] for event in events] == ["append", "replace"]


def test_replace_update_is_never_projected_as_an_unannotated_delta() -> None:
    (event,) = project_to_v1(
        _updated("correct", op="replace"),
        mode="identity_replace",
        context=V1_CONTEXT,
    )

    assert event.event_type == "text.delta"
    assert event.payload == {
        "text": "correct",
        "scope_id": "scope-1",
        "item_id": "item-1",
        "part_id": "part-0",
        "operation": "replace",
        "source_event_id": "native-event",
    }


def test_identity_replace_fails_closed_for_item_snapshot_replacement() -> None:
    event = ItemSnapshotReplaced(
        **_envelope(2),
        item_id="item-1",
        item_kind="message",
        snapshot=ContentSnapshot(
            parts=(
                TextContent(part_id="fresh-a", text="A"),
                TextContent(part_id="fresh-b", text="B"),
            )
        ),
    )

    with pytest.raises(V1ProjectionContextRequiredError, match="item-level snapshot"):
        project_to_v1(event, mode="identity_replace", context=V1_CONTEXT)


def test_a2ui_item_snapshot_replace_validates_typed_ref_then_fails_closed() -> None:
    context = _context(
        a2ui_surfaces={
            ("scope-1", "surface-item"): A2UISurfaceProjectionRef(
                surface_id="surface-1", catalog="basic"
            )
        }
    )
    event = ItemSnapshotReplaced(
        **_envelope(3, source=A2UI_SOURCE),
        item_id="surface-item",
        item_kind="data",
        snapshot=ContentSnapshot(
            parts=(
                DataContent(part_id="title", data={"text": "Fresh"}),
                DataContent(part_id="actions", data={"buttons": []}),
            )
        ),
    )

    with pytest.raises(V1ProjectionContextRequiredError, match="item-level snapshot"):
        project_to_v1(event, mode="identity_replace", context=context)


def test_a2ui_item_snapshot_replace_without_typed_ref_fails_before_projection() -> None:
    event = ItemSnapshotReplaced(
        **_envelope(3, source=A2UI_SOURCE),
        item_id="surface-item",
        item_kind="data",
        snapshot=ContentSnapshot(parts=(DataContent(part_id="title", data={"text": "Fresh"}),)),
    )

    with pytest.raises(V1ProjectionContextRequiredError, match="typed surface projection ref"):
        project_to_v1(event, mode="identity_replace", context=V1_CONTEXT)


def test_artifact_snapshot_replace_fails_closed_even_with_typed_refs() -> None:
    source = SourceRef(
        framework="a2a",
        native_event_id="a2a-snapshot",
        native_run_id="task-native",
    )
    context = _context(
        a2a_tasks={
            ("run-1", "scope-1"): A2ATaskProjectionRef(task_id="task-1", origin="a2a://space/agent")
        },
        artifact_versions={
            ("scope-1", "artifact-item", "artifact-1"): 2,
        },
    )
    event = ItemSnapshotReplaced(
        **_envelope(4, source=source),
        item_id="artifact-item",
        item_kind="artifact",
        snapshot=ContentSnapshot(
            parts=(
                ArtifactContent(
                    part_id="artifact-part",
                    artifact_id="artifact-1",
                    name="report.md",
                ),
            )
        ),
    )

    with pytest.raises(V1ProjectionContextRequiredError, match="item-level snapshot"):
        project_to_v1(event, mode="identity_replace", context=context)


def test_replaying_same_v1_event_id_is_a_noop() -> None:
    parser = RuntimeEventV1Parser()
    (event,) = project_to_v1(_updated("hello"), mode="identity_replace", context=V1_CONTEXT)

    parser.feed(event)
    parser.feed(event)

    assert parser.transcript()["items"][0]["text"] == "hello"


def test_identical_text_in_distinct_v2_items_remains_distinct() -> None:
    parser = RuntimeEventV1Parser()
    events = (
        *project_to_v1(
            _completed("same", seq=1, item_id="item-left"),
            mode="identity_replace",
            context=V1_CONTEXT,
        ),
        *project_to_v1(
            _completed("same", seq=2, item_id="item-right"),
            mode="identity_replace",
            context=V1_CONTEXT,
        ),
    )

    for event in events:
        parser.feed(event)

    items = parser.transcript()["items"]
    assert [(item["item_id"], item["text"]) for item in items] == [
        ("item-left", "same"),
        ("item-right", "same"),
    ]


def test_identity_key_includes_scope_and_part() -> None:
    parser = RuntimeEventV1Parser()
    left = _completed("left", seq=1, scope_id="scope-left")
    right = _completed("right", seq=2, scope_id="scope-right")
    multipart = ItemCompleted(
        **_envelope(3),
        item_id="item-multi",
        item_kind="message",
        snapshot=ContentSnapshot(
            parts=(
                TextContent(part_id="part-a", text="A"),
                TextContent(part_id="part-b", text="B"),
            )
        ),
    )

    for canonical in (left, right, multipart):
        for event in project_to_v1(canonical, mode="identity_replace", context=V1_CONTEXT):
            parser.feed(event)

    items = parser.transcript()["items"]
    assert [(item["scope_id"], item["part_id"], item["text"]) for item in items] == [
        ("scope-left", "part-0", "left"),
        ("scope-right", "part-0", "right"),
        ("scope-1", "part-a", "A"),
        ("scope-1", "part-b", "B"),
    ]


def test_legacy_wire_without_identity_uses_invocation_phase_fallback() -> None:
    parser = RuntimeEventV1Parser()
    base = {
        "agent_id": "agent-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "invocation_id": "run-1",
        "timestamp": 1.0,
        "phase": "final_answer",
    }
    parser.feed(
        RuntimeEventV1.from_dict(
            {
                **base,
                "schema_version": 1,
                "event_id": "legacy-1",
                "event_type": "text.delta",
                "seq_id": 1,
                "payload": {"text": "hel"},
            }
        )
    )
    parser.feed(
        RuntimeEventV1.from_dict(
            {
                **base,
                "schema_version": 1,
                "event_id": "legacy-2",
                "event_type": "text.completed",
                "seq_id": 2,
                "payload": {"text": "lo"},
            }
        )
    )

    assert parser.transcript()["items"] == [
        {
            "kind": "text",
            "invocation_id": "run-1",
            "phase": "final_answer",
            "text": "hello",
            "final": True,
        }
    ]


def test_projection_reads_legacy_envelope_only_from_compatibility_context() -> None:
    (event,) = project_to_v1(_completed("hello"), mode="identity_replace", context=V1_CONTEXT)

    assert event.agent_id == "agent-1"
    assert event.user_id == "user-1"
    assert event.session_id == "session-1"
    assert event.invocation_id == "run-1"
    assert event.seq_id == 2
    assert event.timestamp == 2.0


def test_message_phase_comes_from_compatibility_context_lookup() -> None:
    commentary_projection = RunProjection(
        run_id="run-1",
        items=(_message_projection("scope-1", "item-1", "working", phase="commentary"),),
    )
    commentary_context = _context(commentary_projection)
    (event,) = project_to_v1(
        _completed("working"),
        mode="identity_replace",
        context=commentary_context,
    )

    assert event.phase == "commentary"


def test_message_projection_without_phase_context_is_rejected() -> None:
    context_without_item = _context(RunProjection(run_id="run-1"))
    with pytest.raises(V1ProjectionContextRequiredError, match="message phase"):
        project_to_v1(
            _completed("ambiguous"),
            mode="identity_replace",
            context=context_without_item,
        )


def test_message_start_uses_explicit_phase_but_still_requires_envelope_context() -> None:
    started = ItemStarted(
        **_envelope(3),
        item_id="message-with-initial",
        item_kind="message",
        phase="commentary",
        initial=ContentSnapshot(parts=(TextContent(part_id="part-0", text="working"),)),
    )

    with pytest.raises(V1ProjectionContextRequiredError, match="envelope"):
        project_to_v1(started, mode="identity_replace")

    (event,) = project_to_v1(started, mode="identity_replace", context=V1_CONTEXT)

    assert event.phase == "commentary"
    assert event.payload["operation"] == "replace"


@pytest.mark.parametrize(
    "event",
    [
        RunProgress(**_envelope(31), status="running", progress=0.5),
        UsageReported(
            **_envelope(32),
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
        ),
    ],
    ids=("run", "usage"),
)
def test_any_actual_v1_output_requires_nonempty_envelope_context(event) -> None:
    with pytest.raises(V1ProjectionContextRequiredError, match="envelope"):
        project_to_v1(event)


def test_v1_event_ids_use_one_collision_free_deterministic_namespace() -> None:
    left = ItemCompleted(
        **_envelope(33, event_id="x"),
        item_id="item-1",
        item_kind="message",
        snapshot=ContentSnapshot(
            parts=(
                TextContent(part_id="part-a", text="left-a"),
                TextContent(part_id="part-b", text="left-b"),
            )
        ),
    )
    right = ItemUpdated(
        **_envelope(34, event_id="x:v1:0"),
        item_id="item-1",
        item_kind="message",
        op="append",
        update=TextContent(part_id="part-0", text="right"),
    )

    left_projected = project_to_v1(left, mode="identity_replace", context=V1_CONTEXT)
    left_replayed = project_to_v1(left, mode="identity_replace", context=V1_CONTEXT)
    (right_projected,) = project_to_v1(right, mode="identity_replace", context=V1_CONTEXT)

    assert [event.event_id for event in left_projected] == [
        event.event_id for event in left_replayed
    ]
    assert left_projected[0].event_id != right_projected.event_id


@pytest.mark.parametrize(
    ("event", "expected_types"),
    [
        (RunStarted(**_envelope(1), status="running"), ("run.started",)),
        (RunProgress(**_envelope(2), status="running", progress=0.5), ("run.progress",)),
        (
            RunInterrupted(**_envelope(3), status="interrupted", reason="approval"),
            ("run.interrupted",),
        ),
        (
            RunCompleted(
                **_envelope(4),
                status="completed",
                output_refs=(),
            ),
            ("run.completed",),
        ),
        (
            RunFailed(
                **_envelope(5),
                status="failed",
                error=ErrorInfo(code="boom", source="adk", scope_id="scope-1"),
            ),
            ("run.failed",),
        ),
        (RunCanceled(**_envelope(6), status="canceled", reason="user"), ("run.canceled",)),
        (
            ItemStarted(
                **_envelope(7),
                item_id="call-item",
                item_kind="tool_call",
                initial=ContentSnapshot(
                    parts=(
                        ToolCallContent(
                            part_id="call-part",
                            call_id="call-1",
                            name="search",
                            arguments={"q": "x"},
                        ),
                    )
                ),
            ),
            ("tool.call.begin",),
        ),
        (
            ItemCompleted(
                **_envelope(8),
                item_id="result-item",
                item_kind="tool_result",
                snapshot=ContentSnapshot(
                    parts=(
                        ToolResultContent(
                            part_id="result-part",
                            call_id="call-1",
                            result={"answer": 1},
                        ),
                    )
                ),
            ),
            ("tool.call.end",),
        ),
        (
            ItemStarted(
                **_envelope(9),
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
            ("artifact.created",),
        ),
        (
            InteractionRequested(
                **_envelope(10),
                interaction_id="approval-1",
                interaction_kind="approval",
                request=ApprovalRequest(call_id="call-1", kind="tool", detail={"risk": "high"}),
            ),
            ("approval.requested",),
        ),
        (
            InteractionResolved(
                **_envelope(11),
                interaction_id="approval-1",
                interaction_kind="approval",
                response=ApprovalResponse(decision="approved"),
            ),
            ("approval.resolved",),
        ),
        (
            ContinuationCreated(
                **_envelope(12),
                continuation_id="checkpoint-1",
                continuation_kind="graph_checkpoint",
                resumable=True,
                ref={"granularity": "snapshot"},
            ),
            ("checkpoint.created",),
        ),
        (
            ContinuationResumed(
                **_envelope(13),
                continuation_id="checkpoint-1",
                continuation_kind="graph_checkpoint",
                resume_attempt_id="attempt-1",
            ),
            ("checkpoint.resumed",),
        ),
        (
            ContextCompactionStarted(**_envelope(14), trigger="token_budget"),
            ("context.compaction.started",),
        ),
        (
            ContextCompactionCompleted(
                **_envelope(15), trigger="token_budget", compacted_until_seq=10
            ),
            ("context.compaction.completed",),
        ),
        (
            UsageReported(
                **_envelope(16),
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                cached_tokens=2,
                reasoning_tokens=1,
            ),
            ("usage.reported",),
        ),
    ],
    ids=(
        "run_started",
        "run_progress",
        "run_interrupted",
        "run_completed",
        "run_failed",
        "run_canceled",
        "tool_call",
        "tool_result",
        "artifact",
        "approval_requested",
        "approval_resolved",
        "graph_checkpoint_created",
        "graph_checkpoint_resumed",
        "compaction_started",
        "compaction_completed",
        "usage",
    ),
)
def test_non_text_families_have_explicit_v1_mappings(event, expected_types) -> None:
    assert (
        tuple(projected.event_type for projected in project_to_v1(event, context=V1_CONTEXT))
        == expected_types
    )


@pytest.mark.parametrize(
    "event",
    [
        ItemStarted(**_envelope(20), item_id="message-1", item_kind="message"),
        ItemFailed(
            **_envelope(21),
            item_id="item-1",
            item_kind="message",
            error=ErrorInfo(code="failed", source="adk", scope_id="scope-1"),
        ),
        ItemCompleted(
            **_envelope(22),
            item_id="data-1",
            item_kind="data",
            snapshot=ContentSnapshot(
                parts=(DataContent(part_id="data-part", data={"surface_id": "surface-1"}),)
            ),
        ),
        InteractionRequested(
            **_envelope(23),
            interaction_id="input-1",
            interaction_kind="structured_input",
            request=StructuredInputRequest(prompt="value?", schema={"type": "string"}),
        ),
        ContinuationCreated(
            **_envelope(24),
            continuation_id="thread-1",
            continuation_kind="thread_resume",
            resumable=True,
            ref={"thread_id": "thread-1"},
        ),
    ],
    ids=("message_started", "item_failed", "data_item", "structured_input", "thread_resume"),
)
def test_unsupported_v2_semantics_are_explicitly_suppressed(event) -> None:
    assert project_to_v1(event, context=V1_CONTEXT) == ()


def test_a2ui_data_lifecycle_and_interaction_use_typed_context_refs() -> None:
    context = _context(
        a2ui_surfaces={
            ("scope-1", "surface-item"): A2UISurfaceProjectionRef(
                surface_id="surface-1", catalog="basic"
            )
        },
        a2ui_interactions={
            ("scope-1", "ui-action"): A2UIInteractionProjectionRef(
                surface_id="surface-1", block_id="button-1"
            )
        },
    )
    started = ItemStarted(
        **_envelope(40, source=A2UI_SOURCE),
        item_id="surface-item",
        item_kind="data",
        initial=ContentSnapshot(
            parts=(DataContent(part_id="surface-part", data={"title": "Choose"}),)
        ),
    )
    updated = ItemUpdated(
        **_envelope(41, source=A2UI_SOURCE),
        item_id="surface-item",
        item_kind="data",
        op="replace",
        update=DataContent(part_id="surface-part", data={"title": "Updated"}),
    )
    completed = ItemCompleted(
        **_envelope(42, source=A2UI_SOURCE),
        item_id="surface-item",
        item_kind="data",
        snapshot=ContentSnapshot(
            parts=(DataContent(part_id="surface-part", data={"title": "Done"}),)
        ),
    )
    requested = InteractionRequested(
        **_envelope(43, source=A2UI_SOURCE),
        interaction_id="ui-action",
        interaction_kind="structured_input",
        request=StructuredInputRequest(prompt="choose", schema={"type": "string"}),
    )
    resolved = InteractionResolved(
        **_envelope(44, source=A2UI_SOURCE),
        interaction_id="ui-action",
        interaction_kind="structured_input",
        response=StructuredInputResponse(data="selected"),
    )

    projected = tuple(
        projected_event
        for canonical in (started, updated, completed, requested, resolved)
        for projected_event in project_to_v1(canonical, context=context)
    )

    assert [event.event_type for event in projected] == [
        "a2ui.surface.begin",
        "a2ui.surface.update",
        "a2ui.surface.end",
        "a2ui.interaction",
        "a2ui.action",
    ]
    assert all(event.payload["surface_id"] == "surface-1" for event in projected)


def test_a2ui_protocol_item_with_missing_surface_identity_is_rejected() -> None:
    event = ItemStarted(
        **_envelope(45, source=A2UI_SOURCE),
        item_id="surface-item",
        item_kind="data",
        initial=ContentSnapshot(
            parts=(DataContent(part_id="surface-part", data={"title": "Choose"}),)
        ),
    )

    incomplete_context = _context(
        a2ui_surfaces={("scope-1", "surface-item"): A2UISurfaceProjectionRef(surface_id="")}
    )

    with pytest.raises(V1ProjectionContextRequiredError, match="A2UI surface"):
        project_to_v1(event, context=incomplete_context)


def test_non_protocol_data_and_structured_input_remain_suppressed() -> None:
    data = ItemCompleted(
        **_envelope(46),
        item_id="ordinary-data",
        item_kind="data",
        snapshot=ContentSnapshot(parts=(DataContent(part_id="data-part", data={"value": 1}),)),
    )
    interaction = InteractionRequested(
        **_envelope(47),
        interaction_id="ordinary-input",
        interaction_kind="structured_input",
        request=StructuredInputRequest(prompt="value", schema={"type": "string"}),
    )

    assert project_to_v1(data, context=V1_CONTEXT) == ()
    assert project_to_v1(interaction, context=V1_CONTEXT) == ()


def test_a2a_task_lifecycle_and_artifact_keep_v1_protocol_family() -> None:
    a2a_source = SourceRef(
        framework="a2a",
        native_event_id="a2a-native",
        native_run_id="task-native",
    )
    context = _context(
        a2a_tasks={
            ("run-1", "scope-1"): A2ATaskProjectionRef(task_id="task-1", origin="a2a://space/agent")
        }
    )
    started = RunStarted(**_envelope(50, source=a2a_source), status="running")
    progress = RunProgress(**_envelope(51, source=a2a_source), status="running", progress=0.5)
    completed = RunCompleted(**_envelope(52, source=a2a_source), status="completed", output_refs=())
    artifact = ItemCompleted(
        **_envelope(53, source=a2a_source),
        item_id="artifact-item",
        item_kind="artifact",
        snapshot=ContentSnapshot(
            parts=(
                ArtifactContent(
                    part_id="artifact-part",
                    artifact_id="artifact-1",
                    name="report.md",
                ),
            )
        ),
    )

    projected = tuple(
        projected_event
        for canonical in (started, progress, completed, artifact)
        for projected_event in project_to_v1(canonical, context=context)
    )

    assert [event.event_type for event in projected] == [
        "a2a.task.created",
        "a2a.task.status",
        "a2a.task.status",
        "a2a.task.artifact",
    ]
    assert all(event.payload["task_id"] == "task-1" for event in projected)
    assert all(event.payload["origin"] == "a2a://space/agent" for event in projected)


def test_a2a_lifecycle_without_typed_task_ref_is_rejected() -> None:
    a2a_source = SourceRef(framework="a2a", native_run_id="task-native")
    event = RunStarted(**_envelope(54, source=a2a_source), status="running")

    with pytest.raises(V1ProjectionContextRequiredError, match="A2A task"):
        project_to_v1(event, context=V1_CONTEXT)


def test_artifact_versions_are_explicit_and_can_advance_for_same_artifact() -> None:
    created = ItemStarted(
        **_envelope(55),
        item_id="versioned-artifact",
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
    updated = ItemUpdated(
        **_envelope(56),
        item_id="versioned-artifact",
        item_kind="artifact",
        op="replace",
        update=ArtifactContent(
            part_id="artifact-part",
            artifact_id="artifact-1",
            name="report.md",
        ),
    )
    version_one = _context(artifact_versions={("scope-1", "versioned-artifact", "artifact-1"): 1})
    version_two = _context(artifact_versions={("scope-1", "versioned-artifact", "artifact-1"): 2})

    (created_v1,) = project_to_v1(created, context=version_one)
    (updated_v1,) = project_to_v1(updated, context=version_two)

    assert created_v1.payload["version"] == 1
    assert updated_v1.payload["version"] == 2


def test_artifact_projection_without_explicit_positive_version_fails_closed() -> None:
    event = ItemCompleted(
        **_envelope(57),
        item_id="unversioned-artifact",
        item_kind="artifact",
        snapshot=ContentSnapshot(
            parts=(
                ArtifactContent(
                    part_id="artifact-part",
                    artifact_id="artifact-1",
                    name="report.md",
                ),
            )
        ),
    )

    with pytest.raises(V1ProjectionContextRequiredError, match="artifact version"):
        project_to_v1(event, context=V1_CONTEXT)


def test_artifact_versions_are_isolated_by_scope_item_and_artifact_id() -> None:
    context = _context(
        artifact_versions={
            ("scope-left", "artifact-item", "shared-artifact"): 3,
            ("scope-right", "artifact-item", "shared-artifact"): 7,
        }
    )
    events = tuple(
        ItemCompleted(
            **_envelope(seq, scope_id=scope_id),
            item_id="artifact-item",
            item_kind="artifact",
            snapshot=ContentSnapshot(
                parts=(
                    ArtifactContent(
                        part_id="artifact-part",
                        artifact_id="shared-artifact",
                        name="report.md",
                    ),
                )
            ),
        )
        for seq, scope_id in ((58, "scope-left"), (59, "scope-right"))
    )

    versions = [project_to_v1(event, context=context)[0].payload["version"] for event in events]

    assert versions == [3, 7]


def test_artifact_parser_keeps_same_name_in_distinct_identities_separate() -> None:
    parser = RuntimeEventV1Parser()
    context = _context(
        artifact_versions={
            ("scope-left", "artifact-left", "artifact-left"): 1,
            ("scope-right", "artifact-right", "artifact-right"): 1,
        }
    )
    events = (
        ItemCompleted(
            **_envelope(60, scope_id="scope-left"),
            item_id="artifact-left",
            item_kind="artifact",
            snapshot=ContentSnapshot(
                parts=(
                    ArtifactContent(
                        part_id="artifact-part",
                        artifact_id="artifact-left",
                        name="report.md",
                    ),
                )
            ),
        ),
        ItemCompleted(
            **_envelope(61, scope_id="scope-right"),
            item_id="artifact-right",
            item_kind="artifact",
            snapshot=ContentSnapshot(
                parts=(
                    ArtifactContent(
                        part_id="artifact-part",
                        artifact_id="artifact-right",
                        name="report.md",
                    ),
                )
            ),
        ),
    )
    for canonical in events:
        for projected in project_to_v1(canonical, context=context):
            parser.feed(projected)

    items = parser.transcript()["items"]
    assert [(item["scope_id"], item["item_id"], item["name"]) for item in items] == [
        ("scope-left", "artifact-left", "report.md"),
        ("scope-right", "artifact-right", "report.md"),
    ]


def test_artifact_parser_keeps_legacy_name_fallback_for_old_wire() -> None:
    parser = RuntimeEventV1Parser()
    for event_id, event_type, version in (
        ("legacy-artifact-1", "artifact.created", 1),
        ("legacy-artifact-2", "artifact.updated", 2),
    ):
        parser.feed(
            RuntimeEventV1.from_dict(
                {
                    "schema_version": 1,
                    "event_id": event_id,
                    "event_type": event_type,
                    "timestamp": 1.0,
                    "agent_id": "agent-1",
                    "user_id": "user-1",
                    "session_id": "session-1",
                    "invocation_id": "run-1",
                    "seq_id": version,
                    "payload": {"name": "report.md", "version": version},
                }
            )
        )

    assert parser.transcript()["items"] == [
        {
            "kind": "artifact",
            "name": "report.md",
            "version": 2,
            "text": "",
            "invocation_id": "run-1",
        }
    ]


def test_tool_parser_keys_same_call_id_by_invocation_and_scope() -> None:
    parser = RuntimeEventV1Parser()
    for seq, scope in ((62, "scope-left"), (63, "scope-right")):
        started = ItemStarted(
            **_envelope(seq, scope_id=scope),
            item_id="call-item",
            item_kind="tool_call",
            initial=ContentSnapshot(
                parts=(
                    ToolCallContent(
                        part_id="call-part",
                        call_id="call-1",
                        name="search",
                        arguments={},
                    ),
                )
            ),
        )
        for projected in project_to_v1(started, context=V1_CONTEXT):
            parser.feed(projected)

    assert [item["scope_id"] for item in parser.transcript()["items"]] == [
        "scope-left",
        "scope-right",
    ]


def test_projection_context_derives_tool_name_from_run_projection() -> None:
    result = ItemCompleted(
        **_envelope(64),
        item_id="result-item",
        item_kind="tool_result",
        snapshot=ContentSnapshot(
            parts=(
                ToolResultContent(
                    part_id="result-part",
                    call_id="call-1",
                    result={"answer": 1},
                ),
            )
        ),
    )

    (projected,) = project_to_v1(result, context=V1_CONTEXT)

    assert projected.payload["name"] == "search"


def test_tool_name_lookup_is_scoped_when_call_ids_are_reused() -> None:
    projection = RunProjection(
        run_id="run-1",
        status="completed",
        items=tuple(
            ItemProjection(
                scope_id=scope_id,
                item_id=f"{scope_id}-call",
                item_kind="tool_call",
                status="completed",
                parts=(
                    ToolCallContent(
                        part_id="call-part",
                        call_id="call-1",
                        name=name,
                        arguments={},
                    ),
                ),
            )
            for scope_id, name in (
                ("scope-left", "search-left"),
                ("scope-right", "search-right"),
            )
        ),
    )
    context = _context(projection)
    results = tuple(
        ItemCompleted(
            **_envelope(seq, scope_id=scope_id),
            item_id=f"{scope_id}-result",
            item_kind="tool_result",
            snapshot=ContentSnapshot(
                parts=(
                    ToolResultContent(
                        part_id="result-part",
                        call_id="call-1",
                        result={"ok": True},
                    ),
                )
            ),
        )
        for seq, scope_id in ((65, "scope-left"), (66, "scope-right"))
    )

    names = [project_to_v1(result, context=context)[0].payload["name"] for result in results]

    assert names == ["search-left", "search-right"]


def test_checked_in_identity_fixture_preserves_frozen_v1_wire_contract() -> None:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    for event in document["identity_replace_events"]:
        jsonschema.validate(instance=event, schema=schema)
        model = RuntimeEventV1.from_dict(event)
        assert model.to_dict() == event
        assert model.payload["operation"] in {"append", "replace"}
        assert model.payload["scope_id"]
        assert model.payload["item_id"]
        assert model.payload["part_id"]
        assert model.payload["source_event_id"]


def test_frozen_v1_fixture_roundtrips_through_compatibility_model() -> None:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    events = [{**document["base"], **event} for event in document["events"]]

    assert {event["event_type"] for event in events} == ALL_V1_EVENT_TYPES
    for event in events:
        jsonschema.validate(instance=event, schema=schema)
        assert RuntimeEventV1.from_dict(event).to_dict() == event
