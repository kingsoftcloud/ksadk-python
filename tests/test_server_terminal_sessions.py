from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from ksadk.runners.base_runner import BaseRunner
from ksadk.hermes_terminal import TERMINAL_SUBPROTOCOL
import ksadk.server.terminal_sessions as terminal_sessions
from ksadk.server.terminal_sessions import TerminalSession


class _OpenClawRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="openclaw-agent",
                type=SimpleNamespace(value="openclaw"),
            ),
            project_dir=".",
        )

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        return {"output": "ok"}

    async def stream(self, input_data: dict):
        yield {"type": "final", "output": "ok"}


@pytest.fixture()
def server_app(monkeypatch, tmp_path):
    appmod = importlib.import_module("ksadk.server.app")
    monkeypatch.setenv("AGENTENGINE_TERMINAL_STATE_DIR", str(tmp_path / "terminal"))
    monkeypatch.setenv("KSADK_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    appmod.set_runner(_OpenClawRunner())
    appmod.terminal_manager.reset_for_tests()
    yield appmod
    appmod.terminal_manager.reset_for_tests()


@pytest.mark.asyncio
async def test_terminal_sessions_reuse_by_business_session_and_mode(server_app, monkeypatch):
    spawned: list[tuple[str, list[str]]] = []

    def fake_spawn(session):
        session.pid = 123
        session.fd = None
        session.status = "running"
        spawned.append((session.id, list(session.argv)))

    monkeypatch.setattr(server_app.terminal_manager, "_spawn_session", fake_spawn)

    transport = httpx.ASGITransport(app=server_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        first = await client.post(
            "/_ksadk/terminal/sessions",
            json={"session_id": "biz-1", "mode": "tui", "cols": 100, "rows": 30},
        )
        second = await client.post(
            "/_ksadk/terminal/sessions",
            json={"session_id": "biz-1", "mode": "tui", "cols": 90, "rows": 24},
        )
        forced = await client.post(
            "/_ksadk/terminal/sessions",
            json={"session_id": "biz-1", "mode": "tui", "force_new": True},
        )
        listing = await client.get("/_ksadk/terminal/sessions", params={"session_id": "biz-1", "mode": "tui"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert forced.status_code == 200
    first_id = first.json()["session"]["terminal_session_id"]
    assert second.json()["session"]["terminal_session_id"] == first_id
    assert forced.json()["session"]["terminal_session_id"] != first_id
    assert len(spawned) == 2
    assert listing.status_code == 200
    sessions = listing.json()["sessions"]
    assert {item["session_id"] for item in sessions} == {"biz-1"}
    assert {item["mode"] for item in sessions} == {"tui"}
    state_files = list((server_app.Path(server_app.os.environ["AGENTENGINE_TERMINAL_STATE_DIR"])).glob("term-*.json"))
    assert state_files
    persisted = [json.loads(path.read_text(encoding="utf-8")) for path in state_files]
    assert any(item["terminal_session_id"] == first_id and item["session_id"] == "biz-1" for item in persisted)


@pytest.mark.asyncio
async def test_terminal_session_delete_marks_deleted_and_removes_from_reuse(server_app, monkeypatch):
    def fake_spawn(session):
        session.pid = 123
        session.fd = None
        session.status = "running"

    monkeypatch.setattr(server_app.terminal_manager, "_spawn_session", fake_spawn)

    transport = httpx.ASGITransport(app=server_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        created = await client.post(
            "/_ksadk/terminal/sessions",
            json={"session_id": "biz-2", "mode": "tui"},
        )
        terminal_session_id = created.json()["session"]["terminal_session_id"]
        deleted = await client.delete(f"/_ksadk/terminal/sessions/{terminal_session_id}")
        recreated = await client.post(
            "/_ksadk/terminal/sessions",
            json={"session_id": "biz-2", "mode": "tui"},
        )

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert recreated.json()["session"]["terminal_session_id"] != terminal_session_id


def test_terminal_websocket_attach_replays_and_detaches_without_deleting(server_app):
    session = TerminalSession(
        id="term-replay",
        session_id="biz-3",
        mode="tui",
        status="detached",
        pid=123,
        fd=None,
    )
    session.replay_buffer.extend(b"previous output")
    server_app.terminal_manager.sessions[session.id] = session

    with TestClient(server_app.app) as client:
        with client.websocket_connect(
            f"/_ksadk/terminal/ws?terminal_session_id={session.id}",
            subprotocols=[TERMINAL_SUBPROTOCOL],
        ) as websocket:
            ready = websocket.receive_json()
            replay = websocket.receive_bytes()
            websocket.close()
        retained = server_app.terminal_manager.sessions[session.id]

    assert ready == {"type": "ready", "terminal_session_id": session.id}
    assert replay == b"previous output"
    assert retained.status == "detached"
    assert retained.deleted is False


def test_legacy_terminal_websocket_keeps_ephemeral_cleanup_semantics(server_app, monkeypatch):
    started: list[str] = []
    terminated: list[str] = []

    def fake_spawn(session):
        session.pid = 123
        session.fd = None
        session.status = "running"
        started.append(session.id)

    def fake_terminate(session):
        session.deleted = True
        session.status = "deleted"
        terminated.append(session.id)

    async def fake_attach(_ws, session):
        session.status = "detached"

    monkeypatch.setattr(server_app.terminal_manager, "_spawn_session", fake_spawn)
    monkeypatch.setattr(server_app.terminal_manager, "_terminate_session", fake_terminate)
    monkeypatch.setattr(server_app.terminal_manager, "_attach_existing", fake_attach)

    with TestClient(server_app.app) as client:
        with client.websocket_connect(
            "/_ksadk/terminal/ws",
            subprotocols=[TERMINAL_SUBPROTOCOL],
        ) as websocket:
            websocket.send_json({"type": "start", "session_id": "legacy-biz", "mode": "tui"})

    assert len(started) == 1
    assert terminated == started
    assert started[0] not in server_app.terminal_manager.sessions


def test_terminal_tui_command_binds_product_resume_id(server_app, monkeypatch):
    monkeypatch.setattr(terminal_sessions.shutil, "which", lambda command: f"/usr/bin/{command}")

    session = TerminalSession(
        id="term-command",
        session_id="biz-4",
        mode="tui",
        framework="openclaw",
    )

    assert server_app.terminal_manager._resolve_terminal_command(session) == [
        "openclaw",
        "tui",
        "--session",
        "biz-4",
    ]

    session.framework = "hermes"
    assert server_app.terminal_manager._resolve_terminal_command(session) == [
        "hermes",
        "chat",
        "--resume",
        "biz-4",
    ]


def test_terminal_tui_resume_flag_can_be_disabled(server_app, monkeypatch):
    monkeypatch.setattr(terminal_sessions.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setenv("OPENCLAW_TERMINAL_RESUME_ENABLED", "false")
    monkeypatch.setenv("HERMES_TERMINAL_RESUME_ENABLED", "false")

    session = TerminalSession(
        id="term-command",
        session_id="biz-4",
        mode="tui",
        framework="openclaw",
    )

    assert server_app.terminal_manager._resolve_terminal_command(session) == [
        "openclaw",
        "tui",
        "--session",
        "biz-4",
    ]

    session.framework = "hermes"
    assert server_app.terminal_manager._resolve_terminal_command(session) == ["hermes", "chat"]


def test_terminal_tui_openclaw_session_flag_can_be_overridden(server_app, monkeypatch):
    monkeypatch.setattr(terminal_sessions.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setenv("OPENCLAW_TERMINAL_SESSION_FLAG", "--conversation")

    session = TerminalSession(
        id="term-command",
        session_id="biz-4",
        mode="tui",
        framework="openclaw",
    )

    assert server_app.terminal_manager._resolve_terminal_command(session) == [
        "openclaw",
        "tui",
        "--conversation",
        "biz-4",
    ]
