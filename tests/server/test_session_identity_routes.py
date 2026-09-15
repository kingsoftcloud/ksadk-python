from __future__ import annotations

import httpx
import pytest

from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.invocation_identity import identity_native_user_id


def _headers(tenant_id: str) -> dict[str, str]:
    return {
        "X-AgentEngine-Identity-Namespace": "customer-crm",
        "X-AgentEngine-Business-Tenant-Id": tenant_id,
        "X-AgentEngine-Subject-Type": "user",
        "X-AgentEngine-Subject-Id": "user-7",
    }


def _iam_account_headers(account_id: str) -> dict[str, str]:
    return {
        "X-AgentEngine-Identity-Namespace": "kscloud-iam",
        "X-AgentEngine-Business-Tenant-Id": account_id,
        "X-AgentEngine-Subject-Type": "account",
        "X-AgentEngine-Subject-Id": account_id,
    }


def _iam_user_headers(account_id: str, user_id: str) -> dict[str, str]:
    return {
        "X-AgentEngine-Identity-Namespace": "kscloud-iam",
        "X-AgentEngine-Business-Tenant-Id": account_id,
        "X-AgentEngine-Subject-Type": "user",
        "X-AgentEngine-Subject-Id": user_id,
    }


@pytest.mark.asyncio
async def test_runtime_session_routes_isolate_verified_business_identities() -> None:
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        created = await client.post(
            "/agentengine/api/v1/CreateSession",
            json={"AgentId": "agent-1", "UserId": "bff", "SessionId": "session-1"},
            headers=_headers("tenant-a"),
        )
        foreign = await client.post(
            "/agentengine/api/v1/GetSession",
            json={"AgentId": "agent-1", "UserId": "bff", "SessionId": "session-1"},
            headers=_headers("tenant-b"),
        )
        owner_list = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "agent-1", "UserId": "bff"},
            headers=_headers("tenant-a"),
        )
        foreign_list = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "agent-1", "UserId": "bff"},
            headers=_headers("tenant-b"),
        )

    identity = {
        "identity_namespace": "customer-crm",
        "tenant_id": "tenant-a",
        "subject_type": "user",
        "subject_id": "user-7",
    }
    assert created.status_code == 200
    assert created.json()["Data"]["Session"]["UserId"] == identity_native_user_id(identity)
    assert foreign.status_code == 404
    assert owner_list.json()["Data"]["Total"] == 1
    assert foreign_list.json()["Data"]["Total"] == 0


@pytest.mark.asyncio
async def test_custom_identity_cannot_first_claim_an_unowned_legacy_session() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "legacy-user", "legacy-session")
    await service.append_event(
        "legacy-session",
        SessionEvent(
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "historical message"}]},
        ),
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        owner = await client.post(
            "/agentengine/api/v1/GetSession",
            json={
                "AgentId": "agent-1",
                "UserId": "legacy-user",
                "SessionId": "legacy-session",
            },
            headers=_headers("tenant-a"),
        )
        foreign = await client.post(
            "/agentengine/api/v1/ListSessionMessages",
            json={
                "AgentId": "agent-1",
                "UserId": "legacy-user",
                "SessionId": "legacy-session",
            },
            headers=_headers("tenant-b"),
        )

    assert owner.status_code == 404
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_identity_scoped_get_does_not_create_a_missing_session() -> None:
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetSession",
            json={"AgentId": "agent-1", "UserId": "bff", "SessionId": "missing"},
            headers=_headers("tenant-a"),
        )

    assert response.status_code == 404
    assert await service.get_session_metadata("missing") is None


@pytest.mark.asyncio
async def test_verified_iam_account_list_adopts_and_replays_legacy_sessions() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "old-user-a", "legacy-a")
    await service.create_session("agent-1", "old-user-b", "legacy-b")
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "agent-1"},
            headers=_iam_account_headers("account-1"),
        )

    assert response.status_code == 200
    assert {item["SessionId"] for item in response.json()["Data"]["Sessions"]} == {
        "legacy-a",
        "legacy-b",
    }


