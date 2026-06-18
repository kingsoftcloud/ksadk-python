import io
import json
import os

import pytest
import websockets

from ksadk.hermes_terminal import TERMINAL_SUBPROTOCOL, run_hermes_terminal_session


@pytest.mark.asyncio
async def test_terminal_session_real_websocket_exec_round_trip():
    observed = {}

    async def _handler(ws):
        observed["subprotocol"] = ws.subprotocol
        observed["start"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "ready"}))
        observed["stdin"] = await ws.recv()
        observed["eof"] = json.loads(await ws.recv())
        await ws.send(b"status ok\n")
        await ws.send(json.dumps({"type": "exit", "code": 0}))

    server = await websockets.serve(
        _handler,
        "127.0.0.1",
        0,
        subprotocols=[TERMINAL_SUBPROTOCOL],
    )
    port = server.sockets[0].getsockname()[1]
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"abc")
    os.close(write_fd)
    stdin = os.fdopen(read_fd, "rb", closefd=True)
    stdout = io.BytesIO()

    try:
        exit_code = await run_hermes_terminal_session(
            endpoint=f"http://127.0.0.1:{port}",
            mode="exec",
            argv=["status"],
            stdin=stdin,
            stdout=stdout,
        )
    finally:
        stdin.close()
        server.close()
        await server.wait_closed()

    assert exit_code == 0
    assert observed["subprotocol"] == TERMINAL_SUBPROTOCOL
    assert observed["start"]["mode"] == "exec"
    assert observed["start"]["argv"] == ["status"]
    assert observed["stdin"] == b"abc"
    assert observed["eof"] == {"type": "stdin_eof"}
    assert stdout.getvalue() == b"status ok\n"


@pytest.mark.asyncio
async def test_terminal_session_real_websocket_pairing_start_frame():
    observed = {}

    async def _handler(ws):
        observed["start"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "ready"}))
        await ws.send(json.dumps({"type": "exit", "code": 0}))

    server = await websockets.serve(
        _handler,
        "127.0.0.1",
        0,
        subprotocols=[TERMINAL_SUBPROTOCOL],
    )
    port = server.sockets[0].getsockname()[1]

    try:
        exit_code = await run_hermes_terminal_session(
            endpoint=f"http://127.0.0.1:{port}",
            mode="pairing",
            argv=["list"],
            stdin=io.BytesIO(),
            stdout=io.BytesIO(),
        )
    finally:
        server.close()
        await server.wait_closed()

    assert exit_code == 0
    assert observed["start"]["mode"] == "pairing"
    assert observed["start"]["argv"] == ["list"]


@pytest.mark.asyncio
async def test_terminal_session_real_websocket_connect_start_frame():
    observed = {}

    async def _handler(ws):
        observed["start"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "ready"}))
        await ws.send(json.dumps({"type": "exit", "code": 0}))

    server = await websockets.serve(
        _handler,
        "127.0.0.1",
        0,
        subprotocols=[TERMINAL_SUBPROTOCOL],
    )
    port = server.sockets[0].getsockname()[1]

    try:
        exit_code = await run_hermes_terminal_session(
            endpoint=f"http://127.0.0.1:{port}",
            mode="connect",
            stdin=io.BytesIO(),
            stdout=io.BytesIO(),
        )
    finally:
        server.close()
        await server.wait_closed()

    assert exit_code == 0
    assert observed["start"]["mode"] == "connect"
    assert observed["start"]["argv"] == []


@pytest.mark.asyncio
async def test_terminal_session_real_websocket_tui_start_frame_carries_cwd():
    observed = {}

    async def _handler(ws):
        observed["start"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "ready"}))
        await ws.send(json.dumps({"type": "exit", "code": 0}))

    server = await websockets.serve(
        _handler,
        "127.0.0.1",
        0,
        subprotocols=[TERMINAL_SUBPROTOCOL],
    )
    port = server.sockets[0].getsockname()[1]

    try:
        exit_code = await run_hermes_terminal_session(
            endpoint=f"http://127.0.0.1:{port}",
            mode="tui",
            cwd="demo-workspace",
            stdin=io.BytesIO(),
            stdout=io.BytesIO(),
        )
    finally:
        server.close()
        await server.wait_closed()

    assert exit_code == 0
    assert observed["start"]["mode"] == "tui"
    assert observed["start"]["cwd"] == "demo-workspace"
