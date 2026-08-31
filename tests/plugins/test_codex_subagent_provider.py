from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ksadk.plugins.subagent_providers.codex import (
    DEFAULT_CODEX_CHILD_PROVIDER_REF,
    CodexOneShotSubagentProvider,
)
from ksadk.plugins.subagents import (
    SpawnSubagentRequest,
    SubagentPolicy,
    SubagentProviderError,
)


def _request(**updates: Any) -> SpawnSubagentRequest:
    payload: dict[str, Any] = {
        "provider_ref": DEFAULT_CODEX_CHILD_PROVIDER_REF,
        "parent_session_id": "parent-session",
        "parent_run_id": "parent-run",
        "task": "Inspect the repository and report one fact.",
        "policy": SubagentPolicy(timeout_seconds=5),
    }
    payload.update(updates)
    return SpawnSubagentRequest(**payload)


class _FakeCodexClient:
    def __init__(self, number: int, *, blocked: bool = False) -> None:
        self.thread_id = f"native-thread-{number}"
        self.blocked = blocked
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.start_configs: list[dict[str, Any]] = []
        self.interrupts: list[str] = []
        self.closed = 0

    async def start_thread(self, config: dict[str, Any]) -> str:
        self.start_configs.append(dict(config))
        return self.thread_id

    async def run_turn(self, thread_id: str, prompt: str, *, config: dict[str, Any]):  # noqa: ANN201
        assert thread_id == self.thread_id
        assert prompt
        assert config == self.start_configs[0]
        self.started.set()
        yield {
            "method": "turn/started",
            "params": {"threadId": thread_id, "turn": {"id": "turn-1"}},
        }
        if self.blocked:
            await self.release.wait()
        yield {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": thread_id,
                "turnId": "turn-1",
                "itemId": "message-1",
                "delta": "isolated ",
            },
        }
        yield {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": "turn-1",
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "text": f"isolated result {self.thread_id}",
                },
            },
        }
        yield {
            "method": "turn/completed",
            "params": {"threadId": thread_id, "turn": {"id": "turn-1"}},
        }

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        self.interrupts.append(thread_id)
        return True

    async def close(self) -> None:
        self.closed += 1
        self.release.set()


class _Factory:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.homes: list[Path] = []
        self.clients: list[_FakeCodexClient] = []

    def __call__(self, home: Path) -> _FakeCodexClient:
        client = _FakeCodexClient(len(self.clients) + 1, blocked=self.blocked)
        self.homes.append(home)
        self.clients.append(client)
        return client


@pytest.mark.asyncio
async def test_one_shot_children_have_distinct_clients_homes_threads_and_replay() -> None:
    factory = _Factory()
    provider = CodexOneShotSubagentProvider(
        project_dir=Path.cwd(),
        model="fixture-model",
        base_instructions="Read only.",
        client_factory=factory,
    )

    first = await provider.spawn(_request(parent_run_id="run-1"))
    second = await provider.spawn(_request(parent_run_id="run-2"))
    first_result, second_result = await asyncio.gather(
        provider.result(first), provider.result(second)
    )

    assert first.child_session_id != second.child_session_id
    assert first.handle_id != second.handle_id
    assert first_result.output == f"isolated result {first.child_session_id}"
    assert second_result.output == f"isolated result {second.child_session_id}"
    assert len(set(factory.homes)) == 2
    assert all(home.exists() for home in factory.homes)
    assert all(
        client.start_configs
        == [
            {
                "cwd": str(Path.cwd().resolve()),
                "sandbox_read_only": True,
                "approval_mode": "deny_all",
                "ephemeral": True,
                "model": "fixture-model",
                "base_instructions": "Read only.",
            }
        ]
        for client in factory.clients
    )

    replay = [event async for event in provider.subscribe(first, after_seq=2)]
    assert [event.seq for event in replay] == list(
        range(3, (await provider.status(first)).last_seq + 1)
    )
    assert replay[-1].kind == "terminal"
    assert replay[-1].payload == {"state": "succeeded"}

    await provider.dispose(first)
    await provider.dispose(second)
    assert all(client.closed == 1 for client in factory.clients)
    assert all(not home.exists() for home in factory.homes)
    assert (await provider.status(first)).state == "disposed"


@pytest.mark.asyncio
async def test_cancel_interrupts_native_turn_and_dispose_closes_child() -> None:
    factory = _Factory(blocked=True)
    provider = CodexOneShotSubagentProvider(project_dir=Path.cwd(), client_factory=factory)
    handle = await provider.spawn(_request())
    client = factory.clients[0]
    await asyncio.wait_for(client.started.wait(), timeout=1)

    await provider.cancel(handle)

    result = await provider.result(handle)
    assert result.state == "cancelled"
    assert client.interrupts == [client.thread_id]
    assert (await provider.status(handle)).state == "cancelled"
    events = [event async for event in provider.subscribe(handle)]
    assert events[-1].kind == "terminal"
    assert events[-1].payload == {"state": "cancelled"}

    home = factory.homes[0]
    await provider.dispose(handle)
    await provider.dispose(handle)
    assert client.closed == 1
    assert not home.exists()


@pytest.mark.asyncio
async def test_one_shot_policy_and_followup_fail_closed() -> None:
    provider = CodexOneShotSubagentProvider(project_dir=Path.cwd(), client_factory=_Factory())

    with pytest.raises(SubagentProviderError) as permissions:
        await provider.spawn(
            _request(policy=SubagentPolicy(allowed_permissions=("filesystem:workspace-read",)))
        )
    assert permissions.value.code == "codex_child_policy_unsupported"

    with pytest.raises(SubagentProviderError) as background:
        await provider.spawn(_request(policy=SubagentPolicy(background=True)))
    assert background.value.code == "codex_child_policy_unsupported"

    handle = await provider.spawn(_request())
    await provider.result(handle)
    with pytest.raises(SubagentProviderError) as followup:
        await provider.followup(handle, "another turn")
    assert followup.value.code == "codex_child_one_shot"
    await provider.dispose(handle)