@pytest.mark.asyncio
async def test_verified_iam_user_list_adopts_only_matching_legacy_user() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-a", "legacy-a")
    await service.create_session("agent-1", "user-b", "legacy-b")
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "agent-1"},
            headers=_iam_user_headers("account-1", "user-a"),
        )

    assert response.status_code == 200
    assert [item["SessionId"] for item in response.json()["Data"]["Sessions"]] == ["legacy-a"]


@pytest.mark.asyncio
async def test_runtime_attachments_are_identity_scoped(monkeypatch, tmp_path) -> None:
    from ksadk.conversations import attachment_storage

    monkeypatch.setattr(attachment_storage, "resolve_local_session_dir", lambda: tmp_path)
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"workspace"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        uploaded = await client.post(
            "/agentengine/api/v1/UploadFile",
            files={"file": ("tenant.txt", b"tenant-a", "text/plain")},
            headers=_headers("tenant-a"),
        )
        file_uri = uploaded.json()["Data"]["FileData"]["fileUri"]
        owner = await client.get(
            "/agentengine/api/v1/AttachmentContent",
            params={"FileUri": file_uri},
            headers=_headers("tenant-a"),
        )
        foreign = await client.get(
            "/agentengine/api/v1/AttachmentContent",
            params={"FileUri": file_uri},
            headers=_headers("tenant-b"),
        )

    assert uploaded.status_code == 200
    assert owner.status_code == 200
    assert owner.content == b"tenant-a"
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_runtime_workspace_files_use_distinct_identity_roots(monkeypatch, tmp_path) -> None:
    from ksadk.server.routes import common

    monkeypatch.setattr(common, "resolve_local_session_dir", lambda: tmp_path)
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"workspace"},
            session_service_provider=InMemorySessionService,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        uploaded = await client.post(
            "/agentengine/api/v1/AddWorkspaceFile",
            data={"Path": "reports/result.txt"},
            files={"file": ("result.txt", b"tenant-a", "text/plain")},
            headers=_headers("tenant-a"),
        )
        owner = await client.post(
            "/agentengine/api/v1/ListWorkspaceFiles",
            json={"Path": ".", "Recursive": True},
            headers=_headers("tenant-a"),
        )
        foreign = await client.post(
            "/agentengine/api/v1/ListWorkspaceFiles",
            json={"Path": ".", "Recursive": True},
            headers=_headers("tenant-b"),
        )

    assert uploaded.status_code == 200
    assert [entry["Path"] for entry in owner.json()["Data"]["Entries"]] == [
        "reports",
        "reports/result.txt",
    ]
    assert foreign.json()["Data"]["Entries"] == []


