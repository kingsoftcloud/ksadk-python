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
