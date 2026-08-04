from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.studio.api import create_studio_app
from ksadk.studio.cloud import InMemoryCloudGateway
from ksadk.studio.contracts import Usage
from ksadk.studio.model_client import CredentialResolver, ModelResponse
from ksadk.studio.service import StudioService
from tests.studio.runtime_adapter_fixtures import RuntimeFixture


class FakeModelClient:
    async def complete(self, *_args, **_kwargs):
        return ModelResponse(
            content="AGENTKIT_E2E_OK",
            finish_reason="stop",
            usage=Usage(input_tokens=5, output_tokens=3, total_tokens=8),
            tool_calls=[],
            raw_message={"role": "assistant", "content": "AGENTKIT_E2E_OK"},
        )


async def _runtime_events(request, handle):
    common = {
        "agent_id": request.agent_id or "agent",
        "user_id": request.user_id,
        "session_id": request.session_id,
        "invocation_id": handle.run_id,
    }
    yield RuntimeEvent.create(
        EventType.RUN_STARTED,
        **common,
        seq_id=1,
        payload={"status": "in_progress"},
    )
    yield RuntimeEvent.create(
        EventType.TEXT_COMPLETED,
        **common,
        seq_id=2,
        phase="final_answer",
        payload={"text": "AGENTKIT_E2E_OK"},
    )
    yield RuntimeEvent.create(
        EventType.USAGE_REPORTED,
        **common,
        seq_id=3,
        payload={
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
            "source": "fixture",
        },
    )
    yield RuntimeEvent.create(
        EventType.RUN_COMPLETED,
        **common,
        seq_id=4,
        payload={"status": "completed", "duration_ms": 12},
    )


def _valid_spec():
    return {
        "description": "API test",
        "runtime": {
            "type": "langgraph",
            "projectPath": "agents/demo-agent/source",
            "entryPoint": "agent.py",
            "agentVariable": "graph",
        },
        "instructions": {
            "system": "You are a test agent.",
            "task": "Only answer the request.",
        },
        "model": {
            "provider": "openai-compatible",
            "model": "glm-5.1",
            "endpointUrl": "https://model.example.com/v1/chat/completions",
            "credentialRef": "env://MODEL_API_KEY",
            "parameters": {"temperature": 0.2, "maxTokens": 128},
        },
        "capabilities": {"skills": [], "mcpServers": [], "tools": []},
        "execution": {
            "strategy": "direct",
            "maxSteps": 4,
            "timeoutSeconds": 30,
            "retry": {"maxAttempts": 1, "backoffSeconds": 0},
        },
        "context": {
            "maxInputTokens": 4096,
            "reserveOutputTokens": 512,
            "compaction": {"enabled": True, "thresholdRatio": 0.8},
        },
        "security": {
            "toolPolicy": "deny-by-default",
            "allowedPermissions": [],
            "network": {
                "mode": "restricted",
                "allowedHosts": ["model.example.com"],
                "allowPrivateNetwork": False,
            },
        },
        "evaluation": {"suiteRefs": [], "minimumPassRate": 1},
    }


def _wait(client: TestClient, operation_id: str):
    for _ in range(300):
        payload = client.get(f"/api/v1/operations/{operation_id}").json()
        if payload["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not finish")


