"""Native finish snapshots must not replay text already sent over AG-UI."""

from types import SimpleNamespace

import pytest

from ksadk.agui._agent_types import _ThreadRun, _WireState
from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.events.canonical import (
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemUpdated,
    SourceRef,
)
from ksadk.events.content import ContentSnapshot, TextContent
from ksadk.runtime import RunHandle


def event(kind, text, *, item="item", part="part", scope="scope", seq=1):
    common = dict(
        schema_version=2, event_id=f"event-{seq}", seq=seq, timestamp=1,
        run_id="run", scope_id=scope, source=SourceRef(framework="langgraph"),
        item_id=item, item_kind="message",
    )
    content = TextContent(part_id=part, text=text)
    if kind in {"append", "replace"}:
        return ItemUpdated(**common, op=kind, update=content)
    event_type = ItemCompleted if kind == "completed" else ItemSnapshotReplaced
    return event_type(**common, snapshot=ContentSnapshot(parts=(content,)))


async def project(events):
    agent = KsadkAGUIAgent(name="test", _shared=SimpleNamespace())
    wire = _WireState("session", "run", "assistant", "reasoning")
    run = _ThreadRun(RunHandle(run_id="run", session_id="session", runtime_type="langgraph"))
    output = []
    for item in events:
        output.extend([part async for part in agent._project(item, wire, run)])
    return output, wire


def deltas(events):
    return [item.delta for item in events if item.type == "TEXT_MESSAGE_CONTENT"]


def snapshots(events):
    return [item.value for item in events if item.type == "CUSTOM"
            and item.name == "ksadk.text_message_snapshot"]


def visible_text(events):
    text = ""
    for item in events:
        if item.type == "TEXT_MESSAGE_CONTENT":
            text += item.delta
        elif item.type == "CUSTOM" and item.name == "ksadk.text_message_snapshot":
            text = item.value["content"]
    return text


@pytest.mark.parametrize("replacement", ["replace", "snapshot"])
async def test_repeated_authoritative_snapshot_does_not_reappend_the_same_part(replacement):
    output, wire = await project([
        event("append", "Hel"), event("append", "lo", seq=2),
        event(replacement, "Hello", seq=3), event(replacement, "Hello", seq=4),
        event("completed", "Hello", seq=5),
    ])
    assert deltas(output) == ["Hel", "lo"]
    assert [item.type for item in output].count("TEXT_MESSAGE_START") == 1
    assert [item.type for item in output].count("TEXT_MESSAGE_END") == 1
    assert wire.text_content == "Hello"


@pytest.mark.parametrize("replacement", ["replace", "snapshot", "completed"])
async def test_authoritative_prefix_extension_only_sends_the_missing_suffix(replacement):
    output, wire = await project([
        event("append", "Hello"), event(replacement, "Hello world", seq=2),
        event("completed", "Hello world", seq=3),
    ])
    assert deltas(output) == ["Hello", " world"]
    assert wire.text_content == "Hello world"


async def test_different_item_with_same_text_without_deltas_is_not_swallowed():
    output, wire = await project([
        event("append", "same"), event("completed", "same", seq=2),
        event("completed", "same", item="second-item", seq=3),
    ])
    assert deltas(output) == ["same", "same"]
    assert wire.text_content == "samesame"


async def test_different_part_with_same_text_is_published_and_only_its_replay_is_ignored():
    completed = event("completed", "same", seq=5)
    completed = completed.model_copy(update={"snapshot": ContentSnapshot(parts=(
        TextContent(part_id="part", text="same"), TextContent(part_id="second", text="same"),
    ))})
    output, wire = await project([
        event("append", "same"), event("replace", "same", part="second", seq=2),
        event("replace", "same", part="second", seq=3), completed,
    ])
    assert deltas(output) == ["same", "same"]
    assert wire.text_content == "samesame"


@pytest.mark.parametrize("replacement", ["replace", "snapshot", "completed"])
@pytest.mark.parametrize("corrected", ["corrected", "o", ""])
async def test_actual_rewrite_replaces_only_current_message_without_fake_restart(
    replacement, corrected,
):
    output, wire = await project([
        event("append", "old"), event(replacement, corrected, seq=2),
        event("completed", corrected, seq=3),
    ])
    assert deltas(output) == ["old"]
    assert snapshots(output) == [{"version": 1, "threadId": "session", "runId": "run",
                                  "messageId": "assistant", "content": corrected}]
    assert [item.type for item in output].count("TEXT_MESSAGE_START") == 1
    assert [item.type for item in output].count("TEXT_MESSAGE_END") == 1
    assert not any(item.type == "MESSAGES_SNAPSHOT" for item in output)
    assert visible_text(output) == wire.text_content == corrected


async def test_multipart_snapshot_rewrite_keeps_confirmed_parts_in_the_snapshot():
    snapshot = event("snapshot", "header", part="header", seq=3)
    snapshot = snapshot.model_copy(update={"snapshot": ContentSnapshot(parts=(
        TextContent(part_id="header", text="header"),
        TextContent(part_id="body", text="corrected"),
    ))})
    output, wire = await project([
        event("append", "header", part="header"),
        event("append", "draft", part="body", seq=2), snapshot,
    ])
    assert deltas(output) == ["header", "draft"]
    assert [item["content"] for item in snapshots(output)] == ["headercorrected"]
    assert visible_text(output) == "headercorrected"
    assert wire.text_content == "headercorrected"


