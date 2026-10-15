"""Owned, bounded LSP stdio transport with no workspace command execution."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any

from .contracts import LanguageServerProfile

MAX_MESSAGE_BYTES = 1024 * 1024


class LspFailure(Exception):
    def __init__(self, code: str = "language_server_unavailable"):
        self.code = code
        super().__init__(code)


class LspConnection:
    def __init__(self, profile: LanguageServerProfile, root: Path, notify):
        self.profile, self.root, self.notify = profile, root, notify
        self.process: asyncio.subprocess.Process | None = None
        self.tasks: list[asyncio.Task] = []
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.write_lock = asyncio.Lock()
        self.closed = False
        self.failed = False

    async def start(self) -> dict:
        try:
            for resource in self.profile.resources:
                resource.validate_current()
            launching = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *self.profile.argv,
                    cwd=self.root,
                    env={
                        key: os.environ[key]
                        for key in ("PATH", "LANG", "LC_ALL")
                        if key in os.environ
                    },
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix",
                )
            )
            try:
                self.process = await asyncio.shield(launching)
            except asyncio.CancelledError:
                self.process = await launching
                raise
            self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._drain())]
            result = await self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": self.root.as_uri(),
                    "workspaceFolders": [{"uri": self.root.as_uri(), "name": "workspace"}],
                    "initializationOptions": self.profile.initialization,
                    "capabilities": {
                        "workspace": {"configuration": True, "workspaceFolders": True},
                        "textDocument": {
                            "publishDiagnostics": {"versionSupport": True},
                            "diagnostic": {
                                "dynamicRegistration": True,
                                "relatedDocumentSupport": True,
                            },
                            "synchronization": {"didSave": True},
                        },
                    },
                },
            )
            if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
                raise LspFailure("language_server_protocol_error")
            await self.send("initialized", {})
            await self.send(
                "workspace/didChangeConfiguration", {"settings": self.profile.initialization}
            )
            return result["capabilities"]
        except BaseException:
            await self.close()
            raise

    async def _write(self, value: dict) -> None:
        if self.closed or self.process is None or self.process.stdin is None:
            raise LspFailure()
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(data) > MAX_MESSAGE_BYTES:
            raise LspFailure("language_server_message_limit")
        async with self.write_lock:
            self.process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            await self.process.stdin.drain()

    async def send(self, method: str, params: dict) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: dict) -> Any:
        self.next_id += 1
        identity = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": identity, "method": method, "params": params}
            )
            return await future
        finally:
            self.pending.pop(identity, None)

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                header = await self.process.stdout.readuntil(b"\r\n\r\n")
                lengths = [
                    line.split(b":", 1)[1].strip()
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ]
                if len(header) > 8192 or len(lengths) != 1:
                    raise LspFailure("language_server_protocol_error")
                length = int(lengths[0])
                if length < 1 or length > MAX_MESSAGE_BYTES:
                    raise LspFailure("language_server_message_limit")
                value = json.loads(await self.process.stdout.readexactly(length))
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    raise LspFailure("language_server_protocol_error")
                if "method" in value:
                    if "id" in value:
                        await self._server_request(value)
                    else:
                        self.notify(value.get("method"), value.get("params", {}))
                else:
                    identity = value.get("id")
                    future = self.pending.get(identity) if type(identity) is int else None
                    if future is not None and not future.done():
                        if "error" in value:
                            future.set_exception(LspFailure("language_server_request_error"))
                        else:
                            future.set_result(value.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(LspFailure("language_server_protocol_error"))
            self.notify("host/connectionLost", {})

    async def _server_request(self, value: dict) -> None:
        method, params = value["method"], value.get("params", {})
        result: Any = None
        if method == "workspace/configuration":
            result = []
            for item in params.get("items", []):
                setting: Any = self.profile.initialization
                for part in item.get("section", "").split("."):
                    if part:
                        setting = setting.get(part) if isinstance(setting, dict) else None
                result.append(setting)
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.root.as_uri(), "name": "workspace"}]
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "diagnostic client is read-only"}
        elif method == "client/registerCapability":
            self.notify(method, params)
        elif method not in {
            "window/workDoneProgress/create",
            "workspace/diagnostic/refresh",
            "client/unregisterCapability",
        }:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": value["id"],
                    "error": {"code": -32601, "message": "Method not supported"},
                }
            )
            return
        await self._write({"jsonrpc": "2.0", "id": value["id"], "result": result})

    async def _drain(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while await self.process.stderr.read(8192):
            pass

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process is not None:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
            elif self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(LspFailure())
