"""Real PostgreSQL Kernel/LangGraph takeover fixture; remote wire is synthetic."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from sqlalchemy.ext.asyncio import create_async_engine

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
from ksadk.sessions import bind_session_service
from ksadk.sessions.continuity import ConversationSessionCore
from ksadk.sessions.postgres_service import PostgresSessionService
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
    product_identity = None
    if os.environ.get("R87_KERNEL_IDENTITY_SCOPE") == "product":
        product_identity = {
            "identity_namespace": "a2a-product-key:" + hashlib.sha256(b"sub-1").hexdigest(),
            "tenant_id": hashlib.sha256(b"account-1").hexdigest(),
            "subject_type": "user", "subject_id": hashlib.sha256(b"user-1").hexdigest(),
        }
    from ksadk.sessions.invocation_identity import identity_native_user_id

    def product_command(*args, **kwargs):
        result = command(*args, **kwargs)
        if product_identity is not None:
            result = result.model_copy(update={"payload": {
                **result.payload, "invocation_identity": product_identity,
            }})
        return result

    plain_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    plain_dsn += ("&" if "?" in plain_dsn else "?") + urlencode({"search_path": kernel_schema})
    sessions = PostgresSessionService(dsn=plain_dsn, tenant_id=TENANT)
    if stage == "start":
        await sessions.create_session(
            agent_id=AGENT, session_id=session_id,
            user_id=identity_native_user_id(product_identity) if product_identity else "user",
        )
    else:
        assert await sessions.get_session(session_id) is not None
    log = PostgresKernelEventLog(sessions._pool, tenant_id=TENANT)
    store = PostgresAgentKernelStore(sessions._pool, log, tenant_id=TENANT)
    await store.ensure_schema()
    events = PostgresFencedSessionEventStore(store)
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": kernel_schema}}
    )
    from contextlib import AsyncExitStack

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, MessagesState, StateGraph

    from ksadk.a2a.langgraph_managed import remote_node
    from ksadk.runners.langgraph_runner import LangGraphRunner
    from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter

    resources = AsyncExitStack()
    graph_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    graph_dsn += "?options=-csearch_path%3D" + native_schema
    saver = await resources.enter_async_context(AsyncPostgresSaver.from_conn_string(graph_dsn))
    await saver.setup()
    workflow = os.environ["R85_KERNEL_NATIVE_WORKFLOW"]
    if workflow in {"tool", "handoff"}:
        from langchain_core.messages import AIMessage
        from langgraph.prebuilt import create_react_agent

        from ksadk.a2a.langgraph_managed import handoff_tool
        from ksadk.toolsets.a2a import get_a2a_tools
        from tests.runners.test_langchain_hitl_langgraph import SeqToolModel

        class CheckpointAwareModel(SeqToolModel):
            def _generate(self, messages, *args, **kwargs):
                self.i = int(any(message.type == "tool" for message in messages))
                return super()._generate(messages, *args, **kwargs)

            def _stream(self, messages, *args, **kwargs):
                self.i = int(any(message.type == "tool" for message in messages))
                yield from super()._stream(messages, *args, **kwargs)

        os.environ["KSADK_A2A_SPACE_ID"] = request().spec.space_id
        call_args = {"message": "hello"}
        if workflow == "tool":
            call_args["agent"] = "expert"
        model = CheckpointAwareModel(msgs=[
            AIMessage(content="", tool_calls=[{
                "id": "original-native-call",
                "name": "call_a2a_agent" if workflow == "tool" else "expert",
                "args": call_args,
            }]), AIMessage(content="finished"),
        ])
        graph = create_react_agent(
            model, get_a2a_tools() if workflow == "tool" else [handoff_tool(request().spec)],
            checkpointer=saver,
        )
    else:
        builder = StateGraph(MessagesState)
        builder.add_node("expert", remote_node(request().spec))
        builder.add_edge(START, "expert")
        builder.add_edge("expert", END)
        graph = builder.compile(checkpointer=saver)
    runner = LangGraphRunner(
        SimpleNamespace(entry_point="agent.py", agent_variable="root_agent", name="fixture"), "."
    )
    runner._agent = graph
    await runner.prepare_runtime_capabilities()
    adapter = LangGraphRuntimeAdapter(runner)
    checkpoint_store = PostgresA2AInvocationCheckpointStore(
        engine, account_id="fixture-account", tenant_id=TENANT, runtime_id=AGENT
    )
    await checkpoint_store.initialize()
    wire = Wire()
    completed_task = wire.task
    from ksadk.a2a.interaction_wire import approval_metadata
    from ksadk.events.canonical import ApprovalRequest
    if pause in {"input", "remote_approval"}:
        wire.task = Task(
            id="remote-1",
            context_id="context-1",
            status=TaskStatus(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                message=Message(
                    message_id="question", role=Role.ROLE_AGENT, parts=[Part(text="Provide input")],
                    metadata=(approval_metadata(ApprovalRequest(kind="tool", detail={
                        "name": "expert-c", "args": {"text": "review"},
                    })) if pause == "remote_approval" else {}),
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
                product_command(
                    idempotency_key="native-kernel-start",
                    session_id=session_id,
                    payload={"content": "hello", "session_context": {}},
                ),
                permit=permit_authority.permit(session_id=session_id),
            )
        await runtime.start()

        async def assert_product_identity(run):
            if product_identity is None:
                return
            from ksadk.runtime_context import TRUSTED_IDENTITY_METADATA_KEY
            session = await sessions.get_session_metadata(session_id)
            assert session.user_id == identity_native_user_id(product_identity)
            start = run.metadata["start_request"]
            assert start["user_id"] == session.user_id
            assert start["metadata"][TRUSTED_IDENTITY_METADATA_KEY] == product_identity

        if stage == "resume" and pause == "terminal":

            async def recovered_completion():
                current = await store.load_run(previous["run_id"])
                if current.state in {RunState.FAILED, RunState.INTERRUPTED}:
                    raise AssertionError(f"Unexpected terminal state: {current.state}")
                return current if current.state is RunState.COMPLETED else None

            run = await wait_until(recovered_completion)
            lease = await store.current_lease(AGENT, session_id)
            binding = await ConversationSessionCore(sessions).get_binding_by_session_id(
                session_id, "langgraph"
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
            await assert_product_identity(run)
            report = {
                "product_identity_preserved": product_identity is not None,
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
            await resources.aclose()
            return

        target_kind = "structured_input" if stage == "resume" and pause == "input" else "approval"
        target_schema = (
            "a2a.remote-approval/v1" if stage == "resume" and pause == "remote_approval" else None
        )

        async def submit(record, action, response=None):
            receipt = await runtime.kernel.submit(
                product_command(
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
                and (target_schema is None or record.native_target.get("schema") == target_schema)
            ]
            execution = runtime.worker.execution_for(run.run_id)
            if run.state is RunState.WAITING and pending and execution is not None:
                if pending[0].interaction_id in execution.managed_interaction_waiters:
                    return run, pending[0]
            return None

        run, interaction = await wait_until(waiting)
        if stage == "start" and pause in {"input", "remote_approval"}:
            await submit(interaction, "approve")
            target_kind = "structured_input" if pause == "input" else "approval"
            target_schema = "a2a.input/v1" if pause == "input" else "a2a.remote-approval/v1"
            run, interaction = await wait_until(waiting)
        lease = await store.current_lease(AGENT, session_id)
        binding = await ConversationSessionCore(sessions).get_binding_by_session_id(
            session_id, "langgraph"
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
            expected_sends = 1 if pause in {"input", "remote_approval"} else 0
            assert sum(kind == "send" for kind, _ in wire.calls) == expected_sends
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
        if pause in {"input", "remote_approval"}:
            wire.task, wire.events = completed_task, [completed_task]
            await submit(interaction, "submit" if pause == "input" else "approve",
                         "resumed answer" if pause == "input" else {})
            target_kind = "approval"
            target_schema = "a2a.approval/v1"
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
        await assert_product_identity(run)
        report = {
            "product_identity_preserved": product_identity is not None,
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
    await resources.aclose()


if __name__ == "__main__":
    asyncio.run(main())
