from __future__ import annotations

import pytest
from google.adk.events import Event
from google.adk.events.event import NodeInfo
from google.genai import types

from ksadk.events.adapters.adk import ADKAdapterContext, ADKEventAdapter
from ksadk.events.canonical import ItemCompleted, ItemStarted, ItemUpdated
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent


def _event(
    native_id: str,
    *,
    partial: bool,
    parts: list[types.Part],
    path: str = "workflow/worker@1",
    invocation_id: str = "adk-invocation-1",
) -> Event:
    return Event(
        id=native_id,
        invocation_id=invocation_id,
        author="worker",
        node_info=NodeInfo(path=path),
        branch="ignored-branch",
        partial=partial,
        content=types.Content(role="model", parts=parts),
    )


def _text(event: ItemCompleted | ItemUpdated) -> str:
    part = event.snapshot.parts[0] if isinstance(event, ItemCompleted) else event.update
    assert isinstance(part, TextContent)
    return part.text


def test_partial_and_aggregate_share_one_message_identity() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")

    partial = adapter.map(_event("resp-1", partial=True, parts=[types.Part(text="hel")]), context)
    completed = adapter.map(
        _event("resp-1", partial=False, parts=[types.Part(text="hello")]), context
    )

    starts = [event for event in (*partial, *completed) if isinstance(event, ItemStarted)]
    updates = [event for event in (*partial, *completed) if isinstance(event, ItemUpdated)]
    completions = [event for event in (*partial, *completed) if isinstance(event, ItemCompleted)]
    assert len(starts) == 1
    assert len(updates) == 1
    assert updates[0].op == "append"
    assert _text(updates[0]) == "hel"
    assert len(completions) == 1
    assert _text(completions[0]) == "hello"
    assert {event.item_id for event in (*starts, *updates, *completions)} == {starts[0].item_id}


def test_identical_completed_responses_with_new_native_ids_remain_distinct() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")

    first = adapter.map(_event("resp-1", partial=False, parts=[types.Part(text="same")]), context)
    second = adapter.map(_event("resp-2", partial=False, parts=[types.Part(text="same")]), context)

    completions = [event for event in (*first, *second) if isinstance(event, ItemCompleted)]
    assert [_text(event) for event in completions] == ["same", "same"]
    assert len({event.item_id for event in completions}) == 2


def test_one_native_response_uses_distinct_reasoning_and_message_lanes() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")

    partial = adapter.map(
        _event(
            "resp-1",
            partial=True,
            parts=[types.Part(text="think", thought=True), types.Part(text="ans")],
        ),
        context,
    )
    final = adapter.map(
        _event(
            "resp-1",
            partial=False,
            parts=[types.Part(text="thinking", thought=True), types.Part(text="answer")],
        ),
        context,
    )

    all_events = (*partial, *final)
    starts = [event for event in all_events if isinstance(event, ItemStarted)]
    completions = [event for event in all_events if isinstance(event, ItemCompleted)]
    assert [event.item_kind for event in starts] == ["reasoning", "message"]
    assert [event.item_kind for event in completions] == ["reasoning", "message"]
    assert len({event.item_id for event in starts}) == 2
    assert {event.item_kind: _text(event) for event in completions} == {
        "reasoning": "thinking",
        "message": "answer",
    }
    assert {event.item_kind: event.item_id for event in starts} == {
        event.item_kind: event.item_id for event in completions
    }


def test_node_path_isolates_scopes_and_branch_is_only_empty_path_fallback() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")

    left = adapter.map(
        _event(
            "resp-shared",
            partial=False,
            parts=[types.Part(text="left")],
            path="wf/left/worker@1",
        ),
        context,
    )
    right = adapter.map(
        _event(
            "resp-shared",
            partial=False,
            parts=[types.Part(text="right")],
            path="wf/right/worker@1",
        ),
        context,
    )
    path_wins = _event(
        "resp-path-wins",
        partial=False,
        parts=[types.Part(text="path")],
        path="wf/path",
    )
    path_wins.branch = "branch-a"
    branch_only = _event(
        "resp-path-wins",
        partial=False,
        parts=[types.Part(text="branch")],
        path="",
    )
    branch_only.branch = "branch-a"

    path_events = adapter.map(path_wins, context)
    branch_events = adapter.map(branch_only, context)

    assert left[0].scope_id != right[0].scope_id
    assert path_events[0].scope_id != branch_events[0].scope_id
    assert path_events[0].source.metadata["path"] == "wf/path"
    assert branch_events[0].source.metadata["path_key"] == "branch-a"


