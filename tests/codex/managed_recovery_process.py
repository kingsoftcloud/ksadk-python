"""Local crash fixture: real native child, SQLite Kernel and PostgreSQL A2A state."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from google.protobuf.json_format import MessageToDict
from openai_codex import CodexConfig
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.control_plane import RemoteTaskReference
from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.authorization import AgentControlPermitVerifier
from ksadk.kernel.control import AgentKernel
from ksadk.kernel.recovery import RecoveryCoordinator
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.kernel.state import InboxState, RunState
from ksadk.kernel.store import ActivationLeaseRequest
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.sessions.local_service import LocalSessionService
from tests.a2a.test_invocation_service_r85 import Wire, request
from tests.e2e.codex_app_server_fixture import RealCodexFactory
from tests.kernel.control_harness import AGENT, CLOCK_AT, TENANT, PermitAuthority, command


def report(value):
    print("RECOVERY_FIXTURE:" + json.dumps(value), flush=True)


async def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    wait_kind = os.environ.get("CODEX_FIXTURE_WAIT_KIND", "input")
    metadata_path = root / "run.json"
    log = root / "wire.jsonl"
    engine = create_async_engine(os.environ["KSADK_A2A_TEST_DATABASE_URL"])
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine,
        account_id=root.name,
        tenant_id=TENANT,
        runtime_id=AGENT,
    )
    await checkpoints.initialize()
    pending_task = Task(
        id="remote-1",
        context_id="context-1",
        status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(
                message_id="question",
                role=Role.ROLE_AGENT,
                parts=[Part(text="Please provide input")],
            ),
        ),
    )

    class DurablePeer(Wire):
        def write(self, kind, **values):
            with log.open("a") as stream:
                stream.write(json.dumps({"kind": kind, **values}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            continuation = bool(message.task_id)
            self.write(
                "continue" if continuation else "send",
                task_id=message.task_id,
                context_id=message.context_id,
                message_id=message.message_id,
                data_parts=[
                    part["data"] for part in MessageToDict(message).get("parts", [])
                    if "data" in part
                ],
            )

            async def events():
                yield self.task if continuation or wait_kind == "approval" else pending_task

            yield events()

        async def get_prepared_task(self, prepared):
            self.write("get", task_id=prepared.remote_task.remote_task_id)
            continued = any(
                json.loads(line)["kind"] == "continue" for line in log.read_text().splitlines()
            )
            return self.task if continued or wait_kind == "approval" else pending_task

        async def cancel_prepared_task(self, prepared):
            self.write("cancel")
            return self.task

    wire = DurablePeer()
    if mode == "second" and wait_kind == "input":
        wire.binding = RemoteTaskReference("remote-1", "context-1")
    allow = A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow"))
    bootstrap = SimpleNamespace(
        outbound_enabled=True,
        runtime_metadata=SimpleNamespace(agent_id="caller"),
        invocation_checkpoint_store=checkpoints,
        invocation_service_for_space=lambda _, **deps: A2AInvocationService(
            space_client=wire,
            event_store=deps["event_store"],
            checkpoint_store=checkpoints,
            policy=deps["policy"] if wait_kind == "approval" else allow,
        ),
    )
    factory = RealCodexFactory(responses_url=os.environ["CODEX_FIXTURE_RESPONSES_URL"])
    client = factory(CodexConfig(env={"CODEX_HOME": str(root / mode), "HOME": str(root)}))
    adapter = CodexRuntimeAdapter(
        client, a2a_experts=[request().spec], managed_a2a_bootstrap=bootstrap
    )
    adapter.adopt_app_lifetime()
    sessions = LocalSessionService(root / "sessions.sqlite")
    if mode == "first":
        await sessions.create_session(agent_id=AGENT, user_id="kernel-user", session_id="s1")
    events = SessionServiceEventStore(sessions)
    store = SQLiteAgentKernelStore(root / "sessions.sqlite", events)
    await store.ensure_schema()
    authority = PermitAuthority()
    kernel = AgentKernel(
        store,
        events,
        AgentControlPermitVerifier(authority.jwks()),
        capabilities=adapter.capabilities,
        clock=lambda: CLOCK_AT,
    )
    worker = AgentKernelWorker(
        store,
        adapter_factory=lambda: adapter,
        session_events=events,
        session_service=sessions,
        start_request_defaults={"model": "fixture-model"},
    )
    lease = await store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id=AGENT,
            session_id="s1",
            activation_id=mode,
            lease_ttl_seconds=60 if mode == "second" else 15,
        )
    )
    try:
        if mode == "first":
            receipt = await kernel.submit(
                command(
                    idempotency_key="native-crash",
                    payload={
                        "content": "call expert", "model": "fixture-model",
                        "runtime_options": {
                            "tool_approval_mode": "ask" if wait_kind == "approval" else "full",
                        },
                    },
                ),
                permit=authority.permit(operations=("enqueue",)),
            )
            assert receipt.status == "accepted"
            started = await worker.run_once(AGENT, lease)
            run_id = started.run_id
        else:
            previous = json.loads(metadata_path.read_text())
            run_id = previous["run_id"]
            matrix = adapter.capabilities()
            assert matrix.attach.supported and matrix.durable_restore.supported
            coordinator = RecoveryCoordinator(
                store,
                events,
                lambda: matrix,
                adapter_factory=lambda: adapter,
                session_service=sessions,
                execution_sink=worker.adopt_execution,
                recovery_stream_sink=worker.recover_stream,
            )
            recovered = await coordinator.recover(AGENT, lease, run_id=run_id)
            assert recovered.outcome == "attached", recovered
        async with asyncio.timeout(20):
            while True:
                pending = await store.list_pending_interactions(TENANT, "s1")
                current = await store.load_run(run_id)
                assert current.state not in {RunState.FAILED, RunState.COMPLETED}, current
                if pending:
                    execution = worker.execution_for(run_id)
                    # A real client can reply as soon as the durable request is
                    # visible, before takeover installs its process-local waiter.
                    break
                await asyncio.sleep(0.02)
        interaction = pending[0]
        assert interaction.kind == ("approval" if wait_kind == "approval" else "structured_input")
        if mode == "first":
            metadata = {
                "run_id": run_id,
                "interaction_id": interaction.interaction_id,
                "thread_id": current.metadata["handle"]["run_id"],
                "native_pid": client._codex._client._sync._proc.pid,
                "fence": lease.fencing_token,
            }
            metadata_path.write_text(json.dumps(metadata))
            report({"status": "waiting", **metadata})
            await asyncio.Event().wait()
        assert interaction.interaction_id == previous["interaction_id"]
        assert current.metadata["handle"]["run_id"] == previous["thread_id"]
        assert lease.fencing_token > previous["fence"]
        receipt = await kernel.submit(
            command(
                "submit_interaction",
                idempotency_key="original-reply",
                payload={
                    "run_id": run_id,
                    "interaction_id": interaction.interaction_id,
                    "token_ref": "fixture",
                    "action": "approve" if wait_kind == "approval" else "submit",
                    "response": {} if wait_kind == "approval" else "yes",
                    "expected_revision": interaction.revision,
                },
            ),
            permit=authority.permit(operations=("submit_interaction",)),
        )
        assert receipt.status == "accepted", receipt
        async with asyncio.timeout(20):
            while True:
                processed = await worker.run_once(AGENT, lease)
                if processed.outcome != "idle":
                    assert processed.outcome == "completed", processed
                    break
                await asyncio.sleep(0.02)
        submitted = await store.load_by_idempotency("s1", "original-reply")
        # WorkResult.completed also includes a rejected/discarded command;
        # require the original answer to have actually reached the waiter.
        assert submitted is not None
        assert submitted.status == InboxState.COMPLETED, submitted.status
        async with asyncio.timeout(20):
            while True:
                current = await store.load_run(run_id)
                if current.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                    break
                await asyncio.sleep(0.02)
        assert current.state == RunState.COMPLETED, current
        async with asyncio.timeout(5):
            await execution.stream_task
        assert not await store.list_pending_interactions(TENANT, "s1")
        from ksadk.events.canonical_store import RuntimeEventStore

        facts = await RuntimeEventStore(events).list("s1", run_id=run_id)
        tool_starts = [
            event
            for event in facts
            if event.event_type == "item.started"
            and getattr(event, "item_kind", None) == "tool_call"
            and event.source.framework == "codex"
        ]
        assert len(tool_starts) == 1
        closures = [
            event
            for event in facts
            if event.event_type == "item.completed"
            and getattr(event, "item_id", None) == tool_starts[0].item_id
            and event.scope_id == tool_starts[0].scope_id
        ]
        assert len(closures) == 1
        assert closures[0].source.metadata["recovery_method"] == "native_rollout_output"
        assert closures[0].source.metadata["native_callback_replayed"] is False
        assert sum(event.event_type == "run.completed" for event in facts) == 1
        report(
            {
                "status": "completed",
                "run_id": run_id,
                "thread_id": previous["thread_id"],
                "interaction_id": interaction.interaction_id,
                "fence": lease.fencing_token,
            }
        )
    finally:
        await adapter.close_all()
        assert all(process.poll() is not None for process in factory.processes)
        await store.close()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        report({"status": "error", "error": repr(error)})
        raise
