"""Old explicit surface references retain native input semantics and ownership."""

from types import SimpleNamespace

import pytest
from langchain_core.tools import tool
from langgraph.types import interrupt

from ksadk.events.canonical import SourceRef, StructuredInputRequest
from ksadk.interaction.native_events import NativeInteractionRequested
from ksadk.runtime._runner_adapter.a2ui_surface import (
    _a2ui_surface_event,
    bind_legacy_input_surface,
)
from ksadk.runtime.adapter import RunHandle
from tests.interaction.test_native_confirmation_pipeline import resume, start


def operations(sid="business-input"):
    return [
        {"version": "v0.9", "createSurface": {
            "surfaceId": sid, "catalogId": "a2ui.org/catalogs/basic/v1.0"}},
        {"version": "v0.9", "updateComponents": {"surfaceId": sid, "components": [
            {"id": "choice", "component": "Select", "label": "Choose",
             "options": [{"label": "First", "value": "first"}], "value": ""},
        ]}},
    ]


async def test_original_untyped_interrupt_binds_emitted_card_and_resumes_original_node(tmp_path):
    published = []

    @tool
    async def display(operations: list) -> dict:
        """Display a declared user interface."""
        return {"a2ui_operations": operations}

    async def wait(_state, config):
        if not published:
            published.append(True)
            await display.ainvoke({"operations": operations()}, config=config)
        answer = interrupt({"surface_id": "business-input", "type": "application-owned-name"})
        return {"answer": answer}

    runner, adapter, handle, request, starts = await start(tmp_path, wait)
    try:
        assert request.interaction_kind == "structured_input"
        assert request.request.schema_ == {}
        assert request._presentation["a2ui"]["messages"] == operations()
        assert request._native_target["legacy_a2ui_binding"] is True
        events = await resume(adapter, handle, request, {"value": "first"}, "legacy-input")
        assert events[-1].event_type == "run.completed"
        assert starts == ["before"]
        state = await runner._agent.aget_state({"configurable": {
            "thread_id": request._native_target["thread_id"]}})
        assert state.values["answer"] == {"value": "first"}
    finally:
        await runner.close()


def request(sid):
    event = NativeInteractionRequested(
        schema_version=2, event_id="wait", seq=1, timestamp=1, run_id="run",
        scope_id="scope", source=SourceRef(framework="langgraph"),
        interaction_id="wait", interaction_kind="structured_input",
        request=StructuredInputRequest(schema={"type": "object"}),
    )
    event._native_target = {"a2ui_surface_id": sid, "task_id": "native-task"}
    return event


async def test_sequential_waits_can_reuse_surface_only_after_original_answer_is_consumed(tmp_path):
    published = []

    @tool
    async def display(operations: list) -> dict:
        """Display the user interface."""
        return {"a2ui_operations": operations}

    async def wait(_state, config):
        if not published:
            published.append(True)
            await display.ainvoke({"operations": operations()}, config=config)
        first = interrupt({"surface_id": "business-input"})
        second = interrupt({"surface_id": "business-input"})
        return {"answer": [first, second]}

    runner, adapter, handle, first, starts = await start(tmp_path, wait)
    try:
        events = await resume(adapter, handle, first, {"value": "first"}, "first")
        second = next(e for e in events if isinstance(e, NativeInteractionRequested))
        assert second.interaction_id != first.interaction_id
        assert second._presentation["a2ui"]["messages"] == operations()
        bindings = handle.native_ref["a2ui_presentation"]["interactions"]
        assert bindings["business-input"] == second.interaction_id
        events = await resume(adapter, handle, second, {"value": "second"}, "second")
        assert events[-1].event_type == "run.completed"
        assert starts == ["before"]
    finally:
        await runner.close()


@pytest.mark.parametrize("case", ["missing", "other-run", "deleted", "recreated", "other-wait"])
def test_reference_never_claims_missing_stale_or_already_owned_surface(case):
    sequence = iter(range(100))
    adapter = SimpleNamespace(_runtime_type="langgraph", _next_seq=lambda: next(sequence))
    handle = RunHandle(run_id="run", session_id="session", runtime_type="langgraph")
    _a2ui_surface_event(adapter, handle, {"tool_output": {"a2ui_operations": operations()}})
    event = request("missing" if case == "missing" else "business-input")
    if case == "other-run":
        handle = RunHandle(run_id="different-run", session_id="session", runtime_type="langgraph")
    if case in {"deleted", "recreated"}:
        _a2ui_surface_event(adapter, handle, {"tool_output": {"a2ui_operations": [
            {"version": "v0.9", "deleteSurface": {"surfaceId": "business-input"}}]}})
        if case == "recreated":
            _a2ui_surface_event(adapter, handle, {"call_id": "recreated",
                "tool_output": {"a2ui_operations": operations()}})
    if case == "other-wait":
        handle.native_ref["a2ui_presentation"]["interactions"] = {"business-input": "another"}
    bind_legacy_input_surface(adapter, handle, event)
    assert event._presentation is None
    assert "legacy_a2ui_binding" not in event._native_target
