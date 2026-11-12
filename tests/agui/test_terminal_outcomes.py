"""AG-UI cancellation and stale answers retain the original durable outcome."""

import asyncio
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import AsyncMock

import pytest
from langgraph.graph import END, START, StateGraph

from ksadk.agui._agent_kernel import public_interrupt
from ksadk.events.canonical import ErrorInfo, RunCanceled, RunFailed
from ksadk.interaction.langgraph import request_input
from ksadk.kernel import ingress
from tests.agui.test_kernel_native_input import first_wait, stack, until
from tests.agui.test_runtime_adapter import (
    _Adapter,
    _agent_for,
    _common,
    _events,
    _input,
    _run_started,
    _text_delta,
)


@pytest.mark.parametrize("cancelled", [True, False])
async def test_runtime_terminal_wire_closes_text_and_preserves_failure(cancelled):
    terminal = (
        RunCanceled(status="canceled", reason="user cancelled", **_common(3))
        if cancelled
        else RunFailed(
            status="failed",
            error=ErrorInfo(
                code="fixture",
                message="real failure",
                source="fixture",
                scope_id="scope-1",
            ),
            **_common(3),
        )
    )
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _run_started(),
        _text_delta("partial", 2),
        terminal,
    )
    events = [event async for event in _agent_for(adapter).run(_input())]
    frames = [event.model_dump(mode="json", by_alias=True, exclude_none=True) for event in events]
    assert frames[-2]["type"] == "TEXT_MESSAGE_END"
    if cancelled:
        assert frames[-1]["type"] == "RUN_FINISHED"
        assert frames[-1]["outcome"] == {"type": "cancelled"}
    else:
        assert frames[-1]["type"] == "RUN_ERROR"
        assert frames[-1]["code"] == "RUNTIME_ERROR"
    assert sum(frame["type"] in {"RUN_FINISHED", "RUN_ERROR"} for frame in frames) == 1


async def resume(s, entries, run_id):
    async with asyncio.timeout(8):
        response = await s.client.post(
            "/agentengine/agui",
            json=_input(run_id=run_id, resume=entries).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        )
    assert response.status_code == 200
    import json

    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


@pytest.mark.parametrize("outcome", ["success", "cancelled", "failure"])
async def test_terminal_kernel_resume_replays_without_admission_or_metadata_writes(
    tmp_path,
    monkeypatch,
    outcome,
):
    before, tools = [], []
    builder = None
    if outcome != "cancelled":

        class State(TypedDict, total=False):
            messages: list
            answer: str

        def answer(_state):
            answer = request_input("Choose a value", {"type": "integer", "writeOnly": True})
            tools.append(answer)
            return {"answer": "received"}

        def failing(_state):
            raise RuntimeError("fixture business failure")

        builder = StateGraph(State)
        builder.add_node("wait", answer)
        builder.add_edge(START, "wait")
        if outcome == "failure":
            builder.add_node("fail", failing)
            builder.add_edge("wait", "fail")
            builder.add_edge("fail", END)
        else:
            builder.add_edge("wait", END)

    async with stack(tmp_path, monkeypatch, before, tools, graph_builder=builder) as s:
        record = await first_wait(s)
        entry = {"interruptId": record.interaction_id, "status": "resolved", "payload": 0}
        if outcome == "cancelled":
            entry = {"interruptId": record.interaction_id, "status": "cancelled"}
        first = await resume(s, [entry], "first-answer")

        async def terminal_record():
            return (await s.runtime.kernel_store.get(record.interaction_id)).status in {
                "resolved",
                "cancelled",
            }

        await until(terminal_record)
        messages = await s.runtime.kernel_store.list_messages("local-agent")
        facts = await s.runtime.session_events.read("thread-1", 0, 1000)
        metadata_write = AsyncMock(side_effect=AssertionError("Replay must be read only"))
        monkeypatch.setattr(
            s.agent, "_update_session_metadata_after_assistant_turn", metadata_write
        )
        # The official helper clones the Agent, so also guard the class method.
        monkeypatch.setattr(
            type(s.agent), "_update_session_metadata_after_assistant_turn", metadata_write
        )
        private_read = AsyncMock(side_effect=AssertionError("Replay must use public facts"))
        monkeypatch.setattr(s.runtime.kernel_store, "read_resolved_response", private_read)
        for run_id in ("first-answer", "new-wire-attempt"):
            replay = await resume(s, [entry], run_id)
            if outcome == "failure":
                assert first[-1]["type"] == replay[-1]["type"] == "RUN_ERROR"
                assert replay[-1]["code"] == "RUNTIME_ERROR"
            else:
                assert first[-1]["outcome"] == replay[-1]["outcome"] == {"type": outcome}
            assert not any(event.get("code") == "INPUT_RESPONSE_INVALID" for event in replay)
            if outcome != "cancelled":
                resolved = [
                    event["value"]
                    for event in replay
                    if event["type"] == "CUSTOM"
                    and event["value"].get("event_type") == "interaction.resolved"
                ]
                assert len(resolved) == 1 and resolved[0]["payload"]["response"] is None
        metadata_write.assert_not_awaited()
        private_read.assert_not_awaited()
        assert len(await s.runtime.kernel_store.list_messages("local-agent")) == len(messages)
        assert await s.runtime.session_events.read("thread-1", 0, 1000) == facts
        assert tools == ([] if outcome == "cancelled" else [0])


