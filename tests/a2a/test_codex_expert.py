import json
import queue
import threading

import pytest

from ksadk.a2a.codex import CodexInvocationContextProvider, CodexToolCall, codex_expert, compile_codex_experts
from ksadk.codex.a2a_tools import CodexExpertBridge, denied_tool_result
from ksadk.events.adapters.codex import CodexAdapterContext, CodexEventAdapter
from ksadk.events.run_authority import bind_run_event_authority
from tests.a2a.test_invocation_service_r85 import request
from tests.a2a.test_langgraph_invocation_context import setup_runtime


def call(**changes):
    return {"threadId": "thread-1", "turnId": "turn-1", "callId": "item-1",
            "tool": "expert", "arguments": {"message": "hello"}, **changes}


def test_rejected_codex_turn_cannot_request_another_remote_expert():
    bridge = CodexExpertBridge([request().spec])
    bridge.register_thread("thread-1")
    notifications = queue.Queue()
    bridge.activate("thread-1", notifications)
    first = call()
    results = []
    waiter = threading.Thread(target=lambda: results.append(bridge.request(first)))
    waiter.start()
    assert notifications.get(timeout=1)["method"] == "ksadk/a2a/toolCall"
    bridge.mark_denied(first)
    assert bridge.resolve(first, denied_tool_result())
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert json.loads(results[0]["contentItems"][0]["text"])["state"] == "denied"

    repeated = bridge.request(call(callId="another-call"))
    assert repeated["success"] is True
    assert json.loads(repeated["contentItems"][0]["text"])["error_type"] == "A2A_APPROVAL_DENIED"
    assert notifications.empty()

    next_turn = call(turnId="turn-2", callId="item-2")
    next_results = []
    next_waiter = threading.Thread(target=lambda: next_results.append(bridge.request(next_turn)))
    next_waiter.start()
    assert notifications.get(timeout=1)["method"] == "ksadk/a2a/toolCall"
    assert bridge.resolve(next_turn, {"success": True, "contentItems": [{"type": "inputText", "text": "ok"}]})
    next_waiter.join(timeout=1)
    assert not next_waiter.is_alive()
    assert next_results[0]["success"] is True


async def persist_call(authority, value=None):
    value = value or call()
    native = {"method": "item/started", "params": {
        "threadId": value["threadId"], "turnId": value["turnId"],
        "item": {"id": value["callId"], "type": "dynamicToolCall", "tool": value["tool"],
                 "arguments": value["arguments"], "namespace": None, "status": "inProgress"}}}
    facts = CodexEventAdapter().map_protocol_message(
        native, CodexAdapterContext(run_id=authority.run_id), native_cursor="jsonl:1", timestamp=1.0)
    for event in facts:
        await authority.persist_one("s", event)
    return facts


@pytest.mark.asyncio
async def test_expert_reuses_actual_codex_identity_and_common_service_replay():
    authority, service, wire, _ = await setup_runtime()
    provider = CodexInvocationContextProvider(authority=authority, service_for_space=lambda _: service, caller_agent_id="caller")
    expert = codex_expert(request().spec)
    projected = []
    with bind_run_event_authority(authority):
        native = await persist_call(authority)
        first = await expert.invoke(call(), provider=provider, emit=projected.append)
        second = await expert.invoke(call(), provider=provider, emit=projected.append)
        resolved = await provider.resolve(expert.spec, CodexToolCall.model_validate(call()))
    assert first == second
    assert first["success"] is True
    assert json.loads(first["contentItems"][0]["text"]) == {"ok": True, "state": "completed", "reply": "hello"}
    assert sum(kind == "send" for kind, _ in wire.calls) == 1
    assert resolved.trigger.call_id == "item-1"
    assert resolved.trigger.item_id == native[0].item_id
    assert resolved.request.handle.run_id == "public-run"
    assert native[0].source.metadata["thread_id"] == "thread-1"
    assert native[0].source.native_run_id == "turn-1"
    assert projected and all(event.source.framework == "a2a" for event in projected)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"threadId": "another"}, {"turnId": "another"}, {"callId": "another"},
    {"arguments": {"message": "changed"}}, {"tool": "another"},
    {"arguments": {"message": "hello", "agent": "substituted"}},
])
async def test_foreign_or_modified_tool_call_never_sends(changes):
    authority, service, wire, _ = await setup_runtime()
    provider = CodexInvocationContextProvider(authority=authority, service_for_space=lambda _: service, caller_agent_id="caller")
    with bind_run_event_authority(authority):
        await persist_call(authority)
        with pytest.raises(ValueError, match="A2A_"):
            await codex_expert(request().spec).invoke(call(**changes), provider=provider, emit=lambda _: None)
    assert wire.calls == []


