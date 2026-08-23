import asyncio
import threading

import pytest

from ksadk.api.client import AgentEngineClient


@pytest.mark.asyncio
async def test_delete_session_reports_server_pending_delete(monkeypatch) -> None:
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    monkeypatch.setattr(client, "_action", lambda action, params: {"deleted": False})

    assert await client.delete_session("sess-pending") is False


@pytest.mark.asyncio
async def test_delete_session_reports_completed_delete(monkeypatch) -> None:
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    monkeypatch.setattr(client, "_action", lambda action, params: {"deleted": True})

    assert await client.delete_session("sess-deleted") is True


@pytest.mark.asyncio
async def test_chat_declares_chat_completions_format_for_kop(monkeypatch) -> None:
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    recorded: dict[str, object] = {}

    def fake_action(action: str, params: dict[str, object]) -> dict[str, object]:
        recorded["action"] = action
        recorded["params"] = params
        return {"response": "ok"}

    monkeypatch.setattr(client, "_action", fake_action)

    result = await client.chat("ar-test", "hello", session_id="sess-test")

    assert result == {"response": "ok"}
    assert recorded == {
        "action": "RunAgent",
        "params": {
            "AgentId": "ar-test",
            "ApiFormat": "chat_completions",
            "Messages": [{"role": "user", "content": "hello"}],
            "Stream": False,
            "SessionId": "sess-test",
        },
    }


@pytest.mark.asyncio
async def test_session_actions_do_not_block_the_calling_event_loop(monkeypatch) -> None:
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    release = threading.Event()
    entered = threading.Event()

    def blocking_action(action: str, params: dict[str, object]) -> dict[str, object]:
        entered.set()
        assert release.wait(timeout=2)
        return {"sessions": []}

    monkeypatch.setattr(client, "_action", blocking_action)
    request = asyncio.create_task(client.list_sessions("ar-test"))
    assert await asyncio.to_thread(entered.wait, 1)

    # If the requests transport were still running on this thread, this sleep
    # could not complete until ``release`` was set.
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    release.set()
    assert await request == {"sessions": []}
