"""Real PostgreSQL Kernel/ADK takeover fixture; only the remote wire is synthetic."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.adk import remote_agent
from ksadk.a2a.control_plane import RemoteTaskReference
from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.kernel.authorization import InMemoryNonceStore
from ksadk.kernel.bootstrap import AgentKernelRuntimeConfig, build_agent_kernel_runtime
from ksadk.kernel.contract_fingerprints import runtime_capability_matrix_digest
from ksadk.kernel.postgres_store import (
    PostgresAgentKernelStore,
    PostgresFencedSessionEventStore,
    PostgresKernelEventLog,
)
from ksadk.kernel.state import RunState
from ksadk.runners.adk_runner import ADKRunner
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
from ksadk.sessions import bind_session_service
from ksadk.sessions.continuity import ConversationSessionCore
from ksadk.sessions.postgres_service import PostgresSessionService
from tests.a2a.native_process_recovery_worker import ToolFixtureLlm
from tests.a2a.test_invocation_service_r85 import Wire, request
from tests.kernel.control_harness import AGENT, CLOCK_AT, TENANT, PermitAuthority, command
from tests.kernel.test_production_bootstrap import CONTRACT_DIGEST


async def wait_until(check):
    for _ in range(1000):
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("Kernel process did not reach its expected state")


async def main():
    stage = os.environ["R85_KERNEL_NATIVE_STAGE"]
    pause = os.environ["R85_KERNEL_NATIVE_PAUSE"]
    directory = Path(os.environ["R85_KERNEL_NATIVE_DIRECTORY"])
    state_path = directory / "kernel-native-state.json"
    dsn = os.environ["KSADK_A2A_TEST_DATABASE_URL"]
    kernel_schema = os.environ["R85_KERNEL_NATIVE_SCHEMA"]
    native_schema = os.environ["R85_KERNEL_ADK_SCHEMA"]
    session_id = "kernel-native-session"
    plain_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    plain_dsn += ("&" if "?" in plain_dsn else "?") + urlencode({"search_path": kernel_schema})
    sessions = PostgresSessionService(dsn=plain_dsn, tenant_id=TENANT)
    if stage == "start":
        await sessions.create_session(agent_id=AGENT, user_id="user", session_id=session_id)
    else:
        assert await sessions.get_session(session_id) is not None
    log = PostgresKernelEventLog(sessions._pool, tenant_id=TENANT)
    store = PostgresAgentKernelStore(sessions._pool, log, tenant_id=TENANT)
    await store.ensure_schema()
    events = PostgresFencedSessionEventStore(store)
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": kernel_schema}}
    )
    native_engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": native_schema}}
    )
    native_sessions = DatabaseSessionService(db_engine=native_engine)
    agent = remote_agent(request().spec)
    workflow = os.environ["R85_KERNEL_NATIVE_WORKFLOW"]
    if workflow == "sequential":
        agent = SequentialAgent(name="workflow", sub_agents=[agent])
    elif workflow == "tool":
        from ksadk.toolsets.a2a import call_a2a_agent

        os.environ["KSADK_A2A_SPACE_ID"] = request().spec.space_id
        agent = LlmAgent(name="tool_caller", model=ToolFixtureLlm(), tools=[call_a2a_agent])
    app = App(
        name=agent.name, root_agent=agent, resumability_config=ResumabilityConfig(is_resumable=True)
    )
    runner = ADKRunner(
        SimpleNamespace(entry_point="agent.py", agent_variable="root_agent", name="fixture"), "."
    )
    runner._agent = agent
    runner._module = SimpleNamespace(app=app)
    runner._runner = Runner(app=app, session_service=native_sessions)
    runner._session_service = native_sessions
    runner._short_term_memory = SimpleNamespace(
        backend="database",
        session_service=native_sessions,
        create_session=native_sessions.create_session,
    )
    await runner.prepare_runtime_capabilities()
    adapter = ADKRuntimeAdapter(runner)
    checkpoint_store = PostgresA2AInvocationCheckpointStore(
        engine, account_id="fixture-account", tenant_id=TENANT, runtime_id=AGENT
    )
    await checkpoint_store.initialize()
    wire = Wire()
    completed_task = wire.task
    if pause == "input":
        wire.task = Task(
            id="remote-1",
            context_id="context-1",
            status=TaskStatus(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                message=Message(
                    message_id="question", role=Role.ROLE_AGENT, parts=[Part(text="Provide input")]
                ),
            ),
        )
        wire.events = [wire.task]

    if stage == "resume":
        previous = json.loads(state_path.read_text())
        if previous["remote_binding"] is not None:
            wire.binding = RemoteTaskReference(*previous["remote_binding"])

    async def discover():
        return [
            SimpleNamespace(
                agent_id=request().spec.target_agent_id,
                version_id=request().spec.expected_version_id,
                agent_card=SimpleNamespace(name="expert"),
            )
        ]

    adapter.configure_managed_a2a(
        SimpleNamespace(
            outbound_enabled=True,
            client_for_space=lambda _: SimpleNamespace(discover=discover),
            runtime_metadata=SimpleNamespace(agent_id="caller"),
            invocation_service_for_space=lambda _space, **deps: A2AInvocationService(
                space_client=wire, checkpoint_store=checkpoint_store, **deps
            ),
        )
    )
    permit_authority = PermitAuthority()
    runtime = build_agent_kernel_runtime(
        AgentKernelRuntimeConfig(
            agent_instance_id=AGENT,
            authority_mode="hosted",
            driver="postgres",
            dsn=plain_dsn,
            jwks=permit_authority.jwks(),
            permit_issuer="agentengine-server-test",
            adapter_provider=lambda: adapter,
            contract_digest=CONTRACT_DIGEST,
            capability_digest=runtime_capability_matrix_digest(adapter.capabilities()),
            bundle_digest="b" * 64,
            activation_id="native-kernel-" + stage,
            nonce_store=InMemoryNonceStore(),
            store=store,
            session_events=events,
            session_service=sessions,
            clock=lambda: CLOCK_AT,
            poll_interval=0.01,
            lease_ttl_seconds=60,
        )
    )
    with bind_session_service(sessions):
        if stage == "start":
            await runtime.kernel.submit(
                command(
                    idempotency_key="native-kernel-start",
                    session_id=session_id,
                    payload={"content": "hello", "session_context": {}},
                ),
                permit=permit_authority.permit(session_id=session_id),
            )
        await runtime.start()

        if stage == "resume" and pause == "terminal":

            async def recovered_completion():
                current = await store.load_run(previous["run_id"])
                if current.state in {RunState.FAILED, RunState.INTERRUPTED}:
                    raise AssertionError(f"Unexpected terminal state: {current.state}")
                return current if current.state is RunState.COMPLETED else None

            run = await wait_until(recovered_completion)
            lease = await store.current_lease(AGENT, session_id)
            binding = await ConversationSessionCore(sessions).get_binding_by_session_id(
                session_id, "adk"
            )
            assert binding == previous["binding"]
            assert run.metadata["start_request"] == previous["start_request"]
            assert lease.fencing_token > previous["fence"]
            assert not wire.calls
            facts = await events.read(session_id, 0, 1000)
            terminal = next(event for event in facts if event.event_type == "run.completed")
            assert terminal.payload["output_refs"]
            record = await store.get(
                previous["interaction_id"],
                tenant_id=TENANT,
                agent_instance_id=AGENT,
                session_id=session_id,
                run_id=run.run_id,
            )
            report = {
                "run_id": run.run_id,
                "fence": lease.fencing_token,
                "sends": 0,
                "event_types": [event.event_type for event in facts],
                "ledger_events": [
                    event.event_type for event in facts if event.family == "interaction"
                ],
                "resolved": record.status,
            }
            (directory / "kernel-native-recovered.json").write_text(json.dumps(report))
            await runtime.close()
            await sessions.aclose()
            await engine.dispose()
            await native_engine.dispose()
            return

        target_kind = "structured_input" if stage == "resume" and pause == "input" else "approval"

        async def submit(record, action, response=None):
            receipt = await runtime.kernel.submit(
                command(
                    "submit_interaction",
                    idempotency_key="native-kernel-" + record.interaction_id,
                    session_id=session_id,
                    payload={
                        "run_id": record.run_id,
                        "interaction_id": record.interaction_id,
                        "token_ref": "fixture",
                        "action": action,
                        "response": response or {},
                        "expected_revision": record.revision,
                    },
                ),
                permit=permit_authority.permit(
                    operations=("submit_interaction",), session_id=session_id
                ),
            )
            assert receipt.status == "accepted"

        async def waiting():
            run = await store.find_active_run(AGENT, session_id)
            if run is None:
                return None
            if run.state in {RunState.FAILED, RunState.INTERRUPTED}:
                raise AssertionError(f"Unexpected terminal state: {run.state}")
            pending = [
                record
                for record in await store.list_pending_interactions(TENANT, session_id)
                if record.kind == target_kind
            ]
            execution = runtime.worker.execution_for(run.run_id)
            if run.state is RunState.WAITING and pending and execution is not None:
                if pending[0].interaction_id in execution.managed_interaction_waiters:
                    return run, pending[0]
            return None

        run, interaction = await wait_until(waiting)
        if stage == "start" and pause == "input":
            await submit(interaction, "approve")
            target_kind = "structured_input"
            run, interaction = await wait_until(waiting)
        lease = await store.current_lease(AGENT, session_id)
        binding = await ConversationSessionCore(sessions).get_binding_by_session_id(
            session_id, "adk"
        )
        if stage == "start" and pause == "terminal":
            original_append = events.append

            async def cut_after_terminal(envelope, *, guard):
                committed = await original_append(envelope, guard=guard)
                if (
                    envelope.family == "runtime"
                    and envelope.event_type == "item.completed"
                    and envelope.payload.get("item_kind") == "status"
                ):
                    assert sum(kind == "send" for kind, _ in wire.calls) == 1
                    state_path.write_text(
                        json.dumps(
                            {
                                "run_id": run.run_id,
                                "interaction_id": interaction.interaction_id,
                                "fence": lease.fencing_token,
                                "binding": binding,
                                "start_request": run.metadata["start_request"],
                                "remote_binding": [
                                    wire.binding.remote_task_id,
                                    wire.binding.remote_context_id,
                                ],
                                "sent_messages": [
                                    message.message_id
                                    for kind, message in wire.calls
                                    if kind == "send"
                                ],
                            }
                        )
                    )
                    # Child terminal committed; native delivery and root completion
                    # have not yet been acknowledged.
                    os._exit(23)
                return committed

            events.append = cut_after_terminal
            await submit(interaction, "approve")
            await asyncio.wait_for(asyncio.Event().wait(), 10)

        if stage == "start":
            assert sum(kind == "send" for kind, _ in wire.calls) == (1 if pause == "input" else 0)
            assert all(
                message.status.value == "completed" for message in await store.list_messages(AGENT)
            )
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": run.run_id,
                        "interaction_id": interaction.interaction_id,
                        "fence": lease.fencing_token,
                        "binding": binding,
                        "start_request": run.metadata["start_request"],
                        "remote_binding": (
                            [wire.binding.remote_task_id, wire.binding.remote_context_id]
                            if wire.binding is not None
                            else None
                        ),
                        "sent_messages": [
                            message.message_id for kind, message in wire.calls if kind == "send"
                        ],
                    }
                )
            )
            os._exit(23)
        assert run.run_id == previous["run_id"]
        assert interaction.interaction_id == previous["interaction_id"]
        assert binding == previous["binding"]
        assert run.metadata["start_request"] == previous["start_request"]
        assert lease.fencing_token > previous["fence"]
        assert not any(kind == "send" for kind, _ in wire.calls)
        if pause == "input":
            wire.task, wire.events = completed_task, [completed_task]
            await submit(interaction, "submit", "resumed answer")
            target_kind = "approval"
            _, continuation_approval = await wait_until(waiting)
            await submit(continuation_approval, "approve")
        else:
            await submit(interaction, "approve")

        async def completed():
            current = await store.load_run(run.run_id)
            if current.state in {RunState.FAILED, RunState.INTERRUPTED}:
                raise AssertionError(f"Unexpected terminal state: {current.state}")
            return current if current.state is RunState.COMPLETED else None

        await wait_until(completed)
        facts = await events.read(session_id, 0, 1000)
        report = {
            "run_id": run.run_id,
            "fence": lease.fencing_token,
            "sends": sum(kind == "send" for kind, _ in wire.calls),
            "event_types": [event.event_type for event in facts],
            "ledger_events": [
                event.event_type
                for event in facts
                if event.family == "interaction" and event.family_version == 1
            ],
            "sent_messages": [message.message_id for kind, message in wire.calls if kind == "send"],
            "sent_tasks": [message.task_id for kind, message in wire.calls if kind == "send"],
            "resolved": (
                await store.get(
                    interaction.interaction_id,
                    tenant_id=TENANT,
                    agent_instance_id=AGENT,
                    session_id=session_id,
                    run_id=run.run_id,
                )
            ).status,
        }
        (directory / "kernel-native-recovered.json").write_text(json.dumps(report))
        await runtime.close()
    await sessions.aclose()
    await engine.dispose()
    await native_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
