import asyncio
import json
import threading

import pytest

from ksadk.api.client import AgentEngineAPIError, AgentEngineClient


class _StreamingResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        chunks=(),
        text: str = "",
        content_type: str = "text/event-stream; charset=utf-8",
    ) -> None:
        self.status_code = status_code
        self._chunks = list(chunks)
        self.text = text
        self.headers = {"content-type": content_type}
        self.closed = False

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 8192
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _StreamingSession:
    def __init__(self, response: _StreamingResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def close(self) -> None:
        self.closed = True


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
async def test_chat_forwards_bounded_execution_controls_in_metadata(monkeypatch) -> None:
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    recorded: dict[str, object] = {}

    def fake_action(action: str, params: dict[str, object]) -> dict[str, object]:
        recorded["action"] = action
        recorded["params"] = params
        return {"receipt_status": "accepted"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.chat(
        "ar-test",
        "finish",
        tool_approval_mode="full",
        collaboration_mode="plan",
        goal_objective="complete the release",
    )

    assert recorded["params"] == {
        "AgentId": "ar-test",
        "ApiFormat": "chat_completions",
        "Messages": [{"role": "user", "content": "finish"}],
        "Stream": False,
        "Metadata": {
            "agentengine": {
                "tool_approval_mode": "full",
                "collaboration_mode": "plan",
                "goal_objective": "complete the release",
            }
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        ("http://server.example.test", "http://server.example.test/agentengine/api/v1/RunAgent"),
        (
            "https://aicp.api.ksyun.com",
            "https://aicp.api.ksyun.com/?Action=RunAgent&Version=2024-06-12",
        ),
    ],
)
async def test_chat_stream_opens_signed_foreground_runagent_sse_for_direct_and_kop(
    monkeypatch, base_url: str, expected_url: str
) -> None:
    response = _StreamingResponse(
        chunks=[
            b"event: response.output_text.delta\n",
            b'data: {"delta":"hello"}\n\n',
        ]
    )
    session = _StreamingSession(response)
    monkeypatch.setattr("ksadk.api.client.requests.Session", lambda: session)
    client = AgentEngineClient(base_url=base_url, access_key="ak", secret_key="sk")
    monkeypatch.setattr(client, "_resolve_user_uuid", lambda: None)
    monkeypatch.setattr(client, "_resolve_account_id", lambda: None)

    stream = await client.chat_stream(
        "ar-test",
        "hello",
        session_id="sess-test",
        model="qwen-test",
        tool_approval_mode="risk",
    )
    chunks = [chunk async for chunk in stream]

    assert chunks == [
        b"event: response.output_text.delta\n",
        b'data: {"delta":"hello"}\n\n',
    ]
    assert len(session.calls) == 1
    request = session.calls[0]
    assert request["method"] == "POST"
    assert request["url"] == expected_url
    assert request["stream"] is True
    assert request["timeout"] == (client.timeout, None)
    assert request["auth"] is not None
    assert json.loads(request["data"].decode("utf-8")) == {
        "AgentId": "ar-test",
        "ApiFormat": "chat_completions",
        "Messages": [{"role": "user", "content": "hello"}],
        "Stream": True,
        "Background": False,
        "SessionId": "sess-test",
        "Model": "qwen-test",
        "Metadata": {"agentengine": {"tool_approval_mode": "risk"}},
    }
    assert response.closed is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_chat_stream_closes_upstream_when_consumer_disconnects(monkeypatch) -> None:
    response = _StreamingResponse(chunks=[b"data: first\n\n", b"data: second\n\n"])
    session = _StreamingSession(response)
    monkeypatch.setattr("ksadk.api.client.requests.Session", lambda: session)
    client = AgentEngineClient(base_url="http://server.example.test")

    stream = await client.chat_stream("ar-test", "hello")
    assert await anext(stream) == b"data: first\n\n"
    await stream.aclose()

    assert response.closed is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_chat_stream_raises_structured_non_2xx_before_returning_stream(monkeypatch) -> None:
    response = _StreamingResponse(
        status_code=409,
        text=json.dumps(
            {
                "RequestId": "req-stream",
                "Error": {"Code": "AgentNotReady", "Message": "runtime is starting"},
            }
        ),
    )
    session = _StreamingSession(response)
    monkeypatch.setattr("ksadk.api.client.requests.Session", lambda: session)
    client = AgentEngineClient(base_url="http://server.example.test")

    with pytest.raises(AgentEngineAPIError) as caught:
        await client.chat_stream("ar-test", "hello")

    assert caught.value.code == 409
    assert caught.value.message == "runtime is starting"
    assert caught.value.details == {
        "request_id": "req-stream",
        "remote_error_code": "AgentNotReady",
        "remote_error_message": "runtime is starting",
        "http_status": 409,
    }
    assert response.closed is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_chat_stream_rejects_http_200_kop_json_action_error(monkeypatch) -> None:
    response = _StreamingResponse(
        status_code=200,
        content_type="application/json; charset=utf-8",
        text=json.dumps(
            {
                "Code": 4104,
                "Message": "Agent admission rejected",
                "RequestId": "req-kop-json",
            }
        ),
    )
    session = _StreamingSession(response)
    monkeypatch.setattr("ksadk.api.client.requests.Session", lambda: session)
    client = AgentEngineClient(base_url="https://aicp.api.ksyun.com")

    with pytest.raises(AgentEngineAPIError) as caught:
        await client.chat_stream("ar-test", "hello")

    assert caught.value.code == 4104
    assert caught.value.message == "Agent admission rejected"
    assert caught.value.details == {
        "request_id": "req-kop-json",
        "message": "Agent admission rejected",
        "http_status": 200,
        "content_type": "application/json; charset=utf-8",
    }
    assert response.closed is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_chat_stream_preserves_http_200_kop_nested_error_code(monkeypatch) -> None:
    response = _StreamingResponse(
        status_code=200,
        content_type="application/json",
        text=json.dumps(
            {
                "Error": {
                    "Code": "AgentNotReady",
                    "Message": "runtime is still starting",
                }
            }
        ),
    )
    session = _StreamingSession(response)
    monkeypatch.setattr("ksadk.api.client.requests.Session", lambda: session)
    client = AgentEngineClient(base_url="https://aicp.api.ksyun.com")

    with pytest.raises(AgentEngineAPIError) as caught:
        await client.chat_stream("ar-test", "hello")

    assert caught.value.raw_code == "AgentNotReady"
    assert caught.value.code is None
    assert caught.value.message == "runtime is still starting"
    assert caught.value.details["http_status"] == 200
    assert response.closed is True
    assert session.closed is True


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