async def test_partial_native_batch_returns_remaining_wait_and_mixed_retry_is_atomic(
    tmp_path,
    monkeypatch,
):
    class State(TypedDict, total=False):
        messages: list
        left: int
        right: int
        answer: str

    before, tools = [], []

    def branch(name):
        def node(_state):
            value = request_input(name, {"type": "integer"})
            tools.append((name, value))
            return {name: value}

        return node

    builder = StateGraph(State)
    for name in ("left", "right"):
        builder.add_node(name, branch(name))
        builder.add_edge(START, name)
    builder.add_node("finish", lambda state: {"answer": f"{state['left']}+{state['right']}"})
    builder.add_edge(["left", "right"], "finish")
    builder.add_edge("finish", END)
    async with stack(tmp_path, monkeypatch, before, tools, graph_builder=builder) as s:
        async with asyncio.timeout(8):
            initial = [event async for event in s.agent.run(_input())]
        waits = {item.message: item.id for item in initial[-1].outcome.interrupts}
        left = {"interruptId": waits["left"], "status": "resolved", "payload": 0}
        right = {"interruptId": waits["right"], "status": "resolved", "payload": 1}
        partial = await resume(s, [left], "partial")
        assert [item["id"] for item in partial[-1]["outcome"]["interrupts"]] == [waits["right"]]
        assert tools == [("left", 0)]
        messages = await s.runtime.kernel_store.list_messages("local-agent")
        # Replaying a consumed occurrence reports the current remaining wait,
        # even if no new event will ever be appended until that wait is answered.
        replay = await resume(s, [left], "partial-replay")
        assert [item["id"] for item in replay[-1]["outcome"]["interrupts"]] == [waits["right"]]
        for entries in ([right, left], [left, right]):
            mixed = await resume(s, entries, "mixed")
            assert mixed[-1]["code"] == "INTERACTION_ALREADY_TERMINAL"
            assert tools == [("left", 0)]
            assert len(await s.runtime.kernel_store.list_messages("local-agent")) == len(messages)
        finished = await resume(s, [right], "remaining")
        assert finished[-1]["outcome"] == {"type": "success"}
        assert sorted(tools) == [("left", 0), ("right", 1)]


