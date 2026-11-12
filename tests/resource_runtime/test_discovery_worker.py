import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from ksadk.resource_runtime.ipc import ResourceRequest
from ksadk.resource_runtime.process import ResourceWorkerProcess
from tests.resource_runtime.test_skill_packages import archive, ref
from tests.resource_runtime.test_worker_process import freeze_initialization, initialization
from tests.resource_runtime.test_worker_process import upstream as upstream


@pytest.fixture
def skill_upstream(monkeypatch):
    content = archive()
    state = {"failure": False, "calls": [], "headers": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            selected = ref(state.get("content", content))
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            action = query.get("Action", [url.path])[0]
            state["calls"].append((url.path, query))
            state["headers"].append(dict(self.headers))
            status = 200
            if state["failure"] and action.endswith(state.get("failure_path", "")):
                status, payload = state.get("status", 503), {"error": "private upstream details"}
            elif action == "ListSkillsBySpaceId":
                payload = {
                    "Data": {
                        "SpaceId": "space-a",
                        "Skills": [
                            {
                                "SkillId": selected.skill_id,
                                "VersionId": state.get("version_id", selected.version_id),
                                "Version": selected.version,
                                "Name": selected.name,
                                "ContentHash": selected.content_hash.render(),
                            }
                        ],
                    }
                }
            elif action == "GetSkillDownloadUrl":
                payload = {
                    "Data": {"DownloadUrl": f"http://127.0.0.1:{self.server.server_port}/package"}
                }
            elif url.path == "/package":
                payload = state.get("content", content)
            else:
                status, payload = 404, {}
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_endpoint = f"http://127.0.0.1:{server.server_port}"
    # Keep the real canonical endpoint validator and signer in the isolated
    # worker. Only this fixture's HTTP transport routes its signed requests to
    # the local server; no system DNS, production client or auth rule changes.
    bootstrap = f"""
import requests
from urllib.parse import urlsplit
send = requests.Session.send
def local_send(session, request, **kwargs):
    parsed = urlsplit(request.url)
    if parsed.hostname == "aicp.api.ksyun.com":
        request = request.copy()
        request.url = {local_endpoint!r} + parsed.path + "?" + parsed.query
    return send(session, request, **kwargs)
requests.Session.send = local_send
from ksadk.resource_runtime.worker import main
raise SystemExit(main())
"""
    spawn = asyncio.create_subprocess_exec

    async def local_worker(program, *arguments, **kwargs):
        if arguments == ("-I", "-m", "ksadk.resource_runtime.worker"):
            arguments = ("-I", "-c", bootstrap)
        return await spawn(program, *arguments, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_worker)
    try:
        yield "https://aicp.api.ksyun.com", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def discovery_initialization(endpoint, tmp_path):
    payload = initialization(endpoint).pipe_payload()
    binding = payload["bindings"][0]
    binding["config"]["binding"]["resource"].update(kind="skill-space", id="space-a")
    binding["config"].pop("retrieval", None)
    binding["config"]["selectionMode"] = "discovery"
    binding["selectionRunRef"] = "run-a"
    binding["cacheDirectory"] = str(tmp_path / "cache")
    payload["scopes"][0]["allowedOperations"] = ["list_skills", "load_skill"]
    return freeze_initialization(payload)


async def test_real_worker_download_and_transient_failure_recovery(
    tmp_path, skill_upstream, monkeypatch
):
    endpoint, state = skill_upstream
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "skill-worker-fixture")
    init = discovery_initialization(endpoint, tmp_path)
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "changed-after-initialization")
    worker = await ResourceWorkerProcess.start(init)

    async def invoke(operation, arguments=None):
        request = ResourceRequest(
            v=1,
            request_id="request-test",
            handle="x" * 32,
            deadline=int(time.time() * 1000) + 10000,
            operation=operation,
            arguments=arguments or {},
        )
        return await worker.invoke(request, init.scopes[0])

    try:
        state["failure"] = True
        failure = await invoke("list_skills")
        assert failure == {"status": "failed", "errorCode": "SKILL_RESOURCE_UNAVAILABLE"}
        state["failure"] = False
        assert (await invoke("list_skills"))["items"][0]["skillId"] == "skill-a"
        loaded = await invoke("load_skill", {"skillId": "skill-a"})
        assert loaded["content"] == "original"
        assert loaded["versionId"] == "version-a"
        assert str(tmp_path) not in str(loaded)
        catalogs = [query for _, query in state["calls"]
                    if query.get("Action") == ["ListSkillsBySpaceId"]]
        assert catalogs and all(query == {
            "Action": ["ListSkillsBySpaceId"], "Version": ["2024-06-12"],
            "SpaceId": ["space-a"], "PageNumber": ["1"], "PageSize": ["100"],
        } for query in catalogs)
        for (path, _), headers in zip(state["calls"], state["headers"], strict=True):
            if path != "/package":
                assert headers["X-Traffic-Source"] == "skill-worker-fixture"
                assert headers["Host"] == "aicp.api.ksyun.com"
                assert headers["Authorization"].startswith(
                    "AWS4-HMAC-SHA256 Credential=fake-selected-access/"
                )
    finally:
        await worker.aclose()


