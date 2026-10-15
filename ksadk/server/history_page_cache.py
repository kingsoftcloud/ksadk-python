"""Small serialized snapshot cache after per-request session authorization.

Only clean PostgreSQL, non-Kernel history uses this cache. Its key includes the
durable epoch and observed high watermark; append/delete/source changes cannot
return another sequence space. Execution/replay and dirty sessions bypass it.
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import time
import weakref
from collections import OrderedDict

from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget


class HistoryPageCache:
    def __init__(self, maximum_bytes=32 * 1024 * 1024, ttl_seconds=30.0):
        self.maximum_bytes = maximum_bytes
        self.ttl = ttl_seconds
        self.pages = OrderedDict()
        self.bytes = 0
        self.pending = {}
        self.waiters = {}
        self.lookups = {}
        self.lookup_waiters = {}

    async def lookup(self, key, producer):
        """Share only overlapping, identically scoped metadata reads.

        There is deliberately no completed-result cache: the next request
        must re-read ownership and the high watermark. Each caller receives
        its own session object, and cancelling one caller cannot cancel the
        query needed by another caller.
        """
        task = self.lookups.get(key)
        if task is None or task.done():
            if len(self.lookups) >= 32:
                from ksadk.sessions.errors import SessionBackendUnavailable
                raise SessionBackendUnavailable("Concurrent history lookup capacity exhausted")

            async def produce():
                async with asyncio.timeout(2.4):
                    return await producer()

            context = contextvars.copy_context()
            context.run(CURRENT_HISTORY_READ_BUDGET.set, HistoryReadBudget())
            task = context.run(asyncio.create_task, produce())
            self.lookups[key] = task
        self.lookup_waiters[task] = self.lookup_waiters.get(task, 0) + 1
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            self.lookup_waiters[task] -= 1
            if self.lookup_waiters[task] == 0:
                del self.lookup_waiters[task]
                if self.lookups.get(key) is task:
                    del self.lookups[key]
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def read(self, key, producer):
        now = time.monotonic()
        for old_key, (expires, body) in list(self.pages.items()):
            if expires <= now:
                self.bytes -= len(body)
                del self.pages[old_key]
        if key in self.pages:
            self.pages.move_to_end(key)
            return self.pages[key][1]
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= 32:
                from ksadk.sessions.errors import SessionBackendUnavailable
                raise SessionBackendUnavailable("Concurrent history projection capacity exhausted")
            async def produce():
                async with asyncio.timeout(2.4):
                    data = await producer()
                    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            context = contextvars.copy_context()
            context.run(CURRENT_HISTORY_READ_BUDGET.set, HistoryReadBudget())
            task = context.run(asyncio.create_task, produce())
            self.pending[key] = task
        self.waiters[key] = self.waiters.get(key, 0) + 1
        try:
            body = await asyncio.shield(task)
            if len(body) <= self.maximum_bytes and key not in self.pages:
                self.pages[key] = (time.monotonic() + self.ttl, body)
                self.bytes += len(body)
                while self.bytes > self.maximum_bytes or len(self.pages) > 128:
                    _, (_, removed) = self.pages.popitem(last=False)
                    self.bytes -= len(removed)
            return body
        finally:
            self.waiters[key] -= 1
            if self.waiters[key] == 0:
                del self.waiters[key]
                self.pending.pop(key, None)
                if not task.done():
                    task.cancel()
                    # Storage cancellation has its own bounded driver cleanup.
                    await asyncio.gather(task, return_exceptions=True)


_caches = weakref.WeakKeyDictionary()


def cache_for(service):
    cache = _caches.get(service)
    if cache is None:
        cache = HistoryPageCache()
        _caches[service] = cache
    return cache


def eligible(service, is_live):
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime
    return get_agent_kernel_runtime() is None and bounded_pg_read(service, is_live)


def bounded_pg_read(service, is_live):
    """Storage windows are safe for Kernel too; projected page caching is separate."""
    from ksadk.sessions.postgres_service import PostgresSessionService
    return (not is_live
            and isinstance(getattr(service, "primary", service), PostgresSessionService))
