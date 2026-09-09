from __future__ import annotations

import pytest

from ksadk.events.canonical import ItemCompleted, RunCompleted, RunStarted, UsageReported
from ksadk.events.content import TextContent
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [
    ({"decision": "approve"}, "approved"),
    ({"decision": "approved"}, "approved"),
    ({"decision": "reject"}, "denied"),
    ({"decision": "edit"}, "denied"),
    ({}, "denied"), (None, "denied"), (True, "denied"),
    ("approved", "approved"),
])
async def test_studio_approval_shape_is_normalized(decision, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from ksadk.runtime import ResumePayload, ResumeTarget

    handle = Mock(run_id="run", runtime_type="harness")
    handle.model_copy.return_value = handle
    engine = SimpleNamespace(resume=AsyncMock(return_value=handle))
    adapter = ManagedHarnessRuntimeAdapter(_spec(), engine=engine)
    adapter._external_handles["run"] = handle
    await adapter.resume(handle, ResumeTarget(kind="checkpoint_id", id="checkpoint"),
                         ResumePayload(kind="approval_decision", data=decision))
    assert engine.resume.call_args.args[2].data == expected


def test_tool_error_projection_preserves_result_and_error():
    from ksadk.harness.events import EventType, RuntimeEvent
    from ksadk.harness.managed_runtime import _project_event

    event = RuntimeEvent(
        event_id="event",
        timestamp=1.0,
        user_id="user",
        seq_id=1,
        event_type=EventType.TOOL_CALL_END,
        agent_id="agent",
        session_id="session",
        invocation_id="run",
        payload={"call_id": "call", "error": "server not bound"},
    )
    projected = _project_event(event)
    first = projected[0].model_dump(mode="json", exclude_none=True)
    assert first["initial"]["parts"][0]["result"] == {"error": "server not bound"}
    assert first["initial"]["parts"][0]["is_error"] is True


class _Reasoner:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def complete(self, *, model, prompt, messages, tools, max_output_tokens=None):
        del model, prompt, tools, max_output_tokens
        self.messages.append([dict(message) for message in messages])
        return HarnessReasoningTurn(
            final_text="managed answer",
            usage={"input_tokens": 12, "output_tokens": 3},
        )


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://studio-agent@1",
        model=ModelBinding(profile_ref="model-profile://studio-model@1"),
        prompt=PromptSpec(instructions="You are managed."),
    )


@pytest.mark.asyncio
async def test_managed_adapter_projects_engine_tree_to_canonical_events(tmp_path):
    reasoner = _Reasoner()
    adapter = ManagedHarnessRuntimeAdapter(_spec(), reasoner=reasoner, workspace_root=tmp_path)
    await adapter.preflight()
    handle = await adapter.start(
        StartRequest(
            input="hello",
            user_id="user",
            session_id="session",
            agent_id="agent",
            runtime_type="harness",
            metadata={"invocation_id": "run-managed"},
        )
    )

    assert handle.runtime_type == "harness"
    events = [event async for event in adapter.stream(handle)]

    assert isinstance(events[0], RunStarted)
    assert any(isinstance(event, UsageReported) for event in events)
    final = next(
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "message"
    )
    assert isinstance(final.snapshot.parts[0], TextContent)
    assert final.snapshot.parts[0].text == "managed answer"
    completed = next(event for event in events if isinstance(event, RunCompleted))
    assert completed.output_refs[0].item_id == final.item_id
    assert any(
        isinstance(event, ItemCompleted)
        and event.item_kind == "status"
        and "model.call.started" in event.snapshot.parts[0].text
        for event in events
    )


@pytest.mark.asyncio
async def test_managed_adapter_passes_studio_conversation_history(tmp_path):
    reasoner = _Reasoner()
    adapter = ManagedHarnessRuntimeAdapter(_spec(), reasoner=reasoner, workspace_root=tmp_path)
    handle = await adapter.start(
        StartRequest(
            input="follow up",
            user_id="user",
            session_id="session",
            agent_id="agent",
            runtime_type="harness",
            config={"max_input_tokens": 65536, "reserve_output_tokens": 8192},
            metadata={
                "conversation_request": {
                    "messages": [
                        {"role": "user", "content": "remember 42"},
                        {"role": "assistant", "content": "remembered"},
                        {"role": "user", "content": "follow up"},
                    ]
                }
            },
        )
    )
    events = [event async for event in adapter.stream(handle)]

    assert [message["content"] for message in reasoner.messages[0][-3:]] == [
        "remember 42",
        "remembered",
        "follow up",
    ]
    status_payloads = [
        event.snapshot.parts[0].text
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "status"
    ]
    assert any(
        '"event":"context.planned"' in payload
        and '"window_source":"model_profile"' in payload
        and '"current_input"' in payload
        for payload in status_payloads
    )


@pytest.mark.asyncio
async def test_managed_adapter_emits_reasoning_item_events(tmp_path):
    class _ReasoningReasoner(_Reasoner):
        async def complete(self, *, model, prompt, messages, tools, max_output_tokens=None):
            del model, prompt, tools, max_output_tokens
            self.messages.append([dict(message) for message in messages])
            return HarnessReasoningTurn(
                final_text="managed answer",
                usage={"input_tokens": 12, "output_tokens": 3},
                reasoning="先检查历史，再回答。",
            )

    adapter = ManagedHarnessRuntimeAdapter(
        _spec(),
        reasoner=_ReasoningReasoner(),
        workspace_root=tmp_path,
    )
    handle = await adapter.start(
        StartRequest(
            input="hello",
            user_id="user",
            session_id="session",
            agent_id="agent",
            runtime_type="harness",
            metadata={"invocation_id": "run-managed-reasoning"},
        )
    )
    events = [event async for event in adapter.stream(handle)]

    reasoning = [
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "reasoning"
    ]
    assert reasoning, "managed adapter did not emit reasoning item events"
    assert reasoning[-1].snapshot.parts[0].text == "先检查历史，再回答。"
