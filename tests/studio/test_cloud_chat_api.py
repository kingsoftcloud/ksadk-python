"""Loopback Studio routes for receipt-bound cloud chat."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.api import AgentEngineAPIError
from ksadk.studio.api import create_studio_app
from ksadk.studio.cloud import DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.contracts import DeploymentRecord, DeploymentRequest, DeploymentTarget
from ksadk.studio.service import StudioService


class _TrackedSSEStream(AsyncIterator[bytes]):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> "_TrackedSSEStream":
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class _CloudClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.stream_event_batches: list[list[dict]] | None = None
        self.last_chat_stream: _TrackedSSEStream | None = None
        self.sessions = [{"session_id": "sess-existing", "title": "已有会话"}]

    async def list_sessions(self, agent_id: str, *, page: int, size: int) -> dict:
        self.calls.append(("ListSessions", {"AgentId": agent_id, "Page": page, "PageSize": size}))
        return {"sessions": list(self.sessions)}

    async def list_agents(self, *, page: int, page_size: int) -> dict:
        self.calls.append(("ListAgents", {"Page": page, "PageSize": page_size}))
        return {
            "agents": [
                {
                    "agent_id": "ar-receipt-bound",
                    "name": "Studio Agent",
                    "status": "RUNNING",
                    "endpoint": "http://studio-agent.example.test",
                },
                {
                    "agent_id": "ar-existing-code",
                    "name": "Existing Code Agent",
                    "status": "RUNNING",
                    "endpoint": "http://existing-code.example.test",
                    "framework": "langgraph",
                },
                {
                    "agent_id": "ar-deleted",
                    "name": "Deleted Agent",
                    "status": "DELETED",
                },
            ],
            "total": 3,
        }

    async def get_agent(self, *, agent_id: str) -> dict:
        self.calls.append(("GetAgent", {"AgentId": agent_id}))
        return {
            "basic": {
                "agent_id": agent_id,
                "name": "Existing Code Agent",
                "status": "RUNNING",
                "endpoint": "http://existing-code.example.test",
                "framework": "langgraph",
                "updated_at": "2026-08-24T10:00:00Z",
            },
            "deployment": {"version_id": "version-existing-code"},
        }

    async def create_dashboard_access_link(self, **kwargs) -> dict:
        self.calls.append(("CreateDashboardAccessLink", kwargs))
        return {"access_url": f"http://dashboard.example.test/{kwargs['agent_id']}"}

    async def create_session(self, agent_id: str) -> dict:
        self.calls.append(("CreateSession", {"AgentId": agent_id}))
        return {"session": {"session_id": "sess-created", "title": "新会话"}}

    async def list_session_messages(self, **kwargs) -> dict:
        self.calls.append(("ListSessionMessages", kwargs))
        return {"messages": [{"message_id": "msg-1", "role": "assistant", "content": "已收到"}]}

    async def list_session_events(self, **kwargs) -> dict:
        self.calls.append(("ListSessionEvents", kwargs))
        if self.stream_event_batches is not None:
            return {
                "events": self.stream_event_batches.pop(0)
                if self.stream_event_batches
                else []
            }
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
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
    ) -> dict:
        call = {
            "AgentId": agent_id,
            "SessionId": session_id,
            "Message": message,
            "Model": model,
            "ModelOptions": model_options,
            "ToolApprovalMode": tool_approval_mode,
        }
        if collaboration_mode is not None:
            call["CollaborationMode"] = collaboration_mode
        if goal_objective is not None:
            call["GoalObjective"] = goal_objective
        self.calls.append(
            (
                "RunAgent",
                call,
            )
        )
        return {"receipt_status": "accepted", "run_id": "run-1"}

    async def chat_stream(
        self,
        agent_id: str,
        message,
        *,
        session_id: str | None = None,
        model: str | None = None,
        model_options: dict | None = None,
        tool_approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
    ) -> AsyncIterator[bytes]:
        self.calls.append(
            (
                "RunAgentStream",
                {
                    "AgentId": agent_id,
                    "SessionId": session_id,
                    "Message": message,
                    "Model": model,
                    "ModelOptions": model_options,
                    "ToolApprovalMode": tool_approval_mode,
                    "CollaborationMode": collaboration_mode,
                    "GoalObjective": goal_objective,
                },
            )
        )

        self.last_chat_stream = _TrackedSSEStream(
            [
                b"event: response.output_text.delta\n",
                b'data: {"delta":"hello"}\n\n',
                b"event: response.completed\n",
                b'data: {"status":"completed"}\n\n',
            ]
        )
        return self.last_chat_stream

    async def list_agent_models(self, *, agent_id: str) -> dict:
        self.calls.append(("ListAgentModels", {"AgentId": agent_id}))
        return {
            "models": [{"id": "qwen3-coder-plus", "name": "Qwen3 Coder Plus"}],
            "current": "qwen3-coder-plus",
        }

    async def delete_session(self, session_id: str) -> bool:
        self.calls.append(("DeleteSession", {"SessionId": session_id}))
        self.sessions = [
            session
            for session in self.sessions
            if session["session_id"] != session_id
        ]
        return True

    async def delete_agent(self, agent_id: str) -> bool:
        self.calls.append(("DeleteAgent", {"AgentId": agent_id}))
        return True

    async def list_versions(self, agent_id: str, page: int = 1, size: int = 10) -> dict:
        self.calls.append(
            ("ListVersions", {"AgentId": agent_id, "Page": page, "PageSize": size})
        )
        return {
            "versions": [
                {
                    "version_id": "version-current",
                    "version_name": "v3",
                    "tag": "prod-current",
                    "status": "current",
                    "traffic_percentage": 100,
                    "can_rollback": False,
                    "rollback_disabled_reason": "当前版本不可回滚至自身",
                    "created_at": "2026-08-24T10:00:00+08:00",
                    "created_by": "user-current",
                },
                {
                    "version_id": "version-old",
                    "version_name": "v2",
                    "tag": "prod-old",
                    "status": "historical",
                    "traffic_percentage": 0,
                    "can_rollback": True,
                    "rollback_disabled_reason": "",
                    "created_at": "2026-08-23T10:00:00+08:00",
                    "created_by": "user-old",
                },
            ],
            "total_count": 2,
            "page": page,
            "page_size": size,
        }

    async def rollback_version(
        self,
        agent_id: str,
        target_version_id: str | None = None,
        target_tag: str | None = None,
        **_kwargs,
    ) -> dict:
        self.calls.append(
            (
                "RollbackVersion",
                {
                    "AgentId": agent_id,
                    "TargetVersionId": target_version_id,
                    "TargetTag": target_tag,
                },
            )
        )
        return {
            "agent_id": agent_id,
            "target_version_id": target_version_id,
            "status": "UPDATING",
            "noop": False,
        }

    async def submit_interaction(self, **kwargs) -> dict:
        self.calls.append(("SubmitInteraction", kwargs))
        return {"receipt_status": "accepted"}


class _Uploader:
    def __init__(self, **_kwargs) -> None:
        pass


def _client_with_receipt(
    tmp_path: Path,
    cloud_client: _CloudClient | None = None,
) -> tuple[TestClient, _CloudClient]:
    cloud_client = cloud_client or _CloudClient()
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


def _wait_for_operation(client: TestClient, operation_id: str) -> dict:
    for _ in range(100):
        operation = client.get(f"/api/v1/operations/{operation_id}").json()
        if operation["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not complete")


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


def test_cloud_chat_stream_emits_delta_before_terminal_event(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)
    cloud.stream_event_batches = [
        [
            {
                "event_type": "item.updated",
                "seq_id": 2,
                "run_id": "run-stream",
                "content": {
                    "runtime_event": {"update": {"text": "first delta"}}
                },
            }
        ],
        [
            {
                "event_type": "run.completed",
                "seq_id": 3,
                "run_id": "run-stream",
            }
        ],
    ]

    with client.stream(
        "GET",
        "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/"
        "sess-existing/events/stream?afterSeqId=1",
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert body.index('"event_type": "item.updated"') < body.index(
        '"event_type": "run.completed"'
    )
    assert [call[0] for call in cloud.calls] == [
        "ListSessionEvents",
        "ListSessionEvents",
    ]


def test_cloud_chat_delete_is_absent_from_the_next_server_list(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)

    with client:
        deleted = client.delete(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing"
        )
        remaining = client.get(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions"
        )

    assert deleted.status_code == 204
    assert remaining.status_code == 200
    assert remaining.json()["sessions"] == []
    assert [call[0] for call in cloud.calls] == ["DeleteSession", "ListSessions"]


def test_delete_deployment_uses_receipt_bound_agent_and_removes_local_receipt(
    tmp_path: Path,
) -> None:
    client, cloud = _client_with_receipt(tmp_path)

    with client:
        deleted = client.delete("/api/v1/deployments/dep-cloud-chat")
        remaining = client.get("/api/v1/deployments")

    assert deleted.status_code == 200
    assert deleted.json() == {
        "agentId": "ar-receipt-bound",
        "deletedReceiptIds": ["dep-cloud-chat"],
    }
    assert remaining.json() == {"items": []}
    assert cloud.calls == [("DeleteAgent", {"AgentId": "ar-receipt-bound"})]


def test_account_cloud_agent_without_receipt_supports_directory_chat_dashboard_and_delete(
    tmp_path: Path,
) -> None:
    client, cloud = _client_with_receipt(tmp_path)

    with client:
        listed = client.get("/api/v1/cloud-agents")
        detail = client.get("/api/v1/cloud-agents/ar-existing-code")
        sessions = client.get(
            "/api/v1/deployments/account%3Aar-existing-code/cloud-chat/sessions"
        )
        dashboard = client.post(
            "/api/v1/cloud-agents/ar-existing-code:dashboard"
        )
        deleted = client.delete("/api/v1/cloud-agents/ar-existing-code")

    assert listed.status_code == 200
    assert [item["agentId"] for item in listed.json()["items"]] == [
        "ar-receipt-bound",
        "ar-existing-code",
    ]
    assert listed.json()["total"] == 2
    assert detail.json() == {
        "agentId": "ar-existing-code",
        "name": "Existing Code Agent",
        "creatorName": None,
        "status": "RUNNING",
        "endpoint": "http://existing-code.example.test",
        "framework": "langgraph",
        "runtimeType": "langgraph",
        "capabilities": None,
        "chatTransport": "studio-session-events",
        "chatRoutingReason": "studio-compatible-framework",
        "region": None,
        "instanceId": None,
        "versionId": "version-existing-code",
        "updatedAt": "2026-08-24T10:00:00Z",
    }
    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "sess-existing"
    assert dashboard.json()["access_url"] == (
        "http://dashboard.example.test/ar-existing-code"
    )
    assert deleted.json() == {
        "agentId": "ar-existing-code",
        "deletedReceiptIds": [],
    }
    assert (
        "ListSessions",
        {"AgentId": "ar-existing-code", "Page": 1, "PageSize": 50},
    ) in cloud.calls
    assert cloud.calls[-1] == ("DeleteAgent", {"AgentId": "ar-existing-code"})


def test_account_cloud_agent_versions_proxy_server_rollback_contract(
    tmp_path: Path,
) -> None:
    client, cloud = _client_with_receipt(tmp_path)

    with client:
        versions = client.get(
            "/api/v1/cloud-agents/ar-existing-code/versions",
            params={"page": 1, "size": 100},
        )
        submitted = client.post(
            "/api/v1/cloud-agents/ar-existing-code:rollback-version",
            headers={"Idempotency-Key": "rollback-version-old"},
            json={"versionId": "version-old"},
        )
        completed = _wait_for_operation(client, submitted.json()["id"])

    assert versions.status_code == 200
    assert versions.json() == {
        "items": [
            {
                "versionId": "version-current",
                "versionName": "v3",
                "tag": "prod-current",
                "status": "current",
                "trafficPercentage": 100,
                "canRollback": False,
                "rollbackDisabledReason": "当前版本不可回滚至自身",
                "createdAt": "2026-08-24T10:00:00+08:00",
                "createdBy": "user-current",
            },
            {
                "versionId": "version-old",
                "versionName": "v2",
                "tag": "prod-old",
                "status": "historical",
                "trafficPercentage": 0,
                "canRollback": True,
                "rollbackDisabledReason": "",
                "createdAt": "2026-08-23T10:00:00+08:00",
                "createdBy": "user-old",
            },
        ],
        "total": 2,
        "currentVersionId": "version-current",
    }
    assert submitted.status_code == 202
    assert submitted.json()["metadata"] == {
        "agentId": "ar-existing-code",
        "targetVersionId": "version-old",
        "source": "server-version",
    }
    assert completed["status"] == "SUCCEEDED"
    assert completed["resourceId"] == "ar-existing-code:version-old"
    assert cloud.calls == [
        (
            "ListVersions",
            {"AgentId": "ar-existing-code", "Page": 1, "PageSize": 100},
        ),
        (
            "RollbackVersion",
            {
                "AgentId": "ar-existing-code",
                "TargetVersionId": "version-old",
                "TargetTag": None,
            },
        ),
    ]


def test_account_cloud_agent_rollback_operation_preserves_server_failure(
    tmp_path: Path,
) -> None:
    class _FailingRollbackClient(_CloudClient):
        async def rollback_version(self, *_args, **_kwargs) -> dict:
            raise AgentEngineAPIError(
                4104,
                "目标版本不可回滚",
                details={"request_id": "req-version", "action": "RollbackVersion"},
            )

    client, _cloud = _client_with_receipt(tmp_path, _FailingRollbackClient())

    with client:
        submitted = client.post(
            "/api/v1/cloud-agents/ar-existing-code:rollback-version",
            headers={"Idempotency-Key": "rollback-version-disabled"},
            json={"versionId": "version-disabled"},
        )
        completed = _wait_for_operation(client, submitted.json()["id"])

    assert submitted.status_code == 202
    assert completed["status"] == "FAILED"
    assert completed["error"] == {
        "code": "CLOUD_AGENT_VERSION_ROLLBACK_FAILED",
        "message": "目标版本不可回滚",
        "field": None,
    }


def test_native_runtime_without_session_event_capability_cannot_enter_studio_cloud_chat(
    tmp_path: Path,
) -> None:
    class _NativeCloudClient(_CloudClient):
        async def get_agent(self, *, agent_id: str) -> dict:
            self.calls.append(("GetAgent", {"AgentId": agent_id}))
            return {
                "basic": {
                    "agent_id": agent_id,
                    "name": "Native Runtime",
                    "status": "RUNNING",
                    "framework": "hermes",
                }
            }

    cloud_client = _NativeCloudClient()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=cloud_client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    studio = StudioService(tmp_path, cloud_gateway=gateway)
    client = TestClient(
        create_studio_app(tmp_path, service=studio, security_enabled=False)
    )

    with client:
        sessions = client.get(
            "/api/v1/deployments/account%3Aar-native/cloud-chat/sessions"
        )
        dashboard = client.post("/api/v1/cloud-agents/ar-native:dashboard")

    assert sessions.status_code == 409
    error = sessions.json()["error"]
    assert str(error.pop("requestId")).startswith("req_")
    assert error == {
        "code": "CLOUD_CHAT_TRANSPORT_UNSUPPORTED",
        "message": "该类型 Agent 未声明统一 SessionEvent 会话能力，请使用官方 Dashboard",
        "details": {
            "agentId": "ar-native",
            "chatTransport": "official-dashboard",
            "reason": "native-runtime-without-session-event-chat-capability",
        },
    }
    assert dashboard.status_code == 200
    assert cloud_client.calls[-1] == (
        "CreateDashboardAccessLink",
        {"agent_id": "ar-native", "link_type": "private", "path": "/"},
    )


def test_cloud_chat_route_does_not_accept_browser_agent_override(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)
    with client:
        response = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            json={"content": "你好", "agentId": "ar-not-allowed"},
        )

    assert response.status_code == 422
    assert cloud.calls == []


def test_cloud_chat_forwards_full_approval_plan_and_goal(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)
    with client:
        response = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages",
            json={
                "content": "按计划完成目标",
                "toolApprovalMode": "full",
                "collaborationMode": "plan",
                "goalObjective": "完成云端端到端验证",
            },
        )

    assert response.status_code == 202
    assert cloud.calls[-1] == (
        "RunAgent",
        {
            "AgentId": "ar-receipt-bound",
            "SessionId": "sess-existing",
            "Message": "按计划完成目标",
            "Model": None,
            "ModelOptions": None,
            "ToolApprovalMode": "full",
            "CollaborationMode": "plan",
            "GoalObjective": "完成云端端到端验证",
        },
    )


def test_cloud_chat_stream_proxies_signed_runagent_sse_without_background(tmp_path: Path) -> None:
    client, cloud = _client_with_receipt(tmp_path)
    with client:
        response = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages/stream",
            json={
                "content": "hello",
                "model": "qwen-test",
                "toolApprovalMode": "ask",
                "collaborationMode": "plan",
                "goalObjective": "finish the task",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.content == (
        b"event: response.output_text.delta\n"
        b'data: {"delta":"hello"}\n\n'
        b"event: response.completed\n"
        b'data: {"status":"completed"}\n\n'
    )
    assert cloud.calls[-1] == (
        "RunAgentStream",
        {
            "AgentId": "ar-receipt-bound",
            "SessionId": "sess-existing",
            "Message": "hello",
            "Model": "qwen-test",
            "ModelOptions": {},
            "ToolApprovalMode": "ask",
            "CollaborationMode": "plan",
            "GoalObjective": "finish the task",
        },
    )
    assert cloud.last_chat_stream is not None
    assert cloud.last_chat_stream.closed is True


def test_cloud_chat_stream_preserves_structured_upstream_admission_error(tmp_path: Path) -> None:
    class _RejectedStreamClient(_CloudClient):
        async def chat_stream(self, *_args, **_kwargs) -> AsyncIterator[bytes]:
            raise AgentEngineAPIError(
                409,
                "runtime is starting",
                details={"request_id": "req-upstream", "http_status": 409},
            )

    client, _cloud = _client_with_receipt(tmp_path, _RejectedStreamClient())
    with client:
        response = client.post(
            "/api/v1/deployments/dep-cloud-chat/cloud-chat/sessions/sess-existing/messages/stream",
            json={"content": "hello"},
        )

    assert response.status_code == 502
    error = response.json()["error"]
    assert str(error.pop("requestId")).startswith("req_")
    assert error == {
        "code": "CLOUD_CHAT_STREAM_FAILED",
        "message": "runtime is starting",
        "details": {
            "serverCode": 409,
            "request_id": "req-upstream",
            "http_status": 409,
        },
    }


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
