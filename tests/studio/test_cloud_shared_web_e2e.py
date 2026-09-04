"""E2E: cloud agents over /agentengine/api/v1.

Deterministic fake gateway returns a scripted cloud-chat session + SSE stream;
the test drives the *real* shared-web bridge (cloud_shared_web + api.py) so the
full headless pipeline (bootstrap → sessions → stream frames) is verified
without a live pre-prod pod.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.cloud import AccountCloudAgentReference, CloudDeploymentGateway
from ksadk.studio.service import StudioService

CLOUD_AGENT_ID = "ar-20260901184831-bd084c1c"


class _ScriptedCloudGateway(CloudDeploymentGateway):
    """Minimal cloud-chat surface for E2E; everything else is a stub."""

    def __init__(self, stream_frames: list[str] | None = None) -> None:
        self.last_message_query: dict[str, Any] = {}
        self.last_event_query: dict[str, Any] = {}
        self.stream_frames = stream_frames or [
            'event: response.reasoning.delta\ndata: {"delta": "想一想"}\n\n',
            'event: response.output_text.delta\ndata: {"delta": "你好，E2E"}\n\n',
            'data: {"object": "response", "status": "completed", '
            '"output_text": "你好，E2E", "session_id": "{session_id}"}\n\n',
        ]

    async def get_account_agent(self, agent_id: str) -> dict[str, Any]:
        return {
            "agentId": agent_id,
            "name": "0611agent-e2e",
            "framework": "langgraph",
            "runtimeType": "langgraph",
            "chatTransport": "studio-session-events",
            "chatRoutingReason": "studio-compatible-framework",
            "endpoint": "https://example.test",
            "status": "RUNNING",
        }

    async def list_account_agents(self, *, page: int = 1, size: int = 100) -> dict[str, Any]:
        return {
            "items": [
                {
                    "agentId": CLOUD_AGENT_ID,
                    "name": "0611agent-e2e",
                    "framework": "langgraph",
                    "runtimeType": "langgraph",
                    "chatTransport": "studio-session-events",
                    "chatRoutingReason": "studio-compatible-framework",
                    "endpoint": "https://example.test",
                    "status": "RUNNING",
                }
            ],
            "total": 1,
        }

    async def get_account_agent_info(self, agent_id: str) -> dict[str, Any]:
        return {
            "agentId": agent_id,
            "name": "0611agent-e2e",
            "framework": "langgraph",
            "runtimeType": "langgraph",
            "chatTransport": "studio-session-events",
            "chatRoutingReason": "studio-compatible-framework",
            "endpoint": "https://example.test",
            "status": "RUNNING",
        }

    async def list_deployment_chat_sessions(
        self, deployment: AccountCloudAgentReference, *, page: int = 1, size: int = 50
    ) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "session_id": "sess-e2e-1",
                    "title": "e2e 会话",
                    "updated_at": "2026-09-03T00:00:00Z",
                    "active_run_status": "completed",
                }
            ],
            "total": 1,
        }

    async def create_deployment_chat_session(
        self, deployment: AccountCloudAgentReference
    ) -> dict[str, Any]:
        return {"session": {"session_id": "sess-e2e-new", "title": "新会话"}}

    async def list_deployment_chat_messages(
        self, deployment: AccountCloudAgentReference, *, session_id: str, **query: Any
    ) -> dict[str, Any]:
        self.last_message_query = query
        return {
            "messages": [
                {
                    "message_id": "m1",
                    "role": "user",
                    "content": "你好",
                    "timestamp": "2026-09-03T00:00:00Z",
                    "seq_id": 1,
                }
            ],
            "latest_seq_id": 1,
            "has_more": True,
            "next_cursor": 1,
        }

    async def list_deployment_chat_events(
        self, deployment: AccountCloudAgentReference, *, session_id: str, **query: Any
    ) -> dict[str, Any]:
        self.last_event_query = query
        return {
            "events": [
                {
                    "SeqId": 1,
                    "EventId": "evt-1",
                    "EventType": "user_message",
                    "InvocationId": "run-1",
                    "Content": {
                        "runtime_event": {"event_type": "user_message", "run_id": "run-1", "seq": 1}
                    },
                    "Timestamp": "2026-09-03T00:00:00Z",
                }
            ],
            "total": 1,
            "offset": query.get("offset") or 0,
            "limit": query.get("limit") or 200,
        }

    async def list_deployment_chat_models(
        self, deployment: AccountCloudAgentReference
    ) -> dict[str, Any]:
        return {
            "models": [{"id": "deepseek-v4-flash", "display_name": "deepseek-v4-flash"}],
            "current": "deepseek-v4-flash",
        }

    async def stream_deployment_chat_message(
        self, deployment: AccountCloudAgentReference, *, session_id: str, content: Any, **_: Any
    ) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for frame in self.stream_frames:
                yield frame.replace("{session_id}", session_id).encode()

        # The real gateway returns an awaitable whose result is an async
        # iterator; match that contract (cloud.py awaits sender(...)).
        return _gen()

    async def submit_deployment_chat_interaction(
        self, deployment: AccountCloudAgentReference, **kwargs: Any
    ) -> dict[str, Any]:
        return {"Receipt": {"Status": "applied", **kwargs}}

    async def delete_deployment_chat_session(
        self, deployment: AccountCloudAgentReference, *, session_id: str
    ) -> bool:
        return True


def _client(
    tmp_path: Path,
    gateway: _ScriptedCloudGateway | None = None,
) -> TestClient:
    studio = StudioService(tmp_path)
    # create_studio_app constructs StudioSharedWebBridge from studio.cloud's
    # (usually unavailable) gateway.  Patch it to the scripted E2E gateway so
    # the cloud branch of shared_chat_action drives real bridge code.
    # Assign, don't patch: the bridge holds studio.cloud and reads .gateway per request.
    studio.cloud.gateway = gateway or _ScriptedCloudGateway()
    app = create_studio_app(tmp_path, service=studio, security_enabled=False)
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/agentengine/api/v1/{action}",
        json={"AgentId": CLOUD_AGENT_ID, **payload},
        cookies={"agentkit_studio_chat_agent": "local-agent-that-must-not-win"},
    )
    assert response.status_code == 200, f"{action} -> {response.status_code}: {response.text[:200]}"
    return response.json()


def test_cloud_bootstrap_projects_agent_metadata(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        data = _post(client, "GetAgentUiBootstrap", {})
    agent = data["Data"]["Agent"]
    assert agent["AgentId"] == CLOUD_AGENT_ID
    assert agent["Name"] == "0611agent-e2e"
    assert agent["Framework"] == "langgraph"
    assert data["Data"]["Capabilities"]["HostedChat"]["Enabled"] is True


def test_cloud_sessions_models_messages_events_roundtrip(tmp_path: Path) -> None:
    gateway = _ScriptedCloudGateway()
    with _client(tmp_path, gateway) as client:
        sessions = _post(client, "ListSessions", {"Page": 1, "PageSize": 10})["Data"]
        assert sessions["Sessions"][0]["SessionId"] == "sess-e2e-1"
        assert sessions["Sessions"][0]["Title"] == "e2e 会话"

        models = _post(client, "ListAgentModels", {})["Data"]
        assert models["Current"] == "deepseek-v4-flash"

        messages = _post(client, "ListSessionMessages", {"SessionId": "sess-e2e-1", "Limit": 10})[
            "Data"
        ]
        assert messages["Messages"][0]["Role"] == "user"
        assert messages["Messages"][0]["Content"]["text"] == "你好"
        assert messages["HasMore"] is True
        assert messages["NextCursor"] == 1
        assert gateway.last_message_query == {
            "after_seq_id": None,
            "before_seq_id": None,
            "limit": 10,
        }

        events = _post(
            client,
            "ListSessionEvents",
            {
                "SessionId": "sess-e2e-1",
                "Offset": 7,
                "Limit": 25,
            },
        )["Data"]
        assert events["Events"][0]["EventType"] == "user_message"
        assert events["Offset"] == 7
        assert events["Limit"] == 25
        assert gateway.last_event_query == {
            "after_seq_id": None,
            "offset": 7,
            "limit": 25,
        }

        created = _post(client, "CreateSession", {})["Data"]
        assert created["Session"]["SessionId"] == "sess-e2e-new"


def test_cloud_runagent_stream_frames_are_canonical(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": CLOUD_AGENT_ID,
                "SessionId": "sess-e2e-1",
                "ResponsesInput": [
                    {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
                ],
            },
            cookies={"agentkit_studio_chat_agent": "local-agent-that-must-not-win"},
        )
    body = response.text
    assert response.status_code == 200
    # upstream frames forwarded
    assert "response.reasoning.delta" in body
    assert "response.output_text.delta" in body
    # bridge appends canonical terminal frames the engine requires
    assert "event: response.completed" in body
    assert "event: done" in body
    assert "data: [DONE]" in body


def test_cloud_runagent_preserves_failed_terminal_state(tmp_path: Path) -> None:
    gateway = _ScriptedCloudGateway(
        [
            'event: response.output_text.delta\ndata: {"delta": "partial"}\n\n',
            'data: {"object": "response", "status": "failed", "error": {"message": "boom"}}\n\n',
        ]
    )
    with _client(tmp_path, gateway) as client:
        response = client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": CLOUD_AGENT_ID,
                "SessionId": "sess-e2e-1",
                "ResponsesInput": [{"role": "user", "content": "fail"}],
            },
        )
    assert response.status_code == 200
    assert "event: response.failed" in response.text
    assert "event: response.completed" not in response.text
    assert response.text.count("data: [DONE]") == 1


def test_cloud_runagent_does_not_duplicate_canonical_terminal(tmp_path: Path) -> None:
    gateway = _ScriptedCloudGateway(
        [
            'event: response.failed\ndata: {"type": "response.failed", '
            '"response": {"object": "response", "status": "failed"}}\n\n',
        ]
    )
    with _client(tmp_path, gateway) as client:
        response = client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": CLOUD_AGENT_ID,
                "SessionId": "sess-e2e-1",
                "ResponsesInput": [{"role": "user", "content": "fail"}],
            },
        )
    assert response.status_code == 200
    assert response.text.count("event: response.failed") == 1
    assert response.text.count("data: [DONE]") == 1


def test_cloud_runagent_without_session_is_rejected_before_sse_starts(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": CLOUD_AGENT_ID,
                "ResponsesInput": [{"role": "user", "content": "missing session"}],
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "Code": 400,
        "Message": "云端运行需要会话标识",
        "Data": {"errorCode": "SESSION_ID_REQUIRED"},
    }


def test_cloud_interaction_submit_and_session_delete(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        receipt = _post(
            client,
            "SubmitInteraction",
            {
                "SessionId": "sess-e2e-1",
                "RunId": "run-1",
                "InteractionId": "int-1",
                "ExpectedRevision": 1,
                "Action": "approve",
                "Response": {"decision": "approve"},
                "IdempotencyKey": "e2e-key",
            },
        )
        assert receipt["Code"] == 0

        deleted = _post(client, "DeleteSession", {"SessionId": "sess-e2e-1"})
        assert deleted["Code"] == 0
