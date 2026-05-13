"""通用远程终端 websocket client。

当前 Hermes 与 OpenClaw runtime proxy 都使用同一套 `ks-terminal.v1`
协议。Hermes 仍保留 `ksadk.hermes_terminal` 的兼容入口；新框架应优先
引用本模块，避免把协议命名绑定到某一个框架。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ksadk.hermes_terminal import (
    TERMINAL_SUBPROTOCOL,
    TERMINAL_WS_PATH,
    TerminalSize,
    build_start_frame,
    build_terminal_ws_url,
    detect_terminal_size,
    run_hermes_terminal_session,
    validate_terminal_exec_argv,
)


async def run_terminal_session(
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
) -> int:
    """Attach to a framework-native terminal session over ks-terminal.v1."""

    return await run_hermes_terminal_session(
        endpoint=endpoint,
        api_key=api_key,
        session_id=session_id,
        insecure=insecure,
        mode=mode,
        argv=argv,
        cwd=cwd,
        options=options,
        stdin=stdin,
        stdout=stdout,
        exec_argv_validator=validate_terminal_exec_argv,
    )


__all__ = [
    "TERMINAL_SUBPROTOCOL",
    "TERMINAL_WS_PATH",
    "TerminalSize",
    "build_start_frame",
    "build_terminal_ws_url",
    "detect_terminal_size",
    "run_terminal_session",
]
