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
