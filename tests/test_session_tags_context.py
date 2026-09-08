from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, dataclass
from types import SimpleNamespace

import pytest

from ksadk.conversations.runtime import invoke_conversation_once, stream_conversation_turn
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import StartRequest
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.runtime_context import get_current_invocation_context, get_current_session_tags
from ksadk.session_context import SessionContext
from ksadk.sessions.in_memory import InMemorySessionService


def envelope(value, revision=1):
    return {
        "agentengine": {
            "session_context": {"schema_version": 1, "revision": revision, "tags": {"scene": value}}
        }
    }


class CaptureRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(name="agent", type=SimpleNamespace(value="langgraph")),
            project_dir=".",
        )
        self.seen = []

    def load_agent(self):
        pass

    def prepare_for_request(self, model):
        pass

    async def invoke(self, input_data):
        before = dict(get_current_session_tags())
        await asyncio.sleep(0.005)
        assert before == dict(get_current_session_tags())
        self.seen.append((before, get_current_invocation_context(), input_data))
        return {"output": before.get("scene", "legacy")}

    async def stream(self, input_data):
        result = await self.invoke(input_data)
        yield {"type": "text", "delta": result["output"]}
        yield {"type": "final", "output": result["output"]}


def test_snapshot_is_immutable_and_native_schema_opt_in():
    original = {"scene": "sales"}
    snapshot = SessionContext(tags=original)
    original["scene"] = "changed"
    assert snapshot.tags["scene"] == "sales"
    with pytest.raises(TypeError):
        snapshot.tags["scene"] = "forged"
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = 3
    assert dict(get_current_session_tags()) == {}

    @dataclass
    class OldContext:
        user_id: str
        session: str = "customer-owned"

    @dataclass
    class TaggedContext:
        session: SessionContext

    payload = {"user_id": "user", "session": snapshot.to_payload()}
    assert BaseRunner.build_native_context(payload, context_schema=OldContext) == {
        "user_id": "user"
    }
    native = BaseRunner.build_native_context(payload, context_schema=TaggedContext)
    assert native["session"].tags == snapshot.tags


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_invocation_scope_concurrency_and_request_metadata_separation(stream):
    service = InMemorySessionService()
    runner = CaptureRunner()

    async def run(sid, tags):
        kwargs = dict(
            runner=runner,
            agent_id="agent",
            user_id=sid,
            session_id=sid,
            messages=[{"role": "user", "content": "hello"}],
            model=None,
            request_metadata=envelope(tags),
            custom_metadata={"scene": "request-only"},
            session_service_provider=lambda: service,
            prepare_runner=lambda runner, model: None,
        )
        if stream:
            return [event async for event in stream_conversation_turn(**kwargs)]
        return await invoke_conversation_once(**kwargs)

    results = await asyncio.gather(run("one", "sales"), run("two", "support"))
    assert {item[0]["scene"] for item in runner.seen} == {"sales", "support"}
    for tags, context, payload in runner.seen:
        assert context.session.tags == tags
        assert context.metadata == {"scene": "request-only"}
        assert payload["platform_context"]["session"]["tags"] == tags
    assert get_current_invocation_context() is None
    for sid in ("one", "two"):
        events = await service.get_events(sid)
        assert "session_context" not in str([event.to_dict() for event in events])
    assert "session_context" not in str(results)


@pytest.mark.asyncio
async def test_kernel_adapter_snapshot_and_legacy_start():
    runner = CaptureRunner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="langgraph")
    tagged = await adapter.start(
        StartRequest(
            input="hello",
            user_id="user",
            session_id="tagged",
            metadata={"session_context": envelope("kernel")["agentengine"]["session_context"]},
        )
    )
    _ = [event async for event in adapter.stream(tagged)]
    assert runner.seen[-1][0] == {"scene": "kernel"}
    assert "session_context" not in runner.seen[-1][2]["metadata"]
    legacy = await adapter.start(StartRequest(input="hello", user_id="user", session_id="old"))
    _ = [event async for event in adapter.stream(legacy)]
    assert runner.seen[-1][0] == {}
    assert "platform_context" not in runner.seen[-1][2]


@pytest.mark.asyncio
async def test_kernel_retry_preserves_first_snapshot_after_tag_update():
    from tests.kernel.control_harness import command, kernel_stack

    stack = await kernel_stack()
    first = command(
        idempotency_key="tag-retry",
        payload={
            "content": "hello",
            "session_context": envelope("first")["agentengine"]["session_context"],
        },
    )
    receipt = await stack.kernel.submit(first, permit=stack.permit("enqueue"))
    retry = command(
        idempotency_key="tag-retry",
        payload={
            "content": "hello",
            "session_context": envelope("updated", 2)["agentengine"]["session_context"],
        },
    )
    duplicate = await stack.kernel.submit(retry, permit=stack.permit("enqueue"))
    assert duplicate.status == "duplicate"
    assert duplicate.message_id == receipt.message_id
    stored = await stack.store.load_message(receipt.message_id)
    assert stored.command.payload["session_context"]["tags"] == {"scene": "first"}


@pytest.mark.asyncio
async def test_admitted_snapshot_preserves_prepared_identity_and_metadata(monkeypatch):
    from unittest.mock import AsyncMock
    from ksadk.runtime.preprocessing import PreparedRuntimeStart
    from ksadk.runtime_context import get_current_invocation_context_or_default

    context = get_current_invocation_context_or_default()
    context.account_id = "tenant"
    context.metadata = {"order": "one"}
    prepared = PreparedRuntimeStart(
        runner_input={"platform_context": context.to_payload(), "metadata": {"public": "value"}},
        context=context,
        input_text="hello",
    )
    monkeypatch.setattr(
        "ksadk.runtime.runner_adapter.prepare_runtime_start", AsyncMock(return_value=prepared)
    )
    runner = CaptureRunner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="langgraph")
    handle = await adapter.start(
        StartRequest(
            input="hello",
            user_id="user",
            session_id="prepared",
            metadata={
                "session_context": envelope("snapshot")["agentengine"]["session_context"],
            },
        )
    )
    _ = [event async for event in adapter.stream(handle)]
    tags, actual, payload = runner.seen[-1]
    assert tags == {"scene": "snapshot"}
    assert actual.account_id == "tenant"
    assert actual.metadata == {"order": "one"}
    assert payload["platform_context"]["account_id"] == "tenant"
    assert payload["metadata"] == {"public": "value"}


@pytest.mark.asyncio
async def test_prepared_turn_cannot_reintroduce_reserved_metadata():
    from dataclasses import asdict
    from ksadk.runtime.preprocessing import prepare_runtime_start
    from tests.runtime.test_preprocessing import _prepared_turn

    turn = _prepared_turn()
    turn.session_context = envelope("initial")["agentengine"]["session_context"]
    request = StartRequest(
        input="current",
        user_id="user",
        session_id="session-1",
        metadata={
            "conversation_request": {
                "prepared_turn": asdict(turn),
                "request_metadata": envelope("latest", 2),
            },
        },
    )
    prepared = await prepare_runtime_start(request, CaptureRunner())
    assert prepared.context.session.tags == {"scene": "latest"}
    assert prepared.context.session.revision == 2
    assert "session_context" not in str(prepared.runner_input.get("request_metadata"))
