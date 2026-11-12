"""Real Kernel facts -> durable SDK outbox -> separate Server process.

Authority, registration, permit dispatch and model output are test fixtures;
Gateway router runs in-process; Server RuntimeClient uses actual SDK HTTP ingress.
"""

import asyncio
import hashlib
import json
import os
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent

from ksadk.a2a.control_plane import InternalA2AControlPlaneClient
from ksadk.a2a.product_kernel_facts import ProductKernelFactSink, enqueue_product_kernel_fact
from ksadk.a2a.task_event_dispatcher import A2ATaskEventDispatcher
from ksadk.a2a.task_event_outbox import PostgresA2ATaskEventOutbox, SQLiteA2ATaskEventOutbox
from ksadk.kernel.authorization import AgentControlPermitVerifier
from ksadk.kernel.control import AgentKernel
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from tests.kernel.control_harness import AGENT, kernel_stack
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def fact_outbox_factory(request, tmp_path):
    if request.param == "sqlite":
        yield lambda: SQLiteA2ATaskEventOutbox(tmp_path / "outbox.sqlite3")
        return
    dsn = os.environ.get("A2A_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("A2A_TEST_POSTGRES_DSN required for PostgreSQL outbox")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    schema = "r87_fact_outbox_" + uuid4().hex
    admin = create_async_engine(dsn)
    engine = None
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(dsn, connect_args={"server_settings": {"search_path": schema}})

        def factory():
            return PostgresA2ATaskEventOutbox(engine)

        factory.kernel_database = (dsn, schema)
        yield factory
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def durable_kernel_stack(fact_outbox_factory):
    sessions_to_close = []

    async def build(runtime, session_id, *, restore=False):
        stack = await kernel_stack(adapter=runtime, sessions=())
        database = getattr(fact_outbox_factory, "kernel_database", None)
        if database is None:
            return stack
        from urllib.parse import urlencode

        from ksadk.kernel.postgres_store import (
            PostgresAgentKernelStore,
            PostgresFencedSessionEventStore,
            PostgresKernelEventLog,
        )
        from ksadk.sessions.postgres_service import PostgresSessionService

        dsn, schema = database
        plain = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        plain += ("&" if "?" in plain else "?") + urlencode({"search_path": schema})
        sessions = PostgresSessionService(dsn=plain, tenant_id="account-1")
        sessions_to_close.append(sessions)
        if restore:
            assert await sessions.get_session(session_id) is not None
        else:
            assert await sessions.get_session(session_id) is None
        log = PostgresKernelEventLog(sessions._pool, tenant_id="account-1")
        stack.store = PostgresAgentKernelStore(sessions._pool, log, tenant_id="account-1")
        if not restore:
            await stack.store.ensure_schema()
        stack.events = PostgresFencedSessionEventStore(stack.store)
        stack.session_service = sessions
        return stack

    try:
        yield build
    finally:
        for sessions in sessions_to_close:
            await sessions.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_response", [False, True])
@pytest.mark.parametrize(
    "recovery_method",
    ["outbox", "reconciler", "protocol", "protocol_stream", "protocol_reconnect",
     "protocol_cancel", "protocol_continue", "protocol_continue_restart",
     "protocol_subgraph_restart", "protocol_approve_restart",
     "protocol_reject_restart", "interrupted", "interrupted_output", "protocol_output_crash"],
)
async def test_native_kernel_facts_converge_real_server_task(
    tmp_path, monkeypatch, lose_response, fact_outbox_factory, durable_kernel_stack, recovery_method
):
    if (recovery_method in {"interrupted", "interrupted_output", "protocol_output_crash",
            "protocol_continue_restart",
            "protocol_subgraph_restart",
            "protocol_approve_restart", "protocol_reject_restart"}
            and not hasattr(fact_outbox_factory, "kernel_database")):
        pytest.skip("execution process kill requires PostgreSQL Kernel storage")
    configured = os.environ.get("A2A_TEST_SERVER_REPO")
    gateway_repo = os.environ.get("A2A_TEST_GATEWAY_REPO")
    if not configured or not gateway_repo:
        pytest.skip("A2A_TEST_SERVER_REPO and A2A_TEST_GATEWAY_REPO required")
    # Workers exchange JSON over stdout. Keep their service logs separately.
    monkeypatch.setenv("XLOG_LOG_FILE", str(tmp_path / "services.log"))
    repo = Path(configured).resolve()
    process = await asyncio.create_subprocess_exec(
        str(repo / ".venv/bin/python"),
        "-m",
        "tests.a2a_product_admission_worker",
        str(tmp_path),
        cwd=repo,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def exchange(payload):
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        assert line, "Server bridge exited before reply"
        return json.loads(line)

    network_http = None
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        from tests.a2a.test_local_managed_network import _trust

        _trust(tmp_path)
        setup = await exchange(
            {
                "public_key": public,
                "native_control_fixture": False,
                "https": {
                    "keyfile": str(tmp_path / "server.key"),
                    "certfile": str(tmp_path / "server.crt"),
                },
            }
        )
        network_http = httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=str(tmp_path / "ca.pem")), trust_env=False
        )

        gateway = await asyncio.create_subprocess_exec(
            str(Path(gateway_repo) / ".venv/bin/python"),
            "-m",
            "tests.a2a_product_prepare_worker",
            cwd=gateway_repo,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        config = {
            **setup,
            "registry_postgres": hasattr(fact_outbox_factory, "kernel_database"),
            "ca_file": str(tmp_path / "ca.pem"),
            "private_key": key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
        }
        if recovery_method in {
            "protocol_continue", "protocol_continue_restart", "protocol_subgraph_restart",
            "protocol_approve_restart", "protocol_reject_restart"
        }:
            config["wire"] = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
                "params": {"message": {"messageId": "message-1",
                    "contextId": "remote-context", "role": "ROLE_USER",
                    "parts": [{"text": json.dumps({"path": [], "text": "proof",
                                                   "request_input": True})}]}}}
        try:
            output, errors = await asyncio.wait_for(
                gateway.communicate(json.dumps(config).encode()), 15
            )
        except TimeoutError:
            gateway.kill()
            await gateway.communicate()
            raise
        assert gateway.returncode == 0, errors.decode()[-3000:]
        admitted = json.loads(output)
        native = await exchange({"prepare_native": admitted["A2ATaskId"],
                                 "message_sha256": admitted["MessageSha256"],
                                 "message": admitted["Message"]})
        setup["native"] = native
        setup["ca_file"] = str(tmp_path / "ca.pem")
        setup["protocol"] = recovery_method.startswith("protocol")
        setup["streaming"] = setup["protocol"] and recovery_method not in {
            "protocol", "protocol_continue", "protocol_continue_restart",
            "protocol_subgraph_restart",
            "protocol_approve_restart", "protocol_reject_restart"
        }
        setup["reconnect"] = recovery_method == "protocol_reconnect"
        setup["cancel"] = recovery_method == "protocol_cancel"
        setup["continue_input"] = recovery_method in {
            "protocol_continue", "protocol_continue_restart", "protocol_subgraph_restart",
            "protocol_approve_restart", "protocol_reject_restart"
        }
        setup["restart_input"] = recovery_method in {
            "protocol_continue_restart", "protocol_subgraph_restart",
            "protocol_approve_restart", "protocol_reject_restart"
        }
        setup["approval_action"] = ({"protocol_approve_restart": "approve",
                                     "protocol_reject_restart": "reject"}.get(recovery_method))
        setup["nested_input"] = recovery_method == "protocol_subgraph_restart"
        setup["crash_during_execution"] = recovery_method in {
            "interrupted", "interrupted_output", "protocol_output_crash"}
        setup["crash_after_output"] = recovery_method in {
            "interrupted_output", "protocol_output_crash"}
        setup["public_output_crash"] = recovery_method == "protocol_output_crash"
        config["official_client_python"] = sys.executable
        setup["gateway_config"] = config
        setup["gateway_repo"] = gateway_repo
        setup["gateway_public"] = public
        setup["test_root"] = str(tmp_path)

        database = getattr(fact_outbox_factory, "kernel_database", None)
        if database is None:
            facts = await execute_native_kernel(setup, durable_kernel_stack)
            expected_ids = [str(event.event_id) for event in facts]
        else:
            executor = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.a2a.product_kernel_execution_worker",
                database[1],
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                executor.stdin.write((json.dumps(setup) + "\n").encode())
                await executor.stdin.drain()
                line = await asyncio.wait_for(executor.stdout.readline(), 30)
                if not line:
                    await executor.wait()
                    diagnostic = (await executor.stderr.read()).decode()[-5000:]
                    dsn = os.environ.get("A2A_TEST_POSTGRES_DSN", "")
                    if dsn:
                        diagnostic = diagnostic.replace(dsn, "<redacted-dsn>").replace(
                            dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
                            "<redacted-dsn>",
                        )
                    pytest.fail(
                        "Kernel execution process exited before committing facts: " + diagnostic
                    )
                expected_ids = json.loads(line)["event_ids"]
                if setup.get("public_output_crash"):
                    import signal
                    os.killpg(executor.pid, signal.SIGKILL)
                else:
                    executor.kill()
                await asyncio.wait_for(executor.wait(), 5)
                assert executor.returncode < 0
            finally:
                if setup.get("public_output_crash"):
                    import signal
                    try:
                        os.killpg(executor.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if executor.returncode is None:
                    executor.kill()
                    await executor.wait()
        bound = await exchange({"inspect": True})
        native["remote_task_id"] = bound["native_remote_task"]
        native["remote_context_id"] = bound["native_remote_context"]
        if setup.get("restart_input"):
            recovering = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.a2a.product_kernel_execution_worker", database[1],
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            recovery_errors = asyncio.create_task(recovering.stderr.read())
            try:
                recovery_setup = json.dumps({**setup, "recover_input": True}) + "\n"
                recovering.stdin.write(recovery_setup.encode())
                await recovering.stdin.drain()
                recovered_line = await asyncio.wait_for(recovering.stdout.readline(), 45)
                if not recovered_line:
                    await recovering.wait()
                    diagnostic = (await recovery_errors).decode()[-6000:]
                    diagnostic = diagnostic.replace(os.environ["A2A_TEST_POSTGRES_DSN"], "<dsn>")
                    pytest.fail("waiting-input recovery failed: " + diagnostic)
                recovered_ids = json.loads(recovered_line)["event_ids"]
                assert recovered_ids[:len(expected_ids)] == expected_ids
                expected_ids = recovered_ids
                recovering.kill()
                await recovering.wait()
            finally:
                if recovering.returncode is None:
                    recovering.kill()
                    await recovering.wait()
                await recovery_errors
        database = getattr(fact_outbox_factory, "kernel_database", None)
        if database is not None and setup["protocol"] and not setup.get("public_output_crash"):
            query_process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.a2a.product_kernel_execution_worker", database[1],
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                query_output, query_errors = await asyncio.wait_for(
                    query_process.communicate(
                        json.dumps({**setup, "query_only": True}).encode()), 20)
                assert query_process.returncode == 0, "restarted protocol query process failed"
                assert json.loads(query_output)["event_ids"] == expected_ids
            finally:
                if query_process.returncode is None:
                    query_process.kill()
                    await query_process.wait()
        if database is not None:
            from ksadk.events.session_event import SessionEventEnvelope

            reader = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.a2a.product_kernel_fact_reader",
                database[1],
                native["session_id"],
                *(["--recover"] if setup["crash_during_execution"] else []),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                output, errors = await asyncio.wait_for(reader.communicate(), 10)
            except TimeoutError:
                reader.kill()
                await reader.communicate()
                raise
            assert reader.returncode == 0, "independent Kernel fact reader failed"
            restored = [SessionEventEnvelope.model_validate(item) for item in json.loads(output)]
            restored_ids = [str(event.event_id) for event in restored]
            if recovery_method in {"interrupted", "interrupted_output", "protocol_output_crash"}:
                assert restored_ids[:len(expected_ids)] == expected_ids
                assert any(event.payload.get("state") == "interrupted" for event in restored)
                if setup.get("crash_after_output"):
                    partial = [event for event in restored
                               if "partial-before-crash" in json.dumps(event.payload)]
                    assert partial
                    assert all(str(event.event_id) in expected_ids for event in partial)
                    assert not any("must not execute again" in json.dumps(event.payload)
                                   for event in restored)
            else:
                assert restored_ids == expected_ids
            facts = restored
        replies = []
        response_lost = False

        async def transport(request):
            nonlocal response_lost
            response = await network_http.post(
                str(request.url), headers=dict(request.headers), content=request.content
            )
            result = await exchange({"inspect": True})
            result.update(status=response.status_code, body=response.json())
            replies.append(result)
            if (
                lose_response
                and not response_lost
                and request.url.path.endswith("/AppendA2AProductKernelFacts")
            ):
                response_lost = True
                assert result["status"] == 200
                raise httpx.ReadTimeout("test response lost after Server commit", request=request)
            return httpx.Response(result["status"], json=result["body"])

        class Tokens:
            def get_token(self, audience):
                assert audience == "a2a-task-sink"
                return "fixture-callee-token"

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client = InternalA2AControlPlaneClient(
                setup["origin"], token_provider=Tokens(), httpx_client=http
            )
            if recovery_method in {"reconciler", "interrupted", "interrupted_output",
                                   "protocol_output_crash"}:
                from ksadk.a2a.control_plane import A2AControlPlaneError
                from ksadk.a2a.product_kernel_facts import ProductKernelReconciler

                class CommittedEvents:
                    async def subscribe(self, session_id, after_seq):
                        assert session_id == native["session_id"]
                        for fact in facts:
                            if fact.seq > after_seq:
                                yield fact

                event_source = CommittedEvents()
                if database is not None:
                    reopened = await durable_kernel_stack(None, native["session_id"], restore=True)
                    event_source = reopened.events
                reconciler = ProductKernelReconciler(client, event_source)
                if lose_response:
                    with pytest.raises(A2AControlPlaneError):
                        await reconciler.reconcile_once()
                    reconciler = ProductKernelReconciler(client, event_source)
                await reconciler.reconcile_once()
                await reconciler.reconcile_once()
                assert replies[-1]["body"]["Data"]["Items"] == []
                terminal = "interrupted" if setup["crash_during_execution"] else "completed"
                expected_status = "failed" if terminal == "interrupted" else "completed"
                assert replies[-1]["native_status"] == expected_status
                expected = [
                    fact
                    for fact in facts
                    if fact.event_type == "control.run_transition"
                    and fact.payload.get("state") in {"running", terminal}
                ]
                assert {fact.payload["state"] for fact in expected} == {"running", terminal}
                assert replies[-1]["native_ref_count"] == len(expected)
                assert all(reply["task_count"] == 1 for reply in replies)
                if setup.get("public_output_crash"):
                    query_process = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "tests.a2a.product_kernel_execution_worker",
                        database[1],
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        query_output, query_errors = await asyncio.wait_for(
                            query_process.communicate(
                                json.dumps({**setup, "query_only": True}).encode()), 20)
                        diagnostic = query_errors.decode()[-4000:].replace(
                            os.environ["A2A_TEST_POSTGRES_DSN"], "<redacted-dsn>")
                        assert query_process.returncode == 0, diagnostic
                        assert json.loads(query_output)["event_ids"] == restored_ids
                    finally:
                        if query_process.returncode is None:
                            query_process.kill()
                            await query_process.wait()
                return
            outbox = fact_outbox_factory()
            dispatcher = A2ATaskEventDispatcher(outbox, ProductKernelFactSink(client))
            handle = SimpleNamespace(
                native_ref={
                    "product_kernel_binding": {
                        "platform_task_id": native["task_id"],
                        "target_version_id": setup["version"],
                        "remote_task_id": native["remote_task_id"],
                        "remote_context_id": native["remote_context_id"],
                    }
                }
            )
            if setup["protocol"] and database is not None:
                # Reopen batches actually queued by the killed protocol process.
                assert await outbox.pending()
            else:
                for fact in facts:
                    await enqueue_product_kernel_fact(dispatcher, handle, fact)
            pending = await outbox.pending()
            assert pending
            original_ids = [batch.batch_id for batch in pending]
            if lose_response:
                assert await dispatcher.drain(raise_on_error=False) == 0
                assert [batch.batch_id for batch in await outbox.pending()] == original_ids
            # Reconstruct the dispatcher before delivery: persisted real facts survive it.
            recovered = A2ATaskEventDispatcher(fact_outbox_factory(), ProductKernelFactSink(client))
            assert await recovered.drain(raise_on_error=True) >= 2
            assert not await recovered.outbox.pending()
            assert replies[-1]["native_status"] == ("canceled" if setup["cancel"] else "completed")
            assert all(r["task_count"] == 1 for r in replies)
            assert replies[-1]["native_ref_count"] == len(original_ids)
            if setup.get("approval_action"):
                audit = tmp_path / "approved-operation.log"
                operations = audit.read_text().splitlines() if audit.exists() else []
                assert operations == (["executed"] if setup["approval_action"] == "approve" else [])
            if lose_response:
                assert replies[0]["body"]["Data"]["Accepted"] == 1
                assert replies[1]["body"]["Data"]["Duplicate"] == 1
    finally:
        if network_http is not None:
            await network_http.aclose()
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
        assert process.returncode == 0, (await process.stderr.read()).decode()[-2000:]


async def execute_native_kernel(setup, build_stack):
    if setup.get("continue_input") and setup.get("protocol_pg_schema"):
        from urllib.parse import urlencode

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        dsn = os.environ["A2A_TEST_POSTGRES_DSN"].replace(
            "postgresql+asyncpg://", "postgresql://", 1)
        dsn += ("&" if "?" in dsn else "?") + urlencode({
            "options": "-csearch_path=" + setup["protocol_pg_schema"],
        })
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()

            async def reload_checkpoint(config):
                async with AsyncPostgresSaver.from_conn_string(dsn) as reader:
                    return await reader.aget_tuple(config)

            return await _execute_native_kernel(
                setup, build_stack, checkpoint_saver=saver, reload_checkpoint=reload_checkpoint)
    return await _execute_native_kernel(setup, build_stack)


async def _execute_native_kernel(
    setup, build_stack, *, checkpoint_saver=None, reload_checkpoint=None,
):
    native = setup["native"]

    class Keys:
        async def fetch_verification_keys(self):
            return {k["kid"]: k["x"] for k in setup["control_jwks"]["keys"]}

    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="agent"), ".")
    entered_model = asyncio.Event()
    release_model = asyncio.Event()
    runner._test_release_model = release_model
    runner._test_entered_model = entered_model
    runner._test_model_calls = 0
    runner._test_model_cancelled = False

    class BlockingModel(SeqToolModel):
        async def _astream(self, *args, **kwargs):
            if setup.get("crash_after_output"):
                from langchain_core.messages import AIMessageChunk
                from langchain_core.outputs import ChatGenerationChunk

                yield ChatGenerationChunk(message=AIMessageChunk(content="partial-before-crash"))
                if setup.get("public_output_crash"):
                    yield ChatGenerationChunk(message=AIMessageChunk(content="second-before-crash"))
            entered_model.set()
            await asyncio.Event().wait()
            if False:  # Keep an async generator; the parent kills this running process.
                yield None

    class ResumableStreamModel(SeqToolModel):
        async def _astream(self, *args, **kwargs):
            runner._test_model_calls += 1
            entered_model.set()
            try:
                await release_model.wait()
            except asyncio.CancelledError:
                runner._test_model_cancelled = True
                raise
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk

    if setup.get("crash_during_execution"):
        model_type = BlockingModel
    elif setup.get("reconnect") or setup.get("cancel"):
        model_type = ResumableStreamModel
    else:
        model_type = SeqToolModel
    runner._agent = create_react_agent(model_type(msgs=[AIMessage(content="finished")]), [])
    if setup.get("continue_input"):
        from langgraph.checkpoint.memory import MemorySaver

        from tests.a2a.test_split_examples import load
        runner._agent = load("c").ksadk_graph_factory(
            checkpointer=checkpoint_saver if checkpoint_saver is not None else MemorySaver())
        if setup.get("nested_input"):
            from langgraph.graph import END, START, MessagesState, StateGraph

            parent = StateGraph(MessagesState)
            parent.add_node("child", load("c").ksadk_graph_factory(checkpointer=None))
            parent.add_edge(START, "child")
            parent.add_edge("child", END)
            runner._agent = parent.compile(checkpointer=checkpoint_saver)
        if setup.get("approval_action"):
            from langgraph.graph import END, START, MessagesState, StateGraph
            from langgraph.types import interrupt

            def review(state):
                decision = interrupt({
                    "kind": "approval",
                    "name": "fixture_operation",
                    "args": {"text": "proof"},
                })
                assert decision in ({"decisions": [{"type": "approve"}]},
                                    {"decisions": [{"type": "reject"}]})
                action = decision["decisions"][0]["type"]
                if action == "approve":
                    # Durable local stand-in for the approved operation. Reopening
                    # protocol/query/outbox state must never execute this twice.
                    audit = Path(setup["test_root"]) / "approved-operation.log"
                    with audit.open("a") as stream:
                        stream.write("executed\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                return {"messages": [AIMessage(content="review:" + action)]}

            graph = StateGraph(MessagesState)
            graph.add_node("review", review)
            graph.add_edge(START, "review")
            graph.add_edge("review", END)
            runner._agent = graph.compile(checkpointer=checkpoint_saver)
        runner._test_reload_checkpoint = reload_checkpoint
        runner._managed_a2a_bootstrap = SimpleNamespace(outbound_enabled=False)
    runtime = LangGraphRuntimeAdapter(runner)
    stack = await build_stack(
        runtime, native["session_id"],
        restore=bool(setup.get("query_only") or setup.get("recover_input")))
    kernel = AgentKernel(
        stack.store,
        stack.events,
        AgentControlPermitVerifier(Keys()),
        capabilities=runtime.capabilities,
    )
    if setup.get("protocol"):
        from ksadk.sessions import bind_session_service
        from tests.a2a.product_protocol_roundtrip import execute_protocol_kernel

        with bind_session_service(stack.session_service):
            return await execute_protocol_kernel(setup, kernel, stack, runtime)
    await submit_native_over_http(setup, kernel, stack.session_service, stack.store)
    from ksadk.sessions.invocation_identity import identity_native_user_id
    session = await stack.session_service.get_session_metadata(native["session_id"])
    assert session.user_id == identity_native_user_id({
        "identity_namespace": "a2a-product-key:" + hashlib.sha256(b"sub-1").hexdigest(),
        "tenant_id": hashlib.sha256(b"account-1").hexdigest(),
        "subject_type": "user", "subject_id": hashlib.sha256(b"user-1").hexdigest(),
    })
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: runtime,
        session_events=stack.events,
        session_service=stack.session_service,
    )
    if setup.get("crash_during_execution"):
        from ksadk.kernel.store import ActivationLeaseRequest
        lease = await stack.store.acquire_activation(ActivationLeaseRequest(
            agent_instance_id=AGENT, session_id=native["session_id"], activation_id="crash-owner",
            runtime_type="langgraph", lease_ttl_seconds=3.0,
        ))
    else:
        lease = await stack.lease(native["session_id"])
    pending = await stack.store.list_pending(AGENT)
    assert {item.session_id for item in pending} == {native["session_id"]}
    try:
        outcome = await worker.run_once(AGENT, lease)
    except Exception as exc:
        current = await stack.store.current_lease(AGENT, native["session_id"])
        raise AssertionError(f"Kernel execution failed: lease={lease!r}, current={current!r}, "
                             f"details={getattr(exc, 'details', None)!r}") from exc
    if setup.get("crash_during_execution"):
        await asyncio.wait_for(entered_model.wait(), 5)
        if setup.get("crash_after_output"):
            async with asyncio.timeout(5):
                while True:
                    committed = await stack.events.read(native["session_id"], 0, 100)
                    partial = [event for event in committed
                               if "partial-before-crash" in json.dumps(event.payload)]
                    if partial:
                        assert all(not event.event_type.startswith("control.") for event in partial)
                        break
                    await asyncio.sleep(0.01)
        assert (await stack.store.load_run(outcome.run_id)).state.value == "running"
        return await stack.events.read(native["session_id"], 0, 100)
    async with asyncio.timeout(5):
        while (await stack.store.load_run(outcome.run_id)).state.value != "completed":
            await asyncio.sleep(0.01)
    facts = await stack.events.read(native["session_id"], 0, 100)
    return facts


