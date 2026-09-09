import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from ksadk.plugins.providers.dsh_capabilities import DshMcpConnectorLease
from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from tests.resource_runtime.test_memory_worker import memory_initialization
from tests.resource_runtime.test_memory_worker import memory_upstream as memory_upstream
from tests.resource_runtime.test_worker_process import freeze_initialization


async def test_mcp_memory_failure_pending_and_unknown_are_distinct(tmp_path, memory_upstream):
    modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if not modules:
        pytest.skip("Installed official Cordis and DSH tools are required")
    endpoint, calls = memory_upstream
    payload = memory_initialization(endpoint).pipe_payload()
    payload["scopes"][0]["allowedOperations"] = ["load_memory", "save_memory"]
    init = freeze_initialization(payload)

    class Approval:
        async def authorize(self, request, scope):
            return "fixture-event-" + request.arguments["content"]

    supervisor = ResourceSupervisor(
        "generation-a",
        write_authorizer=Approval(),
        operation_ledger=OperationLedger(tmp_path / "ledger"),
    )
    node = None
    try:
        active = await supervisor.activate(init)
        root = Path(__file__).resolve().parents[2]
        bundles = root / "ksadk/plugins/providers/bundles"
        node = await asyncio.create_subprocess_exec(
            "node",
            str(root / "tests/fixtures/resource_runtime/node_mcp_host.mjs"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        node.stdin.write(
            (
                json.dumps(
                    {
                        "nodeModules": modules,
                        "socketPath": str(active.socket_path),
                        "profileDigest": init.scopes[0].profile_digest,
                        "bridgePlugin": (bundles / "dsh-platform-resources/index.mjs").as_uri(),
                        "knowledgePlugin": (bundles / "dsh-memory/index.mjs").as_uri(),
                        "hostPlugin": (bundles / "ksadk-dsh-capability-host/index.mjs").as_uri(),
                    }
                )
                + "\n"
            ).encode()
        )
        await node.stdin.drain()
        token_line = await asyncio.wait_for(node.stdout.readline(), 10)
        ready_line = await asyncio.wait_for(node.stdout.readline(), 10)
        token_prefix, ready_prefix = (
            b"@@KSADK_DSH_CAPABILITY_TOKEN@@",
            b"@@KSADK_DSH_CAPABILITY_READY@@",
        )
        if not token_line.startswith(token_prefix) or not ready_line.startswith(ready_prefix):
            raise RuntimeError("Memory MCP fixture did not initialize")
        ready = json.loads(ready_line[len(ready_prefix) :])
        connector = DshMcpConnectorLease(
            endpoint=ready["endpoint"],
            profile="studio",
            profile_digest=init.scopes[0].profile_digest,
            descriptor_digest="sha256:" + "d" * 64,
            _bearer_token=token_line[len(token_prefix) :].decode().strip(),
        )
        token = connector.resource_bearer_token(
            {"load": "load_memory", "save": "save_memory"}, active.leases
        )
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:

            async def invoke(name, arguments):
                response = await client.post(
                    ready["endpoint"],
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "jsonrpc": "2.0",
                        "id": "call",
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                return response.json()["result"]

            failed = await invoke("load", {"query": "malformed"})
            assert failed["isError"] is True
            assert failed["structuredContent"]["status"] == "failed"
            assert failed["_meta"]["io.ksadk/dsh"]["code"] == "MEMORY_SEARCH_FAILED"
            for content, status in [
                ("accepted", "accepted_pending"),
                ("unconfirmed-write", "unknown"),
            ]:
                first = await invoke("save", {"content": content})
                duplicate = await invoke("save", {"content": content})
                assert first["isError"] is False and duplicate["isError"] is False
                assert first["structuredContent"] == duplicate["structuredContent"]
                assert first["structuredContent"]["status"] == status
                assert first["structuredContent"]["searchable"] is False
            assert len(calls) == 3
    finally:
        if node is not None and node.returncode is None:
            node.kill()
            await node.wait()
        await supervisor.aclose()
