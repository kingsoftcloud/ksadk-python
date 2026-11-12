import asyncio
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from google.protobuf.json_format import MessageToDict
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.invocation_types import A2AContinuationRequest, A2AInvocationMessage
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.sessions.local_service import LocalSessionService
from tests.a2a.test_invocation_checkpoint import dsn
from tests.a2a.test_invocation_service_r85 import Wire, request, trigger

WORKER = """
import asyncio, os
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine
from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.sessions.local_service import LocalSessionService
from tests.a2a.test_invocation_service_recovery import service
from tests.a2a.test_invocation_service_r85 import Wire, request, trigger
async def main():
    engine = create_async_engine(os.environ["KSADK_A2A_TEST_DATABASE_URL"])
    checkpoints = PostgresA2AInvocationCheckpointStore(engine,
        account_id=os.environ["R85_CALLER_ACCOUNT"], tenant_id="tenant", runtime_id="runtime")
    await checkpoints.initialize()
    sessions = LocalSessionService(Path(os.environ["R85_CALLER_SESSION_DB"]))
    await sessions.create_session("caller", "u", "s")
    caller = service(Wire(), RuntimeEventStore(sessions), checkpoints)
    async for event in caller.invoke(request(), trigger_ref=trigger()):
        if event.event_type == "item.completed" and event.item_kind == "artifact":
            print("READY", flush=True)
            await asyncio.Event().wait()
asyncio.run(main())
"""


def service(wire, events, checkpoints):
    return A2AInvocationService(
        space_client=wire,
        event_store=events,
        checkpoint_store=checkpoints,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "cancel"])
