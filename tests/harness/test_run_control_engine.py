from __future__ import annotations

import contextlib

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import (
    ExecutionStrategySpec,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)
from ksadk.runtime import ResumePayload, ResumeTarget, StartRequest


def _spec(run_control) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://control@1",
        model=ModelBinding(profile_ref="model-profile://test@1"),
        prompt=PromptSpec(instructions="完成任务并报告证据。"),
        execution_strategy=ExecutionStrategySpec(
            config={"run_control": run_control}
        ),
    )


def _request() -> StartRequest:
    return StartRequest(
        agent_id="control-agent",
        user_id="user-1",
        session_id="session-1",
        input="开始",
        runtime_type="managed-langgraph",
    )


async def _drive(engine: ManagedLangGraphEngine, spec: HarnessSpec):
    compiled = await engine.compile(spec)
    handle = await engine.start(_request(), compiled)
    return [event async for event in engine.stream(handle)]


class _Turns(HarnessReasoner):
    def __init__(self, *turns: HarnessReasoningTurn) -> None:
        self.turns = list(turns)
        self.messages = []

    async def complete(self, *, messages, **_kwargs):
        self.messages.append(list(messages))
        return self.turns.pop(0)


@pytest.mark.asyncio
async def test_acceptance_result_is_attached_to_terminal_event():
    engine = ManagedLangGraphEngine(
        reasoner=_Turns(
            HarnessReasoningTurn(
                final_text="DONE",
                usage={"input_tokens": 5, "output_tokens": 1},
            )
        )
    )
    events = await _drive(
        engine,
        _spec(
            {
                "objective": "return DONE",
                "acceptance": [
                    {
                        "check_id": "answer",
                        "kind": "text_contains",
                        "expected": "DONE",
                    }
                ],
                "limits": {"max_model_calls": 2, "max_total_tokens": 1000},
            }
        ),
    )
    terminal = events[-1]
    assert terminal.event_type == EventType.RUN_COMPLETED
    assert terminal.payload["outcome"] == "verified"
    assert terminal.payload["usage"] == {
        "model_calls": 1,
        "tool_calls": 0,
        "total_tokens": 6,
    }
    assert terminal.payload["acceptance"][0]["passed"] is True
    assert run_conformance_suite(events).ok


@pytest.mark.asyncio
async def test_model_budget_stops_before_second_model_side_effect():
    calls = 0

    async def lookup(_arguments):
        nonlocal calls
        calls += 1
        return "result"

    reasoner = _Turns(
        HarnessReasoningTurn(
            tool_calls=(HarnessToolCall(call_id="c1", name="lookup", arguments={}),)
        ),
        HarnessReasoningTurn(final_text="must not execute"),
    )
    events = await _drive(
        ManagedLangGraphEngine(reasoner=reasoner, tools={"lookup": lookup}),
        _spec(
            {
                "objective": "bounded run",
                "limits": {"max_model_calls": 1, "soft_limit_ratio": 0.9},
            }
        ),
    )
    assert calls == 1
    assert len(reasoner.messages) == 1
    assert events[-1].event_type == EventType.RUN_COMPLETED
    assert events[-1].payload["outcome"] == "budget_exhausted"
    assert events[-1].payload["control_reason"] == "model_calls_hard_limit"


@pytest.mark.asyncio
async def test_soft_limit_injects_one_controlled_closing_instruction():
    reasoner = _Turns(HarnessReasoningTurn(final_text="partial answer"))
    events = await _drive(
        ManagedLangGraphEngine(reasoner=reasoner),
        _spec(
            {
                "objective": "bounded run",
                "limits": {
                    "max_model_calls": 2,
                    "soft_limit_ratio": 0.5,
                },
            }
        ),
    )
    prompts = [
        message["content"]
        for message in reasoner.messages[0]
        if message.get("role") == "system"
    ]
    assert any("运行控制器要求立即收口" in prompt for prompt in prompts)
    assert events[-1].payload["outcome"] == "unverified"


@pytest.mark.asyncio
async def test_invalid_control_contract_fails_at_compile_time():
    engine = ManagedLangGraphEngine(reasoner=_Turns(HarnessReasoningTurn(final_text="x")))
    with pytest.raises(ExecutionEngineError, match="run_control"):
        await engine.compile(
            _spec(
                {
                    "objective": "invalid",
                    "limits": {},
                }
            )
        )


