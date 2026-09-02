from __future__ import annotations

import pytest

from ksadk.events.canonical import ItemCompleted, RunCompleted, RunStarted, UsageReported
from ksadk.events.content import TextContent
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


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
        for payload in status_payloads
    )
