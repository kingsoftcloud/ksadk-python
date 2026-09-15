import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from tests.resource_runtime.test_memory_worker import memory_initialization
from tests.resource_runtime.test_worker_process import freeze_initialization, initialization
from tests.resource_runtime.test_worker_process import upstream as upstream


@pytest.fixture
def memory_auth_upstream():
    state = {"status": 200, "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(body)
            if state["status"] != 200:
                result = {"Error": "private upstream authorization detail"}
            elif "DataType" in body:
                result = {"RequestId": "accepted-request"}
            else:
                result = {"Data": [{"Memories": [{
                    "MemoryId": "real-id", "Memory": "preference",
                    "AgentUserId": body["AgentUserId"],
                }]}]}
            encoded = json.dumps(result).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("operation", [
    "load_memory", "save_memory", "update_memory", "delete_memory", "memory_status",
])
async def test_memory_authorization_failure_revokes_reads_and_writes(
    tmp_path, upstream, memory_auth_upstream, status, operation,
):
    kb_endpoint, kb_calls = upstream
    endpoint, state = memory_auth_upstream
    payload = memory_initialization(endpoint).pipe_payload()
    payload["scopes"][0]["allowedOperations"] = [
        "load_memory", "save_memory", "update_memory", "delete_memory", "memory_status",
    ]
    kb = initialization(kb_endpoint).pipe_payload()
    kb["bindings"][0]["config"]["binding"].update(id="kb-binding", connectionRef="kb-connection")
    kb["scopes"][0]["bindingId"] = "kb-binding"
    payload["bindings"].extend(kb["bindings"])
    payload["scopes"].extend(kb["scopes"])
    init = freeze_initialization(payload)

    class Approval:
        async def authorize(self, request, scope):
            return "event-" + request.operation

    ledger = OperationLedger(tmp_path / "ledger")
    supervisor = ResourceSupervisor(
        "generation-a", write_authorizer=Approval(), operation_ledger=ledger,
    )
    try:
        active = await supervisor.activate(init)
        memory_lease, kb_lease = active.leases

        async def invoke(lease, name, arguments):
            return await supervisor._broker.dispatch({
                "v": 1, "requestId": "request", "handle": lease.handle,
                "deadline": int(time.time() * 1000) + 10000,
                "operation": name, "arguments": arguments,
            })

        first = await invoke(memory_lease, "load_memory", {"query": "preference"})
        assert first["result"]["status"] == "ok"
        arguments = {
            "load_memory": {"query": "preference"},
            "save_memory": {"content": "preference"},
            "update_memory": {"memoryId": "real-id", "content": "changed"},
            "delete_memory": {"memoryId": "real-id"},
        }
        accepted_id = None
        if operation == "memory_status":
            accepted = await invoke(memory_lease, "save_memory", {"content": "preference"})
            accepted_id = accepted["operationReceipt"]["operationId"]
            arguments[operation] = {"operationId": accepted_id}
        before = len(state["calls"])
        state["status"] = status
        denied = await invoke(memory_lease, operation, arguments[operation])
        assert denied["error"]["code"] == "RESOURCE_FORBIDDEN"
        assert "private upstream" not in str(denied)
        assert len(state["calls"]) == before + 1
        state["status"] = 200  # Upstream recovery cannot revive the old capability.
        retry = await invoke(memory_lease, operation, arguments[operation])
        assert retry["error"]["code"] == "RESOURCE_SCOPE_INVALID"
        assert len(state["calls"]) == before + 1
        if operation in {"save_memory", "update_memory", "delete_memory"}:
            receipt = denied["operationReceipt"]
            assert receipt["state"] == "unknown"
            assert ledger.lookup(
                receipt["operationId"],
                authority_digest=supervisor._broker._authority(memory_lease.scope),
                operation=operation,
            ).state == "unknown"
        if accepted_id:
            assert ledger.lookup(
                accepted_id, authority_digest=supervisor._broker._authority(memory_lease.scope),
                operation="save_memory",
            ).state == "accepted_pending"
        result = await invoke(kb_lease, "search_knowledge_base", {"query": "hello"})
        assert result["result"]["status"] == "ok" and len(kb_calls) == 1
        observed = await supervisor.runtime_status(agent_id="agent-a", activation_id="activation-a")
        assert [item["runtimeState"] for item in observed["items"]] == ["unavailable", "available"]
    finally:
        await supervisor.aclose()