@pytest.mark.asyncio
async def test_control_budget_survives_process_restart_and_approval_resume(tmp_path):
    db_path = str(tmp_path / "control-checkpoint.db")
    spec = _spec(
        {
            "objective": "approved write",
            "acceptance": [
                {
                    "check_id": "answer",
                    "kind": "text_contains",
                    "expected": "DONE",
                }
            ],
            "limits": {"max_model_calls": 3, "max_tool_calls": 1},
        }
    )

    first_context = AsyncSqliteSaver.from_conn_string(db_path)
    first_saver = await first_context.__aenter__()
    try:
        first_engine = ManagedLangGraphEngine(
            reasoner=_Turns(
                HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="write-1", name="write", arguments={}),
                    )
                )
            ),
            checkpointer=first_saver,
            tools={"write": lambda _arguments: None},
            approval_required={"write"},
        )
        compiled = await first_engine.compile(spec)
        handle = await first_engine.start(_request(), compiled)
        first_events = [event async for event in first_engine.stream(handle)]
        assert first_events[-1].event_type == EventType.RUN_INTERRUPTED
        assert first_engine._runs[handle.run_id].controller.model_calls == 1
    finally:
        with contextlib.suppress(Exception):
            await first_context.__aexit__(None, None, None)

    calls = 0

    async def write(_arguments):
        nonlocal calls
        calls += 1
        return "written"

    second_context = AsyncSqliteSaver.from_conn_string(db_path)
    second_saver = await second_context.__aenter__()
    try:
        second_engine = ManagedLangGraphEngine(
            reasoner=_Turns(HarnessReasoningTurn(final_text="DONE")),
            checkpointer=second_saver,
            tools={"write": write},
            approval_required={"write"},
        )
        compiled = await second_engine.compile(spec)
        attached = await second_engine.attach(handle, compiled)
        assert second_engine._runs[handle.run_id].controller.model_calls == 1
        await second_engine.resume(
            attached,
            ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
            ResumePayload(kind="approval_decision", call_id="write-1", data="approved"),
        )
        events = [event async for event in second_engine.stream(attached)]
    finally:
        with contextlib.suppress(Exception):
            await second_context.__aexit__(None, None, None)

    assert calls == 1
    assert events[-1].payload["outcome"] == "verified"
    assert events[-1].payload["usage"]["model_calls"] == 2
    assert events[-1].payload["usage"]["tool_calls"] == 1


@pytest.mark.asyncio
async def test_repeated_tool_action_is_blocked_and_model_can_replan():
    calls = 0

    async def search(_arguments):
        nonlocal calls
        calls += 1
        return "same result"

    repeated = HarnessReasoningTurn(
        tool_calls=(HarnessToolCall(call_id="search-1", name="search", arguments={"q": "x"}),)
    )
    repeated_again = HarnessReasoningTurn(
        tool_calls=(HarnessToolCall(call_id="search-2", name="search", arguments={"q": "x"}),)
    )
    reasoner = _Turns(
        repeated,
        repeated_again,
        HarnessReasoningTurn(final_text="已改用现有证据收口"),
    )
    events = await _drive(
        ManagedLangGraphEngine(reasoner=reasoner, tools={"search": search}),
        _spec(
            {
                "objective": "avoid stagnant search",
                "limits": {"max_model_calls": 4, "max_tool_calls": 3},
                "stagnation": {
                    "action_repeat_threshold": 2,
                    "error_repeat_threshold": 2,
                    "max_replans": 1,
                },
            }
        ),
    )
    assert calls == 1
    blocked = [
        event
        for event in events
        if event.event_type == EventType.TOOL_CALL_END
        and event.payload.get("call_id") == "search-2"
    ]
    assert len(blocked) == 1
    assert "requires replanning" in str(blocked[0].payload.get("error"))
    third_turn_prompts = [
        str(message.get("content") or "") for message in reasoner.messages[2]
    ]
    assert any("重规划原因：repeated_action" in item for item in third_turn_prompts)
    assert events[-1].event_type == EventType.RUN_COMPLETED
    assert events[-1].payload["usage"]["tool_calls"] == 1


@pytest.mark.asyncio
async def test_token_budget_is_reserved_before_provider_invocation():
    class BudgetReasoner(HarnessReasoner):
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **_kwargs):
            self.calls += 1
            return HarnessReasoningTurn(final_text="must not run")

    reasoner = BudgetReasoner()
    events = await _drive(
        ManagedLangGraphEngine(reasoner=reasoner),
        _spec(
            {
                "objective": "bounded input",
                "limits": {"max_total_tokens": 1, "max_model_calls": 2},
            }
        ),
    )
    assert reasoner.calls == 0
    assert events[-1].event_type == EventType.RUN_COMPLETED
    assert events[-1].payload["outcome"] == "budget_exhausted"
    assert events[-1].payload["control_reason"] == "total_tokens_hard_limit"
