"""WebSocket handshake compatibility and terminal configuration failures."""

import asyncio
import json

import pytest
from fastapi import FastAPI, WebSocket

from ksadk.tunnel import TunnelError, TunnelLimits
from ksadk.tunnel.server import TunnelAgentPolicy, mount_tunnel
from tests.test_mcp_tunnel import connected, connector, serve


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503, 504])
async def test_handshake_status_without_reconnect_is_classified(status):
    class Reject:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    await send({"type": message["type"] + ".complete"})
                    if message["type"] == "lifespan.shutdown":
                        return
            await receive()
            await send({"type": "websocket.http.response.start", "status": status, "headers": []})
            await send({"type": "websocket.http.response.body", "body": b""})

    async with serve(Reject()) as url:
        code = (
            "gateway_unavailable"
            if status in {429, 500, 502, 503, 504}
            else "gateway_handshake_rejected"
        )
        with pytest.raises(TunnelError, match=code):
            await connector(url, "http://127.0.0.1:1/mcp").run(reconnect=False)


async def test_duplicate_connector_is_terminal_and_does_not_retry():
    app = FastAPI()
    registry = mount_tunnel(
        app, policies=[TunnelAgentPolicy("deployment-17", "fake-tunnel-token", ("erp",))]
    )
    try:
        async with serve(app) as url:
            async with connected(connector(url, "http://127.0.0.1:1/mcp")):
                second = connector(url, "http://127.0.0.1:1/mcp")
                async with asyncio.timeout(3):
                    with pytest.raises(TunnelError, match="duplicate_service"):
                        await second.run()
                assert not second.connected.is_set()
                assert len(registry.connections("deployment-17")) == 1
    finally:
        await registry.close()


@pytest.mark.parametrize("generation", ["", "bad/id", 17, None])
async def test_invalid_registration_generation_is_rejected(generation):
    app = FastAPI()

    @app.websocket("/tunnel/connect")
    async def accept(ws: WebSocket):
        await ws.accept()
        await ws.receive_text()
        await ws.send_text(
            json.dumps({"type": "registered", "version": 2, "connector_id": generation})
        )
        try:
            await ws.receive_text()
        except Exception:
            pass

    async with serve(app) as url:
        client = connector(url, "http://127.0.0.1:1/mcp")
        with pytest.raises(TunnelError, match="invalid_registration_ack"):
            await client.run(reconnect=False)
        assert not client.connected.is_set()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0, -1])
@pytest.mark.parametrize("field", ["request_timeout", "send_timeout", "heartbeat_interval"])
def test_invalid_tunnel_time_budgets(field, value):
    with pytest.raises(ValueError):
        TunnelLimits(**{field: value})


@pytest.mark.parametrize("value", [1.5, True, float("nan"), float("inf")])
def test_count_limits_require_positive_integers(value):
    with pytest.raises(ValueError):
        TunnelLimits(max_frame_bytes=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["reconnect_min", "reconnect_max"])
def test_reconnect_budget_must_be_finite(field, value):
    from ksadk.tunnel import LocalMCPService, TunnelConnector

    with pytest.raises(ValueError):
        TunnelConnector(
            "http://localhost:8000",
            token="fixture-token",
            services=[LocalMCPService("test", "http://localhost:8001/mcp")],
            **{field: value},
        )


@pytest.mark.parametrize("grace,expected", [(2.0, "recovered"), (0.0, "rejected")])
async def test_403_recovery_is_bounded_and_requires_prior_registration(grace, expected):
    from tests.test_mcp_tunnel import eventually

    app = FastAPI()
    registry = mount_tunnel(
        app, policies=[TunnelAgentPolicy("deployment-17", "fake-tunnel-token", ("erp",))]
    )
    attempts = 0

    class RollingGateway:
        async def __call__(self, scope, receive, send):
            nonlocal attempts
            if scope["type"] == "websocket":
                attempts += 1
                if attempts in {2, 3}:
                    await receive()
                    await send(
                        {"type": "websocket.http.response.start", "status": 403, "headers": []}
                    )
                    await send({"type": "websocket.http.response.body", "body": b""})
                    return
            await app(scope, receive, send)

    async with serve(RollingGateway()) as url:
        client = connector(url, "http://127.0.0.1:1/mcp")
        client._handshake_grace = grace
        task = asyncio.create_task(client.run())
        try:
            await eventually(lambda: client.connected.is_set())
            await client._ws.close(code=1012)
            if expected == "recovered":
                await eventually(lambda: client._registrations == 2 and client.connected.is_set())
                assert attempts == 4
            else:
                async with asyncio.timeout(3):
                    with pytest.raises(TunnelError, match="gateway_handshake_rejected"):
                        await task
                assert attempts == 2
        finally:
            await client.close()
            await asyncio.gather(task, return_exceptions=True)
            await registry.close()


async def test_initial_403_does_not_enter_recovery_window():
    app = FastAPI()
    attempts = 0

    @app.websocket("/tunnel/connect")
    async def reject(ws: WebSocket):
        nonlocal attempts
        attempts += 1
        await ws.close()

    async with serve(app) as url:
        async with asyncio.timeout(3):
            with pytest.raises(TunnelError, match="gateway_handshake_rejected"):
                await connector(url, "http://127.0.0.1:1/mcp").run()
        assert attempts == 1


