"""Controlled Gateway -> mTLS A2A protocol -> Server -> HTTP Kernel integration."""

import asyncio
import json
import os
import socket
import ssl
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from a2a.types import SendMessageResponse, StreamResponse, Task, TaskState
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.context_store import PostgresA2AContextStore, SQLiteA2AContextStore
from ksadk.a2a.control_plane import InternalA2AControlPlaneClient
from ksadk.a2a.identity import A2AGatewayIdentityMiddleware, A2ATrustedIdentityResolver
from ksadk.a2a.kernel_task_adapter import ManagedKernelA2ATaskAdapter
from ksadk.a2a.platform import JWTGatewayIdentityVerifier
from ksadk.a2a.product_kernel_facts import ProductKernelFactSink
from ksadk.a2a.resume_store import PostgresA2AResumeStateStore, SQLiteA2AResumeStateStore
from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes
from ksadk.a2a.task_event_dispatcher import A2ATaskEventDispatcher
from ksadk.a2a.task_event_outbox import PostgresA2ATaskEventOutbox, SQLiteA2ATaskEventOutbox
from ksadk.a2a.task_store import A2AOwnerContextBuilder
from ksadk.kernel import bootstrap, ingress
from ksadk.kernel.worker import AgentKernelWorker
from tests.kernel.control_harness import AGENT


