import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from a2a.types import Task, TaskState, TaskStatus

from ksadk.a2a.invocation_checkpoint import A2AInvocationCheckpoint
from ksadk.a2a.invocation_service import A2AInvocationService
from tests.a2a.test_invocation_service_r85 import Wire, request, service, trigger


class SerialCheckpoint:
    payload = None

    @asynccontextmanager
    async def lease(self, scope_id, **options):
        yield self

    async def load(self):
        return A2AInvocationCheckpoint.model_validate_json(self.payload) if self.payload else None

    async def save(self, checkpoint):
        self.payload = checkpoint.model_dump_json()

    async def cancel_requested(self):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("eventually_complete", [True, False])
async def test_working_recovery_waits_with_original_deadline_and_no_resend(eventually_complete):
    consumed = asyncio.Event()
    working = Task(id="remote-1", context_id="context-1",
                   status=TaskStatus(state=TaskState.TASK_STATE_WORKING))

    class WorkingWire(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            self.calls.append(("send", message))

            async def stream():
                yield working
                consumed.set()
                await asyncio.Event().wait()
            yield stream()

    wire = WorkingWire(task=working)
    first, events, _ = await service(wire)
    checkpoint = first._checkpoint_store = SerialCheckpoint()
    initial = request().model_copy(update={"deadline_unix_seconds": time.time() + 0.25})

    async def collect(caller):
        return [event async for event in caller.invoke(initial, trigger_ref=trigger())]

    live = asyncio.create_task(collect(first))
    await asyncio.wait_for(consumed.wait(), 1)
    live.cancel()
    await asyncio.gather(live, return_exceptions=True)
    cold = A2AInvocationService(space_client=wire, event_store=events,
                                checkpoint_store=checkpoint, policy=first._policy)
    recovering = asyncio.create_task(collect(cold))
    await asyncio.sleep(0.025)
    assert not recovering.done()
    if eventually_complete:
        wire.task = Wire().task
        replayed = await recovering
        assert cold.snapshot(initial.handle).status == "completed"
        assert [event.seq for event in replayed] == sorted(event.seq for event in replayed)
        assert any(event.event_type == "item.completed" and event.item_kind == "artifact"
                   for event in replayed)
    else:
        with pytest.raises(TimeoutError, match="A2A_OVERALL_TIMEOUT"):
            await recovering
        assert cold.snapshot(initial.handle).status == "working"
    assert sum(kind == "send" for kind, _ in wire.calls) == 1


@pytest.mark.asyncio
async def test_output_commit_before_descriptor_completion_replays_in_canonical_order():
    first, events, wire = await service()
    checkpoint = first._checkpoint_store = SerialCheckpoint()
    initial = request()
    stream = first.invoke(initial, trigger_ref=trigger())
    async for event in stream:
        if event.event_type == "item.completed" and event.item_kind == "artifact":
            break
    await stream.aclose()
    cold = A2AInvocationService(space_client=wire, event_store=events,
                                checkpoint_store=checkpoint, policy=first._policy)
    replayed = [event async for event in cold.invoke(initial, trigger_ref=trigger())]
    assert [event.seq for event in replayed] == sorted(event.seq for event in replayed)
    assert len({event.event_id for event in replayed}) == len(replayed)
    output = next(event for event in replayed
                  if event.event_type == "item.completed" and event.item_kind == "artifact")
    assert output.snapshot.parts[0].text == "hello"
    assert replayed[-1].snapshot.parts[0].data["status"] == "completed"
    assert sum(kind == "send" for kind, _ in wire.calls) == 1
    persisted = await events.list(initial.handle.session_id, run_id=initial.handle.run_id)
    assert len([event for event in persisted
                if event.event_type == "item.completed" and event.item_kind == "artifact"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ["codex", "langgraph"])
async def test_framework_recovery_returns_all_persisted_parts(framework):
    import json

    from a2a.types import Artifact, Part

    wire = Wire(task=Task(id="remote-1", context_id="context-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[Artifact(artifact_id="a1", parts=[Part(text="hello"), Part(text=" world")])]))
    first, events, _ = await service(wire)
    checkpoint = first._checkpoint_store = SerialCheckpoint()
    initial = request()
    stream = first.invoke(initial, trigger_ref=trigger())
    async for event in stream:
        if event.event_type == "item.completed" and event.item_kind == "artifact":
            break
    await stream.aclose()
    cold = A2AInvocationService(space_client=wire, event_store=events,
                                checkpoint_store=checkpoint, policy=first._policy)
    emitted = []
    if framework == "codex":
        from ksadk.a2a.codex import CodexExpertInvocation, codex_expert

        class Provider:
            async def resolve(self, spec, call):
                return CodexExpertInvocation(initial, trigger(), cold)

        response = await codex_expert(initial.spec).invoke(
            {"threadId": "t", "turnId": "turn", "callId": "call_1", "tool": "expert",
             "arguments": {"message": "hello"}}, provider=Provider(), emit=emitted.append)
        assert json.loads(response["contentItems"][0]["text"])["reply"] == "hello world"
    else:
        from ksadk.a2a.langgraph_managed import bind_managed_langgraph, remote_node

        with bind_managed_langgraph(cold, lambda *_: (initial, trigger()), emitted.append):
            result = await remote_node(initial.spec)({"messages": []}, {})
        assert result["messages"][0].content == "hello world"
    assert [event.seq for event in emitted] == sorted(event.seq for event in emitted)
    assert sum(kind == "send" for kind, _ in wire.calls) == 1
