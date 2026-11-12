"""Transport-only SSE keepalive; never creates durable session facts."""

import asyncio
import contextlib
import logging
import math
import os


def heartbeat_seconds():
    raw = os.getenv("KSADK_KERNEL_STREAM_HEARTBEAT_SECONDS", "").strip()
    if not raw:
        return 15.0
    try:
        value = float(raw)
        if math.isfinite(value) and value > 0:
            return value
    except ValueError:
        pass
    logging.getLogger(__name__).warning(
        "Invalid KSADK_KERNEL_STREAM_HEARTBEAT_SECONDS; using 15 seconds"
    )
    return 15.0


async def with_heartbeats(source, *, seconds):
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("heartbeat interval must be positive and finite")
    iterator = source.__aiter__()
    pending = asyncio.create_task(anext(iterator))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=seconds)
            if not done:
                yield None
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = asyncio.create_task(anext(iterator))
            yield item
    finally:
        if not pending.done():
            pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pending
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
