import asyncio
import json
import os
import signal
import stat
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from ksadk.plugins.providers.dsh_capabilities import DshMcpConnectorLease
from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.ipc import decode_body, encode_frame
from ksadk.resource_runtime.leases import ResourceLeaseRegistry
from ksadk.resource_runtime.process import ResourceWorkerProcess
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.resource_runtime.socket_server import ResourceSocketServer
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from ksadk.resource_runtime.worker import WorkerInitialization


@pytest.fixture
def upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((dict(self.headers), body))
            result = json.dumps(
                {
                    "RequestId": "upstream-process",
                    "Records": [
                        {
                            "Score": 0.9,
                            "Segment": {
                                "Id": "segment-a",
                                "DocumentId": "document-a",
                                "Document": {"Name": "Test source"},
                                "Content": "worker-result",
                            },
                        }
                    ],
                }
            ).encode()
            status = 200
            if body.get("Query") == "resource-error":
                status, result = 503, b'{"Error": "fixture upstream failure"}'
            elif body.get("Query") == "resource-empty":
                result = b'{"Records": []}'
            elif body.get("Query") in {"resource-401", "resource-403"}:
                status = int(body["Query"].split("-")[1])
                result = b'{"Error": "private authorization failure"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

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


def initialization(endpoint):
    return freeze_initialization(
        {
            "bindings": [
                {
                    "config": {
                        "binding": {
                            "id": "binding-a",
                            "connectionRef": "connection-a",
                            "resource": {
                                "kind": "knowledge-base",
                                "id": "kb-selected",
                                "region": "region-a",
                            },
                        }
                    },
                    "endpoint": endpoint,
                    "accessKey": "fake-selected-access",
                    "secretKey": "fake-selected-secret",
                }
            ],
            "scopes": [
                {
                    "profileDigest": "sha256:" + "a" * 64,
                    "generationId": "generation-a",
                    "activationId": "activation-a",
                    "buildDigest": "sha256:" + "b" * 64,
                    "bindingSnapshotDigest": "sha256:" + "c" * 64,
                    "bindingId": "binding-a",
                    "identity": {
                        "tenantRef": "tenant-a",
                        "resourcePrincipalRef": "account-a",
                        "actorRef": "actor-a",
                        "memorySubjectRef": "user-a",
                        "agentId": "agent-a",
                        "sessionRef": "session-a",
                    },
                    "allowedOperations": ["search_knowledge_base"],
                }
            ],
        }
    )


def freeze_initialization(payload):
    scope = payload["scopes"][0]
    snapshot = ResourceSnapshot.model_validate(
        {
            "pluginLockDigest": "sha256:" + "e" * 64,
            "bindings": [
                {
                    "config": binding["config"],
                    "connection": {
                        "connectionRef": binding["config"]["binding"]["connectionRef"],
                        "tenantRef": scope["identity"]["tenantRef"],
                        "principalRef": scope["identity"]["resourcePrincipalRef"],
                        "endpoint": binding["endpoint"],
                        "authMode": "signed",
                    },
                }
                for binding in payload["bindings"]
            ],
        }
    )
    payload["resourceSnapshot"] = snapshot.model_dump(by_alias=True, mode="json")
    for item in payload["scopes"]:
        item["bindingSnapshotDigest"] = snapshot.digest
    return WorkerInitialization.model_validate(payload)


@pytest.mark.parametrize("change", ["resource", "endpoint", "principal", "plugin-lock"])
def test_worker_rejects_snapshot_drift_before_start(change):
    payload = initialization("https://resources.example.test").pipe_payload()
    if change == "resource":
        payload["bindings"][0]["config"]["binding"]["resource"]["id"] = "kb-other"
    elif change == "endpoint":
        payload["bindings"][0]["endpoint"] = "https://other.example.test"
    elif change == "principal":
        payload["scopes"][0]["identity"]["resourcePrincipalRef"] = "other-account"
    else:
        payload["resourceSnapshot"]["pluginLockDigest"] = "sha256:" + "9" * 64
    with pytest.raises(ValidationError) as raised:
        WorkerInitialization.model_validate(payload)
    assert "fake-selected-secret" not in str(raised.value)


def test_credential_rotation_preserves_frozen_snapshot():
    original = initialization("https://resources.example.test")
    payload = original.pipe_payload()
    payload["bindings"][0]["accessKey"] = "fake-rotated-access"
    payload["bindings"][0]["secretKey"] = "fake-rotated-secret"
    rotated = WorkerInitialization.model_validate(payload)
    assert rotated.resource_snapshot.digest == original.resource_snapshot.digest


