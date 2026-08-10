"""per-session compaction lock + stale guard 测试（方案 §9.6）。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.conversations.session_lock import (
    clear_session_locks,
    session_compaction_lock,
)


@pytest.mark.asyncio
async def test_session_lock_serializes_same_session():
    clear_session_locks()
    order: list[str] = []

    async def task(name: str, hold: float):
        async with session_compaction_lock("s1", timeout=5.0):
            order.append(f"{name}:start")
            await asyncio.sleep(hold)
            order.append(f"{name}:end")

    await asyncio.gather(task("a", 0.05), task("b", 0.0))
    # a 完整 start→end 后 b 才进入（同 session 串行）
    assert order.index("a:start") < order.index("a:end") < order.index("b:start") < order.index("b:end")
    clear_session_locks()


@pytest.mark.asyncio
async def test_session_lock_isolates_different_sessions():
    clear_session_locks()
    started: list[str] = []

    async def task(session: str):
        async with session_compaction_lock(session, timeout=5.0):
            started.append(session)
            await asyncio.sleep(0.05)

    await asyncio.gather(task("s1"), task("s2"))
    # 不同 session 并发，两个都立即进入
    assert set(started) == {"s1", "s2"}
    clear_session_locks()


@pytest.mark.asyncio
async def test_session_lock_stale_guard_timeout():
    clear_session_locks()
    lock = session_compaction_lock("s1", timeout=5.0)
    # 先占住锁不放
    async with lock:
        with pytest.raises(asyncio.TimeoutError):
            await session_compaction_lock("s1", timeout=0.01).__aenter__()
    clear_session_locks()