async def execute_protocol_kernel(setup, kernel, stack, runtime):
    root = Path(setup["test_root"])
    expected_output = "C:proof:confirmed" if setup.get("continue_input") else "finished"
    if setup.get("approval_action"):
        expected_output = "review:" + setup["approval_action"]
    patches = pytest.MonkeyPatch()
    patches.setattr(ingress, "_is_hosted", lambda: True)
    patches.setattr(ingress, "get_agent_kernel", lambda: kernel)
    patches.setattr(bootstrap, "get_agent_kernel_runtime", lambda: SimpleNamespace(
        config=SimpleNamespace(session_service=stack.session_service)))
    if setup.get("protocol_pg_schema"):
        engine = create_async_engine(os.environ["A2A_TEST_POSTGRES_DSN"], connect_args={
            "server_settings": {"search_path": setup["protocol_pg_schema"]}})
        context_store = PostgresA2AContextStore(engine)
        resume_store = PostgresA2AResumeStateStore(engine)
        outbox = PostgresA2ATaskEventOutbox(engine)
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(root / "protocol-tasks.sqlite"))
        context_store = SQLiteA2AContextStore(root / "protocol-contexts.sqlite")
        resume_store = SQLiteA2AResumeStateStore(root / "protocol-resume.sqlite")
        outbox = SQLiteA2ATaskEventOutbox(root / "protocol-outbox.sqlite")
    await context_store.initialize()
    await resume_store.initialize()
    await outbox.initialize()
    listeners, sockets, tasks = [], [], []
    executing = gateway = None
    recovery_ready = asyncio.Event()

    class Tokens:
        def get_token(self, audience):
            assert audience in {"a2a-registry", "a2a-task-sink"}
            return "fixture-callee-token"

    async with httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=setup["ca_file"]), trust_env=False,
    ) as http:
        control = InternalA2AControlPlaneClient(
            setup["origin"], token_provider=Tokens(), httpx_client=http)
        submit_receipts = []
        original_submit = control.submit_inbound_message

        async def checked_submit(**kwargs):
            result = await original_submit(**kwargs)
            submit_receipts.append((
                result.receipt.status,
                result.receipt.error.code if result.receipt.error else None,
                result.receipt.error.message if result.receipt.error else None,
            ))
            assert result.receipt.status in {"accepted", "duplicate"}, (
                result.receipt.status,
                result.receipt.error.code if result.receipt.error else None,
                result.receipt.error.message if result.receipt.error else None,
            )
            return result

        control.submit_inbound_message = checked_submit
        original_cancel = control.cancel_inbound_task

        async def checked_cancel(**kwargs):
            result = await original_cancel(**kwargs)
            assert result.receipt.status in {"accepted", "duplicate"}, (
                result.receipt.status,
                result.receipt.error.code if result.receipt.error else None,
                result.receipt.error.message if result.receipt.error else None,
            )
            return result

        control.cancel_inbound_task = checked_cancel
        dispatcher = A2ATaskEventDispatcher(outbox, ProductKernelFactSink(control))
        adapter = ManagedKernelA2ATaskAdapter(
            runtime, control_plane=control, session_events=stack.events,
            event_dispatcher=dispatcher, runtime_type="langgraph",
            context_store=context_store, resume_state_store=resume_store)
        app = FastAPI()

        class ReleaseOnSubscription:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                resuming = (b"x-r87-resume", b"1") in scope.get("headers", [])

                async def observe(message):
                    await send(message)
                    if (resuming and message["type"] == "http.response.start"
                            and message["status"] == 200):
                        runtime._runner._test_release_model.set()

                await self.app(scope, receive, observe)

        app.add_middleware(ReleaseOnSubscription)
        app.include_router(ingress.agent_kernel_router())
        app.add_middleware(
            A2AGatewayIdentityMiddleware,
            verifier=JWTGatewayIdentityVerifier(setup["gateway_public"]),
            expected_account_id="account-1", expected_tenant_id="account-1",
            expected_target_agent_id="ar-callee", expected_target_runtime_id="runtime-callee")
        add_a2a_protocol_routes(
            app, A2AConfig(enabled=True, base_url="https://runtime.test", create_table=True),
            task_adapter=adapter, engine=engine,
            context_builder=A2AOwnerContextBuilder(
                identity_resolver=A2ATrustedIdentityResolver(), allow_unverified_identity=False))

        async def listen(tls):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            options = dict(lifespan="off", access_log=False, log_level="error",
                           timeout_graceful_shutdown=1)
            if tls:
                options.update(ssl_keyfile=str(root / "server.key"),
                               ssl_certfile=str(root / "server.crt"),
                               ssl_ca_certs=setup["ca_file"], ssl_cert_reqs=ssl.CERT_REQUIRED)
            server = uvicorn.Server(uvicorn.Config(app, **options))
            server.capture_signals = nullcontext
            task = asyncio.create_task(server.serve(sockets=[sock]))
            listeners.append(server)
            tasks.append(task)
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("protocol listener exited")
                    await asyncio.sleep(0.01)
            return f"{'https' if tls else 'http'}://127.0.0.1:{sock.getsockname()[1]}"

        async def execute():
            async with asyncio.timeout(30):
                worker = AgentKernelWorker(stack.store, adapter_factory=lambda: runtime,
                    session_events=stack.events, session_service=stack.session_service)
                session_id = setup["native"]["session_id"]
                if setup.get("restart_input") or setup.get("public_output_crash"):
                    from ksadk.kernel.store import ActivationLeaseRequest

                    if setup.get("recover_input"):
                        while await stack.store.current_lease(AGENT, session_id) is not None:
                            await asyncio.sleep(0.05)
                    else:
                        while not await stack.store.list_pending(AGENT):
                            await asyncio.sleep(0.01)
                    lease = await stack.store.acquire_activation(ActivationLeaseRequest(
                        agent_instance_id=AGENT, session_id=session_id,
                        activation_id=("input-recovery" if setup.get("recover_input")
                                       else "input-start"),
                        runtime_type="langgraph",
                        lease_ttl_seconds=60 if setup.get("recover_input") else 3,
                    ))
                else:
                    while not await stack.store.list_pending(AGENT):
                        await asyncio.sleep(0.01)
                    lease = await stack.lease(session_id)
                if setup.get("recover_input"):
                    from ksadk.kernel.recovery import RecoveryCoordinator

                    previous = await stack.store.find_active_run(AGENT, session_id)
                    assert previous is not None and previous.state.value == "waiting"
                    recovery = RecoveryCoordinator(
                        stack.store, stack.events, runtime.capabilities,
                        adapter_factory=lambda: runtime, session_service=stack.session_service,
                        execution_sink=worker.adopt_execution,
                        recovery_stream_sink=worker.recover_stream,
                    )
                    report = await recovery.recover(AGENT, lease, session_id=session_id)
                    assert report.outcome in {"attached", "resumed"}, report
                    assert report.run_id == previous.run_id
                    assert lease.fencing_token > previous.activation_fence
                    outcome = SimpleNamespace(run_id=previous.run_id)
                    recovery_ready.set()
                else:
                    outcome = await worker.run_once(AGENT, lease)
                    assert outcome.run_id is not None
                terminal = "cancelled" if setup.get("cancel") else "completed"
                while (await stack.store.load_run(outcome.run_id)).state.value != terminal:
                    if setup.get("cancel") or setup.get("continue_input"):
                        control_result = await worker.run_once(AGENT, lease)
                        assert control_result.error is None, control_result.error
                    await asyncio.sleep(0.01)

        try:
            kernel_origin = await listen(False)
            runtime_origin = await listen(True)
            if setup.get("query_only"):
                repo = Path(setup["gateway_repo"])
                queries = [("GetTask", {"key_id": "rotated-after-restart"}, 200),
                           ("GetTask", {"sub_account_id": "other-sub"}, 404)]
                if setup.get("public_output_crash"):
                    queries = [("SubscribeToTask", {"sub_account_id": "other-sub"}, 404),
                               ("SubscribeToTask", {"key_id": "rotated-after-restart"}, 200),
                               ] + queries
                for method, principal, expected_status in queries:
                    query_config = dict(
                        setup["gateway_config"], runtime_origin=runtime_origin,
                        client_cert=str(root / "client.crt"), client_key=str(root / "client.key"),
                        principal_changes=principal, expected_status=expected_status,
                        expect_terminal_subscription=method == "SubscribeToTask",
                        wire={"jsonrpc": "2.0", "id": 4, "method": method,
                              "params": {"id": setup["native"]["remote_task_id"]}},
                    )
                    gateway = await asyncio.create_subprocess_exec(
                        str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                        cwd=repo, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    output, errors = await asyncio.wait_for(
                        gateway.communicate(json.dumps(query_config).encode()), 15)
                    assert gateway.returncode == 0, errors.decode()[-3000:]
                    queried = json.loads(output)
                    if expected_status == 404:
                        assert queried == {"Status": 404, "Forwarded": 0}
                    else:
                        assert queried["A2ATaskId"] == setup["native"]["task_id"]
                        if method == "SubscribeToTask":
                            assert queried["ProtocolEvents"] == [{"error": "terminal_subscription"}]
                            continue
                        task = ParseDict(queried["ProtocolResult"]["result"], Task())
                        assert task.id == setup["native"]["remote_task_id"]
                        assert task.context_id == setup["native"]["remote_context_id"]
                        expected_state = (TaskState.TASK_STATE_CANCELED if setup.get("cancel")
                                          else TaskState.TASK_STATE_COMPLETED)
                        if setup.get("public_output_crash"):
                            expected_state = TaskState.TASK_STATE_FAILED
                        assert task.status.state == expected_state, task.status
                        if setup.get("public_output_crash"):
                            expected_output = "partial-before-crash"
                        if not setup.get("cancel"):
                            assert any(part.text == expected_output for artifact in task.artifacts
                                       for part in artifact.parts)
                assert await dispatcher._outbox.pending()
                return await stack.events.read(setup["native"]["session_id"], 0, 100)
            configured = await http.post(
                setup["origin"] + "/test/dispatch-native",
                json={
                    "endpoint": kernel_origin,
                    "configure_only": True,
                    "capabilities": runtime.capabilities().model_dump(mode="json"),
                },
            )
            assert configured.status_code == 200, configured.text
            executing = asyncio.create_task(execute())
            if setup.get("recover_input"):
                ready = asyncio.create_task(recovery_ready.wait())
                done, _ = await asyncio.wait(
                    {ready, executing}, return_when=asyncio.FIRST_COMPLETED)
                if executing in done:
                    ready.cancel()
                    await executing
                await ready

            repo = Path(setup["gateway_repo"])
            gateway = await asyncio.create_subprocess_exec(
                str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                cwd=repo, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            config = dict(setup["gateway_config"], runtime_origin=runtime_origin,
                          streaming=setup.get("streaming", False),
                          disconnect_after_task=bool(setup.get("reconnect") or setup.get("cancel")),
                          client_cert=str(root / "client.crt"), client_key=str(root / "client.key"))
            if setup.get("public_output_crash"):
                config["output_observed_file"] = str(root / "public-output-observed.json")
            if setup.get("recover_input"):
                config["wire"] = {"jsonrpc": "2.0", "id": 8, "method": "GetTask",
                                  "params": {"id": setup["native"]["remote_task_id"]}}
            if setup.get("public_output_crash"):
                import sys
                communication = asyncio.create_task(
                    gateway.communicate(json.dumps(config).encode()))
                observed = Path(config["output_observed_file"])
                deadline = asyncio.get_running_loop().time() + 12
                while not observed.exists():
                    if asyncio.get_running_loop().time() >= deadline:
                        facts = await stack.events.read(setup["native"]["session_id"], 0, 100)
                        raise AssertionError({"committed_types": [e.event_type for e in facts],
                            "partial_committed": any(
                                "partial-before-crash" in json.dumps(e.payload) for e in facts),
                            "partial_updates": [(e.payload.get("op"),
                                e.payload.get("update", {}).get("text")) for e in facts
                                if e.event_type == "item.updated"
                                and e.payload.get("item_kind") == "message"],
                            "model_entered": runtime._runner._test_entered_model.is_set()})
                    if executing.done():
                        await executing
                    if communication.done():
                        output, errors = await communication
                        raise AssertionError(
                            "public stream ended before output: " + errors.decode()[-2000:])
                    await asyncio.sleep(0.01)
                assert not communication.done(), "stream must still be active at kill boundary"
                events = json.loads(observed.read_text())
                assert any("partial-before-crash" in json.dumps(event) for event in events)
                facts = await stack.events.read(setup["native"]["session_id"], 0, 100)
                assert any("partial-before-crash" in json.dumps(event.payload) for event in facts)
                active = await stack.store.find_active_run(AGENT, setup["native"]["session_id"])
                assert active is not None and active.state.value == "running"
                print(json.dumps({"event_ids": [str(event.event_id) for event in facts]}),
                      flush=True, file=sys.stdout)
                await asyncio.Event().wait()  # Parent kills the still-running process group.
            try:
                output, errors = await asyncio.wait_for(
                    gateway.communicate(json.dumps(config).encode()), 20
                )
            except TimeoutError:
                if executing.done():
                    await executing
                raise
            assert gateway.returncode == 0, errors.decode()[-3000:]
            result = json.loads(output)
            assert result["A2ATaskId"] == setup["native"]["task_id"]
            assert "error" not in result["ProtocolResult"], result["ProtocolResult"]
            if setup.get("recover_input"):
                result["ProtocolResult"]["result"] = {"task": result["ProtocolResult"]["result"]}
            if setup.get("continue_input"):
                waiting = ParseDict(result["ProtocolResult"]["result"], SendMessageResponse())
                assert waiting.task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
                if setup.get("protocol_pg_schema"):
                    pending = await stack.store.list_pending_interactions(
                        "account-1", setup["native"]["session_id"])
                    assert len(pending) == 1
                    assert pending[0].provider_id == "langgraph"
                    assert pending[0].kind == ("approval" if setup.get("approval_action")
                                               else "structured_input")

                    native_config = {"configurable": dict(pending[0].native_target)}
                    snapshot = await runtime._runner._agent.aget_state(
                        native_config, subgraphs=bool(setup.get("nested_input")))
                    if setup.get("nested_input"):
                        # LangGraph resumes the parent checkpoint, whose task owns
                        # the interrupted child checkpoint in a nonempty namespace.
                        assert pending[0].native_target.get("checkpoint_ns", "") == ""
                        child = next(task for task in snapshot.tasks if task.name == "child")
                        assert child.state.next
                        assert child.state.config["configurable"]["checkpoint_ns"]
                    assert snapshot.next
                    original = await runtime._runner._agent.checkpointer.aget_tuple(snapshot.config)
                    restored = await runtime._runner._test_reload_checkpoint(snapshot.config)
                    assert original is not None and restored is not None
                    assert restored.checkpoint["id"] == original.checkpoint["id"]
                    assert (restored.checkpoint["channel_values"]
                            == original.checkpoint["channel_values"])
                    assert restored.pending_writes == original.pending_writes
                if setup.get("approval_action"):
                    assert not (root / "approved-operation.log").exists()
                if setup.get("restart_input") and not setup.get("recover_input"):
                    facts = await stack.events.read(setup["native"]["session_id"], 0, 100)
                    print(json.dumps({"event_ids": [str(e.event_id) for e in facts]}), flush=True)
                    await asyncio.Event().wait()  # Parent kills this live waiting process.
                answer = ({"value": setup["approval_action"]} if setup.get("approval_action")
                          else {"answer": "confirmed"})
                continuation_config = dict(
                    config, principal_changes={"key_id": "rotated-input-key"},
                    wire={"jsonrpc": "2.0", "id": 7, "method": "SendMessage",
                          "params": {"message": {"messageId": "input-answer",
                              "taskId": waiting.task.id, "contextId": waiting.task.context_id,
                              "role": "ROLE_USER", "parts": [{"data": answer}]}}},
                )
                # Reject another subaccount while the interaction is still pending.
                # A terminal-task denial cannot prove that an answer was not consumed.
                before_denial = await stack.events.read(setup["native"]["session_id"], 0, 100)
                denied_input_config = dict(
                    continuation_config, expected_status=404,
                    principal_changes={"sub_account_id": "other-sub"},
                )
                gateway = await asyncio.create_subprocess_exec(
                    str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                    cwd=repo, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                denied_output, denied_errors = await asyncio.wait_for(
                    gateway.communicate(json.dumps(denied_input_config).encode()), 15)
                assert gateway.returncode == 0, denied_errors.decode()[-3000:]
                assert json.loads(denied_output) == {"Status": 404, "Forwarded": 0}
                after_denial = await stack.events.read(setup["native"]["session_id"], 0, 100)
                assert [e.event_id for e in after_denial] == [e.event_id for e in before_denial]
                assert len(submit_receipts) == (0 if setup.get("recover_input") else 1)
                gateway = await asyncio.create_subprocess_exec(
                    str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                    cwd=repo, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                continued_output, continued_errors = await asyncio.wait_for(
                    gateway.communicate(json.dumps(continuation_config).encode()), 20)
                if gateway.returncode != 0 and executing.done():
                    await executing
                if gateway.returncode != 0:
                    current = await stack.store.find_active_run(
                        AGENT, setup["native"]["session_id"])
                    pending_commands = await stack.store.list_pending(AGENT)
                    recent = await stack.events.read(setup["native"]["session_id"], 0, 100)
                    chain = []
                    awaiting = executing.get_coro()
                    while awaiting is not None:
                        code = getattr(awaiting, "cr_code", None)
                        if code is not None:
                            chain.append(code.co_name)
                        awaiting = getattr(awaiting, "cr_await", None)
                    raise AssertionError({
                        "await_chain": chain,
                        "run_state": current.state.value if current else None,
                        "receipts": submit_receipts,
                        "pending_commands": len(pending_commands),
                        "events": [(e.event_type, e.payload.get("state"),
                                    e.payload.get("error_code")) for e in recent[-12:]],
                    })
                assert all(
                    r[0] in {"accepted", "duplicate"} for r in submit_receipts
                ), submit_receipts
                continued = json.loads(continued_output)
                assert continued["A2ATaskId"] == result["A2ATaskId"]
                assert "error" not in continued["ProtocolResult"], continued["ProtocolResult"]
                result = continued
                resumed = ParseDict(result["ProtocolResult"]["result"], SendMessageResponse())
                assert resumed.task.id == waiting.task.id
            if setup.get("reconnect"):
                await asyncio.wait_for(runtime._runner._test_entered_model.wait(), 5)
                first = next(item["result"]["task"] for item in result["ProtocolEvents"]
                             if item.get("result", {}).get("task"))
                reconnect_config = dict(
                    config, disconnect_after_task=False, release_stream=True,
                    principal_changes={"key_id": "rotated-stream-key"},
                    wire={"jsonrpc": "2.0", "id": 5, "method": "SubscribeToTask",
                          "params": {"id": first["id"]}},
                )
                gateway = await asyncio.create_subprocess_exec(
                    str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                    cwd=repo, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                resumed_output, resumed_errors = await asyncio.wait_for(
                    gateway.communicate(json.dumps(reconnect_config).encode()), 20)
                assert gateway.returncode == 0, resumed_errors.decode()[-3000:]
                resumed = json.loads(resumed_output)
                assert resumed["A2ATaskId"] == result["A2ATaskId"]
                result["ProtocolEvents"].extend(resumed["ProtocolEvents"])
            if setup.get("cancel"):
                await asyncio.wait_for(runtime._runner._test_entered_model.wait(), 5)
                first = next(item["result"]["task"] for item in result["ProtocolEvents"]
                             if item.get("result", {}).get("task"))
                cancel_config = dict(
                    config, disconnect_after_task=False,
                    wire={"jsonrpc": "2.0", "id": 6, "method": "CancelTask",
                          "params": {"id": first["id"]}},
                )
                gateway = await asyncio.create_subprocess_exec(
                    str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                    cwd=repo, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                cancel_output, cancel_errors = await asyncio.wait_for(
                    gateway.communicate(json.dumps(cancel_config).encode()), 20)
                assert gateway.returncode == 0, cancel_errors.decode()[-3000:]
                canceled = json.loads(cancel_output)
                assert canceled["A2ATaskId"] == result["A2ATaskId"]
                assert "error" not in canceled["ProtocolResult"], canceled["ProtocolResult"]
                task = ParseDict(canceled["ProtocolResult"]["result"], Task())
                assert task.id == first["id"]
                assert task.status.state == TaskState.TASK_STATE_CANCELED
                public = SimpleNamespace(task=task)
            elif setup.get("streaming"):
                assert result["ProtocolEvents"]
                assert all("error" not in item for item in result["ProtocolEvents"])
                stream = [ParseDict(item["result"], StreamResponse())
                          for item in result["ProtocolEvents"]]
                initial = next(item.task for item in stream if item.HasField("task"))
                statuses = [item.status_update for item in stream if item.HasField("status_update")]
                artifacts = [item.artifact_update for item in stream
                             if item.HasField("artifact_update")]
                assert statuses[-1].status.state == TaskState.TASK_STATE_COMPLETED
                assert all(item.task_id == initial.id for item in statuses + artifacts)
                assert all(item.context_id == "remote-context" for item in statuses + artifacts)
                assert any(part.text == expected_output for update in artifacts
                           for part in update.artifact.parts)
                public = SimpleNamespace(task=initial)
            else:
                public = ParseDict(result["ProtocolResult"]["result"], SendMessageResponse())
                assert public.HasField("task")
                assert public.task.status.state == TaskState.TASK_STATE_COMPLETED
                assert any(part.text == expected_output for artifact in public.task.artifacts
                           for part in artifact.parts)
            assert public.task.context_id == "remote-context"
            await executing
            if setup.get("reconnect") or setup.get("cancel"):
                assert runtime._runner._test_model_calls == 1
            if setup.get("cancel"):
                assert runtime._runner._test_model_cancelled
            # Exercise the public router against the actual signed Server admission.
            # Another subaccount has the same account and user, but no task ownership.
            for method in ("GetTask", "SubscribeToTask", "CancelTask", "SendMessage"):
                params = {"id": public.task.id}
                if method == "SendMessage":
                    params = {"message": {"messageId": "intruder-continuation",
                              "taskId": public.task.id, "contextId": "remote-context",
                              "role": "ROLE_USER", "parts": [{"text": "unauthorized"}]}}
                denied_config = dict(config, principal_changes={"sub_account_id": "other-sub"},
                                     expected_status=404,
                                     wire={"jsonrpc": "2.0", "id": 2,
                                           "method": method, "params": params})
                gateway = await asyncio.create_subprocess_exec(
                    str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                    cwd=repo, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                denied_output, denied_errors = await asyncio.wait_for(
                    gateway.communicate(json.dumps(denied_config).encode()), 15)
                assert gateway.returncode == 0, denied_errors.decode()[-3000:]
                assert json.loads(denied_output) == {"Status": 404, "Forwarded": 0}
            rotated_config = dict(config, principal_changes={"key_id": "rotated-owner-key"},
                                  wire={"jsonrpc": "2.0", "id": 3, "method": "GetTask",
                                        "params": {"id": public.task.id}})
            gateway = await asyncio.create_subprocess_exec(
                str(repo / ".venv/bin/python"), "-m", "tests.a2a_product_prepare_worker",
                cwd=repo, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            rotated_output, rotated_errors = await asyncio.wait_for(
                gateway.communicate(json.dumps(rotated_config).encode()), 15)
            assert gateway.returncode == 0, rotated_errors.decode()[-3000:]
            rotated_result = json.loads(rotated_output)
            assert rotated_result["A2ATaskId"] == setup["native"]["task_id"]
            assert "error" not in rotated_result["ProtocolResult"], rotated_result
            assert rotated_result["ProtocolResult"]["result"]["id"] == public.task.id
            assert await dispatcher._outbox.pending()
            return await stack.events.read(setup["native"]["session_id"], 0, 100)
        finally:
            if gateway is not None and gateway.returncode is None:
                gateway.kill()
                await gateway.wait()
            if executing is not None:
                if not executing.done():
                    executing.cancel()
                await asyncio.gather(executing, return_exceptions=True)
            for server in listeners:
                server.should_exit = True
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
            for sock in sockets:
                sock.close()
            await engine.dispose()
            patches.undo()
