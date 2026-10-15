"""Own a cancellable blocking HTTP SSE connection from asyncio."""

from __future__ import annotations

import asyncio
import socket
from typing import AsyncIterator, Iterator

import requests


def _next_stream_chunk(iterator: Iterator[bytes]) -> bytes | None:
    """Read one non-empty requests chunk without leaking StopIteration to asyncio."""

    while True:
        try:
            chunk = next(iterator)
        except StopIteration:
            return None
        if chunk:
            return chunk


class AgentEngineSSEStream(AsyncIterator[bytes]):
    """Async owner for one dedicated blocking requests SSE connection."""

    def __init__(self, response: requests.Response, session: requests.Session) -> None:
        self._response: requests.Response | None = response
        self._session: requests.Session | None = session
        # Read SSE as logical lines with the smallest requests read size.  A
        # fixed 8 KiB ``iter_content`` block makes short runs appear
        # non-streaming, while ``chunk_size=None`` waits for EOF on urllib3.
        # ``iter_lines`` performs the byte-at-a-time buffering inside the
        # blocking worker and yields complete lines, avoiding one thread hop
        # per byte.  Re-add the delimiter so downstream SSE parsers keep their
        # normal framing, including the blank line between events.
        self._chunks = (line + b"\n" for line in response.iter_lines(chunk_size=1))
        self._closed = False
        # requests/urllib3 close() waits for the buffered reader's lock. A
        # worker blocked in an unlimited SSE read holds that lock, so closing
        # from another thread must first wake the socket. Capture it before
        # EOF can clear urllib3's connection reference. Connection: close
        # responses may only retain it through HTTPResponse's buffered file.
        raw = getattr(response, "raw", None)
        connection = getattr(raw, "_connection", None)
        transport_socket = getattr(connection, "sock", None)
        if not isinstance(transport_socket, socket.socket):
            response_file = getattr(getattr(raw, "_fp", None), "fp", None)
            transport_socket = getattr(getattr(response_file, "raw", None), "_sock", None)
        self._socket = transport_socket if isinstance(transport_socket, socket.socket) else None

    def __aiter__(self) -> "AgentEngineSSEStream":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await asyncio.to_thread(_next_stream_chunk, self._chunks)
        except Exception:
            if self._closed:
                raise StopAsyncIteration from None
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise
        if chunk is None or self._closed:
            await self.aclose()
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        response, session = self._response, self._session
        self._response = None
        self._session = None
        transport_socket, self._socket = self._socket, None

        def close_transport() -> None:
            try:
                if transport_socket is not None:
                    try:
                        transport_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        # EOF or a simultaneous peer disconnect already closed it.
                        pass
                if response is not None:
                    response.close()
            finally:
                if session is not None:
                    session.close()

        await asyncio.to_thread(close_transport)
