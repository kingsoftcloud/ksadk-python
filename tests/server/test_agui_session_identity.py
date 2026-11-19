import json

import pytest
from fastapi.testclient import TestClient

from ksadk.agui.config import AGUIConfig
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.in_memory import InMemorySessionService
from tests.server.test_app_factory_agui import _runtime_execution

AGENT = "ar-agui-identity-test"


def headers(user):
    return {
        "X-AgentEngine-Identity-Namespace": "example",
        "X-AgentEngine-Business-Tenant-Id": "tenant",
        "X-AgentEngine-Subject-Type": "user",
        "X-AgentEngine-Subject-Id": user,
    }


def body(session, *, resume=None):
    return {
        "threadId": session, "runId": "run-" + session,
        "messages": [{"id": "message", "role": "user", "content": "hello"}],
        # These public hints deliberately disagree with the authenticated owner.
        "forwardedProps": {"agentId": "spoof-agent", "userId": "spoof-user"},
        **({"resume": resume} if resume else {}),
    }


def events(response):
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ID", AGENT)
    executor, launch = _runtime_execution()
    service = InMemorySessionService()
    app = create_runtime_app(RuntimeAppConfig(
        runtime_executor=executor, launch_context=launch,
        session_service_provider=lambda: service,
        agui=AGUIConfig(enabled=True, agent_name="project-name", runtime_type="fake"),
        route_groups={"agui", "sessions", "health_meta"},
    ), configure_runtime_app)
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("precreate", [False, True])
def test_agui_uses_platform_session_owner_and_preserves_history(client, precreate):
    if precreate:
        created = client.post("/agentengine/api/v1/CreateSession", headers=headers("alice"),
                              json={"AgentId": AGENT, "SessionId": "thread"})
        assert created.status_code == 200
    result = client.post("/agentengine/agui", headers=headers("alice"), json=body("thread"))
    assert events(result)[-1]["type"] == "RUN_FINISHED"
    listed = client.post("/agentengine/api/v1/ListSessions", headers=headers("alice"),
                         json={"AgentId": AGENT}).json()["Data"]["Sessions"]
    assert [s["SessionId"] for s in listed] == ["thread"]
    assert listed[0]["AgentId"] == AGENT
    assert listed[0]["UserId"].startswith("aeu_")
    history = client.post("/agentengine/api/v1/ListSessionMessages", headers=headers("alice"),
                          json={"AgentId": AGENT, "SessionId": "thread"})
    assert history.status_code == 200
    assert [m["Role"] for m in history.json()["Data"]["Messages"]] == ["user", "assistant"]


def test_agui_other_user_cannot_start_or_resume_session(client):
    client.post("/agentengine/api/v1/CreateSession", headers=headers("alice"),
                json={"AgentId": AGENT, "SessionId": "private"})
    for resume in [None, [{"interruptId": "fake", "status": "resolved", "payload": True}]]:
        result = client.post("/agentengine/agui", headers=headers("bob"),
                             json=body("private", resume=resume))
        assert events(result) == [{"type": "RUN_ERROR", "message": "Session not found",
                                   "code": "SESSION_NOT_FOUND"}]
    own = client.post("/agentengine/api/v1/ListSessionMessages", headers=headers("alice"),
                      json={"AgentId": AGENT, "SessionId": "private"})
    assert own.json()["Data"]["Messages"] == []
    other = client.post("/agentengine/api/v1/ListSessions", headers=headers("bob"),
                        json={"AgentId": AGENT})
    assert other.json()["Data"]["Sessions"] == []


def test_agui_incomplete_identity_is_rejected_before_stream(client):
    result = client.post("/agentengine/agui", json=body("missing"),
                         headers={"X-AgentEngine-Subject-Id": "alice"})
    assert result.status_code == 401


def test_runtime_without_hosted_admission_keeps_existing_agui_path(client):
    from ksadk.agui.hosted import PREPARE_PATH, VERSION_HEADER

    response = client.post(PREPARE_PATH, headers=headers("alice"), json=body("plain-chat"))
    assert response.status_code == 404 and VERSION_HEADER not in response.headers
    result = client.post("/agentengine/agui", headers=headers("alice"), json=body("plain-chat"))
    assert events(result)[-1]["type"] == "RUN_FINISHED"


def test_parallel_agui_requests_keep_identity_scoped_to_the_request(client):
    from concurrent.futures import ThreadPoolExecutor

    def run(user):
        result = client.post("/agentengine/agui", headers=headers(user), json=body(user))
        assert events(result)[-1]["type"] == "RUN_FINISHED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ("alice", "bob")))
    for user in ("alice", "bob"):
        listed = client.post("/agentengine/api/v1/ListSessions", headers=headers(user),
                             json={"AgentId": AGENT}).json()["Data"]["Sessions"]
        assert [session["SessionId"] for session in listed] == [user]


def test_agui_session_backend_failure_is_a_terminal_error(client, monkeypatch):
    service = client.app.state.runtime.resolve_session_service()

    async def unavailable(_session_id):
        raise ConnectionError("unavailable")

    monkeypatch.setattr(service, "get_session_metadata", unavailable)
    result = client.post("/agentengine/agui", json=body("unavailable"), headers=headers("alice"))
    assert events(result) == [{"type": "RUN_ERROR", "message": "Session admission failed",
                               "code": "RUNTIME_ERROR"}]


def test_legacy_agui_without_trusted_identity_keeps_existing_owner(client):
    client.post('/agentengine/api/v1/CreateSession', json={
        'AgentId': 'project-name', 'UserId': 'legacy-user', 'SessionId': 'legacy'})
    request = body('legacy')
    request['forwardedProps']['userId'] = 'legacy-user'
    response = client.post('/agentengine/agui', json=request)
    assert events(response)[0]['type'] == 'RUN_STARTED'
    assert events(response)[-1]['type'] == 'RUN_FINISHED'


def test_legacy_missing_resume_reaches_legacy_handler(client):
    response = client.post('/agentengine/agui', json=body('missing-legacy', resume=[{'interruptId':'missing', 'status':'resolved','payload':True}]))
    assert events(response)[0]['type'] == 'RUN_STARTED'


@pytest.mark.parametrize('identity_headers', [{}, headers('alice')])
def test_prepare_rejects_missing_authority_before_session_write(monkeypatch, identity_headers):
    from ksadk.kernel import bootstrap, ingress
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    monkeypatch.setattr(bootstrap, 'get_agent_kernel_runtime', lambda: SimpleNamespace())
    monkeypatch.setattr(ingress, 'authority_mode', lambda: 'hosted')
    executor, launch = _runtime_execution()
    service = InMemorySessionService()
    service.create_session = AsyncMock(wraps=service.create_session)
    app = create_runtime_app(RuntimeAppConfig(runtime_executor=executor, launch_context=launch,
        session_service_provider=lambda:service, agui=AGUIConfig(enabled=True), route_groups={'agui'}), configure_runtime_app)
    response = TestClient(app).post('/agentengine/internal/agui/prepare', headers=identity_headers, json=body('unauth'))
    assert response.status_code == 401
    service.create_session.assert_not_awaited()
