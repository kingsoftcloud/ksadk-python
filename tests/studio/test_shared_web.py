from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import RunRecord, RunStatus, Usage
from ksadk.studio.model_client import ModelResponse
from ksadk.studio.service import StudioService
from ksadk.studio.shared_web import StudioSharedWebBridge
from ksadk.studio.templates import default_agent_spec


class RecordingModelClient:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def complete(self, _model, *, messages, **_kwargs):
        self.messages.append(messages)
        turn = len(self.messages)
        content = f"共享会话回复 {turn}"
        return ModelResponse(
            content=content,
            finish_reason="stop",
            usage=Usage(input_tokens=5, output_tokens=3, total_tokens=8),
            tool_calls=[],
            raw_message={"role": "assistant", "content": content},
        )


def _valid_spec():
    return {
        "description": "Shared Web test",
        "instructions": {
            "system": "You are a shared Web test agent.",
            "task": "Answer the request.",
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


def _shared_static(tmp_path: Path) -> Path:
    root = tmp_path / "shared-static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><html><body><div id="root">shared chat</div></body></html>',
        encoding="utf-8",
    )
    (root / "assets" / "app.js").write_text("export {};", encoding="utf-8")
    return root


def _create_agent(client: TestClient) -> None:
    created = client.post(
        "/api/v1/agents",
        json={"id": "demo-agent", "name": "Demo Agent", "template": "blank"},
    )
    assert created.status_code == 201
    updated = client.put(
        "/api/v1/agents/demo-agent",
        headers={"If-Match": '"1"'},
        json=_valid_spec(),
    )
    assert updated.status_code == 200


def _run_shared_chat(
    client: TestClient,
    *,
    session_id: str,
    invocation_id: str,
    text: str,
) -> str:
    with client.stream(
        "POST",
        "/agentengine/api/v1/RunAgent",
        json={
            "AgentId": "demo-agent",
            "SessionId": session_id,
            "InvocationId": invocation_id,
            "ApiFormat": "responses",
            "ResponsesInput": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            ],
        },
    ) as response:
        assert response.status_code == 200
        return "".join(response.iter_text())


def test_shared_chat_static_entry_and_selected_agent_bootstrap(
    tmp_path: Path,
    monkeypatch,
):
    static_root = _shared_static(tmp_path)
    monkeypatch.setattr(
        "ksadk.studio.api.shared_web_static_root",
        lambda: static_root,
    )
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        _create_agent(client)
        page = client.get("/chat/?agentId=demo-agent")
        assert page.status_code == 200
        assert "shared chat" in page.text
        assert 'data-agentkit-studio-chat="workbench"' in page.text
        assert 'href="/static/shared-chat.css"' in page.text
        assert page.headers["x-frame-options"] == "SAMEORIGIN"
        assert client.cookies.get("agentkit_studio_chat_agent") == "demo-agent"

        theme = client.get("/static/shared-chat.css")
        assert theme.status_code == 200
        assert '--ak-chat-canvas: #fbfbfa' in theme.text
        assert 'data-agentkit-studio-chat="workbench"' in theme.text

        bootstrap = client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={},
        ).json()["Data"]
        assert bootstrap["Agent"] == {
            "AgentId": "demo-agent",
            "Name": "Demo Agent",
            "Framework": "agentkit",
        }
        hosted = bootstrap["Capabilities"]["HostedChat"]
        assert hosted["Enabled"] is True
        assert hosted["PreferredTransport"] == "responses"
        assert hosted["Transports"][0]["Capabilities"]["A2UI"] is False

        system = client.get("/api/v1/system/bootstrap").json()
        assert system["features"]["sharedChat"] is True


def test_shared_chat_resolves_bound_model_profile(tmp_path: Path):
    service = StudioService(tmp_path)
    model_profile = service.catalog.list(kind="model")[0]
    spec = default_agent_spec("blank")
    spec.bindings.model_profile_id = model_profile.resource_id
    service.create_agent(
        agent_id="bound-model-agent",
        name="Bound Model Agent",
        spec=spec,
    )

    bridge = StudioSharedWebBridge(service)
    bootstrap = bridge.bootstrap("bound-model-agent")
    models = bridge.list_models("bound-model-agent")

    assert bootstrap["Model"]["id"] == "glm-5.1"
    assert bootstrap["Model"]["display_name"] == "GLM-5.1"
    assert models["Current"] == "glm-5.1"
    assert models["Models"] == [bootstrap["Model"]]


def test_shared_chat_keeps_unbound_agent_model_explicit(tmp_path: Path):
    service = StudioService(tmp_path)
    service.create_agent(agent_id="unbound-agent", name="Unbound Agent")

    model = StudioSharedWebBridge(service).bootstrap("unbound-agent")["Model"]

    assert model["id"] == "unconfigured-model"
    assert model["display_name"] == "未配置模型"


