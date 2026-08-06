from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import uvicorn
from fastmcp import FastMCP
from sse_starlette.sse import AppStatus
from starlette.middleware.base import BaseHTTPMiddleware


@dataclass
class MCPRequestLog:
    authorization: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class RunningMCPServer:
    url: str
    log: MCPRequestLog


@contextmanager
def run_http_app(app) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not uvicorn_server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not uvicorn_server.started:
        raise RuntimeError("Harness HTTP app failed to start")
    try:
        yield f"http://{host}:{port}"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


@contextmanager
def run_fixture_mcp_server(
    *,
    label: str = "fixture",
    required_token: str = "harness-secret",
) -> Iterator[RunningMCPServer]:
    log = MCPRequestLog()
    server = FastMCP(f"harness-{label}")

    @server.tool
    def lookup(value: str) -> str:
        log.calls.append(("lookup", value))
        return f"{label}:{value}"

    @server.tool
    def forbidden(value: str) -> str:
        log.calls.append(("forbidden", value))
        return f"forbidden:{value}"

    app = server.http_app(path="/mcp", transport="streamable-http")

    class _CaptureAuthorization(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            authorization = request.headers.get("authorization", "")
            log.authorization.append(authorization)
            if required_token and authorization != f"Bearer {required_token}":
                from starlette.responses import JSONResponse

                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(_CaptureAuthorization)
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None

    with run_http_app(app) as base_url:
        try:
            yield RunningMCPServer(url=f"{base_url}/mcp", log=log)
        finally:
            AppStatus.should_exit = False
            AppStatus.should_exit_event = None
