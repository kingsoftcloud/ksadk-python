import asyncio
import json
import os
from pathlib import Path

import pytest

from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from tests.resource_runtime.test_memory_worker import memory_initialization
from tests.resource_runtime.test_memory_worker import memory_upstream as memory_upstream
from tests.resource_runtime.test_worker_process import freeze_initialization


@pytest.mark.parametrize("confirmed", [True, False])
async def test_official_core_memory_mutations_and_scope_denial(
    tmp_path, memory_upstream, confirmed
):
    modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if not modules:
        pytest.skip("Installed official Cordis and DSH tools are required")
    endpoint, requests = memory_upstream
    payload = memory_initialization(endpoint).pipe_payload()
    payload["scopes"][0]["allowedOperations"] = ["load_memory", "update_memory", "delete_memory"]
    init = freeze_initialization(payload)

    class Approval:
        async def authorize(self, request, scope):
            if request.arguments.get("content") == "not-approved":
                return None
            return "approved-event-" + request.operation

    supervisor = ResourceSupervisor(
        "generation-a",
        write_authorizer=Approval(),
        operation_ledger=OperationLedger(tmp_path / "ledger"),
    )
    node = None
    try:
        active = await supervisor.activate(init)
        readonly = await supervisor.activate(memory_initialization(endpoint, activation="readonly"))
        handle = active.leases[0].handle
        update = {
            "memoryId": "server-real-id",
            "content": "updated preference" if confirmed else "unconfirmed-mutation",
        }
        remove = {"memoryId": "updated-real-id"}
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
                        "socketPath": str(active.socket_path),
                        "toolName": "update_memory",
                        "missingContextArguments": update,
                        "sequentialCalls": True,
                        "calls": [
                            {
                                "handle": handle,
                                "name": "load_memory",
                                "arguments": {"query": "preference"},
                            },
                            {"handle": handle, "arguments": update},
                            {"handle": handle, "arguments": update},
                            {"handle": handle, "name": "delete_memory", "arguments": remove},
                            {"handle": handle, "name": "delete_memory", "arguments": remove},
                            {"handle": readonly.leases[0].handle, "arguments": update},
                            {"handle": handle, "arguments": {**update, "content": "not-approved"}},
                        ],
                    }
                ).encode()
            ),
            30,
        )
        assert node.returncode == 0, errors.decode()
        result = json.loads(output)
        loaded, changed, duplicate, deleted, duplicate_delete, denied_scope, denied_approval = (
            result["results"]
        )
        for reply in (loaded, changed, duplicate, deleted, duplicate_delete):
            assert reply["isError"] is False, reply
        assert loaded["value"]["items"][0]["memoryId"] == "server-real-id"
        assert changed["value"] == duplicate["value"]
        assert changed["value"]["newMemoryId"] == ("updated-real-id" if confirmed else None)
        assert changed["value"]["status"] == ("succeeded" if confirmed else "unknown")
        assert deleted["value"] == duplicate_delete["value"]
        assert deleted["value"]["status"] == ("succeeded" if confirmed else "failed")
        assert deleted["value"]["newMemoryId"] is None
        assert denied_scope["isError"] and denied_approval["isError"]
        assert result["missingContext"]["isError"]
        assert len(requests) == (3 if confirmed else 2)
        if confirmed:
            assert "soft-deletion" in str(deleted)
        else:
            assert "Do not submit it again automatically" in str(changed)
        assert "fake-selected-secret" not in output.decode()
    finally:
        if node is not None and node.returncode is None:
            node.kill()
            await node.wait()
        await supervisor.aclose()
