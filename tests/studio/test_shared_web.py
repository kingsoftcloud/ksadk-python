from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunStarted,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.plugins.contracts import PluginManifest
from ksadk.studio.api import create_studio_app
from ksadk.studio.codex_provider_build import CODEX_PROVIDER_REF
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


def _codex_provider_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": "io.ksadk.codex-provider", "version": "1.0.0"},
            "spec": {
                "domain": "runtime-native",
                "runtime": "process",
                "entrypoint": "deepseek-harness:profile-agent-provider",
                "provides": [
                    {
                        "definition": "agent.provider/v1",
                        "slot": "agent.execution",
                        "mode": "unique",
                    }
                ],
                "permissions": ["process:host-user"],
                "isolation": "sidecar",
                "compatibility": {
                    "kernelApi": ">=1,<2",
                    "runtimeProtocols": ["AgentControlChannel/v1"],
                },
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "runtime-native",
                    "digest": "sha256:" + "1" * 64,
                },
            },
        }
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
        event_id=f"{handle.run_id}:e1",
        seq=1,
        status="running",
        source=source,
        **common,
    )
    yield ItemUpdated(
        event_id=f"{handle.run_id}:e2",
        seq=2,
        item_id="msg-1",
        item_kind="message",
        op="append",
        update=TextContent(part_id="text-0", text=text),
        source=source,
        **common,
    )
    yield ItemCompleted(
        event_id=f"{handle.run_id}:e3",
        seq=3,
        item_id="msg-1",
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text=text),)),
        source=source,
        **common,
    )
    yield RunCompleted(
        event_id=f"{handle.run_id}:e4",
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


def test_shared_chat_run_output_accepts_serialized_status():
    record = RunRecord(
        id="run_status_only",
        build_id="build_status_only",
        agent_id="demo-agent",
        session_id="ses_status_only",
        trace_id="trace_status_only",
        status=RunStatus.RUNNING,
        input="hello",
    )

    assert StudioSharedWebBridge._run_output(record) == "运行状态：RUNNING"


def test_shared_chat_empty_session_survives_session_reload(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        _create_agent(client)
        created_response = client.post(
            "/agentengine/api/v1/CreateSession",
            json={"AgentId": "demo-agent"},
        )
        assert created_response.status_code == 200
        created = created_response.json()["Data"]["Session"]

    reloaded_app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(reloaded_app) as client:
        sessions = client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "Page": 1, "PageSize": 30},
        ).json()["Data"]
        assert sessions["Total"] == 1
        assert sessions["Sessions"][0]["SessionId"] == created["SessionId"]
        assert sessions["Sessions"][0]["Title"] == "新会话"

        restored = client.post(
            "/agentengine/api/v1/GetSession",
            json={"SessionId": created["SessionId"]},
        )
        assert restored.status_code == 200
        assert restored.json()["Data"]["Session"]["SessionId"] == created["SessionId"]

        messages = client.post(
            "/agentengine/api/v1/ListSessionMessages",
            json={"SessionId": created["SessionId"], "Limit": 50},
        ).json()["Data"]
        assert messages["Messages"] == []

        deleted = client.post(
            "/agentengine/api/v1/DeleteSession",
            json={"SessionId": created["SessionId"]},
        )
        assert deleted.status_code == 200
        sessions_after_delete = client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent"},
        ).json()["Data"]
        assert sessions_after_delete["Total"] == 0


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


@pytest.mark.asyncio
async def test_shared_chat_history_preserves_a2ui_activity(tmp_path: Path):
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

    messages = await StudioSharedWebBridge(service).list_messages("ses_a2ui")
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

    with TestClient(app) as client:
        denied = client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )
        assert denied.status_code == 401

        client.get("/")
        allowed = client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["Data"]["Agent"]["AgentId"] == "demo-agent"
        assert allowed.json()["Data"]["Capabilities"]["InteractionV1"] is True


