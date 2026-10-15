import json
from pathlib import Path

import pytest

from ksadk.events.canonical import parse_runtime_event
from ksadk.events.reducer import StreamConformanceError, StreamReducer


def events():
    fixture = (
        Path(__file__).parents[1] / "a2a/fixtures/remote_agent/v1/a2a_stream_tool_terminal.jsonl"
    )
    return [
        parse_runtime_event(row["payload"])
        for line in fixture.read_text().splitlines()
        if (row := json.loads(line))["kind"] == "runtime_event"
    ]


def test_child_run_terminal_cannot_complete_root():
    stream = events()
    reducer = StreamReducer()
    reducer.apply(stream[0])
    child_terminal = stream[-1].model_copy(
        update={
            "scope_id": "child-scope-1",
            "parent_scope_id": "root-scope",
            "output_refs": (),
        }
    )
    with pytest.raises(StreamConformanceError, match="root"):
        reducer.apply(child_terminal)
    assert reducer.snapshot().status == "running"


def test_descriptor_identity_must_match_canonical_envelope():
    stream = events()
    reducer = StreamReducer()
    for event in stream[:3]:
        reducer.apply(event)
    descriptor = stream[3].model_dump(mode="json")
    descriptor["initial"]["parts"][0]["data"]["scope_id"] = "other-scope"
    with pytest.raises(StreamConformanceError, match="scope"):
        reducer.apply(parse_runtime_event(descriptor))
    assert reducer.snapshot().last_seq == 3


def test_managed_golden_full_and_cursor_replay_match():
    stream = events()
    live, replay = StreamReducer(), StreamReducer()
    for event in stream:
        live.apply(event)
    for event in [*stream[:13], *stream[:13], *stream[13:]]:
        replay.apply(event)
    assert live.snapshot() == replay.snapshot()
