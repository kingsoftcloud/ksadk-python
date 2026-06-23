"""Hermes native terminal websocket client helpers."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import io
import json
import os
import shutil
import signal
import ssl
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from ksadk.terminal_exec_policy import (
    GENERIC_TERMINAL_EXEC_POLICY,
    HERMES_TERMINAL_EXEC_POLICY,
    SHELL_METACHARS,
    TerminalExecPolicy,
)
from ksadk.terminal_exec_policy import (
    validate_terminal_exec_argv as validate_exec_argv_with_policy,
)

TERMINAL_SUBPROTOCOL = "ks-terminal.v1"
TERMINAL_WS_PATH = "/_ksadk/terminal/ws"

_PAIRING_PLATFORMS = {
    "discord",
    "dingtalk",
    "email",
    "feishu",
    "homeassistant",
    "mattermost",
    "matrix",
    "signal",
    "slack",
    "telegram",
    "wecom",
    "wecom_callback",
    "webhook",
    "weixin",
    "whatsapp",
    "wpsxiezuo",
}

_WINDOWS_INPUT_FLAGS = {
    "ENABLE_PROCESSED_INPUT": 0x0001,
    "ENABLE_LINE_INPUT": 0x0002,
    "ENABLE_ECHO_INPUT": 0x0004,
    "ENABLE_QUICK_EDIT_MODE": 0x0040,
    "ENABLE_EXTENDED_FLAGS": 0x0080,
    "ENABLE_VIRTUAL_TERMINAL_INPUT": 0x0200,
}


@dataclass(frozen=True)
class TerminalSize:
    cols: int
    rows: int


def build_terminal_ws_url(endpoint: str) -> str:
    """Build the Hermes native terminal websocket URL for an agent endpoint."""
    parsed = urlsplit((endpoint or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid endpoint: {endpoint}")

    if parsed.scheme == "https":
        ws_scheme = "wss"
    elif parsed.scheme == "http":
        ws_scheme = "ws"
    else:
        ws_scheme = parsed.scheme

    base_path = parsed.path.rstrip("/")
    ws_path = f"{base_path}{TERMINAL_WS_PATH}" if base_path else TERMINAL_WS_PATH
    return urlunsplit((ws_scheme, parsed.netloc, ws_path, "", ""))


def build_start_frame(
    *,
    mode: str,
    argv: Sequence[str],
    cols: int,
    rows: int,
    cwd: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> str:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"tui", "exec", "pairing", "connect"}:
        raise ValueError(f"unsupported terminal mode: {mode}")
    payload = {
        "type": "start",
        "mode": normalized_mode,
        "argv": [str(item) for item in argv],
        "cols": int(cols),
        "rows": int(rows),
    }
    normalized_cwd = str(cwd or "").strip()
    if normalized_cwd:
        payload["cwd"] = normalized_cwd
    if options:
        payload["options"] = dict(options)
    return json.dumps(payload, ensure_ascii=False)


def detect_terminal_size() -> TerminalSize:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return TerminalSize(cols=int(size.columns or 80), rows=int(size.lines or 24))


def validate_hermes_exec_argv(argv: Iterable[str]) -> list[str]:
    return validate_exec_argv_with_policy(argv, policy=HERMES_TERMINAL_EXEC_POLICY)


def validate_terminal_exec_argv(argv: Iterable[str]) -> list[str]:
    return validate_exec_argv_with_policy(argv, policy=GENERIC_TERMINAL_EXEC_POLICY)


def build_terminal_exec_validator(policy: TerminalExecPolicy) -> Callable[[Iterable[str]], list[str]]:
    def _validator(argv: Iterable[str]) -> list[str]:
        return validate_exec_argv_with_policy(argv, policy=policy)

    return _validator


def validate_hermes_pairing_argv(argv: Iterable[str]) -> list[str]:
    normalized = [str(item).strip() for item in argv]
    if not normalized:
        raise ValueError("Hermes pairing requires a subcommand")
    for item in normalized:
        if not item:
            raise ValueError("Hermes pairing argv contains an empty argument")
        if item.startswith("-"):
            raise ValueError(f"Hermes pairing does not allow shell/options: {item}")
        if any(char in SHELL_METACHARS for char in item):
            raise ValueError(f"Hermes pairing does not allow shell metacharacters: {item}")

    action = normalized[0]
    if action == "list":
        if len(normalized) != 1:
            raise ValueError(f"Hermes pairing subcommand is not allowed: {' '.join(normalized)}")
        return normalized
    if action == "clear-pending":
        if len(normalized) != 1:
            raise ValueError(f"Hermes pairing subcommand is not allowed: {' '.join(normalized)}")
        return normalized
    if action in {"approve", "revoke"}:
        if len(normalized) != 3:
            raise ValueError(f"Hermes pairing subcommand is not allowed: {' '.join(normalized)}")
        platform = normalized[1].lower()
        if platform not in _PAIRING_PLATFORMS:
            raise ValueError(f"Hermes pairing platform is not allowed: {normalized[1]}")
        normalized[1] = platform
        return normalized
    raise ValueError(f"Hermes pairing subcommand is not allowed: {' '.join(normalized)}")


@contextlib.contextmanager
def _raw_terminal(stdin: Any):
    if not hasattr(stdin, "fileno") or not hasattr(stdin, "isatty") or not stdin.isatty():
        yield
        return

    if sys.platform == "win32":
        with _windows_raw_terminal(stdin):
            yield
        return

    try:
        import termios
        import tty
    except ImportError:
        yield
        return

    fd = stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


@contextlib.contextmanager
def _windows_raw_terminal(stdin: Any, *, kernel32: Any | None = None, msvcrt_module: Any | None = None):
    if not hasattr(stdin, "fileno") or not hasattr(stdin, "isatty") or not stdin.isatty():
        yield
        return

    try:
        if kernel32 is None:
            kernel32 = ctypes.windll.kernel32
        if msvcrt_module is None:
            import msvcrt as msvcrt_module  # type: ignore[no-redef]
    except Exception:
        yield
        return

    try:
        fd = stdin.fileno()
        handle = msvcrt_module.get_osfhandle(fd)
    except Exception:
        yield
        return

    original_mode = wintypes.DWORD()
    try:
        if not kernel32.GetConsoleMode(handle, ctypes.byref(original_mode)):
            yield
            return
    except Exception:
        yield
        return

    raw_mode = int(original_mode.value)
    raw_mode &= ~(
        _WINDOWS_INPUT_FLAGS["ENABLE_PROCESSED_INPUT"]
        | _WINDOWS_INPUT_FLAGS["ENABLE_LINE_INPUT"]
        | _WINDOWS_INPUT_FLAGS["ENABLE_ECHO_INPUT"]
        | _WINDOWS_INPUT_FLAGS["ENABLE_QUICK_EDIT_MODE"]
    )
    raw_mode |= (
        _WINDOWS_INPUT_FLAGS["ENABLE_EXTENDED_FLAGS"]
        | _WINDOWS_INPUT_FLAGS["ENABLE_VIRTUAL_TERMINAL_INPUT"]
    )

    try:
        if not kernel32.SetConsoleMode(handle, raw_mode):
            yield
            return
    except Exception:
        yield
        return

    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            kernel32.SetConsoleMode(handle, int(original_mode.value))


async def _connect_websocket(ws_url: str, headers: dict[str, str], ssl_context: ssl.SSLContext | None):
    try:
        import websockets
    except ImportError as e:
        raise RuntimeError("missing dependency websockets, please install ksadk with websocket support") from e

    kwargs: dict[str, Any] = {"subprotocols": [TERMINAL_SUBPROTOCOL]}
    if ssl_context is not None:
        kwargs["ssl"] = ssl_context
    try:
        return websockets.connect(ws_url, additional_headers=headers or None, **kwargs)
    except TypeError:
        return websockets.connect(ws_url, extra_headers=headers or None, **kwargs)


def _write_stdout(stdout: Any, data: bytes) -> None:
    if not data:
        return
    buffer = getattr(stdout, "buffer", stdout)
    try:
        buffer.write(data)
        buffer.flush()
        return
    except TypeError:
        text = data.decode("utf-8", errors="replace")
        stdout.write(text)
        stdout.flush()


async def _recv_loop(ws: Any, stdout: Any) -> int:
    async for message in ws:
        if isinstance(message, bytes):
            _write_stdout(stdout, message)
            continue

        if not isinstance(message, str):
            continue
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            _write_stdout(stdout, message.encode("utf-8", errors="replace"))
            continue
        if not isinstance(payload, dict):
            continue

        msg_type = payload.get("type")
        if msg_type == "ready":
            continue
        if msg_type == "error":
            raise RuntimeError(str(payload.get("message") or "Hermes terminal error"))
        if msg_type == "exit":
            return int(payload.get("code") or payload.get("exit_code") or 0)
    return 0


async def _stdin_loop(ws: Any, stdin: Any) -> None:
    if not hasattr(stdin, "fileno"):
        return
    try:
        fd = stdin.fileno()
    except (OSError, ValueError, io.UnsupportedOperation):
        return
    while True:
        chunk = await _read_stdin_chunk(fd)
        if not chunk:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"type": "stdin_eof"}, ensure_ascii=False))
            return
        await ws.send(chunk)


async def _read_stdin_chunk(fd: int) -> bytes:
    """Read stdin in a way that can be cancelled when the remote PTY exits."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()

    def _on_readable() -> None:
        if future.done():
            return
        try:
            future.set_result(os.read(fd, 4096))
        except Exception as exc:  # pragma: no cover - defensive fd error path
            future.set_exception(exc)

    try:
        loop.add_reader(fd, _on_readable)
    except (NotImplementedError, RuntimeError):
        return await asyncio.to_thread(os.read, fd, 4096)

    try:
        return await future
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(fd)


