import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.leases import ResourceLeaseRegistry
from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.socket_server import ResourceSocketServer
from tests.resource_runtime.test_broker import scope


class Approved:
    async def authorize(self, request, scope):
        return (
            "stable-host-event" if request.arguments.get("workflowPrompt") == "approved" else None
        )


class Executed:
    def __init__(self, root):
        self.root = root
        root.mkdir(mode=0o700)
        self.calls = 0
        self.status = "succeeded"
        self.path = root / "report.bin"
        self.content = bytes(range(256)) * 180
        self.path.write_bytes(self.content)

    async def invoke(self, request, scope):
        self.calls += 1
        return {
            "operationId": request.request_id,
            "status": self.status,
            "outputFiles": [str(self.path)],
        }


def configured(tmp_path, worker, *, resource_scope=None, artifact_root=True, discovery_key=None):
    owner = resource_scope or scope().model_copy(
        update={
            "allowed_operations": ("execute_skills", "read_skill_artifact"),
        }
    )
    registry = ResourceLeaseRegistry()
    lease = registry.issue(owner)
    ledger = OperationLedger(tmp_path / "ledger")
    broker = ResourceBroker(
        registry, owner.generation_id, write_authorizer=Approved(), operation_ledger=ledger
    )
    broker.register(
        owner, worker, artifact_root=worker.root if artifact_root else None,
        discovery_key=discovery_key,
    )
    request = {
        "v": 1,
        "requestId": "call-a",
        "handle": lease.handle,
        "deadline": int((time.time() + 15) * 1000),
        "operation": "execute_skills",
        "arguments": {"workflowPrompt": "approved", "skillIds": ["skill-a"]},
    }
    return broker, request, owner


async def test_durable_bytes_recover_without_reexecution_or_host_paths(tmp_path):
    worker = Executed(tmp_path / "outputs")
    broker, request, owner = configured(tmp_path, worker)
    first = await broker.dispatch(request)
    result = first["result"]
    assert result["status"] == first["operationReceipt"]["state"] == "succeeded"
    assert str(tmp_path) not in str(first)
    assert "outputFiles" not in str(first)
    metadata = result["artifacts"][0]
    assert metadata["sha256"] == hashlib.sha256(worker.content).hexdigest()
    worker.path.unlink()
    recovered_scope = owner.model_copy(update={"activation_id": "recovered"})
    recovered, retry, _ = configured(tmp_path, worker, resource_scope=recovered_scope)
    assert (await recovered.dispatch(retry))["result"]["artifacts"] == result["artifacts"]
    assert worker.calls == 1
    chunks = []
    offset = 0
    while offset is not None:
        response = await recovered.dispatch(
            {
                **retry,
                "operation": "read_skill_artifact",
                "arguments": {
                    "operationId": result["operationId"],
                    "artifactId": metadata["artifactId"],
                    "offset": offset,
                },
            }
        )
        item = response["result"]
        chunks.append(base64.b64decode(item["content"]))
        offset = item["nextOffset"]
    assert b"".join(chunks) == worker.content
    assert worker.calls == 1  # Host reads never use the Worker or a sandbox.


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_ref", "other-actor"),
        ("agent_id", "other-agent"),
        ("tenant_ref", "other-tenant"),
        ("resource_principal_ref", "other-account"),
    ],
)
async def test_artifact_id_does_not_authorize_another_owner(tmp_path, field, value):
    worker = Executed(tmp_path / "outputs")
    broker, request, owner = configured(tmp_path, worker)
    result = (await broker.dispatch(request))["result"]
    other = owner.model_copy(update={"identity": owner.identity.model_copy(update={field: value})})
    stranger, read, _ = configured(tmp_path, worker, resource_scope=other)
    response = await stranger.dispatch(
        {
            **read,
            "operation": "read_skill_artifact",
            "arguments": {
                "operationId": result["operationId"],
                "artifactId": result["artifacts"][0]["artifactId"],
            },
        }
    )
    assert response["error"]["code"] == "RESOURCE_ARTIFACT_NOT_FOUND"
    assert worker.calls == 1


