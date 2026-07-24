# -*- coding: utf-8 -*-
"""goal-06 credential provider + 出站 event adapter 的测试(§3.2,review 修复)。

- A2ACredentialProvider:credential_handle 不再 read-but-unused;none/bearer/basic/apikey
  正确物化为出站 HTTP 头;OAuth2/OIDC 报 capability error;未知 handle 报错。
- 出站经 A2AEventAdapter:task/status_update/artifact_update/message → RuntimeEvent。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from a2a.types import Artifact, Message, Part, Role, Task, TaskState, TaskStatus

from ksadk.a2a.card import build_agent_card
from ksadk.a2a.credential import (
    CredentialCapabilityError,
    StaticCredentialProvider,
)
from ksadk.a2a.space_client import A2ASpaceClient, DiscoveredAgent
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import RuntimeEventStore
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


def _agent(agent_id: str = "a1", credential_handle: str | None = None) -> DiscoveredAgent:
    return DiscoveredAgent(
        agent_id=agent_id,
        version_id="v1",
        source="hosted",
        agent_card=build_agent_card(name="echo", base_url="http://testserver", skills=["echo"]),
        credential_handle=credential_handle,
    )


# ---- A2ACredentialProvider ----


@pytest.mark.asyncio
async def test_no_handle_resolves_none() -> None:
    cred = await StaticCredentialProvider().resolve(None)
    assert cred.scheme == "none" and cred.headers == {}


@pytest.mark.asyncio
async def test_bearer_basic_apikey_headers() -> None:
    provider = StaticCredentialProvider(
        {
            "h-bearer": {"scheme": "bearer", "token": "tok"},
            "h-basic": {"scheme": "basic", "username": "u", "password": "p"},
            "h-key": {"scheme": "apikey", "header": "X-Key", "key": "k"},
        }
    )
    assert (await provider.resolve("h-bearer")).headers == {"Authorization": "Bearer tok"}
    basic = (await provider.resolve("h-basic")).headers["Authorization"]
    assert basic.startswith("Basic ")
    assert (await provider.resolve("h-key")).headers == {"X-Key": "k"}


@pytest.mark.asyncio
async def test_oauth_scheme_is_capability_error() -> None:
    provider = StaticCredentialProvider({"h-oauth": {"scheme": "oauth2", "x": 1}})
    with pytest.raises(CredentialCapabilityError):
        await provider.resolve("h-oauth")


@pytest.mark.asyncio
async def test_unknown_handle_raises() -> None:
    with pytest.raises(KeyError):
        await StaticCredentialProvider().resolve("nonexistent")


@pytest.mark.asyncio
async def test_shared_http_client_keeps_agent_credentials_request_scoped(tmp_path) -> None:
    from test_a2a_discovery import _echo_app, _MockDiscoveryBackend

    seen_authorization: list[str] = []
    app = _echo_app(f"sqlite+aiosqlite:///{tmp_path}/credentials.db")

    @app.middleware("http")
    async def capture_authorization(request, call_next):  # noqa: ANN001, ANN202
        if request.url.path.startswith("/a2a/"):
            seen_authorization.append(request.headers.get("authorization", ""))
        return await call_next(request)

    shared_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    provider = StaticCredentialProvider(
        {
            "credential-a": {"scheme": "bearer", "token": "token-a"},
            "credential-b": {"scheme": "bearer", "token": "token-b"},
        }
    )
    agents = [
        _agent("agent-a", "credential-a"),
        _agent("agent-b", "credential-b"),
    ]
    client = A2ASpaceClient(
        "as-test",
        _MockDiscoveryBackend(agents),
        httpx_client=shared_http,
        credential_provider=provider,
    )
    try:
        await client.discover()
        await client.send_message("agent-a", "warmup", return_immediately=True)
        seen_authorization.clear()
        await asyncio.gather(
            client.send_message("agent-a", "a", return_immediately=True),
            client.send_message("agent-b", "b", return_immediately=True),
        )
    finally:
        await shared_http.aclose()

    assert "Bearer token-a" in seen_authorization
    assert "Bearer token-b" in seen_authorization
    assert "authorization" not in shared_http.headers


# ---- 出站经 A2AEventAdapter → RuntimeEvent ----


def _client() -> A2ASpaceClient:
    from test_a2a_discovery import _MockDiscoveryBackend

    return A2ASpaceClient("as-test", _MockDiscoveryBackend([]))


def test_task_to_event_uses_event_adapter() -> None:
    client = _client()
    task = Task(id="t1", context_id="c1", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
    event = client.task_to_event(task, _agent())
    assert isinstance(event, RuntimeEvent)
    assert event.event_type == EventType.RUN_COMPLETED
    assert event.invocation_id == "t1"


def test_stream_item_to_events_converts_status_and_artifact() -> None:
    client = _client()
    agent = _agent()

    class _Item:
        task = None
        message = None
        status_update = type("S", (), {"status": TaskStatus(state=TaskState.TASK_STATE_WORKING)})()
        artifact_update = type(
            "A",
            (),
            {"artifact": Artifact(artifact_id="ar1", parts=[Part(text="hello")])},
        )()
        task_id = "t1"

    events = client._stream_item_to_events(_Item(), agent)
    types = {e.event_type for e in events}
    assert EventType.RUN_PROGRESS in types  # status_update WORKING
    assert EventType.ARTIFACT_CREATED in types  # artifact_update
    assert all(isinstance(e, RuntimeEvent) for e in events)


@pytest.mark.asyncio
async def test_terminal_status_message_is_persisted_before_run_completed() -> None:
    service = InMemorySessionService()
    await service.create_session("a1", "a2a_space", "as-test")
    store = RuntimeEventStore(service)
    client = _client()
    agent = _agent()
    final_message = Message(
        message_id="message-final",
        task_id="t1",
        context_id="as-test",
        role=Role.ROLE_AGENT,
        parts=[Part(text="final answer")],
    )

    class _Item:
        task = None
        message = None
        artifact_update = None
        status_update = type(
            "S",
            (),
            {
                "task_id": "t1",
                "status": TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED,
                    message=final_message,
                ),
            },
        )()

    await store.append(client._stream_item_to_events(_Item(), agent))
    streamed = [event async for event in store.subscribe_run("as-test", "t1", timeout=0.1)]

    assert [event.event_type for event in streamed] == [
        EventType.TEXT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    seq_ids = [event.seq_id for event in streamed]
    assert seq_ids == sorted(set(seq_ids))


@pytest.mark.asyncio
async def test_persist_events_returns_store_assigned_cursor() -> None:
    from test_a2a_discovery import _MockDiscoveryBackend

    service = InMemorySessionService()
    await service.create_session("a1", "a2a_space", "as-test")
    await service.append_event(
        "as-test",
        SessionEvent(session_id="as-test", author="legacy", event_type="legacy"),
    )
    client = A2ASpaceClient(
        "as-test",
        _MockDiscoveryBackend([]),
        event_sink=RuntimeEventStore(service),
    )
    event = client.task_to_event(
        Task(id="t1", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED)),
        _agent(),
    )

    persisted = await client._persist_events([event])

    assert event.seq_id == 1
    assert [item.seq_id for item in persisted] == [2]