async def test_approval_pause_preserves_unsent_request_for_cold_recovery(tmp_path, action):
    from ksadk.a2a.invocation_policy import A2AApprovalRequired

    engine = create_async_engine(dsn())
    account = str(uuid.uuid4())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
    )
    sessions = LocalSessionService(tmp_path / "approval.sqlite")
    await sessions.create_session("caller", "u", "s")
    events = RuntimeEventStore(sessions)
    wire, initial = Wire(), request()
    try:
        await checkpoints.initialize()
        first = A2AInvocationService(
            space_client=wire,
            event_store=events,
            checkpoint_store=checkpoints,
            policy=A2AInvocationPolicy(
                evaluator=lambda *_: A2AInvocationPolicyDecision("approval_required")
            ),
        )
        assert await first.original_request(initial.handle) is None
        with pytest.raises(A2AApprovalRequired):
            _ = [event async for event in first.invoke(initial, trigger_ref=trigger())]
        assert not wire.calls
        assert not await events.list("s", run_id=initial.handle.run_id)
        assert first.handle(initial.scope_id) == initial.handle
        cold_checkpoints = PostgresA2AInvocationCheckpointStore(
            engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
        )
        recovered = service(
            wire,
            RuntimeEventStore(LocalSessionService(tmp_path / "approval.sqlite")),
            cold_checkpoints,
        )
        frozen = await recovered.original_request(initial.handle)
        assert frozen == initial
        assert frozen.deadline_unix_seconds == initial.deadline_unix_seconds
        assert frozen.idempotency_token == initial.idempotency_token
        with pytest.raises(ValueError, match="A2A_HANDLE_CONFLICT"):
            await recovered.original_request(
                initial.handle.model_copy(update={"session_id": "another-session"})
            )
        assert not wire.calls
        if action == "approve":
            result = [event async for event in recovered.invoke(initial, trigger_ref=trigger())]
            assert result
            assert sum(kind == "send" for kind, _ in wire.calls) == 1
            sent = next(value for kind, value in wire.calls if kind == "send")
            assert sent.message_id == initial.message.message_id
            _ = [event async for event in recovered.invoke(initial, trigger_ref=trigger())]
            assert sum(kind == "send" for kind, _ in wire.calls) == 1
        else:
            result = await recovered.cancel(initial.handle)
            assert result.outcome == "local_cancelled_before_send"
            assert result.snapshot.source == "local"
            assert not wire.calls
            _ = [event async for event in recovered.invoke(initial, trigger_ref=trigger())]
            assert not wire.calls
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_event_ack", [False, True])
async def test_cold_get_drains_pending_canonical_delivery_without_duplicate_output(
    tmp_path, lose_event_ack
):
    engine = create_async_engine(dsn())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=str(uuid.uuid4()), tenant_id="tenant", runtime_id="runtime"
    )
    sessions = LocalSessionService(tmp_path / "sessions.sqlite")
    await sessions.create_session("caller", "u", "s")
    events = RuntimeEventStore(sessions)
    wire, initial = Wire(), request()

    class LostAck:
        failed = False

        async def persist_one(self, session_id, event):
            result = await events.persist_one(session_id, event)
            if (
                lose_event_ack
                and event.event_type == "item.completed"
                and event.item_kind == "artifact"
                and not self.failed
            ):
                self.failed = True
                raise ConnectionError("test lost canonical commit ACK")
            return result

        async def list(self, *args, **kwargs):
            return await events.list(*args, **kwargs)

    try:
        await checkpoints.initialize()
        first = service(wire, LostAck(), checkpoints)
        if lose_event_ack:
            with pytest.raises(ConnectionError, match="lost canonical"):
                _ = [event async for event in first.invoke(initial, trigger_ref=trigger())]
        else:
            _ = [event async for event in first.invoke(initial, trigger_ref=trigger())]
        # Both caller objects and the canonical store are reopened from storage.
        cold_events = RuntimeEventStore(LocalSessionService(tmp_path / "sessions.sqlite"))
        recovered = service(wire, cold_events, checkpoints)
        result = await recovered.get(initial.handle)
        assert result.status == "completed"
        facts = await cold_events.list("s", run_id=initial.handle.run_id)
        artifact_completions = [
            event
            for event in facts
            if event.event_type == "item.completed" and event.item_kind == "artifact"
        ]
        assert len(artifact_completions) == 1
        assert len({event.event_id for event in facts}) == len(facts)
        assert len([call for call in wire.calls if call[0] == "send"]) == 1
        assert (await recovered.cancel(initial.handle)).outcome == "already_terminal"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cold_retry_of_unknown_send_never_sends_again(tmp_path):
    engine = create_async_engine(dsn())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=str(uuid.uuid4()), tenant_id="tenant", runtime_id="runtime"
    )
    sessions = LocalSessionService(tmp_path / "sessions.sqlite")
    await sessions.create_session("caller", "u", "s")
    events = RuntimeEventStore(sessions)

    class UnknownSend(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            self.calls.append(("send", message))
            raise ConnectionError("test uncertain send")
            yield

    wire, initial = UnknownSend(), request()
    try:
        await checkpoints.initialize()
        with pytest.raises(ConnectionError):
            _ = [
                event
                async for event in service(wire, events, checkpoints).invoke(
                    initial, trigger_ref=trigger()
                )
            ]
        cold = service(wire, events, checkpoints)
        replayed = [event async for event in cold.invoke(initial, trigger_ref=trigger())]
        assert replayed == await events.list("s", run_id=initial.handle.run_id)
        assert cold.snapshot(initial.handle).status == "unknown"
        assert len([call for call in wire.calls if call[0] == "send"]) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["continue", "cancel"])
