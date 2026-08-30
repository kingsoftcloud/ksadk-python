"""Harness 长任务洞察 API（长任务方案 §3.2 / P3 补强：Studio 实际消费）。

Studio/控制面此前只有 SDK 查询函数（:mod:`ksadk.harness.observability`），
没有真实 HTTP 消费端。本模块补上：

- :class:`HarnessInsightsRegistry`：进程内 Run 事件登记处。引擎
  （``ManagedLangGraphEngine(event_sink=...)``）把每个事件登记进来，
  API 层按 ``run_id`` / ``session_id`` 查询；
- :func:`build_insights_router`：FastAPI 路由，输出纯 dict 投影
  （context-trace / token-report / compaction-trace）——Studio 不解析
  Harness 私有对象，直接消费 JSON；
- Capability Health Snapshot：统一查询 MCP / Skill / Sandbox 健康状态。

输出边界：事件经 :func:`ksadk.harness.events.project_v2` 整流为 v2 信封
（长任务方案 §8），再交给 observability 投影函数。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, HTTPException

from ksadk.harness.events import RuntimeEvent, project_v2
from ksadk.harness.observability import (
    capability_health,
    compaction_trace,
    context_trace,
    token_report,
)

#: 引擎事件回调类型：``event_sink(session_id, run_id, event)``。
EventSink = Callable[[str, str, RuntimeEvent], None]


class HarnessInsightsRegistry:
    """Run 事件的进程内登记处（引擎 → API 的桥）。

    线程安全；事件保留在内存中，按 run_id / session_id 索引。生产部署
    可替换为持久化实现（接口不变）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_run: dict[str, list[RuntimeEvent]] = defaultdict(list)
        self._by_session: dict[str, list[RuntimeEvent]] = defaultdict(list)

    def record(self, session_id: str, run_id: str, event: RuntimeEvent) -> None:
        """登记一个事件（引擎 event_sink 以位置参数调用）。"""
        with self._lock:
            self._by_run[run_id].append(event)
            self._by_session[session_id].append(event)

    def sink(self, *, session_id: str, run_id: str) -> EventSink:
        """构造绑定到指定 Run 的事件回调（传给引擎 ``event_sink``）。"""

        def _sink(_session_id: str, _run_id: str, event: RuntimeEvent) -> None:
            self.record(session_id=session_id, run_id=run_id, event=event)

        return _sink

    def run_events(self, run_id: str) -> list[RuntimeEvent]:
        """整流为 v2 信封后返回（平台输出边界）。"""
        with self._lock:
            events = list(self._by_run.get(run_id) or [])
        return project_v2(events)

    def run_ids(self) -> list[str]:
        """已登记事件的 run_id 列表（调试/管理面用）。"""
        with self._lock:
            return list(self._by_run)

    def session_events(self, session_id: str) -> list[RuntimeEvent]:
        with self._lock:
            events = list(self._by_session.get(session_id) or [])
        return project_v2(events)


def build_insights_router(registry: HarnessInsightsRegistry) -> APIRouter:
    """构建 /insights 路由（挂到 HarnessApp / 独立 FastAPI 均可）。"""

    router = APIRouter(prefix="/insights", tags=["harness-insights"])

    def _events_or_404(run_id: str) -> list[RuntimeEvent]:
        events = registry.run_events(run_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
        return events

    @router.get("/runs/{run_id}/context-trace")
    async def get_context_trace(run_id: str) -> dict[str, Any]:
        """每次模型调用的 Manifest 快照（Planned/Projected/Actual 闭环）。"""
        return {"items": context_trace(_events_or_404(run_id))}

    @router.get("/runs/{run_id}/token-report")
    async def get_token_report(run_id: str) -> dict[str, Any]:
        """单 Run 的 Token 闭环汇总（门禁 / 计费对账输入）。"""
        return token_report(_events_or_404(run_id))

    @router.get("/runs/{run_id}/compaction-trace")
    async def get_compaction_trace(run_id: str) -> dict[str, Any]:
        """压缩历史（前后 Token、触发原因、质量校验、Memory Flush 候选）。"""
        return {"items": compaction_trace(_events_or_404(run_id))}

    @router.get("/runs/{run_id}/capability-health")
    async def get_capability_health(run_id: str) -> dict[str, Any]:
        """Run 级 MCP / Skill / Sandbox 健康快照。"""
        return capability_health(_events_or_404(run_id))

    @router.get("/sessions/{session_id}/token-report")
    async def get_session_token_report(session_id: str) -> dict[str, Any]:
        """会话级 Token 闭环汇总（跨 Run 聚合）。"""
        events = registry.session_events(session_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return token_report(events)

    @router.get("/sessions/{session_id}/capability-health")
    async def get_session_capability_health(session_id: str) -> dict[str, Any]:
        """会话级最新健康快照（跨 Run 按事件时序投影）。"""
        events = registry.session_events(session_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return capability_health(events)

    return router


def mount_insights(app: FastAPI, registry: HarnessInsightsRegistry) -> None:
    """把 insights 路由挂到已有 FastAPI 应用。"""
    app.include_router(build_insights_router(registry))


__all__ = [
    "EventSink",
    "HarnessInsightsRegistry",
    "build_insights_router",
    "mount_insights",
]
