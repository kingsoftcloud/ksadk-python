"""Session 级并发锁与 stale guard（方案 §9.6）。

同一 Session 同时只允许一个 checkpoint/WorkingState 更新提交；并发 Turn 使用乐观版本或
session lock，失败方重新读取最新 checkpoint 后规划（方案 §9.6）。本模块提供进程内
per-session async lock（pod 重启清空，单进程内有效）；跨进程/云端需由 Session Store 的乐观
锁或行锁兜底，本锁只做 best-effort 防同进程并发覆盖。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def _get_or_create_lock(session_id: str) -> asyncio.Lock:
    async with _REGISTRY_LOCK:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


@asynccontextmanager
async def session_compaction_lock(session_id: str, *, timeout: float = 30.0) -> AsyncIterator[None]:
    """获取 per-session compaction 锁（方案 §9.6）。

    超时抛 ``asyncio.TimeoutError``，调用方按 stale guard 处理（重新读最新 checkpoint 再规划）。
    """
    lock = await _get_or_create_lock(session_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        # stale guard：拿不到锁说明另一 turn 正在 compaction，本方放弃提交避免覆盖
        raise
    try:
        yield
    finally:
        lock.release()


def clear_session_locks() -> None:
    """测试/运维用：清空所有 per-session 锁。"""
    _SESSION_LOCKS.clear()


__all__ = ["clear_session_locks", "session_compaction_lock"]
