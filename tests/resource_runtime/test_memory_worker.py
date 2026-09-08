import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.leases import ResourceLeaseRegistry
from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.process import ResourceWorkerProcess
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from ksadk.resource_runtime.worker import MemoryWorkerBinding, WorkerInitialization
from tests.resource_runtime.test_worker_process import freeze_initialization, initialization


def memory_initialization(endpoint, user="user-a", activation="activation-a"):
    payload = initialization(endpoint).pipe_payload()
    payload["bindings"][0]["config"]["binding"]["resource"].update(
        kind="memory-instance", id="memory-selected"
    )
    payload["scopes"][0]["allowedOperations"] = ["load_memory"]
    payload["scopes"][0]["identity"]["memorySubjectRef"] = user
    payload["scopes"][0]["activationId"] = activation
    return freeze_initialization(payload)


@pytest.fixture
def memory_upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((dict(self.headers), body))
            response = json.dumps(
                {
                    "Data": [
                        {
                            "Memories": [
                                {
                                    "MemoryId": "server-real-id",
                                    "Memory": "test preference",
                                    "AgentUserId": body["AgentUserId"],
                                }
                            ]
                        }
                    ]
                }
            ).encode()
            if body.get("DataType") == "conversation":
                response = b'{"RequestId": "accepted-test-write"}'
                if body.get("Data", {}).get("Conversation", [{}])[0].get("Content") == [
                    {"Type": "input_text", "Text": "unconfirmed-write"}
                ]:
                    response = b"{}"
            elif "MemoryId" in body:
                response = (
                    b'{"RequestId": "mutation-ack", "Data": {"NewMemoryId": "updated-real-id"}}'
                )
                if body.get("Content") == "unconfirmed-mutation":
                    response = b"{}"
            elif "Page" in body:
                session = next(
                    (
                        request["SessionId"]
                        for _, request in calls
                        if request.get("DataType") == "conversation"
                    ),
                    "missing",
                )
                response = json.dumps(
                    {"Data": {"Items": [{"SessionId": session, "State": 100}], "TotalCount": 1}}
                ).encode()
                if any(
                    request.get("Data", {}).get("Conversation", [{}])[0].get("Content")
                    == [{"Type": "input_text", "Text": "failed preference"}]
                    for _, request in calls
                ):
                    response = json.dumps(
                        {
                            "Data": {
                                "Items": [{"SessionId": session, "State": -100}],
                                "TotalCount": 1,
                            }
                        }
                    ).encode()
            elif body["Query"] == "no-match":
                response = b'{"Data": []}'
            elif body["Query"] == "malformed":
                response = b'{"unexpected": "not an empty result"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_memory_worker_real_process_preserves_ids_and_partitions(
    memory_upstream, monkeypatch
):
    endpoint, calls = memory_upstream
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "fake-unrelated")
    monkeypatch.setenv("KSADK_LTM_NAMESPACE", "wrong-instance")
    registry = ResourceLeaseRegistry()
    broker = ResourceBroker(registry, "generation-a")
    workers = []
    try:
        for user, activation in [("user-a", "activation-a"), ("user-b", "activation-b")]:
            init = memory_initialization(endpoint, user, activation)
            assert isinstance(init.bindings[0], MemoryWorkerBinding)
            assert "fake-selected-secret" not in init.model_dump_json()
            worker = await ResourceWorkerProcess.start(init)
            workers.append(worker)
            lease = registry.issue(init.scopes[0])
            broker.register(lease.scope, worker)
            reply = await broker.dispatch(
                {
                    "v": 1,
                    "requestId": "memory-read",
                    "handle": lease.handle,
                    "operation": "load_memory",
                    "deadline": int((time.time() + 10) * 1000),
                    "arguments": {"query": "preferences"},
                }
            )
            assert reply["result"]["status"] == "ok", reply
            assert reply["result"]["records"][0]["memory_id"] == "server-real-id"
            assert reply["result"]["records"][0]["version"] is None
        assert len(calls) == 2
        assert calls[0][1]["AgentUserId"] != calls[1][1]["AgentUserId"]
        assert all(body["MemoryCollectionId"] == "memory-selected" for _, body in calls)
        assert all("fake-unrelated" not in str(headers) for headers, _ in calls)
    finally:
        for worker in workers:
            await worker.aclose()