def test_shared_chat_submit_interaction_returns_canonical_receipt(tmp_path: Path):
    service = StudioService(tmp_path)
    service.create_agent(agent_id="demo-agent", name="Demo Agent")
    submitted: list[dict] = []

    async def submit_interaction(run_id: str, interaction_id: str, **kwargs):
        submitted.append({"run_id": run_id, "interaction_id": interaction_id, **kwargs})
        return {"resolutionEventId": 8, "eventId": 9}

    service.run_service.submit_interaction = submit_interaction  # type: ignore[method-assign]
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/agentengine/api/v1/SubmitInteraction",
            json={
                "AgentId": "demo-agent",
                "SessionId": "session-1",
                "RunId": "run-1",
                "InteractionId": "approval-1",
                "ExpectedRevision": 1,
                "Action": "approve",
                "Response": {"decision": "approve"},
                "IdempotencyKey": "interaction:approval-1:revision-1",
            },
        )

    assert response.status_code == 200
    assert response.json()["Data"] == {
        "schema_version": 1,
        "command_id": "9",
        "status": "accepted",
        "message_id": None,
        "run_id": "run-1",
        "accepted_seq": 9,
    }
    assert submitted == [
        {
            "run_id": "run-1",
            "interaction_id": "approval-1",
            "name": "approve",
            "data": {"decision": "approve"},
            "expected_revision": 1,
            "idempotency_key": "interaction:approval-1:revision-1",
        }
    ]