@pytest.mark.asyncio
async def test_missing_native_event_or_policy_denial_cannot_send():
    from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
    from ksadk.a2a.invocation_service import A2AInvocationService
    authority, _, wire, _ = await setup_runtime()
    service = A2AInvocationService(space_client=wire, event_store=authority,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("deny")))
    provider = CodexInvocationContextProvider(authority=authority, service_for_space=lambda _: service, caller_agent_id="caller")
    with bind_run_event_authority(authority):
        with pytest.raises(ValueError, match="A2A_CODEX_NATIVE_TRIGGER_NOT_PERSISTED"):
            await codex_expert(request().spec).invoke(call(), provider=provider, emit=lambda _: None)
        await persist_call(authority)
        with pytest.raises(PermissionError, match="A2A_APPROVAL_REQUIRED"):
            await codex_expert(request().spec).invoke(call(), provider=provider, emit=lambda _: None)
    assert wire.calls == []


def test_fixed_expert_registration_rejects_native_subagent_and_duplicate_exposures():
    from openai_codex.generated.v2_all import DynamicToolSpec
    spec = request().spec
    tool = codex_expert(spec).tool_spec()
    assert DynamicToolSpec.model_validate(tool).model_dump(by_alias=True, exclude_none=True) == tool
    assert set(tool["inputSchema"]["properties"]) == {"message"}
    assert "target_agent_id" not in json.dumps(tool)
    with pytest.raises(ValueError, match="A2A_NATIVE_UNSUPPORTED"):
        codex_expert(spec.model_copy(update={"exposure": "native"}))
    with pytest.raises(ValueError, match="A2A_VERSION_PIN_REQUIRED"):
        codex_expert(spec.model_copy(update={"expected_version_id": None}))
    with pytest.raises(ValueError, match="A2A_EXPOSURE_CONFLICT"):
        compile_codex_experts([spec, spec.model_copy(update={"local_name": "other"})])


@pytest.mark.asyncio
async def test_unknown_remote_outcome_is_not_reported_as_tool_success():
    from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
    from ksadk.a2a.invocation_service import A2AInvocationService
    from tests.a2a.test_invocation_service_r85 import Wire
    authority, _, _, _ = await setup_runtime()
    wire = Wire(events=[])
    svc = A2AInvocationService(space_client=wire, event_store=authority,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")))
    provider = CodexInvocationContextProvider(authority=authority, service_for_space=lambda _: svc, caller_agent_id="caller")
    with bind_run_event_authority(authority):
        await persist_call(authority)
        result = await codex_expert(request().spec).invoke(call(), provider=provider, emit=lambda _: None)
    assert result["success"] is False
    assert json.loads(result["contentItems"][0]["text"])["state"] == "unknown"


@pytest.mark.asyncio
async def test_cancel_uses_shared_service_and_does_not_fabricate_remote_cancellation(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
    from ksadk.a2a.invocation_service import A2AInvocationService
    from tests.a2a.test_invocation_service_r85 import Wire
    entered = asyncio.Event()
    class SlowWire(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            async def stream():
                entered.set()
                await asyncio.Event().wait()
                yield self.task
            yield stream()
    authority, _, _, _ = await setup_runtime()
    svc = A2AInvocationService(space_client=SlowWire(), event_store=authority,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")))
    outcomes = []
    original = svc.cancel
    async def cancel(handle):
        result = await original(handle)
        outcomes.append(result)
        return result
    monkeypatch.setattr(svc, "cancel", cancel)
    provider = CodexInvocationContextProvider(authority=authority, service_for_space=lambda _: svc, caller_agent_id="caller")
    with bind_run_event_authority(authority):
        await persist_call(authority)
        task = asyncio.create_task(codex_expert(request().spec).invoke(call(), provider=provider, emit=lambda _: None))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert len(outcomes) == 1 and outcomes[0].outcome != "remote_cancelled"


@pytest.mark.asyncio
async def test_cold_recovery_rejects_service_without_durable_checkpoint_before_send():
    authority, service, wire, _ = await setup_runtime()
    provider = CodexInvocationContextProvider(
        authority=authority, service_for_space=lambda _: service,
        caller_agent_id="caller", require_durable=True,
    )
    with bind_run_event_authority(authority):
        await persist_call(authority)
        with pytest.raises(ValueError, match="A2A_CODEX_DURABLE_SERVICE_REQUIRED"):
            await codex_expert(request().spec).invoke(call(), provider=provider, emit=lambda _: None)
    assert wire.calls == []