def test_memory_worker_rejects_other_service_operations():
    payload = memory_initialization("https://example.test").pipe_payload()
    payload["scopes"][0]["allowedOperations"] = ["search_knowledge_base"]
    with pytest.raises(ValidationError, match="Unsupported worker operation"):
        WorkerInitialization.model_validate(payload)


@pytest.mark.parametrize("extraction_fails", [False, True])
async def test_approved_memory_write_reaches_sdk_once(memory_upstream, tmp_path, extraction_fails):
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.interaction.contracts import InteractionSubmission
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
    from ksadk.kernel.store import ActivationLeaseRequest
    from ksadk.resource_runtime.ipc import ResourceRequest
    from ksadk.resource_runtime.kernel_approval import KernelResourceWriteAuthorizer
    from ksadk.sessions.local_service import LocalSessionService

    endpoint, calls = memory_upstream
    payload = memory_initialization(endpoint).pipe_payload()
    payload["scopes"][0]["allowedOperations"].extend(["save_memory", "memory_status"])
    init = WorkerInitialization.model_validate(payload)
    identity = init.scopes[0].identity
    sessions = LocalSessionService(tmp_path / "kernel.sqlite")
    await sessions.create_session(identity.agent_id, identity.actor_ref, identity.session_ref)
    kernel = SQLiteAgentKernelStore(sessions.db_path, SessionServiceEventStore(sessions))
    await kernel.ensure_schema()
    kernel_lease = await kernel.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id=identity.agent_id,
            session_id=identity.session_ref,
            activation_id="kernel-activation",
        )
    )
    guard = ActivationWriteGuard(
        activation_id=kernel_lease.activation_id,
        fencing_token=kernel_lease.fencing_token,
    )
    authorizer = KernelResourceWriteAuthorizer(
        kernel,
        run_id="run",
        resolve_interaction=lambda request, scope: "memory-approval",
    )
    registry = ResourceLeaseRegistry()
    broker = ResourceBroker(
        registry,
        "generation-a",
        write_authorizer=authorizer,
        operation_ledger=OperationLedger(tmp_path / "ledger"),
    )
    worker = await ResourceWorkerProcess.start(init)
    lease = registry.issue(init.scopes[0])
    broker.register(lease.scope, worker)
    request = {
        "v": 1,
        "requestId": "caller-id",
        "handle": lease.handle,
        "operation": "save_memory",
        "deadline": int((time.time() + 10) * 1000),
        "arguments": {"content": "failed preference" if extraction_fails else "save preference"},
    }
    try:
        await authorizer.request_memory_approval(
            ResourceRequest.model_validate(request),
            init.scopes[0],
            interaction_id="memory-approval",
            event_id="explicit-event",
            guard=guard,
        )
        assert (await broker.dispatch(request))["error"]["code"] == "RESOURCE_APPROVAL_REQUIRED"
        assert calls == []
        await kernel.resolve(
            InteractionSubmission(
                interaction_id="memory-approval",
                expected_revision=1,
                action="approve",
                idempotency_key="user-decision",
            ),
            guard=guard,
        )
        result = await broker.dispatch(request)
        assert result["result"]["status"] == "accepted_pending", result
        assert result["result"]["searchable"] is False
        assert result["operationReceipt"]["state"] == "accepted_pending"
        second = await broker.dispatch({**request, "requestId": "recovered-call"})
        assert second["result"]["status"] == "accepted_pending"
        assert second["result"]["replayed"] is False
        assert len(calls) == 1
        body = calls[0][1]
        assert body["SessionId"] == result["result"]["operationId"]
        assert len(body["SessionId"]) == 64
        assert body["MemoryCollectionId"] == "memory-selected"
        assert body["Flush"] is True
        assert (
            body["Data"]["Conversation"][0]["Content"][0]["Text"] == request["arguments"]["content"]
        )
        status_request = {
            **request,
            "operation": "memory_status",
            "arguments": {
                "operationId": result["result"]["operationId"],
            },
        }
        status = await broker.dispatch(status_request)
        assert status["result"]["extractionStatus"] == (
            "failed" if extraction_fails else "extracted"
        ), status
        assert status["result"]["status"] == ("failed" if extraction_fails else "accepted_pending")
        assert status["result"]["searchable"] is False
        retried = await broker.dispatch(request)
        assert retried["result"]["status"] == status["result"]["status"]
        assert retried["result"]["replayed"] is False
        count = len(calls)
        forged = await broker.dispatch({**status_request, "arguments": {"operationId": "0" * 64}})
        assert forged["error"]["code"] == "RESOURCE_OPERATION_NOT_FOUND"
        assert len(calls) == count
    finally:
        await worker.aclose()
        await kernel.close()


