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

    async def list_session_events(self, **kwargs) -> dict:
        self.calls.append(("ListSessionEvents", kwargs))
        return {"events": [{"event_type": "interaction.requested", "seq_id": 5}]}

    async def chat(
        self,
        agent_id: str,
        message,
        *,
        session_id: str | None = None,
        model: str | None = None,
        model_options: dict | None = None,
        tool_approval_mode: str | None = None,
    ) -> dict:
        self.calls.append(
            (
                "RunAgent",
                {
                    "AgentId": agent_id,
                    "SessionId": session_id,
                    "Message": message,
                    "Model": model,
                    "ModelOptions": model_options,
                    "ToolApprovalMode": tool_approval_mode,
                },
            )
        )
        return {"receipt_status": "accepted", "run_id": "run-1"}

    async def list_agent_models(self, *, agent_id: str) -> dict:
        self.calls.append(("ListAgentModels", {"AgentId": agent_id}))
        return {
            "models": [{"id": "qwen3-coder-plus", "name": "Qwen3 Coder Plus"}],
            "current": "qwen3-coder-plus",
        }

    async def delete_session(self, session_id: str) -> bool:
        self.calls.append(("DeleteSession", {"SessionId": session_id}))
        return True

    async def submit_interaction(self, **kwargs) -> dict:
        self.calls.append(("SubmitInteraction", kwargs))
        return {"receipt_status": "accepted"}


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
        models = client.get("/api/v1/deployments/dep-cloud-chat/cloud-chat/models")
        created = client.post("/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions")
        messages = client.get(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            params={"afterSeqId": 4},
        )
        events = client.get(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/events",
            params={"afterSeqId": 4},
        )
        sent = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            json={
                "content": [
                    {"type": "input_text", "text": "检查附件"},
                    {
                        "type": "input_file",
                        "filename": "note.txt",
                        "file_data": "data:text/plain;base64,aGVsbG8=",
                    },
                ],
                "model": "qwen3-coder-plus",
                "toolApprovalMode": "ask",
            },
        )
        interaction = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/interactions",
            json={
                "runId": "run-1",
                "interactionId": "int-1",
                "expectedRevision": 1,
                "action": "approve",
                "response": {"decision": "approve"},
                "idempotencyKey": "idem-1",
            },
        )
        deleted = client.delete(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing"
        )

    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "sess-existing"
    assert models.status_code == 200
    assert models.json()["current"] == "qwen3-coder-plus"
    assert created.status_code == 201
    assert created.json()["session"]["session_id"] == "sess-created"
    assert messages.status_code == 200
    assert messages.json()["messages"][0]["content"] == "已收到"
    assert events.status_code == 200
    assert events.json()["events"][0]["event_type"] == "interaction.requested"
    assert sent.status_code == 202
    assert sent.json()["receipt_status"] == "accepted"
    assert interaction.status_code == 202
    assert interaction.json()["receipt_status"] == "accepted"
    assert deleted.status_code == 204
    assert cloud.calls == [
        ("ListSessions", {"AgentId": "ar-receipt-bound", "Page": 1, "PageSize": 50}),
        ("ListAgentModels", {"AgentId": "ar-receipt-bound"}),
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
            "ListSessionEvents",
            {
                "agent_id": "ar-receipt-bound",
                "session_id": "sess-existing",
                "after_seq_id": 4,
                "limit": 200,
            },
        ),
        (
            "RunAgent",
            {
                "AgentId": "ar-receipt-bound",
                "SessionId": "sess-existing",
                "Message": [
                    {"type": "input_text", "text": "检查附件"},
                    {
                        "type": "input_file",
                        "filename": "note.txt",
                        "file_data": "data:text/plain;base64,aGVsbG8=",
                    },
                ],
                "Model": "qwen3-coder-plus",
                "ModelOptions": None,
                "ToolApprovalMode": "ask",
            },
        ),
        (
            "SubmitInteraction",
            {
                "agent_id": "ar-receipt-bound",
                "session_id": "sess-existing",
                "run_id": "run-1",
                "interaction_id": "int-1",
                "expected_revision": 1,
                "action": "approve",
                "response": {"decision": "approve"},
                "idempotency_key": "idem-1",
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


def test_cloud_chat_route_rejects_unbounded_or_unknown_attachment_parts(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)
    with client:
        unknown = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            json={"content": [{"type": "internal_policy", "sandbox": "full-access"}]},
        )
        too_many = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            json={
                "content": [
                    {
                        "type": "input_file",
                        "filename": f"note-{index}.txt",
                        "file_data": "data:text/plain;base64,aA==",
                    }
                    for index in range(9)
                ]
            },
        )

    assert unknown.status_code == 422
    assert too_many.status_code == 422
    assert cloud.calls == []
