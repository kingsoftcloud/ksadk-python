"""Shared byte framing for model SSE, independent of HTTP and provider schemas.

Transport IDs and retry directives never authorize deduplication or retries.
Only a blank line dispatches data. Model adapters reject unfinished data at EOF
rather than treating a discarded terminal fragment as a complete response.
"""

import asyncio
import codecs
import math
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ksadk._async_iterators import owning_async_iterator

_LINE_END = re.compile(r"\r\n|\r|\n")


class SSEFramingError(ValueError):
    """A fixed diagnostic without provider bytes or credentials."""


class ModelSSEIdleTimeout(TimeoutError):
    """No nonempty model data arrived within the host's read interval."""


@dataclass(frozen=True)
class SSEFrame:
    data: str
    event: str = "message"
    last_event_id: str = ""


class _Framer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.data: list[str] = []
        self.event = ""
        self.last_event_id = ""
        self.after_cr = False

    def feed(self, text: str) -> Iterator[SSEFrame]:
        if not text:
            return
        if self.after_cr:
            if text.startswith("\n"):
                text = text[1:]
            self.after_cr = False
        start = 0
        for end in _LINE_END.finditer(text):
            self.parts.append(text[start:end.start()])
            line = "".join(self.parts)
            self.parts.clear()
            start = end.end()
            self.after_cr = end.group() == "\r" and start == len(text)
            frame = self._line(line)
            if frame is not None:
                yield frame
        if start < len(text):
            self.parts.append(text[start:])

    def _line(self, line: str) -> SSEFrame | None:
        if not line:
            frame = (
                SSEFrame("\n".join(self.data), self.event or "message", self.last_event_id)
                if self.data else None
            )
            self.data.clear()
            self.event = ""
            return frame
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "data":
            self.data.append(value)
        elif name == "event":
            self.event = value
        elif name == "id" and "\x00" not in value:
            self.last_event_id = value
        # Unknown fields and retry are transport controls, not model payloads.
        return None

    def finish(self) -> None:
        pending = "".join(self.parts)
        if self.data or pending == "data" or pending.startswith("data:"):
            raise SSEFramingError("unfinished model SSE data")


async def _decode(chunks: AsyncIterator[bytes], max_bytes: int) -> AsyncIterator[SSEFrame]:
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    framer, total = _Framer(), 0
    async for raw in chunks:
        total += len(raw)
        if total > max_bytes:
            raise SSEFramingError("model SSE byte budget exceeded")
        for frame in framer.feed(decoder.decode(raw)):
            yield frame
    for frame in framer.feed(decoder.decode(b"", final=True)):
        yield frame
    framer.finish()


async def _idle_frames(
    frames: AsyncIterator[SSEFrame], timeout: float | None,
) -> AsyncIterator[SSEFrame]:
    if timeout is None:
        async for frame in frames:
            yield frame
        return
    loop = asyncio.get_running_loop()
    deadline = None
    while True:
        if deadline is None:
            deadline = loop.time() + timeout
        try:
            # wait_for joins the cancelled read and also supports Python 3.10.
            # The outer Effect owner handles a transport that refuses to join.
            frame = await asyncio.wait_for(anext(frames), max(0, deadline - loop.time()))
        except StopAsyncIteration:
            if loop.time() >= deadline:
                raise ModelSSEIdleTimeout("model stream idle timeout") from None
            return
        except asyncio.TimeoutError:
            if loop.time() < deadline:
                raise  # Preserve an unrelated timeout raised by the byte source.
            raise ModelSSEIdleTimeout("model stream idle timeout") from None
        if loop.time() >= deadline:
            # A read may swallow cancellation and return a late frame or EOF.
            raise ModelSSEIdleTimeout("model stream idle timeout")
        yield frame
        if frame.data.strip():
            # Time spent by the consumer is not provider idle time. Private
            # reasoning/tool data counts; comments and blank data do not.
            deadline = None


@asynccontextmanager
async def model_sse_frames(
    chunks: AsyncIterator[bytes], *, max_bytes: int, idle_timeout_seconds: float | None = None,
) -> AsyncIterator[AsyncIterator[SSEFrame]]:
    """Join local iterators on EOF, terminal return, rejection and cancellation.

    UTF-8 is deliberately strict: replacing corrupt bytes inside JSON changes
    model facts. Byte limits belong to the adapter; no transport is opened here.
    The existing HTTP response owner remains responsible for external resources.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("model SSE needs a positive byte budget")
    if idle_timeout_seconds is not None and (
        type(idle_timeout_seconds) not in {int, float}
        or not math.isfinite(idle_timeout_seconds) or idle_timeout_seconds <= 0
    ):
        raise ValueError("model SSE needs a positive idle interval")
    decoded = _decode(chunks, max_bytes)
    iterator = _idle_frames(decoded, idle_timeout_seconds)
    async with owning_async_iterator(chunks, owner="model_sse.bytes"):
        async with owning_async_iterator(decoded, owner="model_sse.decode"):
            async with owning_async_iterator(iterator, owner="model_sse.frames"):
                yield iterator
