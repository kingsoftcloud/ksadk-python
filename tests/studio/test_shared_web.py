from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import ModelSpec, RunRecord, RunStatus, Usage
from ksadk.studio.model_client import ModelResponse
from ksadk.studio.service import StudioService
from ksadk.studio.shared_web import StudioSharedWebBridge
from ksadk.studio.templates import default_agent_spec
from tests.studio.runtime_adapter_fixtures import RuntimeFixture


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
        "runtime": {
            "type": "langgraph",
            "projectPath": "agents/demo-agent/source",
            "entryPoint": "agent.py",
            "agentVariable": "graph",
        },
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


async def _shared_runtime_events(request, handle):
    turn = int(handle.run_id.rsplit("-", 1)[-1])
    text = f"共享会话回复 {turn}"
    common = {
        "schema_version": 2,
        "timestamp": 1.0,
        "run_id": handle.run_id,
        "scope_id": f"scope-{handle.run_id}",
    }
    source = SourceRef(framework="langgraph")
    yield RunStarted(
        event_id="e1",
        seq=1,
        status="running",
        source=source,
        **common,
    )
    yield ItemUpdated(
        event_id="e2",
        seq=2,
        item_id="msg-1",
        item_kind="message",
        op="append",
        update=TextContent(part_id="text-0", text=text),
        source=source,
        **common,
    )
    yield ItemCompleted(
        event_id="e3",
        seq=3,
        item_id="msg-1",
        item_kind="message",
        snapshot=ContentSnapshot(
            parts=(TextContent(part_id="text-0", text=text),)
        ),
        source=source,
        **common,
    )
    yield RunCompleted(
        event_id="e4",
        seq=4,
        status="completed",
        output_refs=(
            OutputRef(
                scope_id=common["scope_id"],
                item_id="msg-1",
                part_id="text-0",
            ),
        ),
        source=SourceRef(
            framework="langgraph",
            metadata={"duration_ms": 9},
        ),
        **common,
    )


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


def test_react_chat_has_one_root_entry_and_no_standalone_chat(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        _create_agent(client)
        root = client.get("/")
        standalone = client.get("/chat?agentId=demo-agent")
        shared_theme = client.get("/static/shared-chat.css")

        system = client.get("/api/v1/system/bootstrap").json()
        route_paths = {getattr(route, "path", "") for route in app.routes}

        assert root.status_code == 200
        assert standalone.status_code == 404
        assert standalone.headers["x-frame-options"] == "DENY"
        assert shared_theme.status_code == 404
        assert "/chat" not in route_paths
        assert "/chat/" not in route_paths
        assert system["features"]["reactChat"] is True
        assert "sharedChat" not in system["features"]


def test_shared_chat_resolves_bound_model_profile(tmp_path: Path):
    service = StudioService(tmp_path)
    service.catalog.create_model_profile(
        name="glm-5.1",
        display_name="GLM-5.1",
        version="1.0.0",
        description="",
        spec=ModelSpec(
            provider="openai-compatible",
            model="glm-5.1",
            endpoint_url="https://api.openai.com/v1/chat/completions",
            credential_ref="env://AGENTKIT_MODEL_API_KEY",
        ),
    )
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


def test_shared_chat_explains_unbound_model_profile(tmp_path: Path):
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


def test_shared_chat_runs_and_replays_two_turn_session(tmp_path: Path):
    model_client = RecordingModelClient()
    runtime_fixture = RuntimeFixture(
        _shared_runtime_events,
        runtime_types=("langgraph",),
    )
    service = StudioService(
        tmp_path,
        model_client=model_client,
        runtime_executor=runtime_fixture.executor,
    )
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
        conversation = runtime_fixture.start_requests[-1].conversation_preprocessing()
        assert conversation is not None
        assert [message["role"] for message in conversation.messages] == [
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


def test_shared_chat_api_requires_local_studio_session(tmp_path: Path):
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