async def test_input_required_can_continue_or_cancel_after_cold_restore(tmp_path, action):
    engine = create_async_engine(dsn())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=str(uuid.uuid4()), tenant_id="tenant", runtime_id="runtime"
    )
    sessions = LocalSessionService(tmp_path / "sessions.sqlite")
    await sessions.create_session("caller", "u", "s")
    events = RuntimeEventStore(sessions)
    waiting = Task(
        id="remote-1",
        context_id="context-1",
        status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(
                message_id="ask-1",
                task_id="remote-1",
                context_id="context-1",
                role=Role.ROLE_AGENT,
                parts=[Part(text="Continue?")],
                metadata={"input_schema": {"type": "string"}},
            ),
        ),
    )
    wire, initial = Wire(events=[waiting], task=waiting), request()
    try:
        await checkpoints.initialize()
        first = service(wire, events, checkpoints)
        _ = [event async for event in first.invoke(initial, trigger_ref=trigger())]
        assert first.snapshot(initial.handle).status == "input_required"
        bound = first.handle(initial.scope_id)
        cold = service(
            wire, RuntimeEventStore(LocalSessionService(tmp_path / "sessions.sqlite")), checkpoints
        )
        if action == "continue":
            wire.task = Wire().task
            wire.events = [wire.task]
            continuation = A2AContinuationRequest(
                spec=initial.spec,
                handle=bound,
                message=A2AInvocationMessage(message_id="continue-1", text_parts=("yes",)),
                context=initial.context,
                deadline_unix_seconds=time.time() + 60,
                idempotency_token="continue-idem-1",
            )
            assert [event async for event in cold.continue_task(continuation)]
            again = service(wire, events, checkpoints)
            replayed = [event async for event in again.continue_task(continuation)]
            assert replayed and replayed[-1].snapshot.parts[0].data["status"] == "completed"
            assert again.snapshot(bound).status == "completed"
            assert len([call for call in wire.calls if call[0] == "send"]) == 2
        else:
            wire.task = Task(
                id="remote-1",
                context_id="context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
            result = await cold.cancel(bound)
            assert result.outcome == "remote_cancelled"
            assert result.snapshot.status == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_process_get_after_worker_kill_reuses_canonical_artifact(tmp_path):
    from ksadk.a2a.control_plane import RemoteTaskReference
    from ksadk.a2a.invocation_checkpoint import InvocationCheckpointBusy

    account = str(uuid.uuid4())
    database = tmp_path / "sessions.sqlite"
    engine = create_async_engine(dsn())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
    )
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        WORKER,
        env={**os.environ, "R85_CALLER_ACCOUNT": account, "R85_CALLER_SESSION_DB": str(database)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert await asyncio.wait_for(worker.stdout.readline(), 20) == b"READY\n"
        handle = request().handle
        with pytest.raises(InvocationCheckpointBusy):
            async with checkpoints.lease(handle.scope_id):
                pytest.fail("worker should own invocation")
        worker.kill()
        await asyncio.wait_for(worker.wait(), 10)
        wire = Wire()
        wire.binding = RemoteTaskReference("remote-1", "context-1")
        events = RuntimeEventStore(LocalSessionService(database))
        recovered = service(wire, events, checkpoints)
        result = await recovered.get(handle)
        assert result.status == "completed"
        assert not any(call[0] == "send" for call in wire.calls)
        facts = await events.list("s", run_id=handle.run_id)
        assert (
            len(
                [e for e in facts if e.event_type == "item.completed" and e.item_kind == "artifact"]
            )
            == 1
        )
    finally:
        if worker.returncode is None:
            worker.kill()
            await worker.wait()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_concurrent_cancel_is_delivered_without_stealing_lease_or_losing_chunk(
    tmp_path, supported
):
    engine = create_async_engine(dsn(), pool_size=1, max_overflow=0)
    lease_engine = create_async_engine(dsn(), poolclass=NullPool)
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine,
        account_id=str(uuid.uuid4()),
        tenant_id="tenant",
        runtime_id="runtime",
        lease_engine=lease_engine,
    )
    sessions = LocalSessionService(tmp_path / "sessions.sqlite")
    await sessions.create_session("caller", "u", "s")
    events = RuntimeEventStore(sessions)
    waiting, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Stalled(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            self.calls.append(("send", message))

            async def stream():
                yield Task(
                    id="remote-1",
                    context_id="context-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
                waiting.set()
                await release.wait()
                yield self.task

            yield stream()

        async def cancel_prepared_task(self, prepared):
            attempted.set()
            if not supported:

                class UnsupportedOperationError(Exception):
                    pass

                raise UnsupportedOperationError()
            self.task = Task(
                id="remote-1",
                context_id="context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
            return self.task

    wire, initial = Stalled(), request()
    owner = service(wire, events, checkpoints)

    async def consume():
        return [event async for event in owner.invoke(initial, trigger_ref=trigger())]

    running = None
    try:
        await checkpoints.initialize()
        running = asyncio.create_task(consume())
        await asyncio.wait_for(waiting.wait(), 5)
        queued = await service(wire, events, checkpoints).cancel(initial.handle)
        assert queued.outcome == "cancel_requested_but_remote_unknown"
        await asyncio.wait_for(attempted.wait(), 5)
        if not supported:
            release.set()
        delivered = await asyncio.wait_for(running, 5)
        assert owner.snapshot(initial.handle).status == ("cancelled" if supported else "completed")
        descriptors = [
            event.snapshot.parts[0].data
            for event in delivered
            if event.event_type in {"item.completed", "item.snapshot_replaced"}
            and event.item_kind == "status"
        ]
        assert any(
            value["cancel"]["request_state"] == ("confirmed" if supported else "unsupported")
            for value in descriptors
        )
        if not supported:
            assert any(
                event.event_type == "item.completed" and event.item_kind == "artifact"
                for event in delivered
            )
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await engine.dispose()
        await lease_engine.dispose()


@pytest.mark.asyncio
async def test_cancel_during_prepare_is_local_and_survives_cold_get(tmp_path):
    engine = create_async_engine(dsn())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=str(uuid.uuid4()), tenant_id="tenant", runtime_id="runtime"
    )
    sessions = LocalSessionService(tmp_path / "sessions.sqlite")
    await sessions.create_session("caller", "u", "s")
    events = RuntimeEventStore(sessions)
    preparing, release = asyncio.Event(), asyncio.Event()

    class Preparing(Wire):
        async def prepare_call(self, **kwargs):
            preparing.set()
            await release.wait()
            return await super().prepare_call(**kwargs)

    wire, initial = Preparing(), request()
    owner = service(wire, events, checkpoints)

    async def consume():
        return [event async for event in owner.invoke(initial, trigger_ref=trigger())]

    running = None
    try:
        await checkpoints.initialize()
        running = asyncio.create_task(consume())
        await asyncio.wait_for(preparing.wait(), 5)
        queued = await service(wire, events, checkpoints).cancel(initial.handle)
        assert queued.outcome == "cancel_requested_but_remote_unknown"
        assert not queued.confirmed_cancelled
        assert queued.handle.platform_task_id is None
        release.set()
        delivered = await asyncio.wait_for(running, 5)
        assert delivered[-1].snapshot.parts[0].data["cancel"]["request_state"] == "local_confirmed"
        assert not any(call[0] == "send" for call in wire.calls)
        cold = service(wire, events, checkpoints)
        assert (await cold.get(initial.handle)).status == "cancelled"
        assert not any(call[0] == "get" for call in wire.calls)
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "cancel"])
async def test_pending_continuation_is_frozen_before_approval_and_cold_recoverable(
    tmp_path, action
):
    from ksadk.a2a.runtime_approval import approval_id, invoke_with_approval

    engine = create_async_engine(dsn())
    account = str(uuid.uuid4())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
    )
    sessions = LocalSessionService(tmp_path / "continuation.sqlite")
    await sessions.create_session("caller", "u", "s")
    pending = Task(
        id="remote-1",
        context_id="context-1",
        status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(
                message_id="question", role=Role.ROLE_AGENT, parts=[Part(text="next?")]
            ),
        ),
    )
    wire, initial = Wire(events=[pending], task=pending), request()
    try:
        await checkpoints.initialize()
        owner = A2AInvocationService(
            space_client=wire,
            event_store=RuntimeEventStore(sessions),
            checkpoint_store=checkpoints,
            policy=A2AInvocationPolicy(
                evaluator=lambda req, _: A2AInvocationPolicyDecision(
                    "approval_required" if isinstance(req, A2AContinuationRequest) else "allow"
                )
            ),
        )
        _ = [event async for event in owner.invoke(initial, trigger_ref=trigger())]
        handle = owner.handle(initial.scope_id)
        followup = A2AContinuationRequest(
            spec=initial.spec,
            handle=handle,
            message=A2AInvocationMessage(message_id="supplement-1", text_parts=("answer",)),
            context=initial.context,
            deadline_unix_seconds=time.time() + 60,
            idempotency_token="supplement-idempotency",
        )
        before = len(wire.calls)
        paused = invoke_with_approval(owner, followup, trigger_ref=trigger())
        approval = await anext(paused)
        assert approval.interaction_id == approval_id(followup)
        await paused.aclose()
        assert len(wire.calls) == before
        cold = service(
            wire,
            RuntimeEventStore(LocalSessionService(tmp_path / "continuation.sqlite")),
            PostgresA2AInvocationCheckpointStore(
                engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
            ),
        )
        restored = await cold.pending_continuation(handle)
        assert restored == followup
        assert await cold.original_request(handle) == initial
        changed = followup.model_copy(update={"deadline_unix_seconds": time.time() + 120})
        with pytest.raises(ValueError, match="A2A_PENDING_CONTINUATION_CONFLICT"):
            _ = [event async for event in cold.continue_task(changed)]
        assert len(wire.calls) == before
        assert await cold.pending_continuation(handle) == followup
        if action == "approve":
            wire.task = Task(
                id="remote-1",
                context_id="context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
            wire.events = [wire.task]
            from ksadk.a2a.runtime_input import invoke_with_input

            _ = [event async for event in invoke_with_input(cold, initial, trigger_ref=trigger())]
            sends = [value for kind, value in wire.calls if kind == "send"]
            assert len(sends) == 2
            assert sends[-1].message_id == "supplement-1"
            assert sends[-1].task_id == "remote-1"
            assert sends[-1].context_id == "context-1"
            assert sends[-1].parts[0].text == "answer"
        else:
            wire.task = Task(
                id="remote-1",
                context_id="context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
            result = await cold.cancel(handle)
            assert result.outcome == "remote_cancelled"
            assert len([kind for kind, _ in wire.calls if kind == "send"]) == 1
        assert await cold.pending_continuation(handle) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["input", "approve", "reject"])
async def test_cold_input_wait_reconstructs_exact_prompt_and_same_task_continuation(tmp_path, decision):
    from ksadk.a2a.runtime_input import ManagedA2AInputRequested, invoke_with_input
    from ksadk.interaction.contracts import InteractionReceipt
    from ksadk.a2a.interaction_wire import approval_metadata
    from ksadk.events.canonical import ApprovalRequest

    engine = create_async_engine(dsn())
    account = str(uuid.uuid4())
    path = tmp_path / "pending-input.sqlite"
    sessions = LocalSessionService(path)
    await sessions.create_session("caller", "u", "s")
    initial = request()
    task = Task(
        id="remote-1",
        context_id="context-1",
        status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(
                message_id="question",
                role=Role.ROLE_AGENT,
                parts=[Part(text="Original input prompt")],
                metadata=(approval_metadata(ApprovalRequest(
                    kind="tool", detail={"name": "expert-c", "args": {"text": "review"}},
                )) if decision != "input" else {}),
            ),
        ),
    )
    wire = Wire(events=[task], task=task)

    def cold_service():
        return service(
            wire,
            RuntimeEventStore(LocalSessionService(path)),
            PostgresA2AInvocationCheckpointStore(
                engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
            ),
        )

    paused = None
    try:
        checkpoints = PostgresA2AInvocationCheckpointStore(
            engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
        )
        await checkpoints.initialize()
        owner = service(wire, RuntimeEventStore(sessions), checkpoints)
        original_events = [event async for event in owner.invoke(initial, trigger_ref=trigger())]
        original_input = next(e for e in original_events if e.event_type == "interaction.requested")
        cold = cold_service()
        paused = invoke_with_input(cold, initial, trigger_ref=trigger())
        async for event in paused:
            if isinstance(event, ManagedA2AInputRequested):
                restored = event
                break
        else:
            pytest.fail("cold invocation did not reconstruct its input waiter")
        assert restored.event_id == original_input.event_id
        assert restored.interaction_id == original_input.interaction_id
        assert restored.request == original_input.request
        assert restored.interaction_kind == ("structured_input" if decision == "input" else "approval")
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        wire.task = Task(
            id="remote-1",
            context_id="context-1",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        )
        wire.events = [wire.task]
        restored._managed_input_waiter.deliver(
            InteractionReceipt(
                interaction_id=restored.interaction_id,
                revision=2,
                status="resolved",
                outcome={"input": "submitted", "approve": "approved", "reject": "rejected"}[decision],
            ),
            {"result": "Recovered answer"},
        )
        if decision == "reject":
            with pytest.raises(PermissionError, match="A2A_INPUT_REJECTED"):
                _ = [event async for event in paused]
            assert sum(kind == "send" for kind, _ in wire.calls) == 1
            return
        _ = [event async for event in paused]
        sends = [message for kind, message in wire.calls if kind == "send"]
        assert len(sends) == 2
        assert sends[-1].task_id == "remote-1" and sends[-1].context_id == "context-1"
        assert MessageToDict(sends[-1].parts[0].data) == (
            "Recovered answer" if decision == "input" else "approve"
        )
        assert (
            await cold.original_request(initial.handle)
        ).deadline_unix_seconds == initial.deadline_unix_seconds
        facts = await RuntimeEventStore(LocalSessionService(path)).list("s")
        assert sum(e.event_type == "interaction.requested" for e in facts) == 1
    finally:
        if paused is not None:
            await paused.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_result_survives_cold_checkpoint_and_event_store_reopen(tmp_path):
    engine = create_async_engine(dsn())
    account = str(uuid.uuid4())
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
    )
    path = tmp_path / "terminal-replay.sqlite"
    sessions = LocalSessionService(path)
    await sessions.create_session("caller", "u", "s")
    wire, initial = Wire(), request()
    try:
        await checkpoints.initialize()
        first = service(wire, RuntimeEventStore(sessions), checkpoints)
        original = [event async for event in first.invoke(initial, trigger_ref=trigger())]
        fresh_wire = Wire()
        cold = service(
            fresh_wire,
            RuntimeEventStore(LocalSessionService(path)),
            PostgresA2AInvocationCheckpointStore(
                engine, account_id=account, tenant_id="tenant", runtime_id="runtime"
            ),
        )
        replayed = [event async for event in cold.invoke(initial, trigger_ref=trigger())]
        assert replayed == original
        assert not fresh_wire.calls
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_wait_reloads_completed_call_without_resending(tmp_path):
    engine = create_async_engine(dsn())
    store = PostgresA2AInvocationCheckpointStore(
        engine, account_id=str(uuid.uuid4()), tenant_id="tenant", runtime_id="runtime"
    )
    sessions = LocalSessionService(tmp_path / "wait-recovery.sqlite")
    await sessions.create_session("caller", "u", "s")
    events, wire, initial = RuntimeEventStore(sessions), Wire(), request()
    pending = None
    try:
        await store.initialize()
        first = service(wire, events, store)
        _ = [event async for event in first.invoke(initial, trigger_ref=trigger())]
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        recovered = A2AInvocationService(
            space_client=wire, event_store=events, checkpoint_store=store,
            recover_checkpoint_leases=True,
            policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")),
        )
        async with store.lease(initial.scope_id):
            pending = asyncio.create_task(recovered.original_request(initial.handle))
            await asyncio.sleep(0.15)
            assert not pending.done()
        original = await asyncio.wait_for(pending, 2)
        assert original == initial
        replay = [event async for event in recovered.invoke(original, trigger_ref=trigger())]
        assert replay
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await engine.dispose()