def test_discovery_worker_rejects_execution_scope(tmp_path):
    payload = discovery_initialization("https://skills.example.test", tmp_path).pipe_payload()
    payload["scopes"][0]["allowedOperations"].append("execute_skills")
    with pytest.raises(ValueError):
        freeze_initialization(payload)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("action", ["ListSkillsBySpaceId", "GetSkillDownloadUrl"])
async def test_authorization_failure_revokes_binding(
    tmp_path, skill_upstream, upstream, status, action,
):
    endpoint, state = skill_upstream
    kb_endpoint, kb_calls = upstream
    payload = discovery_initialization(endpoint, tmp_path).pipe_payload()
    kb = initialization(kb_endpoint).pipe_payload()
    kb["bindings"][0]["config"]["binding"].update(id="kb-binding", connectionRef="kb-connection")
    kb["scopes"][0]["bindingId"] = "kb-binding"
    payload["bindings"].extend(kb["bindings"])
    payload["scopes"].extend(kb["scopes"])
    init = freeze_initialization(payload)
    worker = await ResourceWorkerProcess.start(init)
    try:
        state.update(failure=True, status=status, failure_path=action)
        request = ResourceRequest(
            v=1,
            request_id="denied-request",
            handle="x" * 32,
            deadline=int(time.time() * 1000) + 10000,
            operation="load_skill",
            arguments={"skillId": "skill-a"},
        )
        expected = {"status": "unauthorized", "errorCode": "RESOURCE_FORBIDDEN"}
        assert await worker.invoke(request, init.scopes[0]) == expected
        count = len(state["calls"])
        assert count == (1 if action == "ListSkillsBySpaceId" else 2)
        state["failure"] = False
        assert await worker.invoke(request, init.scopes[0]) == expected
        assert len(state["calls"]) == count
        assert worker.is_running
        knowledge = request.model_copy(update={
            "operation": "search_knowledge_base", "arguments": {"query": "hello"},
        })
        assert (await worker.invoke(knowledge, init.scopes[1]))["status"] == "ok"
        assert len(kb_calls) == 1
    finally:
        await worker.aclose()


@pytest.mark.parametrize("status", [401, 403])
async def test_object_download_denial_does_not_claim_space_revocation(
    tmp_path, skill_upstream, status,
):
    endpoint, state = skill_upstream
    init = discovery_initialization(endpoint, tmp_path)
    worker = await ResourceWorkerProcess.start(init)
    request = ResourceRequest(
        v=1, request_id="download", handle="x" * 32,
        deadline=int(time.time() * 1000) + 10000,
        operation="load_skill", arguments={"skillId": "skill-a"},
    )
    try:
        state.update(failure=True, status=status, failure_path="/package")
        result = await worker.invoke(request, init.scopes[0])
        assert result == {"status": "failed", "errorCode": "SKILL_RESOURCE_UNAVAILABLE"}
        assert "private upstream" not in str(result)
        state["failure"] = False
        # A fresh download URL may now succeed; an object-store 403 alone did
        # not establish that the principal's Skill Space grant was revoked.
        loaded = await worker.invoke(request, init.scopes[0])
        assert loaded["content"] == "original"
        assert worker.is_running
        assert len([path for path, _ in state["calls"] if path == "/package"]) == 2
    finally:
        await worker.aclose()