def test_api_complete_create_build_run_and_deploy_flow(tmp_path: Path):
    cloud = InMemoryCloudGateway()
    service = StudioService(
        tmp_path,
        model_client=FakeModelClient(),
        cloud_gateway=cloud,
        runtime_executor=RuntimeFixture(
            _runtime_events,
            runtime_types=("langgraph",),
        ).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agents",
            json={
                "id": "demo-agent",
                "name": "Demo Agent",
                "description": "created",
                "template": "blank",
            },
        )
        assert created.status_code == 201
        assert created.json()["metadata"]["revision"] == 1

        updated = client.put(
            "/api/v1/agents/demo-agent",
            headers={"If-Match": '"1"'},
            json=_valid_spec(),
        )
        assert updated.status_code == 200
        assert updated.json()["metadata"]["revision"] == 2

        validation = client.post(
            "/api/v1/agents/demo-agent/validations",
            json={"revision": 2, "level": "build"},
        )
        assert validation.json()["valid"] is True

        build_operation = client.post(
            "/api/v1/agents/demo-agent/builds",
            headers={"Idempotency-Key": "demo-agent-r2"},
            json={"revision": 2, "runEvaluation": False},
        )
        assert build_operation.status_code == 202
        completed_build = _wait(client, build_operation.json()["id"])
        assert completed_build["status"] == "SUCCEEDED"
        build_id = completed_build["resourceId"]
        build = client.get(f"/api/v1/builds/{build_id}").json()
        assert build["bundleDigest"].startswith("sha256:")
        manifest = client.get(f"/api/v1/builds/{build_id}/manifest").json()
        assert manifest["bundleDigest"] == build["bundleDigest"]

        run_operation = client.post(
            f"/api/v1/builds/{build_id}/runs",
            headers={"Idempotency-Key": "run-one"},
            json={
                "input": {"role": "user", "content": "say ok"},
                "environment": "local",
                "stream": True,
            },
        )
        completed_run = _wait(client, run_operation.json()["id"])
        assert completed_run["status"] == "SUCCEEDED"
        run = client.get(f"/api/v1/runs/{completed_run['resourceId']}").json()
        assert run["output"] == "AGENTKIT_E2E_OK"
        assert run["usage"]["totalTokens"] == 8
        events = client.get(
            f"/api/v1/runs/{run['id']}/events",
            headers={"Last-Event-ID": "2"},
        )
        assert events.headers["content-type"].startswith("text/event-stream")
        assert "run.completed" in events.text

        deployment_operation = client.post(
            f"/api/v1/builds/{build_id}/deployments",
            headers={"Idempotency-Key": "deploy-one"},
            json={
                "target": {
                    "region": "cn-beijing-6",
                    "environment": "development",
                },
                "binding": {
                    "model": {"primary": "cloud-model://glm-5.1"},
                    "secrets": {"model-api-key": "secret-manager://agentkit/model"},
                },
                "releasePolicy": {"strategy": "rolling", "approval": "none"},
            },
        )
        completed_deployment = _wait(client, deployment_operation.json()["id"])
        assert completed_deployment["status"] == "SUCCEEDED"
        deployment = client.get(f"/api/v1/deployments/{completed_deployment['resourceId']}").json()
        assert deployment["bundleDigest"] == build["bundleDigest"]
        assert len(cloud.uploads) == 1


def test_framework_stream_forwards_created_event_to_the_browser(tmp_path: Path):
    """The generic runtime path must expose session identity before deltas."""
    service = StudioService(
        tmp_path,
        model_client=FakeModelClient(),
        runtime_executor=RuntimeFixture(
            _runtime_events,
            runtime_types=("langgraph",),
        ).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/agents",
            json={"id": "stream-agent", "name": "Stream Agent", "template": "blank"},
        ).status_code == 201
        assert client.put(
            "/api/v1/agents/stream-agent",
            headers={"If-Match": '"1"'},
            json=_valid_spec(),
        ).status_code == 200
        operation = client.post(
            "/api/v1/agents/stream-agent/builds",
            headers={"Idempotency-Key": "framework-stream-build"},
            json={"revision": 2},
        ).json()
        build_id = _wait(client, operation["id"])["resourceId"]

        with client.stream(
            "POST",
            f"/api/v1/builds/{build_id}/run:stream",
            headers={"Idempotency-Key": "framework-stream-run"},
            json={
                "sessionId": "ses-framework-stream",
                "input": {"role": "user", "content": "reply ok"},
                "environment": "local",
                "stream": True,
            },
        ) as response:
            stream = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: run.created" in stream
    assert '"sessionId":"ses-framework-stream"' in stream
    assert "event: message.completed" in stream
    assert "event: run.completed" in stream


