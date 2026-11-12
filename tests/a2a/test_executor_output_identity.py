"""Exporter snapshots are addressed by scope/item/part, never text type."""

from types import SimpleNamespace

import pytest

from ksadk.a2a.executor import A2ARuntimeExecutor, _ArtifactStreamEmitter
from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.runtime import RunHandle
from tests.a2a.test_reasoning_streaming import _RecordingUpdater


@pytest.mark.asyncio
async def test_a2a_artifact_stream_coalesces_many_deltas_without_losing_text():
    updater = _RecordingUpdater()
    now = [0.0]
    emitter = _ArtifactStreamEmitter(updater, "task", clock=lambda: now[0])
    for _ in range(1000):
        await emitter.push("text", "字", identity=("scope", "message"))
        now[0] += 0.01
    await emitter.close()

    assert len(updater.artifacts) <= 25
    assert "".join(item["parts"][0].text for item in updater.artifacts) == "字" * 1000
    assert updater.artifacts[0]["append"] is False
    assert all(item["append"] for item in updater.artifacts[1:])
    assert updater.artifacts[-1]["last_chunk"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_existing", [False, True])
async def test_empty_snapshot_does_not_stall_first_visible_stream_output(clear_existing):
    updater = _RecordingUpdater()
    emitter = _ArtifactStreamEmitter(updater, "task", clock=lambda: 0.0)
    identity = ("scope", "message")
    if clear_existing:
        await emitter.push("text", "old", identity=identity)
        await emitter.push("text", " tail", identity=identity)
    await emitter.push("text", "", replace_snapshot=True, identity=identity)
    start = len(updater.artifacts)
    await emitter.push("text", "partial-before-pause", identity=identity)
    await emitter.push("text", "second-before-pause", identity=identity)

    # The provider can now wait indefinitely. Text must already be on the
    # wire, without close(), a third chunk or advancing the batching clock.
    visible = updater.artifacts[start:]
    assert any(part.text == "partial-before-pause" for row in visible for part in row["parts"])
    assert any(not row["append"] and row["parts"][0].text == "" for row in visible)
    await emitter.close()
    assert updater.artifacts[-1]["parts"][0].text == "second-before-pause"
    assert updater.artifacts[-1]["last_chunk"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_part_replacement_and_explicit_output_reference(empty):
    common = dict(
        schema_version=2,
        seq=0,
        timestamp=1.0,
        run_id="run",
        scope_id="scope",
        source=SourceRef(framework="adk"),
    )
    final_first = "" if empty else "corrected"
    events = [
        ItemUpdated(
            **common,
            event_id="first",
            item_id="message",
            item_kind="message",
            op="append",
            update=TextContent(part_id="first", text="old"),
        ),
        ItemUpdated(
            **common,
            event_id="second",
            item_id="message",
            item_kind="message",
            op="append",
            update=TextContent(part_id="second", text="tail"),
        ),
        ItemUpdated(
            **common,
            event_id="replace",
            item_id="message",
            item_kind="message",
            op="replace",
            update=TextContent(part_id="first", text=final_first),
        ),
        ItemCompleted(
            **common,
            event_id="complete",
            item_id="message",
            item_kind="message",
            snapshot=ContentSnapshot(
                parts=(
                    TextContent(part_id="first", text=final_first),
                    TextContent(part_id="second", text="tail"),
                )
            ),
        ),
        RunCompleted(
            **common,
            event_id="root",
            status="completed",
            output_refs=(OutputRef(scope_id="scope", item_id="message", part_id="second"),),
        ),
    ]

    async def stream(_handle):
        for event in events:
            yield event

    updater = _RecordingUpdater()
    result = await A2ARuntimeExecutor(SimpleNamespace(stream_task=stream))._run_runtime(
        SimpleNamespace(task_id="task"),
        updater,
        RunHandle(runtime_type="test", run_id="run", session_id="session"),
    )
    assert result == "tail"
    assert len({value["artifact_id"] for value in updater.artifacts}) == 1
    replacements = [value["parts"][0].text for value in updater.artifacts if not value["append"]]
    assert replacements == ["old", final_first + "tail", final_first + "tail"]