def test_shared_chat_explains_unbound_model_profile(tmp_path: Path, monkeypatch):
    static_root = _shared_static(tmp_path)
    monkeypatch.setattr(
        "ksadk.studio.api.shared_web_static_root",
        lambda: static_root,
    )
    service = StudioService(tmp_path)
    service.create_agent(agent_id="unbound-agent", name="Unbound Agent")
    app = create_studio_app(
        tmp_path,
        service=service,
        security_enabled=False,
    )

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "unbound-agent",
                "SessionId": "ses_unbound",
                "InvocationId": "run_unbound",
                "ResponsesInput": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "你好"}],
                    }
                ],
            },
        ) as response:
            stream = "".join(response.iter_text())

    assert "当前 Agent 未绑定 Model Profile" in stream
    assert "API Key 只提供访问凭证" in stream


def test_shared_chat_runs_and_replays_two_turn_session(tmp_path: Path, monkeypatch):
    static_root = _shared_static(tmp_path)
    monkeypatch.setattr(
        "ksadk.studio.api.shared_web_static_root",
        lambda: static_root,
    )
    model_client = RecordingModelClient()
    service = StudioService(tmp_path, model_client=model_client)
    app = create_studio_app(
        tmp_path,
        service=service,
        security_enabled=False,
    )

    with TestClient(app) as client:
        _create_agent(client)
        first_stream = _run_shared_chat(
            client,
            session_id="ses_shared",
            invocation_id="invocation-one",
            text="第一轮",
        )
        assert "response.output_text.delta" in first_stream
        assert "共享会话回复 1" in first_stream
        assert "response.completed" in first_stream

        second_stream = _run_shared_chat(
            client,
            session_id="ses_shared",
            invocation_id="invocation-two",
            text="第二轮",
        )
        assert "共享会话回复 2" in second_stream
        assert len(model_client.messages) == 2
        assert [message["role"] for message in model_client.messages[-1]] == [
            "system",
            "user",
            "assistant",
            "user",
        ]

        sessions = client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "Page": 1, "PageSize": 30},
        ).json()["Data"]
        assert sessions["Total"] == 1
        assert sessions["Sessions"][0]["SessionId"] == "ses_shared"
        assert sessions["Sessions"][0]["TokenUsage"]["turns"] == 2

        messages = client.post(
            "/agentengine/api/v1/ListSessionMessages",
            json={"SessionId": "ses_shared", "Limit": 50},
        ).json()["Data"]
        assert messages["LatestSeqId"] == 4
        assert [message["Role"] for message in messages["Messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert messages["Messages"][-1]["Content"]["text"] == "共享会话回复 2"

        deleted = client.post(
            "/agentengine/api/v1/DeleteSession",
            json={"SessionId": "ses_shared"},
        )
        assert deleted.status_code == 200
        sessions_after_delete = client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent"},
        ).json()["Data"]
        assert sessions_after_delete["Total"] == 0


def test_shared_chat_history_preserves_a2ui_activity(tmp_path: Path):
    service = StudioService(tmp_path)
    service.create_agent(agent_id="demo-agent", name="Demo Agent")
    record = RunRecord(
        id="run_a2ui",
        build_id="build_a2ui",
        agent_id="demo-agent",
        session_id="ses_a2ui",
        trace_id="trace_a2ui",
        status=RunStatus.COMPLETED,
        input="打开配置面板",
        output="请完成配置。",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    service.event_store.create(record)
    operations = [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": "surface-config",
                "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
            },
        }
    ]
    service.event_store.append(
        record.id,
        "a2ui.surface.created",
        {
            "surfaceId": "surface-config",
            "a2ui_operations": operations,
        },
    )

    messages = StudioSharedWebBridge(service).list_messages("ses_a2ui")
    assistant = messages["Messages"][1]
    assert assistant["Activities"] == [
        {
            "SeqId": 1,
            "Type": "a2ui.surface.created",
            "MessageId": "run_a2ui:assistant",
            "SurfaceId": "surface-config",
            "Content": {"a2ui_operations": operations},
        }
    ]


def test_shared_chat_api_requires_local_studio_session(tmp_path: Path, monkeypatch):
    static_root = _shared_static(tmp_path)
    monkeypatch.setattr(
        "ksadk.studio.api.shared_web_static_root",
        lambda: static_root,
    )
    service = StudioService(tmp_path)
    service.create_agent(agent_id="demo-agent", name="Demo Agent")
    app = create_studio_app(
        tmp_path,
        service=service,
        session_token="local-session-token-that-is-long-enough",
        csrf_token="csrf-token-that-is-long-enough",
        security_enabled=True,
    )

    with TestClient(app) as anonymous:
        denied = anonymous.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )
        assert denied.status_code == 401

    with TestClient(app) as client:
        client.get("/")
        allowed = client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["Data"]["Agent"]["AgentId"] == "demo-agent"
