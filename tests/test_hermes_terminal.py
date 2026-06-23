import asyncio
import contextlib
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

import ksadk.hermes_terminal as hermes_terminal
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
from ksadk.terminal_client import run_terminal_session
from ksadk.terminal_exec_policy import (
    OPENCLAW_TERMINAL_EXEC_POLICY,
)
from ksadk.terminal_exec_policy import (
    validate_terminal_exec_argv as validate_exec_argv_with_policy,
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


def test_build_start_frame_supports_whitelisted_terminal_options():
    payload = json.loads(
        build_start_frame(
            mode="tui",
            argv=[],
            cols=120,
            rows=40,
            options={
                "message": "你好",
                "thinking": "medium",
                "history_limit": 50,
                "timeout_ms": 30000,
                "deliver": True,
            },
        )
    )

    assert payload["mode"] == "tui"
    assert payload["options"] == {
        "message": "你好",
        "thinking": "medium",
        "history_limit": 50,
        "timeout_ms": 30000,
        "deliver": True,
    }


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


def test_validate_hermes_exec_argv_accepts_env_allowlisted_prefix(monkeypatch):
    monkeypatch.setenv("KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST", "config")

    assert validate_hermes_exec_argv(["config", "set", "memory.provider", "hindsight"]) == [
        "config",
        "set",
        "memory.provider",
        "hindsight",
    ]


def test_validate_hermes_exec_argv_env_allowlist_still_rejects_shell_metacharacters(monkeypatch):
    monkeypatch.setenv("KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST", "config set")

    with pytest.raises(ValueError):
        validate_hermes_exec_argv(["config", "set", "memory.provider", "hindsight;rm"])


def test_validate_terminal_exec_argv_defaults_to_common_commands(monkeypatch):
    monkeypatch.delenv("KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST", raising=False)

    assert hermes_terminal.validate_terminal_exec_argv(["ls", "-la"]) == ["ls", "-la"]
    assert hermes_terminal.validate_terminal_exec_argv(["git", "status", "--short"]) == [
        "git",
        "status",
        "--short",
    ]
    with pytest.raises(ValueError):
        hermes_terminal.validate_terminal_exec_argv(["openclaw", "config", "set", "memory.provider", "hindsight"])


def test_validate_terminal_exec_argv_rejection_mentions_allowlist_env(monkeypatch):
    monkeypatch.delenv("KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST", raising=False)

    with pytest.raises(ValueError) as exc_info:
        hermes_terminal.validate_terminal_exec_argv(["openclaw", "config", "set"])

    message = str(exc_info.value)
    assert "KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST='openclaw'" in message
    assert "KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST='*'" in message


def test_validate_terminal_exec_argv_accepts_common_env_allowlisted_prefix(monkeypatch):
    monkeypatch.setenv("KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST", "openclaw config")

    assert hermes_terminal.validate_terminal_exec_argv(
        ["openclaw", "config", "set", "memory.provider", "hindsight"]
    ) == ["openclaw", "config", "set", "memory.provider", "hindsight"]


def test_validate_terminal_exec_argv_accepts_wildcard_env_allowlist(monkeypatch):
    monkeypatch.setenv("KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST", "*")

    assert hermes_terminal.validate_terminal_exec_argv(["python", "-c", "print('ok')"]) == [
        "python",
        "-c",
        "print('ok')",
    ]


def test_openclaw_exec_policy_allows_remote_cli_fallback_by_default(monkeypatch):
    monkeypatch.delenv("KSADK_TERMINAL_EXEC_SUBCOMMAND_ALLOWLIST", raising=False)

    assert validate_exec_argv_with_policy(
        ["openclaw", "channels", "login", "--channel", "openclaw-weixin"],
        policy=OPENCLAW_TERMINAL_EXEC_POLICY,
    ) == ["openclaw", "channels", "login", "--channel", "openclaw-weixin"]


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["approve", "feishu", "ABC123"],
        ["approve", "weixin", "XYZ789"],
        ["approve", "wpsxiezuo", "WPS123"],
        ["revoke", "feishu", "user-1"],
        ["revoke", "wpsxiezuo", "user-1"],
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


class _FakeKernel32:
    def __init__(self, mode: int):
        self.mode = mode
        self.handles = []
        self.set_modes = []

    def GetConsoleMode(self, handle, mode_ptr):
        self.handles.append(handle)
        mode_ptr._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_modes.append((handle, mode))
        return 1


class _FakeWindowsStdin:
    def __init__(self, fd: int = 11):
        self._fd = fd

    def isatty(self):
        return True

    def fileno(self):
        return self._fd


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


def test_windows_raw_terminal_enables_console_raw_mode_and_restores(monkeypatch):
    stdin = _FakeWindowsStdin()
    fake_kernel32 = _FakeKernel32(mode=0x00FF)
    fake_msvcrt = SimpleNamespace(get_osfhandle=lambda fd: fd + 1000)

    with hermes_terminal._windows_raw_terminal(
        stdin,
        kernel32=fake_kernel32,
        msvcrt_module=fake_msvcrt,
    ):
        pass

    assert fake_kernel32.handles == [1011]
    assert fake_kernel32.set_modes[0] == (1011, 0x02B8)
    assert fake_kernel32.set_modes[1] == (1011, 0x00FF)


@pytest.mark.asyncio
async def test_hermes_terminal_session_uses_windows_raw_terminal_on_windows(monkeypatch):
    fake_ws = _FakeTerminalWebSocket()
    fake_stdin = _FakeWindowsStdin()
    entered = []

    @contextlib.contextmanager
    def _fake_windows_raw_terminal(stdin, **_kwargs):
        entered.append(stdin)
        yield

    async def _fake_connect(*_args, **_kwargs):
        return _FakeTerminalConnection(fake_ws)

    monkeypatch.setattr("ksadk.hermes_terminal._connect_websocket", _fake_connect)
    monkeypatch.setattr(hermes_terminal.sys, "platform", "win32")
    monkeypatch.setattr(hermes_terminal, "_windows_raw_terminal", _fake_windows_raw_terminal)
    monkeypatch.setattr(hermes_terminal, "_read_stdin_chunk", lambda _fd: asyncio.sleep(0, result=b""))

    exit_code = await run_hermes_terminal_session(
        endpoint="https://agent.example.com",
        mode="exec",
        argv=["status"],
        stdin=fake_stdin,
        stdout=io.BytesIO(),
    )

    assert exit_code == 0
    assert entered == [fake_stdin]


@pytest.mark.asyncio
async def test_terminal_exec_with_openclaw_policy_allows_openclaw_cli_argv(monkeypatch):
    fake_ws = _FakeTerminalWebSocket()

    async def _fake_connect(*_args, **_kwargs):
        return _FakeTerminalConnection(fake_ws)

    monkeypatch.setattr("ksadk.hermes_terminal._connect_websocket", _fake_connect)
    monkeypatch.setattr(sys, "stdin", _NonTtyDefaultStdin())

    exit_code = await run_terminal_session(
        endpoint="https://agent.example.com",
        mode="exec",
        argv=["openclaw", "channels", "login", "--channel", "openclaw-weixin"],
        exec_policy=OPENCLAW_TERMINAL_EXEC_POLICY,
        stdout=io.BytesIO(),
    )

    assert exit_code == 0
    assert json.loads(fake_ws.sent[0]) == {
        "type": "start",
        "mode": "exec",
        "argv": ["openclaw", "channels", "login", "--channel", "openclaw-weixin"],
        "cols": 80,
        "rows": 24,
    }