@pytest.mark.asyncio
async def test_legacy_attachment_replays_only_through_its_adopted_session(
    monkeypatch, tmp_path
) -> None:
    from ksadk.conversations import attachment_storage
    from ksadk.conversations.attachment_storage import AttachmentStorageService

    monkeypatch.setattr(attachment_storage, "resolve_local_session_dir", lambda: tmp_path)
    storage = AttachmentStorageService()
    file_uri, _path = storage.store_sync(
        data=b"historical",
        file_id="legacy-file",
        display_name="history.txt",
        mime_type="text/plain",
    )
    service = InMemorySessionService()
    await service.create_session("agent-1", "legacy-user", "legacy-session")
    await service.append_event(
        "legacy-session",
        SessionEvent(
            author="user",
            event_type="user_message",
            content={
                "role": "user",
                "parts": [
                    {
                        "fileData": {
                            "fileUri": file_uri,
                            "displayName": "history.txt",
                            "mimeType": "text/plain",
                        }
                    }
                ],
            },
            metadata={
                "attachments": [
                    {
                        "file_uri": file_uri,
                        "display_name": "history.txt",
                        "mime_type": "text/plain",
                    }
                ]
            },
        ),
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions", "workspace"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        history = await client.post(
            "/agentengine/api/v1/ListSessionMessages",
            json={
                "AgentId": "agent-1",
                "UserId": "legacy-user",
                "SessionId": "legacy-session",
            },
            headers=_iam_user_headers("account-1", "legacy-user"),
        )
        url = history.json()["Data"]["Messages"][0]["Attachments"][0]["url"]
        owner = await client.get(url, headers=_iam_user_headers("account-1", "legacy-user"))
        foreign = await client.get(url, headers=_iam_user_headers("account-1", "different-user"))

    assert "SessionId=legacy-session" in url
    assert owner.status_code == 200
    assert owner.content == b"historical"
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_legacy_attachment_cannot_use_a_missing_session_as_authorization(
    monkeypatch, tmp_path
) -> None:
    from ksadk.conversations import attachment_storage
    from ksadk.conversations.attachment_storage import AttachmentStorageService

    monkeypatch.setattr(attachment_storage, "resolve_local_session_dir", lambda: tmp_path)
    file_uri, _path = AttachmentStorageService().store_sync(
        data=b"historical",
        file_id="legacy-file",
        display_name="history.txt",
        mime_type="text/plain",
    )
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"workspace"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.get(
            "/agentengine/api/v1/AttachmentContent",
            params={"FileUri": file_uri, "SessionId": "missing", "AgentId": "agent-1"},
            headers=_headers("tenant-a"),
        )

    assert response.status_code == 404
    assert await service.get_session_metadata("missing") is None


@pytest.mark.asyncio
async def test_feedback_and_run_event_routes_enforce_session_identity() -> None:
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions", "feedback", "run"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        await client.post(
            "/agentengine/api/v1/CreateSession",
            json={"AgentId": "agent-1", "SessionId": "session-1"},
            headers=_headers("tenant-a"),
        )
        await service.append_event(
            "session-1",
            SessionEvent(
                author="assistant",
                event_type="assistant_message",
                invocation_id="run-1",
                content={"role": "assistant", "parts": [{"text": "answer"}]},
                metadata={"response_id": "resp-1"},
            ),
        )
        owner_feedback = await client.post(
            "/agentengine/api/v1/UpsertResponseFeedback",
            json={
                "AgentId": "agent-1",
                "SessionId": "session-1",
                "ResponseId": "resp-1",
                "Rating": "up",
            },
            headers=_headers("tenant-a"),
        )
        foreign_feedback = await client.post(
            "/agentengine/api/v1/GetResponseFeedback",
            json={
                "AgentId": "agent-1",
                "SessionId": "session-1",
                "ResponseId": "resp-1",
            },
            headers=_headers("tenant-b"),
        )
        foreign_events = await client.get(
            "/agentengine/api/v1/SubscribeRunEvents",
            params={
                "AgentId": "agent-1",
                "SessionId": "session-1",
                "InvocationId": "run-1",
            },
            headers=_headers("tenant-b"),
        )

    assert owner_feedback.status_code == 200
    assert foreign_feedback.status_code == 404
    assert foreign_events.status_code == 404


@pytest.mark.asyncio
async def test_adk_compat_session_routes_keep_identity_owners_separate() -> None:
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            route_groups={"sessions_adk_compat"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        created = await client.post(
            "/apps/agent-1/users/web-user/sessions",
            json={"sessionId": "adk-session"},
            headers=_headers("tenant-a"),
        )
        owner = await client.get(
            "/apps/agent-1/users/web-user/sessions/adk-session",
            headers=_headers("tenant-a"),
        )
        foreign = await client.get(
            "/apps/agent-1/users/web-user/sessions/adk-session",
            headers=_headers("tenant-b"),
        )

    assert created.status_code == 200
    assert owner.status_code == 200
    assert foreign.status_code == 404