def test_shared_response_approval_item_uses_interaction_identity():
    event_name, payload = StudioSharedWebBridge._response_item_event(
        "approval.requested",
        {
            "approvalId": "approval-1",
            "callId": "call-1",
            "runId": "run-1",
            "kind": "command",
            "detail": {"command": "echo safe"},
        },
        {},
    )

    assert event_name == "response.output_item.added"
    assert payload["item"] == {
        "id": "approval-1",
        "call_id": "call-1",
        "type": "mcp_approval_request",
        "name": "command",
        "arguments": '{"command": "echo safe"}',
        "run_id": "run-1",
        "status": "in_progress",
    }


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.WAITING_INPUT])
def test_session_active_run_survives_newer_failed_turn(status):
    bridge = StudioSharedWebBridge.__new__(StudioSharedWebBridge)
    active = RunRecord(
        id="active",
        build_id="b",
        agent_id="a",
        session_id="s",
        trace_id="t1",
        status=status,
        input="first",
        started_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    failed = RunRecord(
        id="failed",
        build_id="b",
        agent_id="a",
        session_id="s",
        trace_id="t2",
        status=RunStatus.FAILED,
        input="second",
        started_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
    )
    result = bridge._session_record([active, failed])
    assert result["ActiveInvocationId"] == "active"
    assert result["ActiveRunStatus"] == "running"


@pytest.mark.asyncio
async def test_run_subscription_waits_through_user_input_and_replays_terminal(tmp_path):
    studio = StudioService(tmp_path)
    bridge = StudioSharedWebBridge(studio)
    record = RunRecord(
        id="waiting",
        build_id="b",
        agent_id="a",
        session_id="s",
        trace_id="t",
        input="ask",
        status=RunStatus.WAITING_INPUT,
    )
    studio.event_store.create(record)
    studio.event_store.append(record.id, "a2ui.interaction", {"interactionId": "q"})
    stream = bridge.subscribe_run_events("s", record.id, after_seq_id=0)
    first = await anext(stream)
    assert "a2ui.interaction" in first
    # A waiting request is live; subscription must not emit [DONE].
    heartbeat = await anext(stream)
    assert "[DONE]" not in heartbeat
    assert "ping" in heartbeat
    record.status = RunStatus.COMPLETED
    studio.event_store.save(record)
    studio.event_store.append(record.id, "run.completed", {})
    terminal = await anext(stream)
    assert "run.completed" in terminal
    assert "[DONE]" in await anext(stream)
    await stream.aclose()
    await studio.aclose()


@pytest.mark.asyncio
async def test_history_has_one_activity_per_surface_snapshot(tmp_path):
    studio = StudioService(tmp_path)
    bridge = StudioSharedWebBridge(studio)
    record = RunRecord(
        id="r", build_id="b", agent_id="a", session_id="s", trace_id="t", input="ask"
    )
    studio.event_store.create(record)
    operations = [{"createSurface": {"surfaceId": "input-q"}}]
    for kind in ["a2ui.surface.begin", "a2ui.surface.end"]:
        studio.event_store.append("r", kind, {"surfaceId": "input-q", "a2uiOperations": operations})
    activities = await bridge._run_activities(record)
    assert len(activities) == 1
    assert activities[0]["Content"]["a2ui_operations"] == operations
    await studio.aclose()


def test_model_input_budget_is_not_reported_as_model_window():
    from types import SimpleNamespace

    draft = SimpleNamespace(spec=SimpleNamespace(context=SimpleNamespace(max_input_tokens=32000)))
    spec = ModelSpec.model_validate(_valid_spec()["model"])
    descriptor = StudioSharedWebBridge._model_descriptor_from_spec(draft, spec)
    assert descriptor["context_window_tokens"] is None
    assert descriptor["input_budget_tokens"] == 32000


@pytest.mark.asyncio
async def test_compaction_rejects_active_and_foreign_sessions(tmp_path):
    from types import SimpleNamespace

    from ksadk.studio.errors import StudioError

    store = SimpleNamespace(
        list_runs=lambda **kwargs: (
            [
                SimpleNamespace(
                    runtime_type="codex",
                    status=RunStatus.WAITING_INPUT,
                )
            ]
            if kwargs["agent_id"] == "owner"
            else []
        )
    )
    bridge = StudioSharedWebBridge(
        SimpleNamespace(
            event_store=store,
            run_service=SimpleNamespace(_active_sessions=set()),
            _require_direct_session=lambda _session_id: None,
        )
    )
    with pytest.raises(StudioError) as active:
        await bridge.compact_session("owner", "s")
    assert active.value.code == "SESSION_RUN_ACTIVE"
    with pytest.raises(StudioError) as foreign:
        await bridge.compact_session("other", "s")
    assert foreign.value.status_code == 404


def test_shared_history_uses_public_run_identity_without_mutating_native_event():
    native = {"run_id": "native-run", "event_type": "item.completed", "item_id": "answer"}
    persisted = {"runtimeEvent": native, "text": "answer"}
    projected = StudioSharedWebBridge._shared_event_content(persisted, "studio-run")
    assert projected["runtimeEvent"]["run_id"] == "studio-run"
    assert native["run_id"] == "native-run"
    assert projected["runtimeEvent"]["item_id"] == "answer"


def test_shared_history_keeps_resumable_interruption_pending():
    pending = {"runtimeEvent": {"interaction_id": "question-1"}}
    assert StudioSharedWebBridge._shared_event_type("run.interrupted", pending) == "run.waiting"
    assert StudioSharedWebBridge._shared_event_type("run.interrupted", {}) == "run.interrupted"
    assert StudioSharedWebBridge._shared_event_type("run.cancelled", pending) == "run.cancelled"


@pytest.mark.asyncio
async def test_old_interaction_receipt_replay_preserves_new_pending_form(tmp_path):
    import hashlib
    import json

    studio = StudioService(tmp_path)
    record = RunRecord(
        id="run_replay", build_id="b", agent_id="a", session_id="s", trace_id="t",
        input="ask", status=RunStatus.WAITING_INPUT,
    )
    studio.event_store.create(record)
    digest = hashlib.sha256(json.dumps(
        {"name": "approve", "data": {}}, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    receipt = {"interactionId": "old-approval", "revision": 2}
    studio.event_store.append("run_replay", "a2ui.action", {
        "interactionId": "old-approval", "revision": 2, "idempotencyKey": "old-key",
        "requestDigest": digest, "receipt": receipt,
    })
    studio.event_store.append("run_replay", "a2ui.interaction", {
        "interactionId": "new-form", "revision": 1, "kind": "form",
    })
    studio.run_service._waiting_modes["run_replay"] = "resume"
    try:
        result = await studio.run_service.submit_interaction(
            "run_replay", "old-approval", name="approve", data={},
            expected_revision=1, idempotency_key="old-key",
        )
        assert result == receipt
        assert studio.event_store.get("run_replay").status == RunStatus.WAITING_INPUT
        assert studio.run_service._waiting_modes["run_replay"] == "resume"
        history = await StudioSharedWebBridge(studio).list_messages("s")
        assert history["Messages"][-1]["Content"]["text"] == ""
    finally:
        await studio.aclose()


@pytest.mark.parametrize("legacy_route", [False, True])
def test_cold_chat_model_catalog_reports_provider_window(tmp_path, monkeypatch, legacy_route):
    from ksadk.studio.contracts import AgentSpec, Instructions, RuntimeRef

    async def catalog(**kwargs):
        return [{"id": "deepseek-v4-flash", "context_window_tokens": 1_024_000}]

    monkeypatch.setattr("ksadk.studio.resource_catalog.fetch_provider_model_catalog", catalog)
    studio = StudioService(
        tmp_path, codex_runtime_inspector=lambda runtime: ("0.8.4", "0.147.0", "codex-cli 0.147.0"),
    )
    studio.create_studio_agent(
        agent_id="cold-model", name="Cold Model",
        spec=AgentSpec(
            instructions=Instructions(system="Answer the user."),
            runtime=RuntimeRef(type="codex", version="0.147.0"),
            model=ModelSpec(
                model="deepseek-v4-flash",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
        ),
    )
    assert not studio.catalog._provider_models
    with TestClient(create_studio_app(tmp_path, service=studio, security_enabled=False)) as client:
        response = (client.get("/api/v1/agents/cold-model/models") if legacy_route else client.post(
            "/agentengine/api/v1/ListAgentModels", json={"AgentId": "cold-model"},
        ))
        assert response.status_code == 200
        data = response.json() if legacy_route else response.json()["Data"]
        assert data["Models"][0]["context_window_tokens"] == 1_024_000


def test_cold_build_discovers_provider_model_before_submit(tmp_path, monkeypatch):
    from ksadk.studio.contracts import (
        AgentBindings,
        AgentSpec,
        Instructions,
        NetworkPolicy,
        RuntimeRef,
        SecuritySpec,
    )

    async def catalog(**kwargs):
        return [{"id": "deepseek-v4-flash", "context_window_tokens": 1_024_000}]

    monkeypatch.setattr("ksadk.studio.resource_catalog.fetch_provider_model_catalog", catalog)
    studio = StudioService(
        tmp_path,
        codex_runtime_inspector=lambda runtime: (
            "0.8.4",
            "0.147.0",
            "codex-cli 0.147.0",
        ),
    )
    asyncio.run(
        studio.catalog.discover_provider_models(
            api_base="https://model.example.com/v1",
            api_key=None,
            current_model="deepseek-v4-flash",
        )
    )
    studio.create_studio_agent(
        agent_id="cold-build",
        name="Cold Build",
        spec=AgentSpec(
            instructions=Instructions(system="Answer the user."),
            runtime=RuntimeRef(type="codex", version="0.147.0"),
            bindings=AgentBindings(
                model_profile_id="model:provider:deepseek-v4-flash:live",
                model_profile_ids=["model:provider:deepseek-v4-flash:live"],
            ),
            security=SecuritySpec(
                allowed_permissions=["process:host-user"],
                network=NetworkPolicy(allowed_hosts=["model.example.com"])
            ),
        ),
    )
    studio.catalog._provider_models.clear()
    assert not studio.catalog._provider_models

    with TestClient(create_studio_app(tmp_path, service=studio, security_enabled=False)) as client:
        provider_manifest = _codex_provider_manifest()
        provider_manifests = {CODEX_PROVIDER_REF: provider_manifest}
        studio._active_provider_manifests.update(provider_manifests)
        studio.plugin_compositions.replace_provider_registrations(provider_manifests)
        submitted = client.post(
            "/api/v1/agents/cold-build/builds",
            headers={"Idempotency-Key": "cold-build-r1"},
            json={"revision": 1},
        )
        assert submitted.status_code == 202
        operation_id = submitted.json()["id"]
        for _ in range(200):
            operation = client.get(f"/api/v1/operations/{operation_id}").json()
            if operation["status"] in {"SUCCEEDED", "FAILED"}:
                break
        assert operation["status"] == "SUCCEEDED", operation
