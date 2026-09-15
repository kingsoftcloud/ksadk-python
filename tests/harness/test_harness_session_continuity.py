from __future__ import annotations

import asyncio

import pytest

from ksadk.harness import HarnessApp, HarnessConfig
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.runtime import HarnessRuntimeAdapter
from ksadk.runtime import StartRequest


class _TranscriptReasoner:
    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, tools
        snapshot = [dict(message) for message in messages]
        self.requests.append(snapshot)
        return HarnessReasoningTurn(final_text=f"answer-{len(self.requests)}")


@pytest.mark.asyncio
async def test_same_session_receives_successful_prior_turns(tmp_path) -> None:
    reasoner = _TranscriptReasoner()
    adapter = HarnessApp(
        HarnessConfig(model="fixture", prompt="system-role"),
        reasoner=reasoner,
        workspace_root=tmp_path,
    ).adapter()
    assert isinstance(adapter, HarnessRuntimeAdapter)

    await adapter.execute_request(
        StartRequest(
            input="remember alpha",
            agent_id="agent-a",
            user_id="user-a",
            session_id="session-a",
        )
    )
    await adapter.execute_request(
        StartRequest(
            input="what did I say?",
            agent_id="agent-a",
            user_id="user-a",
            session_id="session-a",
        )
    )

    assert reasoner.requests[1] == [
        {"role": "system", "content": "system-role"},
        {"role": "user", "content": "remember alpha"},
        {"role": "assistant", "content": "answer-1"},
        {"role": "user", "content": "what did I say?"},
    ]


@pytest.mark.asyncio
async def test_session_history_is_isolated_by_agent_user_and_session(tmp_path) -> None:
    reasoner = _TranscriptReasoner()
    adapter = HarnessApp(
        HarnessConfig(model="fixture", prompt="system-role"),
        reasoner=reasoner,
        workspace_root=tmp_path,
    ).adapter()
    assert isinstance(adapter, HarnessRuntimeAdapter)

    for agent_id, user_id, session_id, text in (
        ("agent-a", "user-a", "session", "first"),
        ("agent-b", "user-a", "session", "other agent"),
        ("agent-a", "user-b", "session", "other user"),
        ("agent-a", "user-a", "other", "other session"),
    ):
        await adapter.execute_request(
            StartRequest(
                input=text,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
            )
        )

    assert all(len(request) == 2 for request in reasoner.requests)


class _SerialReasoner:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, tools
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return HarnessReasoningTurn(final_text=str(messages[-1]["content"]))


@pytest.mark.asyncio
async def test_same_session_turns_are_serialized(tmp_path) -> None:
    reasoner = _SerialReasoner()
    adapter = HarnessApp(
        HarnessConfig(model="fixture", prompt="system-role"),
        reasoner=reasoner,
        workspace_root=tmp_path,
    ).adapter()
    assert isinstance(adapter, HarnessRuntimeAdapter)
    request = {
        "agent_id": "agent-a",
        "user_id": "user-a",
        "session_id": "session-a",
    }

    await asyncio.gather(
        adapter.execute_request(StartRequest(input="one", **request)),
        adapter.execute_request(StartRequest(input="two", **request)),
    )

    assert reasoner.max_active == 1


class _FailOnceReasoner:
    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, tools
        self.requests.append([dict(message) for message in messages])
        if len(self.requests) == 1:
            raise RuntimeError("transient failure")
        return HarnessReasoningTurn(final_text="recovered")


@pytest.mark.asyncio
async def test_failed_turn_does_not_pollute_session_history(tmp_path) -> None:
    reasoner = _FailOnceReasoner()
    adapter = HarnessApp(
        HarnessConfig(model="fixture", prompt="system-role"),
        reasoner=reasoner,
        workspace_root=tmp_path,
    ).adapter()
    assert isinstance(adapter, HarnessRuntimeAdapter)
    identity = {
        "agent_id": "agent-a",
        "user_id": "user-a",
        "session_id": "session-a",
    }

    with pytest.raises(RuntimeError, match="transient failure"):
        await adapter.execute_request(StartRequest(input="failed input", **identity))
    result = await adapter.execute_request(
        StartRequest(input="retry input", **identity)
    )

    assert result["output"] == "recovered"
    assert reasoner.requests[1] == [
        {"role": "system", "content": "system-role"},
        {"role": "user", "content": "retry input"},
    ]