def test_api_revision_idempotency_and_validation_errors(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        client.post(
            "/api/v1/agents",
            json={"id": "demo-agent", "name": "Demo", "template": "blank"},
        )

        missing_match = client.put(
            "/api/v1/agents/demo-agent",
            json=_valid_spec(),
        )
        assert missing_match.status_code == 428
        assert missing_match.json()["error"]["code"] == "AGENT_REVISION_REQUIRED"

        invalid = client.post(
            "/api/v1/agents/demo-agent/builds",
            json={"revision": 1},
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        malformed = client.post(
            "/api/v1/agents",
            json={"id": "../escape", "name": "Bad"},
        )
        assert malformed.status_code == 422
        assert malformed.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_api_local_session_origin_host_and_csrf_security(tmp_path: Path):
    app = create_studio_app(
        tmp_path,
        session_token="session-token-that-is-long-enough",
        csrf_token="csrf-token-that-is-long-enough",
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/system/health").status_code == 200
        unauthorized = client.get("/api/v1/system/bootstrap")
        assert unauthorized.status_code == 401

        bad_session = client.post(
            "/api/v1/system/session",
            json={"token": "wrong-token-that-is-long-enough"},
        )
        assert bad_session.status_code == 401
        exchanged = client.post(
            "/api/v1/system/session",
            json={"token": "session-token-that-is-long-enough"},
        )
        assert exchanged.status_code == 200
        assert exchanged.json()["csrfToken"] == "csrf-token-that-is-long-enough"

        no_csrf = client.post(
            "/api/v1/agents",
            json={"id": "demo-agent", "name": "Demo"},
        )
        assert no_csrf.status_code == 403
        credential_without_csrf = client.put(
            "/api/v1/credentials/AGENTKIT_MODEL_API_KEY",
            json={"value": "must-not-be-accepted", "persistence": "session"},
        )
        assert credential_without_csrf.status_code == 403
        credential_with_csrf = client.put(
            "/api/v1/credentials/AGENTKIT_MODEL_API_KEY",
            headers={"X-CSRF-Token": "csrf-token-that-is-long-enough"},
            json={"value": "accepted-session-secret", "persistence": "session"},
        )
        assert credential_with_csrf.status_code == 200
        assert "accepted-session-secret" not in credential_with_csrf.text
        created = client.post(
            "/api/v1/agents",
            headers={"X-CSRF-Token": "csrf-token-that-is-long-enough"},
            json={"id": "demo-agent", "name": "Demo"},
        )
        assert created.status_code == 201

        bad_origin = client.get(
            "/api/v1/system/bootstrap",
            headers={"Origin": "https://attacker.example"},
        )
        assert bad_origin.status_code == 403
        bad_host = client.get(
            "/api/v1/system/health",
            headers={"Host": "attacker.example"},
        )
        assert bad_host.status_code == 403


def test_root_navigation_establishes_current_browser_session(tmp_path: Path):
    app = create_studio_app(
        tmp_path,
        session_token="session-token-that-is-long-enough",
        csrf_token="csrf-token-that-is-long-enough",
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/system/bootstrap").status_code == 401

        root = client.get("/")
        assert root.status_code == 200
        cookie = root.headers["set-cookie"]
        assert "agentkit_studio_session=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie

        bootstrap = client.get("/api/v1/system/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["workspace"]["path"] == str(tmp_path.resolve())


def test_static_studio_shell_is_served(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        response = client.get("/")
        stylesheet = client.get("/static/app.css")
        script = client.get("/static/app.js")

    assert response.status_code == 200
    assert "<title>AgentKit Studio</title>" in response.text
    assert 'id="view-create"' in response.text
    assert 'id="agentTemplatePicker"' in response.text
    assert 'data-template="blank"' in response.text
    assert 'id="agentPrompt"' in response.text
    assert 'id="agentToolList"' in response.text
    assert 'id="agentSkillList"' in response.text
    assert 'id="agentMcpList"' in response.text
    assert 'id="policyTemplate"' in response.text
    assert 'id="generatedSystemPrompt"' in response.text
    assert 'id="view-chat"' in response.text
    assert 'id="invocationCode"' in response.text
    assert 'id="workspaceOverlay"' in response.text
    assert 'id="reconnectWorkspace"' in response.text
    assert 'id="environmentStateLabel"' in response.text
    assert 'id="modelCredentialOverlay"' in response.text
    assert 'id="modelCredentialValue"' in response.text
    assert 'id="configureSelectedModel"' in response.text
    assert 'type="password"' in response.text
    assert stylesheet.status_code == 200
    assert "--accent: #2167d5" in stylesheet.text
    assert "--accent-soft: #eaf3ff" in stylesheet.text
    assert "min-width: 1280px" in stylesheet.text
    assert "--font-size-body: 15px" in stylesheet.text
    assert "font-size: var(--font-size-body)" in stylesheet.text
    assert "font-size: 9px" not in stylesheet.text
    assert "font-size: 10px" not in stylesheet.text
    assert script.status_code == 200
    assert "composeAgent" in script.text
    assert "/agent-templates/${state.wizard.template}:compose" in script.text
    assert "sendChatMessage" in script.text
    assert "saveModelCredential" in script.text
    assert "/credentials/${encodeURIComponent(name)}" in script.text
    assert "openWorkspaceConnection" in script.text
    assert 'api("/workspaces:open"' in script.text
    assert "setRuntimeStatus" in script.text
    assert "reconnectSessionFromHash" in script.text
    assert 'window.addEventListener("hashchange"' in script.text


def test_api_workspace_connection_is_bound_to_daemon_root(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        connected = client.post(
            "/api/v1/workspaces:open",
            json={"path": str(tmp_path)},
        )
        assert connected.status_code == 200
        assert connected.json() == {
            "name": tmp_path.name,
            "path": str(tmp_path.resolve()),
        }

        rejected = client.post(
            "/api/v1/workspaces:open",
            json={"path": str(tmp_path.parent)},
        )
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "WORKSPACE_PATH_FORBIDDEN"


def test_api_session_credential_lifecycle_and_model_connection(tmp_path: Path):
    class CredentialAwareModelClient:
        def __init__(self):
            self.credential_resolver = CredentialResolver()
            self.observed_credential = ""

        async def complete(self, model, **_kwargs):
            self.observed_credential = self.credential_resolver.resolve(
                model.credential_ref
            )
            return ModelResponse(
                content="OK",
                finish_reason="stop",
                usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "OK"},
            )

    model_client = CredentialAwareModelClient()
    service = StudioService(tmp_path, model_client=model_client)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    secret = "webui-session-secret"

    with TestClient(app) as client:
        missing = client.get("/api/v1/credentials/AGENTKIT_MODEL_API_KEY")
        assert missing.json()["configured"] is False
        assert missing.json()["source"] == "missing"

        configured = client.put(
            "/api/v1/credentials/AGENTKIT_MODEL_API_KEY",
            json={"value": secret, "persistence": "session"},
        )
        assert configured.status_code == 200
        assert configured.json()["configured"] is True
        assert configured.json()["source"] == "session"
        assert secret not in configured.text

        resource_id = "model:builtin:glm-5-1:1.0.0"
        connection = client.post(
            f"/api/v1/model-profiles/{quote(resource_id, safe='')}:test"
        )
        assert connection.status_code == 200
        assert connection.json()["ok"] is True
        assert connection.json()["model"] == "glm-5.1"
        assert model_client.observed_credential == secret
        assert secret not in connection.text

        cleared = client.delete(
            "/api/v1/credentials/AGENTKIT_MODEL_API_KEY"
        )
        assert cleared.status_code == 200
        assert cleared.json()["configured"] is False
        assert cleared.json()["source"] == "missing"

    workspace_contents = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert secret not in workspace_contents


def test_api_blank_agent_create_build_and_chat_flow(tmp_path: Path):
    service = StudioService(
        tmp_path,
        model_client=FakeModelClient(),
        runtime_executor=RuntimeFixture(
            _runtime_events,
            runtime_types=("langgraph",),
        ).executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    prompt = (
        "你是一名企业技术支持助手。先识别问题类型，再给出准确、可执行的处理步骤；信息不足时先提问。"
    )

    with TestClient(app) as client:
        templates = client.get("/api/v1/agent-templates")
        assert templates.status_code == 200
        assert templates.json()["items"][0]["id"] == "blank"
        resources = client.get("/api/v1/catalog/resources?limit=200").json()["items"]
        model = next(item for item in resources if item["kind"] == "model")
        read_tool = next(item for item in resources if item["name"] == "read_workspace_file")

        composed = client.post(
            "/api/v1/agent-templates/blank:compose",
            json={
                "prompt": prompt,
                "description": "回答企业技术支持问题",
                "modelProfileId": model["resourceId"],
                "toolResourceIds": [read_tool["resourceId"]],
                "skillResourceIds": [],
                "mcpResourceIds": [],
                "policyTemplate": "loose",
                "executionStrategy": "direct",
                "maxSteps": 8,
                "timeoutSeconds": 120,
            },
        )
        assert composed.status_code == 200
        composition = composed.json()
        assert composition["templateId"] == "blank"
        assert composition["spec"]["instructions"]["system"] == prompt
        assert composition["spec"]["bindings"]["policyTemplate"] == "loose"
        assert composition["spec"]["bindings"]["tools"] == [
            {
                "resourceId": read_tool["resourceId"],
                "enabled": True,
                "approval": None,
                "config": {},
            }
        ]
        assert composition["spec"]["bindings"]["skills"] == []
        assert composition["spec"]["bindings"]["mcpServers"] == []
        composition["spec"]["runtime"] = {
            "type": "langgraph",
            "projectPath": "agents/support-agent/source",
            "entryPoint": "agent.py",
            "agentVariable": "graph",
        }

        created = client.post(
            "/api/v1/agents",
            json={
                "id": "support-agent",
                "name": "技术支持助手",
                "description": composition["spec"]["description"],
                "template": "blank",
                "spec": composition["spec"],
            },
        )
        assert created.status_code == 201
        draft = created.json()
        assert draft["metadata"]["labels"]["agentkit.ksyun.com/template"] == "blank"

        build_operation = client.post(
            "/api/v1/agents/support-agent/builds",
            headers={"Idempotency-Key": "support-agent-r1"},
            json={"revision": 1},
        )
        completed_build = _wait(client, build_operation.json()["id"])
        assert completed_build["status"] == "SUCCEEDED"
        run_operation = client.post(
            f"/api/v1/builds/{completed_build['resourceId']}/runs",
            headers={"Idempotency-Key": "support-agent-chat"},
            json={
                "input": {"role": "user", "content": "如何排查服务启动失败？"},
                "environment": "local",
                "stream": True,
            },
        )
        completed_run = _wait(client, run_operation.json()["id"])
        run = client.get(f"/api/v1/runs/{completed_run['resourceId']}").json()
        assert run["output"] == "AGENTKIT_E2E_OK"


def test_api_research_template_create_build_and_chat_flow(tmp_path: Path):
    class CapturingModelClient:
        def __init__(self):
            self.messages = []

        async def complete(self, *_args, **kwargs):
            self.messages.append(kwargs["messages"])
            return ModelResponse(
                content="RESEARCH_CHAT_OK",
                finish_reason="stop",
                usage=Usage(input_tokens=11, output_tokens=5, total_tokens=16),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "RESEARCH_CHAT_OK"},
            )

    model_client = CapturingModelClient()
    runtime_fixture = RuntimeFixture(
        _runtime_events,
        runtime_types=("langgraph",),
    )
    service = StudioService(
        tmp_path,
        model_client=model_client,
        runtime_executor=runtime_fixture.executor,
    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        templates = client.get("/api/v1/agent-templates")
        assert templates.status_code == 200
        assert [item["id"] for item in templates.json()["items"]] == [
            "blank",
            "research",
        ]

        composed = client.post(
            "/api/v1/agent-templates/research:compose",
            json={
                "goal": "调研企业深度研究 Agent 的产品能力和落地风险",
                "audience": "产品与技术负责人",
                "language": "zh-CN",
                "depth": "deep",
                "outputFormat": "report",
            },
        )
        assert composed.status_code == 200
        composition = composed.json()
        assert composition["templateId"] == "research"
        assert composition["spec"]["bindings"]["skills"]
        composition["spec"]["runtime"] = {
            "type": "langgraph",
            "projectPath": "agents/research-agent/source",
            "entryPoint": "agent.py",
            "agentVariable": "graph",
        }

        created = client.post(
            "/api/v1/agents",
            json={
                "id": "research-agent",
                "name": "Research Agent",
                "description": composition["spec"]["description"],
                "template": "research",
                "spec": composition["spec"],
            },
        )
        assert created.status_code == 201
        draft = created.json()
        assert draft["metadata"]["revision"] == 1
        assert draft["metadata"]["labels"]["agentkit.ksyun.com/template"] == "research"

        build_operation = client.post(
            "/api/v1/agents/research-agent/builds",
            headers={"Idempotency-Key": "research-agent-r1"},
            json={"revision": 1},
        )
        completed_build = _wait(client, build_operation.json()["id"])
        assert completed_build["status"] == "SUCCEEDED"
        build_id = completed_build["resourceId"]

        first_operation = client.post(
            f"/api/v1/builds/{build_id}/runs",
            headers={"Idempotency-Key": "research-chat-one"},
            json={
                "input": {
                    "role": "user",
                    "content": "先给出这个课题的调研计划。",
                },
                "environment": "local",
                "stream": True,
            },
        )
        first_completed = _wait(client, first_operation.json()["id"])
        first_run = client.get(f"/api/v1/runs/{first_completed['resourceId']}").json()
        assert first_run["output"] == "AGENTKIT_E2E_OK"

        second_operation = client.post(
            f"/api/v1/builds/{build_id}/runs",
            headers={"Idempotency-Key": "research-chat-two"},
            json={
                "sessionId": first_run["sessionId"],
                "input": {
                    "role": "user",
                    "content": "继续执行第一阶段。",
                },
                "environment": "local",
                "stream": True,
            },
        )
        second_completed = _wait(client, second_operation.json()["id"])
        second_run = client.get(f"/api/v1/runs/{second_completed['resourceId']}").json()
        assert second_run["sessionId"] == first_run["sessionId"]

        session_runs = client.get(f"/api/v1/runs?sessionId={first_run['sessionId']}").json()[
            "items"
        ]
        assert [item["id"] for item in session_runs] == [
            first_run["id"],
            second_run["id"],
        ]
        conversation = runtime_fixture.start_requests[-1].conversation_preprocessing()
        assert conversation is not None
        assert len(conversation.messages) == 3
        assert "工作原则" in runtime_fixture.start_requests[-1].config[
            "base_instructions"
        ]


def test_api_mcp_probe_returns_discovered_tool_contracts(tmp_path: Path):
    service = StudioService(tmp_path)

    class FakeMCPRuntime:
        async def probe(self, server, *, timeout_seconds):
            return {
                "serverInfo": {"name": server.name, "version": server.version},
                "tools": [
                    {
                        "name": "mcp_echo",
                        "version": server.version,
                        "executor": "mcp",
                        "mcpServer": server.name,
                    }
                ],
                "timeoutSeconds": timeout_seconds,
            }

    service.mcp_runtime = FakeMCPRuntime()
    app = create_studio_app(
        tmp_path,
        service=service,
        security_enabled=False,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/mcp-servers:probe?timeoutSeconds=12",
            json={
                "name": "demo-mcp",
                "version": "1.0.0",
                "transport": "stdio",
                "command": "demo-mcp",
                "args": [],
                "envRefs": {},
            },
        )

    assert response.status_code == 200
    assert response.json()["tools"][0]["mcpServer"] == "demo-mcp"
    assert response.json()["timeoutSeconds"] == 12


def test_api_catalog_binding_policy_and_schema_flow(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        resources = client.get("/api/v1/catalog/resources?limit=100")
        assert resources.status_code == 200
        items = resources.json()["items"]
        model = next(item for item in items if item["kind"] == "model")
        read = next(item for item in items if item["name"] == "read_workspace_file")
        write = next(item for item in items if item["name"] == "write_workspace_file")

        client.post(
            "/api/v1/agents",
            json={"id": "bound-agent", "name": "Bound Agent"},
        )
        bindings = {
            "modelProfileId": model["resourceId"],
            "policyTemplate": "strict",
            "tools": [
                {"resourceId": read["resourceId"]},
                {"resourceId": write["resourceId"]},
            ],
            "mcpServers": [],
            "skills": [],
        }
        updated = client.put(
            "/api/v1/agents/bound-agent/bindings",
            headers={"If-Match": '"1"'},
            json=bindings,
        )
        assert updated.status_code == 200
        saved_bindings = updated.json()["spec"]["bindings"]
        assert saved_bindings["modelProfileId"] == bindings["modelProfileId"]
        assert [item["resourceId"] for item in saved_bindings["tools"]] == [
            item["resourceId"] for item in bindings["tools"]
        ]

        preview = client.post(
            "/api/v1/tool-policies:preview",
            json={"bindings": bindings},
        )
        assert preview.status_code == 200
        approvals = {item["name"]: item["approval"] for item in preview.json()["tools"]}
        assert approvals == {
            "read_workspace_file": "never",
            "write_workspace_file": "always",
        }

        schema = client.post(
            "/api/v1/tool-schemas:validate",
            json={
                "schema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
                "sample": {"query": 42},
            },
        )
        assert schema.status_code == 200
        assert schema.json()["valid"] is False
        assert schema.json()["diagnostics"][0]["path"] == ["query"]

        build = client.post(
            "/api/v1/agents/bound-agent/builds",
            headers={"Idempotency-Key": "bound-agent-r2"},
            json={"revision": 2},
        )
        completed = _wait(client, build.json()["id"])
        assert completed["status"] == "SUCCEEDED"

        stale = client.put(
            "/api/v1/agents/bound-agent/bindings",
            headers={"If-Match": '"1"'},
            json=bindings,
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "AGENT_REVISION_CONFLICT"

        invalid_bindings = {
            **bindings,
            "tools": [{"resourceId": "tool:local:missing:1.0.0"}],
        }
        invalid = client.put(
            "/api/v1/agents/bound-agent/bindings",
            headers={"If-Match": '"2"'},
            json=invalid_bindings,
        )
        assert invalid.status_code == 404
        assert invalid.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_api_persists_mcp_probe_and_imports_skill_zip(tmp_path: Path):
    service = StudioService(tmp_path)

    class FakeMCPRuntime:
        async def probe(self, server, *, timeout_seconds):
            return {
                "serverInfo": {"name": server.name, "version": server.version},
                "tools": [
                    {
                        "name": "mcp_lookup",
                        "version": server.version,
                        "description": "Look up a record",
                        "executor": "mcp",
                        "mcpServer": server.name,
                        "permissions": ["network:external"],
                        "sideEffect": "external",
                        "approval": "always",
                    }
                ],
                "timeoutSeconds": timeout_seconds,
            }

    service.mcp_runtime = FakeMCPRuntime()
    app = create_studio_app(
        tmp_path,
        service=service,
        security_enabled=False,
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/catalog/mcp-servers",
            json={
                "displayName": "Records MCP",
                "description": "Test server",
                "server": {
                    "name": "records-mcp",
                    "version": "1.0.0",
                    "transport": "stdio",
                    "command": "records-mcp",
                    "args": [],
                    "envRefs": {},
                },
            },
        )
        assert created.status_code == 201
        resource_id = created.json()["resourceId"]
        probe = client.post(f"/api/v1/catalog/mcp-servers/{resource_id}:probe?timeoutSeconds=7")
        assert probe.status_code == 200
        assert probe.json()["health"]["toolCount"] == 1
        fetched = client.get(f"/api/v1/catalog/resources/{resource_id}").json()
        assert fetched["contract"]["discoveredTools"][0]["name"] == "mcp_lookup"

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as skill_zip:
            skill_zip.writestr(
                "SKILL.md",
                "---\n"
                "name: API Skill\n"
                "description: Imported through API\n"
                "version: 1.0.0\n"
                "---\n"
                "Follow the contract.\n",
            )
        imported = client.post(
            "/api/v1/catalog/skills:import",
            files={
                "file": (
                    "api-skill.zip",
                    archive.getvalue(),
                    "application/zip",
                )
            },
        )
        assert imported.status_code == 201
        assert imported.json()["name"] == "api-skill"