@pytest.mark.parametrize("corrected", ["o", ""])
async def test_append_after_truncation_or_clear_uses_the_corrected_part(corrected):
    output, wire = await project([
        event("append", "old"), event("replace", corrected, seq=2),
        event("append", "!", seq=3), event("completed", corrected + "!", seq=4),
    ])
    assert deltas(output) == ["old", "!"]
    assert [item["content"] for item in snapshots(output)] == [corrected]
    assert visible_text(output) == wire.text_content == corrected + "!"


@pytest.mark.parametrize("kind", ["snapshot", "completed"])
@pytest.mark.parametrize("parts,expected", [
    ([("header", "H")], "Htail"),
    ([], "tail"),
    ([("body", "B"), ("header", "H")], "BHtail"),
])
async def test_item_snapshot_removes_or_reorders_its_parts_and_keeps_other_items(
    kind, parts, expected,
):
    replaced = event(kind, "unused", seq=4).model_copy(update={"snapshot": ContentSnapshot(
        parts=tuple(TextContent(part_id=part, text=text) for part, text in parts),
    )})
    output, wire = await project([
        event("append", "H", part="header"), event("append", "B", part="body", seq=2),
        event("append", "tail", item="other", part="header", seq=3), replaced,
    ])
    assert [item["content"] for item in snapshots(output)] == [expected]
    assert visible_text(output) == wire.text_content == expected


async def test_part_rewrite_preserves_another_part_and_same_item_id_in_another_scope():
    output, wire = await project([
        event("append", "H", part="header"), event("append", "draft", part="body", seq=2),
        event("append", "other", scope="another-scope", seq=3),
        event("replace", "corrected", part="body", seq=4),
    ])
    assert [item["content"] for item in snapshots(output)] == ["Hcorrectedother"]
    assert visible_text(output) == wire.text_content == "Hcorrectedother"


async def test_append_to_an_earlier_part_preserves_authoritative_part_order():
    output, wire = await project([
        event("append", "A", part="a"), event("append", "B", part="b", seq=2),
        event("append", "!", part="a", seq=3),
    ])
    assert deltas(output) == ["A", "B"]
    assert [item["content"] for item in snapshots(output)] == ["A!B"]
    assert visible_text(output) == wire.text_content == "A!B"


async def test_legitimate_repeated_append_is_not_snapshot_deduped():
    output, wire = await project([
        event("append", "ha"), event("append", "ha", seq=2),
        event("completed", "haha", seq=3),
    ])
    assert deltas(output) == ["ha", "ha"] and snapshots(output) == []
    assert visible_text(output) == wire.text_content == "haha"


async def test_failed_item_removes_only_its_text_across_scopes_and_keeps_other_output():
    from ksadk.events.canonical import ErrorInfo, ItemFailed

    original = event("append", "partial", seq=2)
    failure = ItemFailed(
        **original.model_dump(include={"schema_version", "event_id", "seq", "timestamp", "run_id",
                                       "scope_id", "source", "item_id", "item_kind"}),
        error=ErrorInfo(code="canceled", source="test", scope_id="scope", item_id="item"),
    )
    output, wire = await project([
        event("completed", "before", item="good"), original,
        event("append", "other", scope="another-scope", seq=3), failure,
        event("completed", "recovered", item="fallback", seq=4),
    ])
    assert visible_text(output) == wire.text_content == "beforeotherrecovered"
    assert [item["content"] for item in snapshots(output)] == ["beforeother"]
    assert not any(item.type == "MESSAGES_SNAPSHOT" for item in output)


@pytest.mark.parametrize("refs,expected", [
    ([("scope", "good"), ("another", "good")], "leftright"),
    ([("scope", "answer")], "recovered"),
    ([], "leftrecoveredright"),
])
async def test_completed_refs_reconcile_only_current_message(refs, expected):
    from ksadk.events.canonical import OutputRef, RunCompleted

    terminal = RunCompleted(
        schema_version=2, event_id="end", seq=4, timestamp=1, run_id="run",
        scope_id="scope", source=SourceRef(framework="langgraph"), status="completed",
        output_refs=tuple(OutputRef(scope_id=scope, item_id=item) for scope, item in refs),
    )
    output, wire = await project([
        event("completed", "left", item="good"),
        event("completed", "recovered", item="answer", seq=2),
        event("completed", "right", item="good", scope="another", seq=3), terminal,
    ])
    assert visible_text(output) == wire.text_content == expected
    assert not any(item.type == "MESSAGES_SNAPSHOT" for item in output)


async def test_completed_refs_select_parts_in_reference_order():
    from ksadk.events.canonical import OutputRef, RunCompleted

    terminal = RunCompleted(
        schema_version=2, event_id="end", seq=5, timestamp=1, run_id="run",
        scope_id="scope", source=SourceRef(framework="langgraph"), status="completed",
        output_refs=(
            OutputRef(scope_id="scope", item_id="answer", part_id="second"),
            OutputRef(scope_id="scope", item_id="answer", part_id="first"),
        ),
    )
    output, wire = await project([
        event("completed", "unselected", item="prior"),
        event("append", "A", item="answer", part="first", seq=2),
        event("append", "B", item="answer", part="second", seq=3),
        event("append", "discard", item="answer", part="third", seq=4),
        terminal,
    ])
    assert visible_text(output) == wire.text_content == "BA"
    assert [item["content"] for item in snapshots(output)] == ["BA"]