async def test_revocation_prevents_artifact_read(tmp_path):
    worker = Executed(tmp_path / "outputs")
    broker, request, owner = configured(tmp_path, worker)
    result = (await broker.dispatch(request))["result"]
    broker.revoke_activation(owner.activation_id)
    response = await broker.dispatch(
        {
            **request,
            "operation": "read_skill_artifact",
            "arguments": {
                "operationId": result["operationId"],
                "artifactId": result["artifacts"][0]["artifactId"],
            },
        }
    )
    assert response["error"]["code"] == "RESOURCE_SCOPE_INVALID"


async def test_invalid_worker_paths_do_not_publish_or_reexecute(tmp_path):
    worker = Executed(tmp_path / "outputs")
    worker.path = tmp_path / "private.txt"
    worker.path.write_text("fixture-secret")
    broker, request, _ = configured(tmp_path, worker)
    first = await broker.dispatch(request)
    assert first["error"]["code"] == "RESOURCE_WRITE_UNKNOWN"
    assert "fixture-secret" not in str(first)
    assert (await broker.dispatch(request))["result"]["status"] == "unknown"
    assert worker.calls == 1


async def test_unknown_execution_does_not_publish_reported_files(tmp_path):
    worker = Executed(tmp_path / "outputs")
    worker.status = "unknown"
    broker, request, _ = configured(tmp_path, worker)
    result = (await broker.dispatch(request))["result"]
    assert result == {"status": "unknown", "operationId": result["operationId"], "artifacts": []}


async def test_confirmed_failure_retains_partial_artifacts_without_reexecution(tmp_path):
    worker = Executed(tmp_path / "outputs")
    worker.status = "failed"
    broker, request, _ = configured(tmp_path, worker)
    first = await broker.dispatch(request)
    assert first["result"]["status"] == first["operationReceipt"]["state"] == "failed"
    assert first["result"]["artifacts"][0]["name"] == "report.bin"
    assert (await broker.dispatch(request))["result"]["status"] == "failed"
    assert worker.calls == 1


async def test_execution_requires_publication_root_before_dispatch(tmp_path):
    worker = Executed(tmp_path / "outputs")
    broker, request, _ = configured(tmp_path, worker, artifact_root=False)
    assert (await broker.dispatch(request))["error"][
        "code"
    ] == "RESOURCE_ARTIFACT_DELIVERY_REQUIRED"
    assert worker.calls == 0


async def test_concurrent_retries_publish_once(tmp_path):
    worker = Executed(tmp_path / "outputs")
    broker, request, _ = configured(tmp_path, worker)
    results = await asyncio.gather(*(broker.dispatch(request) for _ in range(4)))
    assert all(item["result"]["status"] == "succeeded" for item in results)
    assert worker.calls == 1


async def test_official_core_execute_and_artifact_tools(tmp_path):
    node_modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if not node_modules:
        pytest.skip("Set KSADK_TEST_DSH_NODE_MODULES to the installed official Core fixture")
    worker = Executed(tmp_path / "outputs")
    broker, request, _ = configured(tmp_path, worker)
    server = ResourceSocketServer(broker)
    root = Path(__file__).resolve().parents[2]
    bundles = root / "ksadk/plugins/providers/bundles"
    node = None
    try:
        await server.start()
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
                        "nodeModules": node_modules,
                        "socketPath": str(server.path),
                        "bridgePlugin": (bundles / "dsh-platform-resources/index.mjs").as_uri(),
                        "businessPlugin": (bundles / "dsh-skill-center/index.mjs").as_uri(),
                        "toolName": "execute_skills",
                        "checkSkillArtifact": True,
                        "missingContextArguments": request["arguments"],
                        "calls": [{"handle": request["handle"], "arguments": request["arguments"]}],
                    }
                ).encode()
            ),
            20,
        )
        assert node.returncode == 0, errors.decode()
        result = json.loads(output)
        assert result["missingContext"]["isError"]
        assert result["results"][0]["value"]["status"] == "succeeded", result
        assert not result["skillArtifact"]["isError"], result
        artifact = result["skillArtifact"]["value"]
        assert base64.b64decode(artifact["content"]) == worker.content[:32768]
        assert str(tmp_path) not in output.decode()
    finally:
        if node is not None and node.returncode is None:
            node.kill()
            await node.wait()
        await server.aclose()
