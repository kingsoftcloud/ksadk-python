import pytest

from ksadk.harness.engine.completion_guard import unfinished_delegations
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


def _progress(seq: int, call_id: str, status: str) -> RuntimeEvent:
    return RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=seq,
        payload={
            "kind": "subagent.event",
            "call_id": call_id,
            "label": f"子任务 {call_id}",
            "status": status,
        },
    )


def test_unfinished_delegations_requires_terminal_evidence_for_every_child() -> None:
    events = [
        _progress(1, "one", "running"),
        _progress(2, "two", "running"),
        _progress(3, "one", "succeeded"),
    ]

    assert unfinished_delegations(events) == {"two": "子任务 two"}
    assert unfinished_delegations([*events, _progress(4, "two", "failed")]) == {}


@pytest.mark.asyncio
async def test_parent_engine_fails_closed_when_completion_guard_finds_open_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FinalReasoner:
        async def complete(self, **kwargs):  # noqa: ANN003
            del kwargs
            return HarnessReasoningTurn(final_text="answer")

    monkeypatch.setattr(
        "ksadk.harness.engine.completion_guard.unfinished_delegations",
        lambda events: {"child-one": "仍在运行的子任务"},
    )
    engine = ManagedLangGraphEngine(reasoner=_FinalReasoner())
    compiled = await engine.compile(
        HarnessSpec(
            agent_revision_ref="agent-revision://completion-guard@1",
            model=ModelBinding(profile_ref="model-profile://fixture@1"),
            prompt=PromptSpec(instructions="answer"),
        )
    )
    handle = await engine.start(
        StartRequest(
            input="answer",
            user_id="user",
            session_id="session",
            agent_id="agent",
            metadata={"invocation_id": "guard-run"},
        ),
        compiled,
    )
    events = [event async for event in engine.stream(handle)]

    assert any(event.event_type == EventType.RUN_FAILED for event in events)
    assert not any(event.event_type == EventType.RUN_COMPLETED for event in events)
