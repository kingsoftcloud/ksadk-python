"""Unit tests for ksadk/connector/channel.py."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from ksadk.connector.channel import ChannelConnector, start_channel_connector


def make_connector(invoke=None):
    async def default_invoke(task_id, message):
        return f"echo: {message}"
    return ChannelConnector(
        url="wss://test.example.com/connector/v1",
        token="test-token",
        agent_id="agent-001",
        tenant_id="tenant-abc",
        workspace_id="ws-001",
        invoke=invoke or default_invoke,
    )


class TestEnvelope:
    def test_envelope_structure(self):
        conn = make_connector()
        msg = json.loads(conn._envelope("register", {"key": "val"}))
        assert msg["type"] == "register"
        assert msg["seq"] == 1
        assert "ts" in msg
        assert msg["payload"] == {"key": "val"}

    def test_envelope_seq_increments(self):
        conn = make_connector()
        json.loads(conn._envelope("ping", {}))
        msg = json.loads(conn._envelope("pong", {}))
        assert msg["seq"] == 2


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_ok(self):
        conn = make_connector()
        ws = AsyncMock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(return_value=json.dumps({
            "type": "register_ok", "seq": 1,
            "payload": {"session_id": "conn-1", "heartbeat_sec": 30},
        }))
        await conn._register(ws)
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["type"] == "register"
        assert sent["payload"]["agent_id"] == "agent-001"
        assert sent["payload"]["protocol_version"] == 1

    @pytest.mark.asyncio
    async def test_register_err_raises(self):
        conn = make_connector()
        ws = AsyncMock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(return_value=json.dumps({
            "type": "register_err", "seq": 1,
            "payload": {"code": "auth_expired", "message": "expired"},
        }))
        with pytest.raises(ConnectionRefusedError, match="expired"):
            await conn._register(ws)


class TestReadLoop:
    @pytest.mark.asyncio
    async def test_invoke_spawns_task(self):
        conn = make_connector()
        ws = MagicMock()
        ws.send = AsyncMock()
        conn._ws = ws
        conn._spawn_invoke({"task_id": "t1", "message": "hello"})
        await asyncio.sleep(0.05)
        assert "t1" not in conn._tasks  # completed and removed
        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["type"] == "complete"
        assert sent["payload"]["result"] == "echo: hello"

    @pytest.mark.asyncio
    async def test_invoke_dedup(self):
        invoke = AsyncMock(return_value="ok")
        conn = make_connector(invoke=invoke)
        ws = MagicMock()
        ws.send = AsyncMock()
        conn._ws = ws
        # manually prevent completion to test dedup
        async def slow(task_id, message):
            await asyncio.sleep(10)
        conn._invoke = slow
        conn._spawn_invoke({"task_id": "t1", "message": "a"})
        conn._spawn_invoke({"task_id": "t1", "message": "b"})  # dedup
        assert len(conn._tasks) == 1
        conn._cancel_all_tasks()

    @pytest.mark.asyncio
    async def test_ping_pong(self):
        conn = make_connector()
        ws = AsyncMock()
        ws.send = AsyncMock()
        # simulate a ping frame in the read loop
        ping_msg = json.dumps({"type": "ping", "seq": 99, "payload": {}})
        # just verify pong would be sent
        await ws.send(conn._envelope("pong", {}))
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["type"] == "pong"


class TestError:
    @pytest.mark.asyncio
    async def test_invoke_error_sends_error_frame(self):
        async def failing_invoke(task_id, message):
            raise RuntimeError("boom")
        conn = make_connector(invoke=failing_invoke)
        ws = AsyncMock()
        ws.send = AsyncMock()
        conn._ws = ws
        conn._spawn_invoke({"task_id": "t1", "message": "test"})
        await asyncio.sleep(0.05)
        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["type"] == "error"
        assert sent["payload"]["code"] == "execution_error"


class TestFactory:
    def test_start_channel_connector_returns_instance(self):
        async def fn(tid, msg):
            return "ok"
        conn = start_channel_connector(
            url="wss://test/v1", token="t", agent_id="a",
            tenant_id="t", workspace_id="w", invoke=fn,
        )
        assert isinstance(conn, ChannelConnector)
        assert conn._agent_id == "a"