@pytest.mark.parametrize("write", [False, True])
async def test_official_core_memory_tool_reaches_worker(memory_upstream, tmp_path, write):
    modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if not modules:
        pytest.skip("Pinned official DSH test installation is required")
    endpoint, calls = memory_upstream
    init = memory_initialization(endpoint)
    if write:
        payload = init.pipe_payload()
        payload["scopes"][0]["allowedOperations"].extend(["save_memory", "memory_status"])
        init = WorkerInitialization.model_validate(payload)

    class HostApproval:
        async def authorize(self, request, scope):
            return "stable-event" if request.arguments == {"content": "approved"} else None

    supervisor = ResourceSupervisor(
        "generation-a",
        write_authorizer=HostApproval(),
        operation_ledger=OperationLedger(tmp_path / "ledger"),
    )
    node = None
    try:
        active = await supervisor.activate(init)
        lease = active.leases[0]
        socket_path = active.socket_path
        probe_calls = (
            [
                {"handle": lease.handle, "query": query}
                for query in ("preference", "no-match", "malformed")
            ]
            if not write
            else [
                {"handle": lease.handle, "arguments": {"content": content}}
                for content in ("approved", "approved", "not approved")
            ]
        )
        root = Path(__file__).resolve().parents[2]
        bundles = root / "ksadk/plugins/providers/bundles"
        node = await asyncio.create_subprocess_exec(
            "node",
            str(root / "tests/fixtures/resource_runtime/node_core_probe.mjs"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, errors = await asyncio.wait_for(
            node.communicate(
                json.dumps(
                    {
                        "nodeModules": modules,
                        "bridgePlugin": (bundles / "dsh-platform-resources/index.mjs").as_uri(),
                        "businessPlugin": (bundles / "dsh-memory/index.mjs").as_uri(),
                        "toolName": "save_memory" if write else "load_memory",
                        "checkMemoryStatus": write,
                        "missingContextArguments": {"content": "approved"}
                        if write
                        else {"query": "forbidden"},
                        "socketPath": str(socket_path),
                        "calls": probe_calls,
                    }
                ).encode()
            ),
            30,
        )
        assert node.returncode == 0, errors.decode()
        result = json.loads(output)
        assert "load_memory" in result["names"]
        assert result["missingContext"]["isError"] is True
        if write:
            assert "save_memory" in result["names"]
            first, duplicate, denied = result["results"]
            assert first["isError"] is False, first
            assert duplicate["isError"] is False, duplicate
            assert first["value"] == duplicate["value"]
            assert first["value"]["status"] == "accepted_pending"
            assert first["value"]["searchable"] is False
            assert denied["isError"] is True
            assert result["memoryStatus"]["isError"] is False, result["memoryStatus"]
            assert result["memoryStatus"]["value"]["extractionStatus"] == "extracted"
            assert result["memoryStatus"]["value"]["searchable"] is False
            assert len(calls) == 2
            return
        tool_result = result["results"][0]
        assert tool_result["isError"] is False, tool_result
        assert tool_result["value"]["items"] == [
            {
                "memoryId": "server-real-id",
                "content": "test preference",
                "score": None,
                "version": None,
            }
        ]
        assert "scope_id" not in json.dumps(tool_result)
        assert result["results"][1]["value"]["status"] == "empty"
        assert result["results"][2]["value"]["status"] == "failed"
        assert result["results"][2]["value"]["items"] == []
        assert len(calls) == 3
    finally:
        if node is not None and node.returncode is None:
            node.kill()
            await node.wait()
        await supervisor.aclose()