async def test_real_mcp_resource_scope_and_legacy_rejection(upstream):
    modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if not modules:
        pytest.skip("Pinned Cordis/DSH installation is required for resource MCP E2E")
    endpoint, calls = upstream
    init = initialization(endpoint)
    supervisor = ResourceSupervisor("generation-a")
    active = await supervisor.activate(init)
    node = None
    try:
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
                        "knowledgePlugin": (bundles / "dsh-knowledge/index.mjs").as_uri(),
                        "hostPlugin": (bundles / "ksadk-dsh-capability-host/index.mjs").as_uri(),
                        "extraPlugin": (
                            root / "tests/fixtures/resource_runtime/plain_status_plugin.mjs"
                        ).as_uri(),
                    }
                )
                + "\n"
            ).encode()
        )
        await node.stdin.drain()
        token_line = await asyncio.wait_for(node.stdout.readline(), 10)
        ready_line = await asyncio.wait_for(node.stdout.readline(), 10)
        token_prefix = b"@@KSADK_DSH_CAPABILITY_TOKEN@@"
        ready_prefix = b"@@KSADK_DSH_CAPABILITY_READY@@"
        if not token_line.startswith(token_prefix) or not ready_line.startswith(ready_prefix):
            raise RuntimeError("Resource MCP fixture did not produce valid readiness records")
        root_token = token_line[len(token_prefix) :].decode().strip()
        ready = json.loads(ready_line[len(ready_prefix) :])
        connector = DshMcpConnectorLease(
            endpoint=ready["endpoint"],
            profile="studio",
            profile_digest=init.scopes[0].profile_digest,
            descriptor_digest="sha256:" + "d" * 64,
            _bearer_token=root_token,
        )
        aliases = {"agent_knowledge": "search_knowledge_base"}
        legacy = connector.bearer_token_for_runtime(aliases)
        resource_token = connector.resource_bearer_token(aliases, active.leases)
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:

            async def request(token, method, params):
                response = await client.post(
                    ready["endpoint"],
                    headers={
                        "Authorization": "Bearer " + token,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": "request-mcp",
                        "method": method,
                        "params": params,
                    },
                )
                return response

            old_list = await request(legacy, "tools/list", {})
            assert old_list.json()["result"]["tools"] == []
            for token, name in ((legacy, "agent_knowledge"), (root_token, "search_knowledge_base")):
                rejected = await request(
                    token, "tools/call", {"name": name, "arguments": {"query": "denied"}}
                )
                assert rejected.json()["result"]["isError"] is True
            assert calls == []
            ordinary = await request(
                root_token, "tools/call", {"name": "plain_status", "arguments": {}}
            )
            assert ordinary.json()["result"]["isError"] is False
            assert ordinary.json()["result"]["structuredContent"] == {"status": "failed"}
            listed = await request(resource_token, "tools/list", {})
            assert [tool["name"] for tool in listed.json()["result"]["tools"]] == [
                "agent_knowledge"
            ]
            assert calls == []  # Inventory lease checks never retrieve knowledge.
            called = await request(
                resource_token,
                "tools/call",
                {
                    "name": "agent_knowledge",
                    "arguments": {"query": "mcp-approved"},
                },
            )
            result = called.json()["result"]
            assert result.get("isError", False) is False, result
            assert "worker-result" in json.dumps(result)
            assert calls[0][1]["DatasetId"] == "kb-selected"
            failure = await request(
                resource_token,
                "tools/call",
                {
                    "name": "agent_knowledge",
                    "arguments": {"query": "resource-error"},
                },
            )
            failure_result = failure.json()["result"]
            assert failure_result["isError"] is True
            assert failure_result["structuredContent"]["status"] == "failed"
            assert failure_result["structuredContent"]["errorCode"] == "KNOWLEDGE_RETRIEVAL_FAILED"
            empty = await request(
                resource_token,
                "tools/call",
                {
                    "name": "agent_knowledge",
                    "arguments": {"query": "resource-empty"},
                },
            )
            assert empty.json()["result"]["isError"] is False
            assert empty.json()["result"]["structuredContent"]["status"] == "empty"
            forbidden = await request(resource_token, "tools/call", {
                "name": "agent_knowledge", "arguments": {"query": "resource-403"},
            })
            assert forbidden.json()["result"]["isError"] is True
            # The bearer token has not expired or been revoked at the Core, but
            # the broker revoked the binding after observing upstream denial.
            hidden = await request(resource_token, "tools/list", {})
            assert hidden.json()["result"]["tools"] == []
            assert len(calls) == 4
            revoked = await request(root_token, "io.ksadk/scopes/revoke", {"token": resource_token})
            assert revoked.json()["result"]["revoked"] is True
            after = await request(
                resource_token,
                "tools/call",
                {"name": "agent_knowledge", "arguments": {"query": "denied"}},
            )
            assert after.status_code == 401
            assert len(calls) == 4
    finally:
        if node is not None:
            if node.returncode is None:
                node.stdin.close()
                try:
                    await asyncio.wait_for(node.wait(), 5)
                except asyncio.TimeoutError:
                    node.kill()
                    await node.wait()
        await supervisor.aclose()