async def test_registration_rejection_survives_gateway_stripping_close_code():
    app = FastAPI()
    registry = mount_tunnel(
        app, policies=[TunnelAgentPolicy("deployment-17", "fake-tunnel-token", ("erp",))]
    )

    class Gateway:
        async def __call__(self, scope, receive, send):
            async def strip_close(message):
                if message["type"] == "websocket.close":
                    message = {**message, "code": 1000, "reason": ""}
                await send(message)

            await app(scope, receive, strip_close)

    async with serve(Gateway()) as url:
        client = connector(url, "http://127.0.0.1:1/mcp")
        client._token = "wrong-fixture-token"
        async with asyncio.timeout(3):
            with pytest.raises(TunnelError, match="unauthorized"):
                await client.run()
        assert not client.connected.is_set()
        assert registry.connections("deployment-17") == []
        await registry.close()


def _duplicate_gateway(*, receipt, reject_until=None, rejection_delay=0):
    app = FastAPI()
    attempts = []

    @app.websocket("/tunnel/connect")
    async def accept(ws: WebSocket):
        attempts.append(asyncio.get_running_loop().time())
        attempt = len(attempts)
        await ws.accept()
        await ws.receive_text()
        rejected = attempt > 1 and (reject_until is None or attempt <= reject_until)
        if rejected:
            await asyncio.sleep(rejection_delay)
            if receipt:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "registration_rejected",
                            "version": 2,
                            "error": "duplicate_service",
                        }
                    )
                )
            await ws.close(code=4409)
            return
        await ws.send_text(
            json.dumps(
                {"type": "registered", "version": 2, "connector_id": f"generation-{attempt}"}
            )
        )
        try:
            await ws.receive_text()
        except Exception:
            pass

    return app, attempts


@pytest.mark.parametrize("receipt", [False, True], ids=["close-code", "rejection-receipt"])
async def test_registered_connector_recovers_from_temporary_duplicate_service(receipt):
    from tests.test_mcp_tunnel import eventually

    app, attempts = _duplicate_gateway(receipt=receipt, reject_until=3)
    async with serve(app) as url:
        client = connector(url, "http://127.0.0.1:1/mcp")
        client._handshake_grace = 2
        task = asyncio.create_task(client.run())
        try:
            await eventually(client.connected.is_set)
            await client._ws.close(code=1012)
            await eventually(lambda: task.done() or client._registrations == 2)
            if task.done():
                await task
            assert client.connected.is_set()
            assert len(attempts) == 4
        finally:
            await client.close()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("receipt", [False, True], ids=["close-code", "rejection-receipt"])
async def test_duplicate_service_recovery_deadline_never_extends_on_rejection(receipt):
    from tests.test_mcp_tunnel import eventually

    app, attempts = _duplicate_gateway(receipt=receipt, rejection_delay=0.035)
    async with serve(app) as url:
        client = connector(url, "http://127.0.0.1:1/mcp")
        client._handshake_grace = 0.08
        task = asyncio.create_task(client.run())
        try:
            await eventually(client.connected.is_set)
            start = asyncio.get_running_loop().time()
            await client._ws.close(code=1012)
            async with asyncio.timeout(1):
                with pytest.raises(TunnelError, match="duplicate_service"):
                    await task
            elapsed = asyncio.get_running_loop().time() - start
            assert 0.06 <= elapsed < 0.5
            assert 3 <= len(attempts) <= 6
            assert client._registrations == 1
        finally:
            await client.close()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("receipt", [False, True], ids=["close-code", "rejection-receipt"])
async def test_initial_duplicate_service_refusal_remains_terminal(receipt):
    app = FastAPI()
    attempts = []

    @app.websocket("/tunnel/connect")
    async def reject(ws: WebSocket):
        attempts.append(1)
        await ws.accept()
        await ws.receive_text()
        if receipt:
            await ws.send_text(
                json.dumps(
                    {"type": "registration_rejected", "version": 2, "error": "duplicate_service"}
                )
            )
        await ws.close(code=4409)

    async with serve(app) as url:
        client = connector(url, "http://127.0.0.1:1/mcp")
        async with asyncio.timeout(1):
            with pytest.raises(TunnelError, match="duplicate_service"):
                await client.run()
        assert len(attempts) == 1
        assert client._registrations == 0


async def test_reconnect_false_does_not_recover_after_registration():
    app = FastAPI()
    attempts = []

    @app.websocket("/tunnel/connect")
    async def accept_then_close(ws: WebSocket):
        attempts.append(1)
        await ws.accept()
        await ws.receive_text()
        await ws.send_text(
            json.dumps({"type": "registered", "version": 2, "connector_id": "initial-generation"})
        )
        await ws.close(code=4409)

    async with serve(app) as url:
        client = connector(url, "http://127.0.0.1:1/mcp")
        with pytest.raises(TunnelError, match="duplicate_service"):
            await client.run(reconnect=False)
        assert len(attempts) == 1
        assert client._registrations == 1