async def _send_control(ws: Any, payload: dict[str, Any]) -> None:
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def run_hermes_terminal_session(
    *,
    endpoint: str,
    api_key: str | None = None,
    session_id: str | None = None,
    insecure: bool = False,
    mode: str = "tui",
    argv: Sequence[str] | None = None,
    cwd: str | None = None,
    options: Mapping[str, Any] | None = None,
    stdin: Any | None = None,
    stdout: Any | None = None,
    exec_argv_validator: Callable[[Iterable[str]], list[str]] | None = None,
) -> int:
    """Attach to a Hermes native terminal session over the platform websocket."""
    normalized_mode = str(mode or "tui").strip().lower()
    normalized_argv = list(argv or [])
    if normalized_mode == "exec":
        validator = exec_argv_validator or validate_hermes_exec_argv
        normalized_argv = validator(normalized_argv)
    elif normalized_mode == "pairing":
        normalized_argv = validate_hermes_pairing_argv(normalized_argv)
    elif normalized_mode == "connect":
        if normalized_argv:
            raise ValueError(f"Hermes connect does not allow argv: {' '.join(normalized_argv)}")
    elif normalized_mode != "tui":
        raise ValueError(f"unsupported terminal mode: {mode}")

    stdin_was_provided = stdin is not None
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    ws_url = build_terminal_ws_url(endpoint)

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if session_id:
        headers["X-Session-Id"] = session_id

    ssl_context = None
    if insecure and ws_url.startswith("wss://"):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    connection = await _connect_websocket(ws_url, headers, ssl_context)
    async with connection as ws:
        if getattr(ws, "subprotocol", TERMINAL_SUBPROTOCOL) not in {None, TERMINAL_SUBPROTOCOL}:
            raise RuntimeError(f"server rejected terminal subprotocol {TERMINAL_SUBPROTOCOL}")

        size = detect_terminal_size()
        await ws.send(
            build_start_frame(
                mode=normalized_mode,
                argv=normalized_argv,
                cols=size.cols,
                rows=size.rows,
                cwd=cwd,
                options=options,
            )
        )

        loop = asyncio.get_running_loop()
        previous_winch = None
        previous_int = None

        def _schedule_resize(*_args):
            current = detect_terminal_size()
            loop.create_task(
                _send_control(ws, {"type": "resize", "cols": current.cols, "rows": current.rows})
            )

        def _schedule_sigint(*_args):
            loop.create_task(_send_control(ws, {"type": "signal", "signal": "SIGINT"}))

        try:
            if hasattr(signal, "SIGWINCH"):
                previous_winch = signal.getsignal(signal.SIGWINCH)
                signal.signal(signal.SIGWINCH, _schedule_resize)
            previous_int = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _schedule_sigint)
        except (ValueError, RuntimeError):
            previous_winch = None
            previous_int = None

        try:
            with _raw_terminal(stdin):
                recv_task = asyncio.create_task(_recv_loop(ws, stdout))
                if (
                    normalized_mode in {"exec", "pairing"}
                    and not stdin_was_provided
                    and (not hasattr(stdin, "isatty") or not stdin.isatty())
                ):
                    await _send_control(ws, {"type": "stdin_eof"})
                    return int(await recv_task or 0)

                stdin_task = asyncio.create_task(_stdin_loop(ws, stdin))
                done, pending = await asyncio.wait(
                    {recv_task, stdin_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if recv_task in done:
                    stdin_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stdin_task
                    return int(recv_task.result() or 0)

                # stdin EOF is only an input-side event; keep receiving until
                # the remote PTY exits so command output and exit code are not lost.
                stdin_task.result()
                return int(await recv_task or 0)
        finally:
            if previous_winch is not None and hasattr(signal, "SIGWINCH"):
                with contextlib.suppress(Exception):
                    signal.signal(signal.SIGWINCH, previous_winch)
            if previous_int is not None:
                with contextlib.suppress(Exception):
                    signal.signal(signal.SIGINT, previous_int)
