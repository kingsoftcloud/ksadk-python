"""LangGraph Store → MemoryProvider 适配层（长任务方案 §7.6 / P3）。

LangGraph ``BaseStore`` 语义映射到 KsADK MemoryProvider Protocol：

- namespace = ``(tenant_id, workspace_id, scope, scope_id)``（作用域隔离）；
- key = ``memory_id``；
- value = MemoryRecord ``to_payload`` 形态的 dict（KsADK 公共协议，
  Store 不感知 Harness 类型）。

KsADK ``MemoryRecord`` 和权限语义始终是公共协议；更换 Backend 不改变
HarnessSpec 与 RuntimeEvent（方案 §10 P3 验收）。

Store API 是 async（``aput/aget/asearch/adelete``）；本适配器在同步
Protocol 方法内用 :func:`asyncio.run` 驱动（事件循环已运行时抛明确错误，
调用方应改走异步通道或独立线程）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any

from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryCapabilities,
    MemoryDeleteRequest,
    MemoryDeleteResult,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
)


def _run(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "LangGraphStoreAdapter 的同步方法不能在事件循环内调用"
        "（请在线程/进程边界使用，或经 MemoryCoordinator 异步通道）"
    )


class LangGraphStoreAdapter:
    """把 LangGraph ``BaseStore`` 适配为 MemoryProvider Protocol。"""

    capabilities_def = MemoryCapabilities(
        semantic_search=True,  # Store 按 value 匹配语义（视实现而定）
        keyword_search=False,
        metadata_filter=True,
        versioned_update=False,
        hard_delete=True,
        ttl=False,
        max_record_chars=8192,
    )

    def __init__(self, store: Any, *, tenant_id: str = "local") -> None:
        self._store = store
        self._tenant_id = tenant_id

    # ---- namespace 映射 ----

    def _ns(self, scope: str, scope_id: str) -> tuple[str, ...]:
        return (self._tenant_id, "store", scope, scope_id)

    # ---- MemoryProvider Protocol ----

    def capabilities(self) -> MemoryCapabilities:
        return self.capabilities_def

    async def _aget(self, memory_id: str, ns: tuple[str, ...]) -> MemoryRecord | None:
        item = await self._store.aget(ns, memory_id)
        return MemoryRecord(**item.value) if item else None

    def get(self, memory_id: str) -> MemoryRecord | None:
        # Store get 需要 namespace；扫描本适配器管理的 scope 前缀不现实，
        # 调用方（Coordinator）经 search 定位。此处按最近一次 search 缓存解析。
        raise NotImplementedError("请经 search(memory_id 过滤) 定位记录")

    async def search_async(
        self, request: MemorySearchRequest
    ) -> MemorySearchResult:
        start = time.monotonic()
        records: list[MemoryRecord] = []
        for scope, scope_id in request.scopes:
            items = await self._store.asearch(
                self._ns(scope, scope_id), query=request.query or None,
                limit=request.top_k,
            )
            for item in items:
                try:
                    records.append(MemoryRecord(**dict(item.value)))
                except TypeError:
                    continue
        return MemorySearchResult(
            status="ok",
            records=records[: request.top_k],
            error_code=None,
            provider="langgraph-store",
            latency_ms=int((time.monotonic() - start) * 1000),
            accounting_accuracy="estimated",
        )

    def search(self, request: MemorySearchRequest) -> MemorySearchResult:
        return _run(self.search_async(request))

    async def upsert_async(
        self, record: MemoryRecord, *, expected_version: int | None
    ) -> MemoryRecord:
        if expected_version is not None:
            existing = await self._aget(record.memory_id, self._ns(record.scope, record.scope_id))
            if existing is not None and existing.version != expected_version:
                raise RuntimeError(
                    f"version_conflict:expected={expected_version},"
                    f"actual={existing.version}"
                )
        payload = asdict(record)
        payload["source_artifact_refs"] = list(record.source_artifact_refs)
        payload["source_artifacts"] = [asdict(item) for item in record.source_artifacts]
        payload["supersedes"] = list(record.supersedes)
        await self._store.aput(self._ns(record.scope, record.scope_id), record.memory_id, payload)
        return record

    def upsert(self, record: MemoryRecord, *, expected_version: int | None) -> MemoryRecord:
        return _run(self.upsert_async(record, expected_version=expected_version))

    async def delete_async(self, request: MemoryDeleteRequest) -> MemoryDeleteResult:
        ns = self._ns(request.scope, request.scope_id)
        existing = await self._aget(request.memory_id, ns)
        if existing is None:
            return MemoryDeleteResult(status="ok", deleted=False, error_code="not_found")
        if request.hard:
            await self._store.adelete(ns, request.memory_id)
        else:
            deleted = {**asdict(existing), "status": "deleted"}
            await self._store.aput(ns, request.memory_id, deleted)
        return MemoryDeleteResult(status="ok", deleted=True, error_code=None)

    def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult:
        return _run(self.delete_async(request))

    def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]:
        # Store 无常驻 Block 概念；Core Memory 交由 Coordinator 预算层。
        return []


__all__ = ["LangGraphStoreAdapter"]