def test_native_invocations_with_same_path_and_event_id_use_distinct_scopes() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="runtime-run-1")

    first = adapter.map(
        _event(
            "same-event",
            partial=False,
            parts=[types.Part(text="first")],
            invocation_id="adk-invocation-1",
        ),
        context,
    )
    second = adapter.map(
        _event(
            "same-event",
            partial=False,
            parts=[types.Part(text="second")],
            invocation_id="adk-invocation-2",
        ),
        context,
    )

    assert first[0].scope_id != second[0].scope_id
    assert first[0].source.native_run_id == "adk-invocation-1"
    assert second[0].source.native_run_id == "adk-invocation-2"


def test_finalized_tool_call_and_result_preserve_native_call_id() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")
    call_event = _event(
        "tool-event",
        partial=False,
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    id="call-7", name="lookup", args={"query": "weather"}
                )
            )
        ],
    )
    result_event = _event(
        "result-event",
        partial=False,
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id="call-7", name="lookup", response={"temperature": 23}
                )
            )
        ],
    )

    mapped = (*adapter.map(call_event, context), *adapter.map(result_event, context))
    completed = [event for event in mapped if isinstance(event, ItemCompleted)]
    tool_call = next(event for event in completed if event.item_kind == "tool_call")
    tool_result = next(event for event in completed if event.item_kind == "tool_result")
    call_content = tool_call.snapshot.parts[0]
    result_content = tool_result.snapshot.parts[0]
    assert isinstance(call_content, ToolCallContent)
    assert isinstance(result_content, ToolResultContent)
    assert call_content.call_id == "call-7"
    assert result_content.call_id == "call-7"
    assert tool_call.item_id != tool_result.item_id


def test_missing_native_tool_call_id_fails_closed() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")
    event = _event(
        "tool-event",
        partial=False,
        parts=[types.Part(function_call=types.FunctionCall(name="lookup", args={}))],
    )

    with pytest.raises(ValueError, match="call id"):
        adapter.map(event, context)


def test_request_input_function_response_is_not_a_tool_result() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")
    event = _event(
        "request-input-response",
        partial=False,
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id="input-7",
                    name="adk_request_input",
                    response={"value": 42},
                )
            )
        ],
    )

    assert adapter.map(event, context) == ()


def test_text_accompanying_a_function_call_is_not_registered_as_final_output() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")
    event = _event(
        "tool-event",
        partial=False,
        parts=[
            types.Part(text="I need to look that up."),
            types.Part(
                function_call=types.FunctionCall(
                    id="call-7",
                    name="lookup",
                    args={"query": "weather"},
                )
            ),
        ],
    )

    mapped = adapter.map(event, context)

    assert any(
        isinstance(mapped_event, ItemCompleted) and mapped_event.item_kind == "message"
        for mapped_event in mapped
    )
    assert context.output_refs == ()


def test_explicit_output_snapshot_replaces_prior_output_refs() -> None:
    adapter = ADKEventAdapter()
    context = ADKAdapterContext(run_id="run-1")
    first = adapter.map(_event("resp-1", partial=False, parts=[types.Part(text="old")]), context)
    replacement = adapter.map(
        _event(
            "resp-2",
            partial=False,
            parts=[
                types.Part(
                    text="new",
                    part_metadata={"ksadk_output_snapshot": True},
                )
            ],
        ),
        context,
    )
    first_completed = next(event for event in first if isinstance(event, ItemCompleted))
    replacement_completed = next(event for event in replacement if isinstance(event, ItemCompleted))

    assert first_completed.item_id != replacement_completed.item_id
    assert [ref.item_id for ref in context.output_refs] == [replacement_completed.item_id]