async def call_active(resources):
    reader, writer = await asyncio.open_unix_connection(str(resources.socket_path))
    try:
        writer.write(
            encode_frame(
                {
                    "v": 1,
                    "requestId": "supervisor-call",
                    "handle": resources.leases[0].handle,
                    "operation": "search_knowledge_base",
                    "deadline": int((time.time() + 10) * 1000),
                    "arguments": {"query": "managed"},
                }
            )
        )
        await writer.drain()
        (size,) = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 10))
        return decode_body(await reader.readexactly(size))
    finally:
        writer.close()
        await writer.wait_closed()


async def test_supervisor_activation_and_shutdown_reclaim_resources(upstream):
    endpoint, calls = upstream
    supervisor = ResourceSupervisor("generation-a")
    active = await supervisor.activate(initialization(endpoint))
    try:
        assert (await call_active(active))["result"]["status"] == "ok"
        assert len(calls) == 1
        await supervisor.deactivate(active.activation_id)
        assert (await call_active(active))["error"]["code"] == "RESOURCE_SCOPE_INVALID"
        with pytest.raises(ValueError, match="ALREADY_USED"):
            await supervisor.activate(initialization(endpoint))
        assert active.leases[0].handle not in repr(active)
    finally:
        await supervisor.aclose()
    assert not active.socket_path.exists()
    assert not supervisor._running
    assert not supervisor._monitors


async def test_supervisor_detects_worker_crash_without_restart(upstream):
    endpoint, calls = upstream
    supervisor = ResourceSupervisor("generation-a")
    active = await supervisor.activate(initialization(endpoint))
    try:
        monitors = tuple(supervisor._monitors)
        os.kill(active.worker_pid, signal.SIGKILL)
        await asyncio.wait_for(asyncio.gather(*monitors), 5)
        assert (await call_active(active))["error"]["code"] == "RESOURCE_SCOPE_INVALID"
        assert not supervisor._running
        assert not calls
    finally:
        await supervisor.aclose()


async def test_supervisor_enforces_worker_capacity(upstream):
    endpoint, _ = upstream
    supervisor = ResourceSupervisor("generation-a", max_workers=1)
    first = await supervisor.activate(initialization(endpoint))
    payload = initialization(endpoint).pipe_payload()
    payload["scopes"][0]["activationId"] = "activation-b"
    second = WorkerInitialization.model_validate(payload)
    try:
        with pytest.raises(RuntimeError, match="WORKER_CAPACITY"):
            await supervisor.activate(second)
        await supervisor.deactivate(first.activation_id)
        active = await supervisor.activate(second)
        assert active.activation_id == "activation-b"
    finally:
        await supervisor.aclose()