@pytest.mark.parametrize("another_submission_wins", [True, False])
async def test_rejection_after_preflight_distinguishes_terminal_race_from_invalid_answer(
    tmp_path,
    monkeypatch,
    another_submission_wins,
):
    from langgraph.types import interrupt

    from tests.interaction.test_langgraph_upgrade_semantics import EvenAnswer

    class State(TypedDict, total=False):
        messages: list
        answer: str

    before, tools = [], []

    def wait(_state):
        answer = interrupt("Choose a value", response_schema=EvenAnswer)
        tools.append(answer.amount)
        return {"answer": f"accepted {answer.amount}"}

    def later(_state):
        request_input("Later value", {"type": "integer"})
        return {}

    builder = StateGraph(State)
    builder.add_node("wait", wait)
    builder.add_node("later", later)
    builder.add_edge(START, "wait")
    builder.add_edge("wait", "later")
    builder.add_edge("later", END)
    async with stack(tmp_path, monkeypatch, before, tools, graph_builder=builder) as s:
        record = await first_wait(s)
        submit = ingress.submit_command
        preflight_complete, release_admission = asyncio.Event(), asyncio.Event()
        raced_commands = []

        async def blocked_submit(command, *, permit):
            if command.source.ref == "racing-attempt":
                raced_commands.append(str(command.command_id))
                preflight_complete.set()
                await release_admission.wait()
            return await submit(command, permit=permit)

        monkeypatch.setattr(ingress, "submit_command", blocked_submit)
        # Both payloads satisfy the public JSON Schema. Odd numbers fail only
        # the real native validator, after admission, and leave the wait pending.
        entry = {
            "interruptId": record.interaction_id,
            "status": "resolved",
            "payload": {"amount": 2 if another_submission_wins else 3},
        }
        raced = asyncio.create_task(resume(s, [entry], "racing-attempt"))
        try:
            await asyncio.wait_for(preflight_complete.wait(), 3)
            if another_submission_wins:
                winner = await resume(s, [{**entry, "payload": {"amount": 0}}], "winner")
                assert winner[-1]["outcome"]["type"] == "interrupt"
                assert (
                    await s.runtime.kernel_store.get(record.interaction_id)
                ).status == "resolved"
        finally:
            release_admission.set()
        result = await raced
        assert result[-1]["type"] == "RUN_ERROR"
        assert result[-1]["code"] == (
            "INTERACTION_ALREADY_TERMINAL" if another_submission_wins else "INPUT_RESPONSE_INVALID"
        )
        facts = await s.runtime.session_events.read("thread-1", 0, 1000)
        rejected = [
            fact
            for fact in facts
            if fact.event_type == "control.command_rejected"
            and fact.payload["command_id"] in raced_commands
        ]
        assert len(rejected) == 1
        assert rejected[0].payload["reason"] == (
            "invalid_command" if another_submission_wins else "input_response_invalid"
        )
        assert tools == ([0] if another_submission_wins else [])
        assert (await s.runtime.kernel_store.get(record.interaction_id)).status == (
            "resolved" if another_submission_wins else "pending"
        )


async def test_expired_resume_is_explicit_and_never_admitted(monkeypatch):
    record = {
        "interaction_id": "expired",
        "run_id": "original",
        "status": "expired",
        "revision": 2,
        "kind": "structured_input",
        "request_schema": {"type": "integer"},
    }
    kernel = SimpleNamespace(
        pending_interactions=AsyncMock(return_value=[]),
        public_interaction=AsyncMock(return_value=record),
    )
    trusted = SimpleNamespace(
        tenant_id="tenant",
        agent_instance_id="agent",
        permit=SimpleNamespace(permit_id="permit"),
    )
    monkeypatch.setattr(ingress, "get_agent_kernel", lambda: kernel)
    monkeypatch.setattr(ingress, "trusted_context", lambda **_: trusted)
    submit = AsyncMock(side_effect=AssertionError("Expired input must not be admitted"))
    monkeypatch.setattr(ingress, "submit_command", submit)
    events = [
        event
        async for event in _agent_for(_Adapter())._kernel_run(
            _input(
                resume=[
                    {
                        "interruptId": "expired",
                        "status": "resolved",
                        "payload": 0,
                    }
                ]
            )
        )
    ]
    assert events[-1].code == "INTERACTION_ALREADY_TERMINAL"
    submit.assert_not_awaited()


@pytest.mark.parametrize("nested", [True, False])
def test_public_interrupt_preserves_existing_expiry(nested):
    request = {"expires_at": "2026-12-01T00:00:00Z", "request_schema": {"type": "integer"}}
    payload = {"interaction_id": "wait", "revision": 1, "run_id": "run"}
    payload.update({"request": request} if nested else request)
    wire = public_interrupt(payload).model_dump(mode="json", by_alias=True, exclude_none=True)
    assert wire["expiresAt"] == request["expires_at"]
    assert "toolCallId" not in wire
