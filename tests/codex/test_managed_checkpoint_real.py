"""Real native process plus Kernel; model and remote A2A peer are local fixtures."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ksadk.a2a.codex_recovery import validated_recovery_checkpoint
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.codex.rollout_checkpoint import NativeRolloutCheckpoint
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.kernel.state import RunState
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runtime.adapter import RunHandle
from tests.a2a.test_invocation_service_r85 import Wire, request
from tests.e2e.codex_app_server_fixture import RealCodexFactory
from tests.e2e.codex_responses_stub import (
    DeterministicResponsesStub,
    _completed_event,
    _message_events,
)
from tests.kernel.control_harness import AGENT, command, kernel_stack

pytestmark = pytest.mark.local_process_heavy


def expert_response(payload):
    if any(item.get("type") == "function_call_output" for item in payload.get("input", [])):
        return _message_events("expert complete", suffix="expert-final")
    item = {
        "type": "function_call",
        "id": "fc-expert-1",
        "call_id": "call-expert-1",
        "name": "expert",
        "arguments": '{"message":"hello"}',
    }
    return (
        {"type": "response.created", "response": {"id": "response-expert"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "output_index": 0,
            "arguments": item["arguments"],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        _completed_event("response-expert", [item]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_failure", [False, True])
async def test_native_expert_requires_private_checkpoint_before_send(
    tmp_path, monkeypatch, storage_failure
):
    from openai_codex import CodexConfig

    wire, observed = Wire(), []

    # Authorization has separate real ledger tests; this fixture isolates write-before-send.
    def service_for_space(space, *, event_store, policy, recover_checkpoint_leases=False):
        observed.append(event_store)
        return A2AInvocationService(
            space_client=wire,
            event_store=event_store,
            policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")),
        )

    bootstrap = SimpleNamespace(
        outbound_enabled=True,
        runtime_metadata=SimpleNamespace(agent_id="caller"),
        invocation_service_for_space=service_for_space,
    )
    with DeterministicResponsesStub() as stub:
        stub._events = expert_response
        factory = RealCodexFactory(responses_url=stub.base_url)
        client = factory(
            CodexConfig(env={"CODEX_HOME": str(tmp_path / "codex"), "HOME": str(tmp_path)})
        )
        adapter = CodexRuntimeAdapter(
            client, a2a_experts=[request().spec], managed_a2a_bootstrap=bootstrap
        )
        adapter.adopt_app_lifetime()
        stack = await kernel_stack(adapter=adapter)
        lease = await stack.lease()
        worker = AgentKernelWorker(
            stack.store, adapter_factory=lambda: adapter, session_events=stack.events
        )
        original_stream = wire.open_prepared_message_stream

        @asynccontextmanager
        async def checked_stream(*args, **kwargs):
            run = await stack.store.load_run(observed[-1].run_id)
            saved = run.metadata["runtime_checkpoint"]
            validated = validated_recovery_checkpoint(
                run, RunHandle.model_validate(run.metadata['handle']),
                experts=client._a2a_expert_bridge.experts,
            )
            assert validated.run_id == run.run_id
            assert saved["run_id"] == run.run_id and saved["session_id"] == run.session_id
            call = saved["calls"][0]
            NativeRolloutCheckpoint.model_validate(saved["rollout"]).require_call(
                turn_id=call["turnId"],
                call_id=call["callId"],
                tool=call["tool"],
                arguments=call["arguments"],
            )
            async with original_stream(*args, **kwargs) as events:
                yield events

        monkeypatch.setattr(wire, "open_prepared_message_stream", checked_stream)
        if storage_failure:

            async def unavailable(*args, **kwargs):
                raise RuntimeError("fixture private store unavailable")

            monkeypatch.setattr(stack.store, "save_runtime_checkpoint", unavailable)
        try:
            receipt = await stack.kernel.submit(
                command(
                    idempotency_key="native-checkpoint",
                    payload={"content": "call expert", "model": "fixture-model"},
                ),
                permit=stack.permit("enqueue"),
            )
            assert receipt.status == "accepted"
            started = await worker.run_once(AGENT, lease)
            async with asyncio.timeout(20):
                while True:
                    run = await stack.store.load_run(started.run_id)
                    if run.state in {RunState.COMPLETED, RunState.FAILED}:
                        break
                    await asyncio.sleep(0.01)
            assert run.state == (RunState.FAILED if storage_failure else RunState.COMPLETED)
            assert sum(kind == "send" for kind, _ in wire.calls) == int(not storage_failure)
            assert ("runtime_checkpoint" in run.metadata) is not storage_failure
            if storage_failure:
                thread_id = run.metadata["handle"]["native_ref"]["thread_id"]
                async with asyncio.timeout(3):
                    while True:
                        native = await client._threads[thread_id].read(include_turns=True)
                        if any(turn.status.value != "inProgress" for turn in native.thread.turns):
                            break
                        await asyncio.sleep(0.02)
                assert any(turn.status.value == "interrupted" for turn in native.thread.turns), [
                    turn.status for turn in native.thread.turns
                ]
        finally:
            await adapter.close_all()
        assert all(process.poll() is not None for process in factory.processes)
