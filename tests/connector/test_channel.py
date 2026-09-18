"""Unit tests for the SDK-side Channel connector."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from ksadk.connector.channel import (
    ChannelConnector,
    InvokeRequest,
    ProtocolError,
    create_channel_connector,
)


def make_connector(invoke=None, **kwargs):
    async def default_invoke(request):
        return f"echo: {request.message}"

    return ChannelConnector(
        url="wss://test.example.com/connector/v1",
        token="test-token",
        agent_id="agent-001",
        tenant_id="tenant-abc",
        workspace_id="ws-001",
        invoke=invoke or default_invoke,
        **kwargs,
    )


def frame(raw):
    return json.loads(raw)


class TestRequest:
    def test_request_preserves_context(self):
        req = InvokeRequest.from_payload(
            {
                "task_id": "t1",
                "session_id": "s1",
                "message": "hello",
                "channel": "feishu",
                "idempotency_key": "i1",
                "deadline": "2030-01-01T00:00:00Z",
            }
        )
        assert req.task_id == "t1"
        assert req.session_id == "s1"
        assert req.channel == "feishu"
        assert req.idempotency_key == "i1"
        assert req.deadline == datetime(2030, 1, 1, tzinfo=timezone.utc)

    def test_request_requires_task_id(self):
        with pytest.raises(ProtocolError):
            InvokeRequest.from_payload({"message": "missing"})


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_and_identity(self):
        conn = make_connector()
        ws = AsyncMock()
        ws.recv.return_value = json.dumps(
            {
                "type": "register_ok",
                "payload": {
                    "session_id": "conn-1",
                    "identity": {
                        "tenant_id": "tenant-abc",
                        "workspace_id": "ws-001",
                        "agent_id": "agent-001",
                    },
                },
            }
        )
        await conn._register(ws)
        sent = frame(ws.send.call_args[0][0])
        assert sent["payload"]["protocol_version"] == 1
        assert sent["payload"]["capabilities"] == ["invoke", "cancel"]

    @pytest.mark.asyncio
    async def test_register_rejects_identity_mismatch(self):
        conn = make_connector()
        ws = AsyncMock()
        ws.recv.return_value = json.dumps(
            {
                "type": "register_ok",
                "payload": {"identity": {"agent_id": "other"}},
            }
        )
        with pytest.raises(ProtocolError, match="identity mismatch"):
            await conn._register(ws)


class TestInvocation:
    @pytest.mark.asyncio
    async def test_callback_gets_full_request_and_completes(self):
        received = []

        async def invoke(request):
            received.append(request)
            return "ok"

        conn = make_connector(invoke=invoke)
        ws = AsyncMock()
        conn._ws = ws
        await conn._spawn_invoke(
            {
                "task_id": "t1",
                "session_id": "s1",
                "message": "hello",
                "channel": "wps",
                "idempotency_key": "idem-1",
            }
        )
        await asyncio.sleep(0.01)
        assert received[0].session_id == "s1"
        assert received[0].channel == "wps"
        assert received[0].idempotency_key == "idem-1"
        sent = frame(ws.send.call_args[0][0])
        assert sent["type"] == "complete"

    @pytest.mark.asyncio
    async def test_duplicate_terminal_is_replied_without_reexecution(self):
        invoke = AsyncMock(return_value="once")
        conn = make_connector(invoke=invoke)
        ws = AsyncMock()
        conn._ws = ws
        payload = {"task_id": "t1", "session_id": "s", "message": "hello"}
        await conn._spawn_invoke(payload)
        await asyncio.sleep(0.01)
        ws.reset_mock()
        await conn._spawn_invoke(payload)
        assert invoke.await_count == 1
        assert frame(ws.send.call_args[0][0])["payload"]["result"] == "once"

    @pytest.mark.asyncio
    async def test_expired_deadline_does_not_call_agent(self):
        invoke = AsyncMock(return_value="bad")
        conn = make_connector(invoke=invoke)
        ws = AsyncMock()
        conn._ws = ws
        await conn._spawn_invoke(
            {
                "task_id": "t1",
                "message": "hello",
                "deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            }
        )
        await asyncio.sleep(0.01)
        invoke.assert_not_awaited()
        assert frame(ws.send.call_args[0][0])["payload"]["code"] == "deadline_exceeded"

    @pytest.mark.asyncio
    async def test_deadline_limits_callback(self):
        async def slow(_request):
            await asyncio.sleep(1)

        conn = make_connector(invoke=slow)
        ws = AsyncMock()
        conn._ws = ws
        await conn._spawn_invoke(
            {
                "task_id": "t1",
                "message": "hello",
                "deadline": (datetime.now(timezone.utc) + timedelta(seconds=0.01)).isoformat(),
            }
        )
        await asyncio.sleep(0.05)
        assert frame(ws.send.call_args[0][0])["payload"]["code"] == "deadline_exceeded"

    @pytest.mark.asyncio
    async def test_remote_cancel_propagates_and_reports_cancelled(self):
        started = asyncio.Event()

        async def slow(_request):
            started.set()
            await asyncio.sleep(10)

        conn = make_connector(invoke=slow)
        ws = AsyncMock()
        conn._ws = ws
        await conn._spawn_invoke({"task_id": "t1", "message": "hello"})
        await started.wait()
        await conn._cancel_remote("t1")
        await asyncio.sleep(0.01)
        assert frame(ws.send.call_args[0][0])["payload"]["code"] == "cancelled"

    @pytest.mark.asyncio
    async def test_runtime_error_does_not_leak_exception_text(self):
        async def failing(_request):
            raise RuntimeError("secret token should not cross protocol")

        conn = make_connector(invoke=failing)
        ws = AsyncMock()
        conn._ws = ws
        await conn._spawn_invoke({"task_id": "t1", "message": "hello"})
        await asyncio.sleep(0.01)
        sent = frame(ws.send.call_args[0][0])
        assert sent["payload"]["code"] == "execution_error"
        assert "secret" not in json.dumps(sent)

    @pytest.mark.asyncio
    async def test_terminal_is_queued_and_flushed_after_reconnect(self):
        conn = make_connector()
        await conn._finish("t1", "complete", {"task_id": "t1", "result": "ok"})
        assert "t1" in conn._pending_terminal
        ws = AsyncMock()
        conn._ws = ws
        await conn._flush_pending_terminal(ws)
        assert "t1" not in conn._pending_terminal
        assert frame(ws.send.call_args[0][0])["type"] == "complete"


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_clean_peer_close_enters_reconnect_backoff(self, monkeypatch):
        attempts = []

        class ClosedWebSocket:
            async def send(self, _raw):
                return None

            async def recv(self):
                return json.dumps({"type": "register_ok", "payload": {}})

            def __aiter__(self):
                async def empty():
                    return
                    yield  # pragma: no cover

                return empty()

            async def close(self):
                return None

        class Connection:
            def __init__(self):
                self.ws = ClosedWebSocket()

            async def __aenter__(self):
                return self.ws

            async def __aexit__(self, *_args):
                return False

        def connect(url, **kwargs):
            assert "test-token" not in url
            assert (kwargs.get("additional_headers") or kwargs.get("extra_headers"))[
                "Authorization"
            ] == "Bearer test-token"
            attempts.append(time.monotonic())
            return Connection()

        import websockets

        monkeypatch.setattr(websockets, "connect", connect)
        conn = make_connector()
        task = asyncio.create_task(conn.start())
        for _ in range(30):
            if len(attempts) >= 2:
                break
            await asyncio.sleep(0.1)
        await conn.stop()
        await task
        assert len(attempts) >= 2
        assert attempts[1] - attempts[0] >= 0.8

    @pytest.mark.asyncio
    async def test_stop_waits_for_invoke_cancellation(self):
        cancelled = asyncio.Event()

        async def slow(_request):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        conn = make_connector(invoke=slow)
        await conn._spawn_invoke({"task_id": "t1", "message": "hello"})
        await asyncio.sleep(0)
        await asyncio.wait_for(conn.stop(), timeout=1)
        assert cancelled.is_set()
        assert conn.status.active_tasks == 0

    @pytest.mark.asyncio
    async def test_stop_is_bounded_for_non_cooperative_invoke(self):
        release = asyncio.Event()

        async def stubborn(_request):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return "eventually done"

        conn = make_connector(invoke=stubborn)
        await conn._spawn_invoke({"task_id": "t1", "message": "hello"})
        await asyncio.sleep(0)
        await asyncio.wait_for(conn.stop(), timeout=3)
        assert conn.status.active_tasks == 0
        release.set()
        await asyncio.sleep(0.01)

    def test_factory_and_safe_status(self):
        conn = create_channel_connector(
            url="wss://test/v1",
            token="super-secret",
            agent_id="a",
            tenant_id="t",
            workspace_id="w",
            invoke=AsyncMock(),
        )
        assert conn.status.connected is False
        assert "super-secret" not in repr(conn.status)
        assert "super-secret" not in conn._connection_url()


def test_connector_requires_wss_for_non_loopback():
    async def invoke(request):
        return request.message

    for url in ("ws://remote.example.test/connector", "wss://user:secret@example.test/connector"):
        with pytest.raises(ValueError, match="requires WSS"):
            create_channel_connector(
                url=url, token="test", agent_id="a", tenant_id="t", workspace_id="w", invoke=invoke
            )