async def test_broker_to_real_subprocess_to_sdk_http(upstream, monkeypatch):
    endpoint, calls = upstream
    for name in ("KSYUN_ACCESS_KEY", "KSYUN_SECRET_KEY", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.setenv(name, "fake-unrelated-environment-value")
    init = initialization(endpoint)
    assert "fake-selected-secret" not in repr(init)
    assert "fake-selected-secret" not in init.model_dump_json()
    registry = ResourceLeaseRegistry()
    lease = registry.issue(init.scopes[0])
    worker = await ResourceWorkerProcess.start(init)
    assert worker.pid != os.getpid()
    broker = ResourceBroker(registry, "generation-a")
    broker.register(lease.scope, worker)
    request = {
        "v": 1,
        "requestId": "request-process",
        "handle": lease.handle,
        "operation": "search_knowledge_base",
        "deadline": int((time.time() + 20) * 1000),
        "arguments": {"query": "from-process"},
    }
    try:
        reply = await broker.dispatch(request)
        assert reply["result"]["status"] == "ok", reply
        assert reply["result"]["items"][0]["content"] == "worker-result"
        assert reply["result"]["requestId"] == "upstream-process"
        assert calls[0][1]["DatasetId"] == "kb-selected"
        assert calls[0][1]["Query"] == "from-process"
        headers = {key.lower(): value for key, value in calls[0][0].items()}
        assert "fake-selected-access" in headers["authorization"]
        assert "fake-unrelated" not in str(headers)
        broker.revoke_activation("activation-a")
        assert (await broker.dispatch(request))["error"]["code"] == "RESOURCE_SCOPE_INVALID"
        assert len(calls) == 1
    finally:
        await worker.aclose()
    assert worker._process.returncode is not None


async def test_private_socket_to_worker_to_sdk(upstream):
    endpoint, calls = upstream
    init = initialization(endpoint)
    registry = ResourceLeaseRegistry()
    lease = registry.issue(init.scopes[0])
    worker = await ResourceWorkerProcess.start(init)
    broker = ResourceBroker(registry, "generation-a")
    broker.register(lease.scope, worker)
    server = ResourceSocketServer(broker)
    path = await server.start()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        request = {
            "v": 1,
            "requestId": "socket-request",
            "handle": lease.handle,
            "operation": "search_knowledge_base",
            "deadline": int((time.time() + 20) * 1000),
            "arguments": {"query": "socket-question"},
        }
        writer.write(encode_frame(request))
        await writer.drain()
        (size,) = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 10))
        reply = decode_body(await reader.readexactly(size))
        assert reply["result"]["items"][0]["content"] == "worker-result"
        assert calls[0][1]["Query"] == "socket-question"
    finally:
        writer.close()
        await writer.wait_closed()
        await server.aclose()
        await worker.aclose()
    assert not path.exists()
    assert not path.parent.exists()


@pytest.mark.parametrize("core", [False, True])
async def test_node_async_context_isolates_two_worker_activations(upstream, core):
    modules = os.environ.get("KSADK_TEST_DSH_NODE_MODULES")
    if core and not modules:
        pytest.skip("Set KSADK_TEST_DSH_NODE_MODULES to the pinned Cordis/DSH test installation")
    endpoint, calls = upstream
    first = initialization(endpoint)
    second_payload = first.pipe_payload()
    second_payload["bindings"][0]["config"]["binding"]["resource"]["id"] = "kb-second"
    second_payload["scopes"][0]["activationId"] = "activation-b"
    second_payload["scopes"][0]["identity"]["agentId"] = "agent-b"
    second_payload["scopes"][0]["identity"]["memorySubjectRef"] = "user-b"
    second_payload["scopes"][0]["buildDigest"] = "sha256:" + "f" * 64
    second = freeze_initialization(second_payload)
    registry = ResourceLeaseRegistry()
    broker = ResourceBroker(registry, "generation-a")
    server = ResourceSocketServer(broker)
    workers = []
    node = None
    try:
        node_calls = []
        for init, query in ((first, "query-a"), (second, "query-b")):
            worker = await ResourceWorkerProcess.start(init)
            workers.append(worker)
            lease = registry.issue(init.scopes[0])
            broker.register(lease.scope, worker)
            node_calls.append({"handle": lease.handle, "query": query})
        path = await server.start()
        root = Path(__file__).resolve().parents[2]
        module = root / "ksadk/plugins/providers/bundles/dsh-platform-resources/client.mjs"
        node = await asyncio.create_subprocess_exec(
            "node",
            str(
                root
                / "tests/fixtures/resource_runtime"
                / ("node_core_probe.mjs" if core else "node_bridge_probe.mjs")
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, errors = await asyncio.wait_for(
            node.communicate(
                json.dumps(
                    {
                        "module": module.as_uri(),
                        "nodeModules": modules,
                        "bridgePlugin": (module.parent / "index.mjs").as_uri(),
                        "knowledgePlugin": (
                            module.parent.parent / "dsh-knowledge/index.mjs"
                        ).as_uri(),
                        "socketPath": str(path),
                        "calls": node_calls,
                    }
                ).encode()
            ),
            30,
        )
        assert node.returncode == 0, errors.decode()
        result = json.loads(output)
        if core:
            assert "search_knowledge_base" in result["names"]
            assert result["missingContext"]["isError"] is True
            assert all(reply["isError"] is False for reply in result["results"]), result
            assert [reply["value"]["status"] for reply in result["results"]] == ["ok", "ok"]
        else:
            assert result["missingContext"] == "RESOURCE_CONTEXT_REQUIRED"
            assert [reply["status"] for reply in result["results"]] == ["ok", "ok"]
        actual = {body["Query"]: body["DatasetId"] for _, body in calls}
        assert actual == {"query-a": "kb-selected", "query-b": "kb-second"}
    finally:
        if node is not None and node.returncode is None:
            node.kill()
            await node.wait()
        await server.aclose()
        for worker in workers:
            await worker.aclose()
