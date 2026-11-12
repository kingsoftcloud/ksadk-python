"""Local PostgreSQL gate for selected Kernel storage and independent A2A stores.

Kernel assembly, native LangGraph, A2A protocol HTTP/SSE, and all storage are
production implementations. Registry discovery/permit issuance is a local
contract fixture; this does not validate cloud authorization or routing.
Only explicitly configured loopback PostgreSQL databases are accepted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from a2a.server.context import ServerCallContext
from a2a.types import AgentCard, TaskState
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.protobuf.json_format import ParseDict
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.bootstrap import AgentEngineA2ABootstrap, RuntimeA2AMetadata
from ksadk.a2a.context_store import PostgresA2AContextStore
from ksadk.a2a.control_plane import (
    A2ARoute,
    A2ARouteInterface,
    A2ATarget,
    DiscoveredAgent,
    PreparedA2AOperation,
    RemoteTaskReference,
    SpaceAgentPage,
)
from ksadk.a2a.identity import A2AIngressIdentity
from ksadk.a2a.invocation_checkpoint import (
    A2AInvocationCheckpoint,
    PostgresA2AInvocationCheckpointStore,
)
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.platform import PostgresA2ACheckpointStore, _managed_database_dsn
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.a2a.resume_store import PostgresA2AResumeStateStore
from ksadk.a2a.task_event_outbox_owned import OwnedPostgresA2ATaskEventOutbox
from ksadk.a2a.task_store import build_a2a_task_store
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.harness.engine.postgres_checkpointer import create_bounded_postgres_saver
from ksadk.kernel.bootstrap import clear_agent_kernel_runtime
from ksadk.kernel.ingress import clear_agent_kernel, map_run_request, trusted_context
from ksadk.kernel.runtime_environment import bootstrap_runtime_from_env
from ksadk.kernel.state import RunState
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.toolsets.a2a import get_a2a_tools


def _databases():
    configured = os.getenv("KSADK_SELECTED_KERNEL_TEST_PG", "").strip()
    if not configured:
        pytest.skip("KSADK_SELECTED_KERNEL_TEST_PG must name the local Docker test PG")
    parts = urlsplit(configured)
    assert parts.hostname in {"localhost", "127.0.0.1", "::1"}
    assert parts.scheme in {"postgresql", "postgres"}
    return {
        name: urlunsplit(parts._replace(path="/kernel_" + name))
        for name in ("user", "platform", "native", "a2a")
    }


async def _until(probe, *, timeout=20):
    async with asyncio.timeout(timeout):
        while True:
            result = await probe()
            if result:
                return result
            await asyncio.sleep(0.025)


@asynccontextmanager
async def _remote_process(settings):
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AGENT_KERNEL_", "KSADK_A2A_"))
    }
    child_env.update(
        {
            "AGENT_KERNEL_ENABLED": "0",
            "KSADK_AGENT_KERNEL": "0",
            "KSADK_A2A_AGENT_ID": "ar-callee-" + settings["identity"],
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.a2a.selected_kernel_database_worker",
        cwd=Path(__file__).resolve().parents[2],
        env=child_env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        process.stdin.write((json.dumps(settings) + "\n").encode())
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 20)
        if not line:
            raise AssertionError((await process.stderr.read()).decode()[-3000:])
        yield json.loads(line)["url"]
    finally:
        failed = sys.exc_info()[0] is not None
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except TimeoutError:
                process.kill()
                await process.wait()
        if failed:
            diagnostics = (await process.stderr.read()).decode()
            if diagnostics:
                print("Local A2A callee diagnostics:\n" + diagnostics[-6000:])


class _RegistryFixture:
    """Test-owned discovery/permit seam; data-plane calls use real HTTP/SSE."""

    task_event_format = "canonical_v2"

    def __init__(self, card, url, identity):
        self.agent_id = "a2a-agent-" + identity
        self.version_id = "a2a-version-" + identity
        self.task_id = "a2a-task-" + identity
        self.card, self.url = card, url
        self.binding = None
        self.prepares = 0
        self.operations = []
        self.received = []
        self.deliver = asyncio.Event()

    async def list_space_agents(self, *_args, **_kwargs):
        return SpaceAgentPage(
            agents=[
                DiscoveredAgent(
                    self.agent_id,
                    self.version_id,
                    "hosted",
                    self.card,
                    route_kind="hosted_gateway",
                )
            ]
        )

    def prepared(self):
        return PreparedA2AOperation(
            self.task_id,
            A2ATarget(self.agent_id, self.version_id, "local-card"),
            A2ARoute("hosted_gateway", A2ARouteInterface(self.url, "JSONRPC", "1.0"), True),
            "local-registry-fixture",
            "2099-01-01T00:00:00Z",
            remote_task=self.binding,
        )

    async def prepare_call(self, **kwargs):
        assert kwargs["target_agent_id"] == self.agent_id
        assert kwargs["expected_version_id"] == self.version_id
        self.prepares += 1
        return self.prepared()

    async def prepare_task_operation(self, **kwargs):
        assert kwargs["platform_task_id"] == self.task_id
        self.operations.append(kwargs["operation"])
        return self.prepared()

    async def bind_remote_task(self, **kwargs):
        assert kwargs["platform_task_id"] == self.task_id
        self.binding = RemoteTaskReference(kwargs["remote_task_id"], kwargs["remote_context_id"])

    def gateway_token(self):
        return ""

    async def append_task_events(self, *, platform_task_id, events):
        assert platform_task_id == self.task_id
        await self.deliver.wait()
        self.received.extend(events)
        return {}


class _CallerState(MessagesState):
    starts: int
    answer: str


def _caller_graph(saver, calls, *, collect_input=True):
    def before(state):
        calls.append("before")
        return {"starts": state.get("starts", 0) + 1}

    def collect(state):
        answer = (
            interrupt(
                {
                    "kind": "structured_input",
                    "prompt": "Select the local resource",
                    "schema": {
                        "type": "object",
                        "properties": {"resource": {"type": "string"}},
                        "required": ["resource"],
                    },
                }
            )
            if collect_input
            else {"resource": "local cancellation resource"}
        )
        return {
            "answer": answer["resource"],
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "call_a2a_agent",
                            "id": "local-expert-call",
                            "args": {"agent": "expert", "message": answer["resource"]},
                        }
                    ],
                )
            ],
        }

    def finish(state):
        return {"messages": [AIMessage(content="Caller completed: " + state["answer"])]}

    graph = StateGraph(_CallerState)
    graph.add_node("before", before)
    graph.add_node("collect", collect)
    graph.add_node("tools", ToolNode(get_a2a_tools()))
    graph.add_node("finish", finish)
    graph.add_edge(START, "before")
    graph.add_edge("before", "collect")
    graph.add_edge("collect", "tools")
    graph.add_edge("tools", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=saver)


def _runner(graph, name):
    runner = LangGraphRunner(
        SimpleNamespace(
            entry_point="agent.py",
            agent_variable="root_agent",
            name=name,
        ),
        ".",
    )
    runner._agent = graph
    runner._module = SimpleNamespace(
        ksadk_prepare_state=lambda payload, _context: {
            "messages": [HumanMessage(content=str(payload.get("input", "local proof")))],
        }
    )
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_before_send", [False, True], ids=["resume-complete-reopen", "cancel-before-send"]
)
async def test_selected_kernel_pg_preserves_independent_managed_a2a_stores(
    monkeypatch,
    cancel_before_send,
):
    databases = _databases()
    identity = uuid4().hex
    agent, tenant, session = (
        "ar-selected-" + identity,
        "selected-" + identity,
        "session-" + identity,
    )
    namespace = "selected-" + identity
    for key, value in {
        "AGENT_KERNEL_ENABLED": "1",
        "AGENT_KERNEL_STORE_DRIVER": "postgres",
        "AGENT_KERNEL_AUTHORITY_MODE": "local",
        "AGENT_KERNEL_STORE_DSN": databases["platform"],
        "AGENT_INSTANCE_ID": agent,
        "KSADK_TENANT_ID": tenant,
        "KSADK_SESSION_NAMESPACE": namespace,
        "KSADK_SESSION_BACKEND": "postgres",
        "KSADK_SESSION_DSN": databases["user"],
        "KSADK_A2A_DATABASE_DSN": databases["a2a"],
        "KSADK_LANGGRAPH_CHECKPOINT_DSN": databases["native"],
        "KSADK_A2A_AGENT_ID": agent,
        "KSADK_A2A_SPACE_ID": "a2a-space-" + identity,
        "KSADK_TOOL_APPROVAL_MODE": "full",
    }.items():
        monkeypatch.setenv(key, value)
    clear_agent_kernel_runtime()
    clear_agent_kernel()
    assert _managed_database_dsn().endswith("/kernel_a2a")
    engine = create_async_engine(_managed_database_dsn(), pool_size=3, max_overflow=2)
    native_saver, native_pool = await create_bounded_postgres_saver(databases["native"])
    context_store = PostgresA2AContextStore(engine)
    resume_store = PostgresA2AResumeStateStore(engine)
    task_store = build_a2a_task_store(engine=engine)
    await context_store.initialize()
    await resume_store.initialize()
    caller_calls = []
    caller_runner = _runner(
        _caller_graph(native_saver, caller_calls, collect_input=not cancel_before_send),
        "caller",
    )
    caller_adapter = LangGraphRuntimeAdapter(caller_runner)
    runtime = bootstrap = registry = None
    gateway_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    gateway_public = (
        gateway_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    wire_methods = []

    async def sign_gateway_fixture(request):
        if request.content:
            method = json.loads(request.content).get("method")
            if method:
                wire_methods.append(method)
        now = time.time()
        target = "ar-callee-" + identity
        request.headers["x-agentengine-route-target"] = target
        request.headers["x-agentengine-a2a-assertion"] = jwt.encode(
            {
                "schema": "a2a-forward-identity/v1",
                "iss": "agentengine-a2a-gateway",
                "aud": "a2a-runtime",
                "iat": now,
                "exp": now + 120,
                "jti": uuid4().hex,
                "account_id": tenant,
                "tenant_id": tenant,
                "caller_principal_type": "runtime",
                "caller_principal_id": agent,
                "caller_runtime_id": agent,
                "authn_mode": "workload_permit",
                "target_agent_id": target,
                "target_runtime_id": target,
                "target_a2a_agent_id": "a2a-agent-" + identity,
                "body_sha256": hashlib.sha256(request.content).hexdigest(),
                "query_sha256": hashlib.sha256(request.url.query).hexdigest(),
                "http_method": request.method,
                "path": request.url.path,
            },
            gateway_key,
            algorithm="RS256",
        )

    try:
        async with _remote_process(
            {
                "dsn": databases["a2a"],
                "identity": identity,
                "namespace": namespace,
                "tenant": tenant,
                "slow": cancel_before_send,
                "gateway_public_key": gateway_public,
            }
        ) as url:
            async with httpx.AsyncClient(trust_env=False) as unsigned:
                assert (await unsigned.get(url + "/.well-known/agent-card.json")).status_code == 401
            http = httpx.AsyncClient(
                trust_env=False,
                timeout=20,
                event_hooks={"request": [sign_gateway_fixture]},
            )
            card = ParseDict(
                (await http.get(url + "/.well-known/agent-card.json")).json(), AgentCard()
            )
            registry = _RegistryFixture(
                card,
                card.supported_interfaces[0].url,
                identity,
            )
            outbox = OwnedPostgresA2ATaskEventOutbox(
                engine,
                account_id=tenant,
                tenant_id=tenant,
                runtime_id=agent,
            )
            invocation_store = PostgresA2AInvocationCheckpointStore(
                engine,
                account_id=tenant,
                tenant_id=tenant,
                runtime_id=agent,
            )
            bootstrap = AgentEngineA2ABootstrap.from_platform(
                runtime_metadata=RuntimeA2AMetadata(
                    tenant,
                    tenant,
                    agent,
                    "",
                    agent,
                    "",
                    "",
                    "",
                    (),
                ),
                task_store=task_store,
                context_store=context_store,
                checkpoint_store=PostgresA2ACheckpointStore(engine),
                resume_state_store=resume_store,
                control_plane=registry,
                hosted_http_client=http,
                event_outbox=outbox,
                invocation_checkpoint_store=invocation_store,
                inbound_enabled=False,
                outbound_enabled=True,
                managed_resource_closers=(http.aclose,),
            )
            await bootstrap.start()
            caller_adapter.configure_managed_a2a(bootstrap)
            runtime = await bootstrap_runtime_from_env(adapter_provider=lambda: caller_adapter)
            assert runtime is not None
            assert runtime.config.session_service.dsn == databases["user"]
            assert runtime.config.dsn == databases["user"]
            sessions = runtime.config.session_service
            await sessions.create_session(agent_id=agent, user_id="local-user", session_id=session)

            async def submit(kind, payload, key):
                trusted = trusted_context(
                    source_kind="studio",
                    source_ref="selected-db-integration",
                    tenant_id=tenant,
                    agent_instance_id=agent,
                    session_id=session,
                    operations=(kind,),
                )
                if kind == "enqueue":
                    command = map_run_request(
                        session_id=session,
                        idempotency_key=key,
                        trusted=trusted,
                        content="query local resource",
                        runtime_options={"tool_approval_mode": "ask"},
                    )
                else:
                    from ksadk.kernel.contracts import AgentControlCommand

                    command = AgentControlCommand(
                        command_id=uuid4(),
                        idempotency_key=key,
                        tenant_id=tenant,
                        agent_instance_id=agent,
                        session_id=session,
                        command_type=kind,
                        payload=payload,
                        source=trusted.source,
                        authorization_ref=trusted.permit.permit_id,
                        submitted_at=trusted.received_at,
                    )
                receipt = await runtime.kernel.submit(command, permit=trusted.permit)
                assert receipt.status == "accepted", receipt
                return receipt

            original_run = ""

            async def pending(provider):
                records = await runtime.config.store.list_pending_interactions(tenant, session)
                if not records and original_run:
                    run = await runtime.config.store.load_run(original_run)
                    assert run.state not in {
                        RunState.COMPLETED,
                        RunState.CANCELLED,
                        RunState.FAILED,
                    }, (
                        "Expected interaction",
                        provider,
                        run.state,
                    )
                return next((item for item in records if item.provider_id == provider), None)

            async def remote_calls():
                result = await http.get(url + "/fixture/status")
                result.raise_for_status()
                calls = result.json()["calls"]
                if not calls and original_run and not cancel_before_send:
                    run = await runtime.config.store.load_run(original_run)
                    assert run.state not in {
                        RunState.COMPLETED,
                        RunState.CANCELLED,
                        RunState.FAILED,
                    }, (
                        "Remote did not start",
                        run.state,
                    )
                return calls

            await submit("enqueue", {}, "enqueue-" + identity)
            if not cancel_before_send:
                form = await _until(lambda: pending("langgraph"))
                original_run = form.run_id
                checkpoint = await native_saver.aget_tuple(
                    {
                        "configurable": {
                            "thread_id": form.native_target["thread_id"],
                            "checkpoint_ns": form.native_target.get("checkpoint_ns", ""),
                            "checkpoint_id": form.native_target["checkpoint_id"],
                        }
                    }
                )
                assert checkpoint is not None
                assert checkpoint.checkpoint["channel_values"]["starts"] == 1
                await submit(
                    "submit_interaction",
                    {
                        "run_id": original_run,
                        "interaction_id": form.interaction_id,
                        "token_ref": "local-test",
                        "action": "submit",
                        "response": {"resource": "resource-" + identity},
                        "expected_revision": form.revision,
                    },
                    "form-" + identity,
                )
            approval = await _until(lambda: pending("managed-a2a"))
            if cancel_before_send:
                original_run = approval.run_id
            assert approval.run_id == original_run
            assert await remote_calls() == []
            if cancel_before_send:
                await submit("interrupt", {"run_id": original_run}, "cancel-" + identity)
            else:
                await submit(
                    "submit_interaction",
                    {
                        "run_id": original_run,
                        "interaction_id": approval.interaction_id,
                        "token_ref": "local-test",
                        "action": "approve",
                        "response": {},
                        "expected_revision": approval.revision,
                    },
                    "approve-" + identity,
                )
                await _until(remote_calls)

            async def terminal():
                run = await runtime.config.store.load_run(original_run)
                return (
                    run
                    if run.state in {RunState.COMPLETED, RunState.CANCELLED, RunState.FAILED}
                    else None
                )

            finished = await _until(terminal)
            expected = RunState.CANCELLED if cancel_before_send else RunState.COMPLETED
            assert finished.state is expected
            assert caller_calls == ["before"]
            assert not await runtime.config.store.list_pending_interactions(tenant, session)

            async def settled_interactions():
                expected_status = "cancelled" if cancel_before_send else "resolved"
                records = [approval] if cancel_before_send else [form, approval]
                saved_records = [
                    await runtime.config.store.get(record.interaction_id) for record in records
                ]
                return all(record.status == expected_status for record in saved_records)

            await _until(settled_interactions)
            events = await runtime.config.session_events.read(session, 0, 1000)
            assert len({event.event_id for event in events}) == len(events)
            reopened_sessions = PostgresSessionService(
                dsn=databases["user"],
                namespace=namespace,
                tenant_id=tenant,
            )
            platform = PostgresSessionService(dsn=databases["platform"], namespace=namespace)
            try:
                assert await reopened_sessions.get_session_metadata(session) is not None
                assert await platform.get_session_metadata(session) is None
            finally:
                await reopened_sessions.aclose()
                await platform.aclose()

            async def saved_invocations():
                async with engine.connect() as connection:
                    assert (
                        await connection.scalar(text("SELECT current_database()")) == "kernel_a2a"
                    )
                    return list(
                        (
                            await connection.execute(
                                text(
                                    "SELECT checkpoint_json FROM ksadk_a2a_invocation_checkpoints "
                                    "WHERE checkpoint_json LIKE :run"
                                ),
                                {"run": "%" + original_run + "%"},
                            )
                        ).scalars()
                    )

            checkpoints = await saved_invocations()
            assert len(checkpoints) == 1
            saved = A2AInvocationCheckpoint.model_validate_json(checkpoints[0])
            reopened = PostgresA2AInvocationCheckpointStore(
                engine,
                account_id=tenant,
                tenant_id=tenant,
                runtime_id=agent,
            )
            saved_checkpoint = await reopened.read(saved.handle.scope_id)
            assert saved_checkpoint is not None
            if cancel_before_send:
                assert await remote_calls() == []
                assert registry.prepares == 0 and registry.binding is None
                assert wire_methods == []
                assert not saved_checkpoint.send_entered
                assert saved_checkpoint.handle.platform_task_id is None
                assert not await outbox.pending()
                record = await runtime.config.store.get(approval.interaction_id)
                assert record.status == "cancelled"
                assert sum(event.event_type == "run.canceled" for event in events) == 1
                assert not any(event.event_type == "run.failed" for event in events)
                return

            assert len(await remote_calls()) == 1
            assert registry.binding is not None
            owner_context = ServerCallContext()
            owner_context.state["a2a_identity"] = A2AIngressIdentity(
                account_id=tenant,
                tenant_id=tenant,
                caller_principal_type="runtime",
                caller_principal_id=agent,
                caller_runtime_id=agent,
                target_agent_id="ar-callee-" + identity,
                target_runtime_id="ar-callee-" + identity,
                target_a2a_agent_id="a2a-agent-" + identity,
                authn_mode="workload_permit",
            )
            expected_task = TaskState.TASK_STATE_COMPLETED

            async def remote_terminal():
                task = await build_a2a_task_store(engine=engine).get(
                    registry.binding.remote_task_id,
                    owner_context,
                )
                if task.status.state == expected_task:
                    return task
                assert task.status.state in {
                    TaskState.TASK_STATE_SUBMITTED,
                    TaskState.TASK_STATE_WORKING,
                }, ("Unexpected remote terminal", task.status.state, registry.operations)
                return None

            try:
                stored_task = await _until(remote_terminal, timeout=8)
            except TimeoutError:
                pytest.fail(
                    "Remote Task did not reach the confirmed terminal; "
                    f"operations={registry.operations}, wire_methods={wire_methods}"
                )
            assert stored_task.status.state == expected_task

            queued = await OwnedPostgresA2ATaskEventOutbox(
                engine,
                account_id=tenant,
                tenant_id=tenant,
                runtime_id=agent,
            ).pending()
            assert any(original_run in json.dumps(item.events) for item in queued)

            if not cancel_before_send:
                replay = bootstrap.invocation_service_for_space(
                    "a2a-space-" + identity,
                    event_store=RuntimeEventStore(sessions),
                    policy=A2AInvocationPolicy(
                        evaluator=lambda *_: A2AInvocationPolicyDecision("allow")
                    ),
                )
                request = saved_checkpoint.request
                before = len(await remote_calls())
                events = [
                    event
                    async for event in replay.invoke(
                        request,
                        trigger_ref=ScopeTriggerRef(
                            scope_id=request.handle.parent_scope_id,
                            item_id=saved_checkpoint.state["descriptor"]["trigger_ref"]["item_id"],
                            call_id=request.handle.call_id,
                        ),
                    )
                ]
                assert events
                assert len(await remote_calls()) == before
            registry.deliver.set()
            await bootstrap.event_dispatcher.drain(raise_on_error=True)
            assert not await outbox.pending()
            assert registry.received
            events = await runtime.config.session_events.read(session, 0, 1000)
            assert any(event.event_type == "interaction.resolved" for event in events)
    finally:
        if registry is not None:
            registry.deliver.set()
        if runtime is not None:
            await runtime.close()
        clear_agent_kernel_runtime()
        clear_agent_kernel()
        if bootstrap is not None:
            await bootstrap.stop()
        await caller_runner.close()
        await native_pool.close()
        await engine.dispose()
