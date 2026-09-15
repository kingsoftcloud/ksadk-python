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
def skill_upstream():
    content = archive()
    state = {"failure": False, "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            selected = ref(state.get("content", content))
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            state["calls"].append((url.path, query))
            status = 200
            if state["failure"] and url.path.endswith(state.get("failure_path", "")):
                status, payload = state.get("status", 503), {"error": "private upstream details"}
            elif url.path.endswith("ListSkillsBySpaceId"):
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
            elif url.path.endswith("GetSkillDownloadUrl"):
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
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
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


async def test_real_worker_download_and_transient_failure_recovery(tmp_path, skill_upstream):
    endpoint, state = skill_upstream
    init = discovery_initialization(endpoint, tmp_path)
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
        catalogs = [query for path, query in state["calls"] if path.endswith("ListSkillsBySpaceId")]
        assert catalogs and all(query == {"SpaceId": ["space-a"]} for query in catalogs)
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
