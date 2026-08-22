"""Loopback Studio routes for receipt-bound cloud chat."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.cloud import DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.contracts import DeploymentRecord, DeploymentRequest, DeploymentTarget
from ksadk.studio.service import StudioService


class _CloudClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def list_sessions(self, agent_id: str, *, page: int, size: int) -> dict:
        self.calls.append(("ListSessions", {"AgentId": agent_id, "Page": page, "PageSize": size}))
        return {"sessions": [{"session_id": "sess-existing", "title": "已有会话"}]}

    async def create_session(self, agent_id: str) -> dict:
        self.calls.append(("CreateSession", {"AgentId": agent_id}))
        return {"session": {"session_id": "sess-created", "title": "新会话"}}

    async def list_session_messages(self, **kwargs) -> dict:
        self.calls.append(("ListSessionMessages", kwargs))
        return {"messages": [{"message_id": "msg-1", "role": "assistant", "content": "已收到"}]}

    async def chat(self, agent_id: str, message: str, *, session_id: str | None = None) -> dict:
        self.calls.append(("RunAgent", {"AgentId": agent_id, "SessionId": session_id, "Message": message}))
        return {"receipt_status": "accepted", "run_id": "run-1"}

    async def delete_session(self, session_id: str) -> bool:
        self.calls.append(("DeleteSession", {"SessionId": session_id}))
        return True


class _Uploader:
    def __init__(self, **_kwargs) -> None:
        pass


def _client_with_receipt(tmp_path: Path) -> tuple[TestClient, _CloudClient]:
    cloud_client = _CloudClient()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=cloud_client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    studio = StudioService(tmp_path, cloud_gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )
    studio.cloud._save(
        DeploymentRecord(
            id="dep-cloud-chat",
            build_id="build-cloud-chat",
            bundle_digest="sha256:" + "a" * 64,
            version_id="version-cloud-chat",
            status="READY",
            target=request.target,
            agent_id="ar-receipt-bound",
        ),
        request,
    )
    return TestClient(
        create_studio_app(tmp_path, service=studio, security_enabled=False)
    ), cloud_client


def test_cloud_chat_routes_keep_agent_scope_in_the_local_receipt(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)
    with client:
        sessions = client.get("/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions")
        created = client.post("/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions")
        messages = client.get(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            params={"afterSeqId": 4},
        )
        sent = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            json={"content": "你好"},
        )
        deleted = client.delete(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing"
        )

    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "sess-existing"
    assert created.status_code == 201
    assert created.json()["session"]["session_id"] == "sess-created"
    assert messages.status_code == 200
    assert messages.json()["messages"][0]["content"] == "已收到"
    assert sent.status_code == 202
    assert sent.json()["receipt_status"] == "accepted"
    assert deleted.status_code == 204
    assert cloud.calls == [
        ("ListSessions", {"AgentId": "ar-receipt-bound", "Page": 1, "PageSize": 50}),
        ("CreateSession", {"AgentId": "ar-receipt-bound"}),
        (
            "ListSessionMessages",
            {
                "agent_id": "ar-receipt-bound",
                "session_id": "sess-existing",
                "after_seq_id": 4,
                "limit": 100,
            },
        ),
        (
            "RunAgent",
            {
                "AgentId": "ar-receipt-bound",
                "SessionId": "sess-existing",
                "Message": "你好",
            },
        ),
        ("DeleteSession", {"SessionId": "sess-existing"}),
    ]


def test_cloud_chat_route_does_not_accept_browser_agent_override(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)
    with client:
        response = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            json={"content": "你好", "agentId": "ar-not-allowed"},
        )

    assert response.status_code == 422
    assert cloud.calls == []
