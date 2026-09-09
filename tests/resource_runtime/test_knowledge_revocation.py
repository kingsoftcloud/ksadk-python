import time

import pytest

from ksadk.resource_runtime.leases import LeaseRejected
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from tests.resource_runtime.test_memory_worker import memory_initialization
from tests.resource_runtime.test_memory_worker import memory_upstream as memory_upstream
from tests.resource_runtime.test_worker_process import freeze_initialization, initialization
from tests.resource_runtime.test_worker_process import upstream as upstream


@pytest.mark.parametrize("status", [401, 403])
async def test_real_kb_auth_failure_revokes_only_its_binding(upstream, memory_upstream, status):
    endpoint, calls = upstream
    memory_endpoint, memory_calls = memory_upstream
    payload = initialization(endpoint).pipe_payload()
    memory = memory_initialization(memory_endpoint).pipe_payload()
    memory["bindings"][0]["config"]["binding"].update(
        id="memory-binding", connectionRef="memory-connection",
    )
    memory["scopes"][0]["bindingId"] = "memory-binding"
    payload["bindings"].extend(memory["bindings"])
    payload["scopes"].extend(memory["scopes"])
    init = freeze_initialization(payload)
    supervisor = ResourceSupervisor("generation-a")
    try:
        active = await supervisor.activate(init)
        knowledge_lease, memory_lease = active.leases

        async def invoke(lease, operation, query):
            return await supervisor._broker.dispatch({
                "v": 1, "requestId": "request-test", "handle": lease.handle,
                "deadline": int(time.time() * 1000) + 10000,
                "operation": operation, "arguments": {"query": query},
            })

        transient = await invoke(knowledge_lease, "search_knowledge_base", "resource-error")
        assert transient["result"]["errorCode"] == "KNOWLEDGE_RETRIEVAL_FAILED"
        recovered = await invoke(knowledge_lease, "search_knowledge_base", "recovered")
        assert recovered["result"]["status"] == "ok"
        forbidden = await invoke(knowledge_lease, "search_knowledge_base", f"resource-{status}")
        assert forbidden["error"]["code"] == "RESOURCE_FORBIDDEN"
        assert "private authorization" not in str(forbidden)
        assert len(calls) == 3
        rejected = await invoke(knowledge_lease, "search_knowledge_base", "no retry")
        assert rejected["error"]["code"] == "RESOURCE_SCOPE_INVALID"
        assert len(calls) == 3
        with pytest.raises(LeaseRejected):
            supervisor._registry.renew(knowledge_lease.handle)
        recalled = await invoke(memory_lease, "load_memory", "preference")
        assert recalled["result"]["status"] == "ok"
        assert len(memory_calls) == 1
        observed = await supervisor.runtime_status(agent_id="agent-a", activation_id="activation-a")
        assert [item["runtimeState"] for item in observed["items"]] == [
            "unavailable", "available",
        ]
        assert supervisor._running[active.activation_id].worker.pid == active.worker_pid
        assert supervisor._running[active.activation_id].worker.is_running
    finally:
        await supervisor.aclose()
