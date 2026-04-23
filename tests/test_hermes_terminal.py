import json
import io
import os
import asyncio
import sys
from types import SimpleNamespace

import pytest

from ksadk.hermes_terminal import (
    TERMINAL_SUBPROTOCOL,
    _recv_loop,
    _send_control,
    _stdin_loop,
    build_start_frame,
    build_terminal_ws_url,
    run_hermes_terminal_session,
    validate_hermes_exec_argv,
    validate_hermes_pairing_argv,
)


def test_build_terminal_ws_url_uses_terminal_path_and_ws_scheme():
    assert (
        build_terminal_ws_url("https://agent.example.com/runtime/")
        == "wss://agent.example.com/runtime/_ksadk/terminal/ws"
    )
    assert build_terminal_ws_url("http://agent.example.com") == "ws://agent.example.com/_ksadk/terminal/ws"


def test_build_start_frame_encodes_protocol_contract():
    payload = json.loads(build_start_frame(mode="exec", argv=["status"], cols=120, rows=40))

    assert payload == {
        "type": "start",
        "mode": "exec",
        "argv": ["status"],
        "cols": 120,
        "rows": 40,
    }


def test_build_start_frame_supports_pairing_mode():
    payload = json.loads(build_start_frame(mode="pairing", argv=["list"], cols=120, rows=40))

    assert payload["mode"] == "pairing"
    assert payload["argv"] == ["list"]


def test_build_start_frame_supports_connect_mode():
    payload = json.loads(build_start_frame(mode="connect", argv=[], cols=120, rows=40))

    assert payload["mode"] == "connect"
    assert payload["argv"] == []


def test_build_start_frame_supports_workspace_cwd():
    payload = json.loads(build_start_frame(mode="tui", argv=[], cols=120, rows=40, cwd="demo-workspace"))

    assert payload["mode"] == "tui"
    assert payload["cwd"] == "demo-workspace"


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["doctor"],
        ["version"],
        ["sessions", "list"],
        ["sessions", "show", "session-1"],
        ["sessions", "export", "session-1"],
        ["config", "show"],
        ["config", "check"],
        ["skills", "list"],
        ["skills", "audit"],
        ["tools", "list"],
        ["insights"],
        ["cron", "list"],
        ["cron", "status"],
        ["gateway", "status"],
    ],
)
def test_validate_hermes_exec_argv_accepts_read_only_subcommands(argv):
    assert validate_hermes_exec_argv(argv) == argv


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["setup"],
        ["auth"],
        ["update"],
        ["install"],
        ["uninstall"],
        ["gateway", "start"],
        ["gateway", "restart"],
        ["cron", "add"],
        ["cron", "remove"],
        ["pairing"],
        ["skills", "install"],
        ["doctor", "--fix"],
        ["config", "query"],
        ["config", "query", "model.context_length"],
        ["status;rm", "-rf"],
        ["sessions", "list", "|", "cat"],
    ],
)
def test_validate_hermes_exec_argv_rejects_mutating_or_shell_like_commands(argv):
    with pytest.raises(ValueError):
        validate_hermes_exec_argv(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["approve", "feishu", "ABC123"],
        ["approve", "weixin", "XYZ789"],
        ["revoke", "feishu", "user-1"],
        ["clear-pending"],
    ],
)
def test_validate_hermes_pairing_argv_accepts_safe_pairing_commands(argv):
    assert validate_hermes_pairing_argv(argv) == argv


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["approve"],
        ["approve", "unknown-platform", "ABC123"],
        ["approve", "feishu", "ABC123", "extra"],
        ["revoke", "unknown", "user-1"],
        ["clear-pending", "now"],
        ["list", "--json"],
        ["approve", "feishu", "A;B"],
        ["pairing", "list"],
    ],
)
def test_validate_hermes_pairing_argv_rejects_unsafe_or_unsupported_commands(argv):
    with pytest.raises(ValueError):
        validate_hermes_pairing_argv(argv)


def test_terminal_session_helpers_are_importable_without_real_tty():
    assert SimpleNamespace is not None


class _FakeReceiveWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FakeSendWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class _FakeTerminalConnection:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeTerminalWebSocket(_FakeSendWebSocket):
    subprotocol = TERMINAL_SUBPROTOCOL

    def __init__(self):
        super().__init__()
        self._messages = [
            json.dumps({"type": "ready"}),
            json.dumps({"type": "exit", "code": 0}),
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _NonTtyDefaultStdin:
    def isatty(self):
        return False

    def fileno(self):  # pragma: no cover - should not be reached
        raise AssertionError("default non-tty stdin should not be read for exec/pairing")


@pytest.mark.asyncio
async def test_recv_loop_writes_binary_output_and_returns_exit_code():
    ws = _FakeReceiveWebSocket([b"hello", json.dumps({"type": "ready"}), json.dumps({"type": "exit", "code": 7})])
    stdout = io.BytesIO()

    exit_code = await _recv_loop(ws, stdout)

    assert exit_code == 7
    assert stdout.getvalue() == b"hello"


@pytest.mark.asyncio
async def test_send_control_encodes_text_control_frame():
    ws = _FakeSendWebSocket()

    await _send_control(ws, {"type": "resize", "cols": 100, "rows": 30})
    await _send_control(ws, {"type": "signal", "signal": "SIGINT"})

    assert [json.loads(item) for item in ws.sent] == [
        {"type": "resize", "cols": 100, "rows": 30},
        {"type": "signal", "signal": "SIGINT"},
    ]


@pytest.mark.asyncio
async def test_stdin_loop_sends_binary_stdin_and_eof_control_frame():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"abc")
    os.close(write_fd)
    reader = os.fdopen(read_fd, "rb", closefd=True)
    ws = _FakeSendWebSocket()

    try:
        await _stdin_loop(ws, reader)
    finally:
        reader.close()

    assert ws.sent[0] == b"abc"
    assert json.loads(ws.sent[1]) == {"type": "stdin_eof"}


@pytest.mark.asyncio
async def test_terminal_session_cancels_blocked_stdin_after_remote_exit(monkeypatch):
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb", closefd=True)
    fake_ws = _FakeTerminalWebSocket()

    async def _fake_connect(*_args, **_kwargs):
        return _FakeTerminalConnection(fake_ws)

    monkeypatch.setattr("ksadk.hermes_terminal._connect_websocket", _fake_connect)

    try:
        exit_code = await asyncio.wait_for(
            run_hermes_terminal_session(
                endpoint="https://agent.example.com",
                mode="tui",
                stdin=reader,
                stdout=io.BytesIO(),
            ),
            timeout=1,
        )
    finally:
        os.close(write_fd)
        reader.close()

    assert exit_code == 0
    assert json.loads(fake_ws.sent[0])["mode"] == "tui"


@pytest.mark.asyncio
async def test_exec_session_does_not_read_default_non_tty_stdin(monkeypatch):
    fake_ws = _FakeTerminalWebSocket()

    async def _fake_connect(*_args, **_kwargs):
        return _FakeTerminalConnection(fake_ws)

    monkeypatch.setattr("ksadk.hermes_terminal._connect_websocket", _fake_connect)
    monkeypatch.setattr(sys, "stdin", _NonTtyDefaultStdin())

    exit_code = await run_hermes_terminal_session(
        endpoint="https://agent.example.com",
        mode="exec",
        argv=["status"],
        stdout=io.BytesIO(),
    )

    assert exit_code == 0
    assert json.loads(fake_ws.sent[0])["mode"] == "exec"
    assert json.loads(fake_ws.sent[1]) == {"type": "stdin_eof"}
