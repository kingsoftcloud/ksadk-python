from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.local_service import LocalSessionService


class _ContinuityRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                type=SimpleNamespace(value="langchain"),
            ),
            project_dir=".",
        )

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        return {"output": "ok"}

    async def stream(self, input_data: dict):
        yield {"type": "final", "output": "ok"}


@pytest.mark.asyncio
async def test_local_session_service_migrates_legacy_tables_to_namespaced_schema(tmp_path):
    db_path = tmp_path / "sessions.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            title_source TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            first_prompt TEXT NOT NULL DEFAULT '',
            last_prompt TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            author TEXT NOT NULL,
            event_type TEXT NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            timestamp REAL NOT NULL,
            state_delta_json TEXT NOT NULL DEFAULT '{}',
            seq_id INTEGER NOT NULL,
            invocation_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE states (
            scope TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL DEFAULT '{}',
            version INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (scope, agent_id, user_id, session_id)
        );
        INSERT INTO sessions (
            id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
            state_json, created_at, updated_at, version
        ) VALUES (
            'sess-1', 'demo-agent', 'user', 'old title', 'heuristic', 'summary',
            'first', 'last', '{\"topic\": \"billing\"}', 1, 2, 3
        );
        INSERT INTO events (
            id, session_id, author, event_type, content_json, timestamp, state_delta_json, seq_id, invocation_id, metadata_json
        ) VALUES (
            'evt-1', 'sess-1', 'user', 'user_message', '{\"role\": \"user\", \"parts\": [{\"text\": \"hello\"}]}',
            1, '{}', 1, NULL, '{}'
        );
        INSERT INTO states (
            scope, agent_id, user_id, session_id, state_json, version, updated_at
        ) VALUES (
            'session', 'demo-agent', 'user', 'sess-1', '{\"topic\": \"billing\"}', 1, 2
        );
        """
    )
    connection.commit()
    connection.close()

    service = LocalSessionService(db_path=db_path)
    session = await service.get_session("sess-1")

    assert session is not None
    assert session.state == {"topic": "billing"}
    assert [event.id for event in session.events] == ["evt-1"]

    tables = {
        row[0]
        for row in sqlite3.connect(db_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "ksadk_sessions" in tables
    assert "ksadk_events" in tables
    assert "ksadk_states" in tables


@pytest.mark.asyncio
async def test_get_session_action_exposes_continuity_metadata(monkeypatch, tmp_path):
    server_app_module = __import__("ksadk.server.app", fromlist=["app"])
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await service.create_session("demo-agent", "user", session_id="sess-1")
    await service.update_session_metadata(
        "sess-1",
        title="hello",
        title_source="heuristic",
        summary="assistant says hi",
        first_prompt="hello",
        last_prompt="hello",
    )
    runner = _ContinuityRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetSession",
            json={"SessionId": "sess-1"},
        )

    assert response.status_code == 200
    continuity = response.json()["Data"]["Session"]["Continuity"]
    assert continuity["Level"] == "semantic"
    assert continuity["Path"] == "replay"
    assert continuity["Runner"] == "langchain"


@pytest.mark.asyncio
async def test_bootstrap_exposes_session_backend_diagnostics(monkeypatch):
    server_app_module = __import__("ksadk.server.app", fromlist=["app"])
    runner = _ContinuityRunner()
    server_app_module.set_runner(runner)
    monkeypatch.setattr(
        server_app_module,
        "describe_session_backend",
        lambda: {
            "Backend": "postgres",
            "Shared": True,
            "ProductionSafe": True,
            "ContinuityDefault": "semantic/replay",
        },
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    session_backend = response.json()["Data"]["SessionBackend"]
    assert session_backend["Backend"] == "postgres"
    assert session_backend["Shared"] is True
    assert session_backend["ProductionSafe"] is True
    assert session_backend["ContinuityDefault"] == "semantic/replay"
    assert "Dsn" not in session_backend
