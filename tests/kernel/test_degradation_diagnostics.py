# -*- coding: utf-8 -*-
"""degraded / quarantine 路径的可诊断性：每条失败路径都必须留下结构化日志。

真实故障复盘：坏 session 使 ``StreamConformanceError``，``_recover_safely``
两条路径全部失败，runtime 只把 ``_degraded`` 置 True —— 没有日志、没有
原因、没有 session_id，运维只能看到一个 not-ready 的 pod。

期望行为：
- ``recover`` 失败：ERROR 日志（agent_instance_id / session_id /
  activation_id / 异常类型+摘要）。
- ``settle_interrupted`` 兜底成功（半恢复）：WARNING 日志。
- 兜底也失败：ERROR 日志；隔离 / 降级决策各有醒目日志。
- 进程级 degraded：一条 "agent kernel degraded: reason=..." ERROR，
  health 端点附 ``degradation_reason`` / ``degraded_at`` /
  ``degradation_last_error`` / ``quarantined_session_ids``。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from ksadk.kernel.bootstrap import build_agent_kernel_runtime
from tests.kernel.control_harness import AGENT
from tests.kernel.test_session_quarantine import (
    _break_recovery_for,
    _build,
    _seed_orphan_run,
    _wait_until,
)

LOGGER = "ksadk.kernel.bootstrap"


def _messages(caplog, level: int, logger: str = LOGGER) -> list[str]:
    return [
        r.message
        for r in caplog.records
        if r.name == logger and r.levelno >= level
    ]


async def test_recover_failure_logs_error_and_settle_fallback_logs_warning(
    caplog,
):
    """主恢复失败 -> ERROR；durable interrupted 兜底成功 -> WARNING。"""

    stack, runtime = await _build(sessions=("s1",))
    lease = SimpleNamespace(session_id="s1", activation_id="act-1")

    async def _recover_boom(agent_instance_id, lease, **kwargs):
        raise RuntimeError("replay exploded: run.started after interrupted")

    async def _settle_ok(agent_instance_id, lease, **kwargs):
        return None

    runtime.recovery.recover = _recover_boom  # type: ignore[method-assign]
    runtime.recovery.settle_interrupted = _settle_ok  # type: ignore[method-assign]

    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            failure = await runtime._recover_safely(lease)
        assert failure is None  # 兜底收口成功，不降级
        errors = _messages(caplog, logging.ERROR)
        assert any(
            "takeover recovery failed" in m
            and AGENT in m
            and "session_id=s1" in m
            and "activation_id=act-1" in m
            and "RuntimeError" in m
            for m in errors
        ), errors
        warnings = [
            r.message
            for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.WARNING
        ]
        assert any(
            "settled interrupted after recovery failure" in m
            and "session_id=s1" in m
            and "RuntimeError" in m
            for m in warnings
        ), warnings
        assert not runtime.degraded
    finally:
        await runtime.close()


async def test_both_recovery_paths_fail_logs_error_and_quarantines(caplog):
    """两条恢复路径都失败：fallback ERROR + 隔离 WARNING + health 明细。"""

    stack, runtime = await _build(sessions=("s1",))
    await _seed_orphan_run(stack, "s1")
    _break_recovery_for(runtime, "s1")

    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await runtime.start()
            await _wait_until(lambda: "s1" in runtime.quarantined_sessions())
        assert any(
            "interrupted-settlement fallback failed" in m and "session_id=s1" in m
            for m in _messages(caplog, logging.ERROR)
        )
        assert any(
            "quarantined after takeover recovery failed" in m
            and AGENT in m
            and "s1" in m
            for m in _messages(caplog, logging.WARNING)
        )
        health = await runtime.readiness.check()
        assert health["degraded"] is False
        assert health["quarantined_session_ids"] == ["s1"]
    finally:
        await runtime.close()


async def test_process_degradation_logs_prominent_error_and_enriches_health(
    caplog,
):
    """恢复失败扩散到阈值：醒目 degraded ERROR + health 诊断字段。"""

    sessions = ("s1", "s2", "s3", "s4", "s5")
    stack, runtime = await _build(sessions=sessions)
    for sid in sessions:
        await _seed_orphan_run(stack, sid)
    _break_recovery_for(runtime, *sessions)

    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            await runtime.start()
            await _wait_until(lambda: runtime.degraded)
        degraded_logs = [
            m for m in _messages(caplog, logging.ERROR)
            if "agent kernel degraded" in m
        ]
        assert degraded_logs, _messages(caplog, logging.ERROR)
        assert any(
            "reason=" in m and AGENT in m and "last_error=" in m
            for m in degraded_logs
        )
        await asyncio.sleep(0.05)
        health = await runtime.readiness.check()
        assert health["degraded"] is True
        assert health["ready"] is False
        assert health["degradation_reason"]
        assert health["degradation_reason"].startswith("recovery_failures_spread")
        assert health["degraded_at"]
        assert "RuntimeError" in (health["degradation_last_error"] or "")
        assert set(health["quarantined_session_ids"]) <= set(sessions)
        assert health["quarantined_session_ids"]
    finally:
        await runtime.close()


async def test_store_outage_during_recovery_degrades_with_reason(caplog):
    """store 断连叠加恢复失败：degraded 且 reason=store_unreachable..."""

    stack, runtime = await _build(sessions=("s1",))
    await _seed_orphan_run(stack, "s1")

    original_list = stack.store.list_messages

    async def _recover_boom(agent_instance_id, lease, **kwargs):
        raise RuntimeError("recover exploded")

    async def _settle_boom(agent_instance_id, lease, **kwargs):
        # 收口失败的同时 store 断连：区分不了 session 级故障，必须降级。
        stack.store.list_messages = _list_broken  # type: ignore[method-assign]
        raise RuntimeError("settle exploded")

    async def _list_broken(agent_instance_id, **kwargs):
        raise ConnectionError("store is down")

    runtime.recovery.recover = _recover_boom  # type: ignore[method-assign]
    runtime.recovery.settle_interrupted = _settle_boom  # type: ignore[method-assign]

    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            await runtime.start()
            await _wait_until(lambda: runtime.degraded)
        assert any(
            "agent kernel degraded" in m for m in _messages(caplog, logging.ERROR)
        )
        health = await runtime.readiness.check()
        assert health["degraded"] is True
        assert health["degradation_reason"] == "store_unreachable_during_recovery"
        assert health["degraded_at"]
    finally:
        stack.store.list_messages = original_list  # type: ignore[method-assign]
        await runtime.close()


async def test_persistent_store_outage_degrades_with_reason(caplog):
    """run loop 的 store 持续不可达路径：reason=store_unreachable。"""

    stack, runtime = await _build(sessions=("s1",))

    async def _list_broken(agent_instance_id, **kwargs):
        raise ConnectionError("store is down")

    stack.store.list_messages = _list_broken  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            await runtime.start()
            await _wait_until(lambda: runtime.degraded, timeout=10.0)
        assert any(
            "agent kernel degraded" in m for m in _messages(caplog, logging.ERROR)
        )
        health = await runtime.readiness.check()
        assert health["degraded"] is True
        assert health["degradation_reason"] == "store_unreachable"
        assert health["degraded_at"]
    finally:
        await runtime.close()