async def submit_native_over_http(setup, kernel, session_service, store):
    """Server RuntimeClient -> actual hosted SDK ingress over loopback TCP."""
    import socket
    from contextlib import nullcontext

    import uvicorn
    from fastapi import FastAPI

    from ksadk.kernel import ingress

    patches = pytest.MonkeyPatch()
    patches.setattr(ingress, "_is_hosted", lambda: True)
    patches.setattr(ingress, "get_agent_kernel", lambda: kernel)
    from ksadk.kernel import bootstrap
    patches.setattr(bootstrap, "get_agent_kernel_runtime", lambda: SimpleNamespace(
        config=SimpleNamespace(session_service=session_service)))
    app = FastAPI()
    app.include_router(ingress.agent_kernel_router())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False,
                                         log_level="error"))
    server.capture_signals = nullcontext
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    raise RuntimeError("test Runtime listener exited")
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=setup["ca_file"]), trust_env=False
        ) as control:
            response = await control.post(setup["origin"] + "/test/dispatch-native",
                                          json={"endpoint": endpoint})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "accepted"
            pending = await store.list_pending(AGENT)
            assert len(pending) == 1
            original = pending[0].command
            assert str(original.command_id) == setup["native"]["command_id"]
            assert original.session_id == setup["native"]["session_id"]
            async with httpx.AsyncClient(trust_env=False) as direct:
                denied = await direct.post(endpoint + ingress.KERNEL_INGRESS_SUBMIT_PATH,
                                           json={"command": original.model_dump(mode="json")})
                assert denied.status_code == 401
            repeated = await control.post(setup["origin"] + "/test/dispatch-native",
                                          json={"endpoint": endpoint})
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["status"] == "duplicate"
            assert repeated.json()["command_id"] == response.json()["command_id"]
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        sock.close()
        patches.undo()
