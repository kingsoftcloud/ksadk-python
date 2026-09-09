"""Private Unix socket ingress for the existing host's resource broker."""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
from pathlib import Path

from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.ipc import MAX_FRAME_BYTES, FrameError, decode_body, encode_frame


class ResourceSocketServer:
    def __init__(self, broker: ResourceBroker, *, max_connections: int = 64):
        if type(max_connections) is not int or not 1 <= max_connections <= 1024:
            raise ValueError("Socket connection limit must be 1..1024")
        self._broker = broker
        self._max_connections = max_connections
        self._directory: tempfile.TemporaryDirectory | None = None
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task] = set()
        self._closing = False
        self.path: Path | None = None

    async def start(self) -> Path:
        if os.name != "posix":
            raise RuntimeError("RESOURCE_IPC_UNSUPPORTED")
        if self._directory is not None or self._closing:
            raise RuntimeError("Resource socket server cannot be restarted")
        directory = tempfile.TemporaryDirectory(prefix="ksr-")
        self._directory = directory
        self.path = Path(directory.name) / "broker.sock"
        try:
            os.chmod(directory.name, 0o700)
            # Private parent directory prevents access during the bind/chmod gap.
            self._server = await asyncio.start_unix_server(self._accept, path=str(self.path))
            os.chmod(self.path, 0o600)
            return self.path
        except BaseException:
            await self.aclose()
            raise

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        if self._closing or len(self._clients) >= self._max_connections:
            writer.close()
            await writer.wait_closed()
            return
        self._clients.add(task)
        try:
            while not self._closing:
                payload = await asyncio.wait_for(self._read(reader), 15)
                if payload is None:
                    return
                response = await self._broker.dispatch(payload)
                writer.write(encode_frame(response))
                await asyncio.wait_for(writer.drain(), 5)
        except (
            FrameError,
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
        ):
            # Closing rejects malformed/slow ingress; never echo raw input.
            pass
        finally:
            self._clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    async def _read(reader: asyncio.StreamReader) -> dict | None:
        try:
            prefix = await reader.readexactly(4)
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return None
            raise
        (size,) = struct.unpack("!I", prefix)
        if not 0 < size <= MAX_FRAME_BYTES:
            raise FrameError("Invalid IPC frame length")
        return decode_body(await reader.readexactly(size))

    async def aclose(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._broker.revoke_generation()
        clients = tuple(self._clients)
        for task in clients:
            task.cancel()
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        if self._directory is not None:
            self._directory.cleanup()
