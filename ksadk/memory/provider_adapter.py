"""Memory Provider Adapter —— 统一不同 Provider 接口为 MemoryProvider Protocol（方案 §2）。

不同后端接口不统一：
- SqliteMemoryProvider: upsert/search/delete（MemoryProvider Protocol）
- BaseLongTermMemoryBackend: save_memory/search_memory
- LongTermMemoryService: save_event_strings/search_entries

本模块把它们统一适配为 MemoryCoordinator 可消费的接口。
"""

from __future__ import annotations

from typing import Any

from ksadk.memory.models import (
    MemoryDeleteRequest,
    MemoryDeleteResult,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
)


class LegacyMemoryAdapter:
    """把 BaseLongTermMemoryBackend / LongTermMemoryService 适配为 MemoryProvider Protocol。

    save_memory → upsert（每条 event_string 构造 MemoryRecord）
    search_memory → search（返回 MemorySearchResult）
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self.last_error = getattr(backend, "last_error", "")

    def capabilities(self):
        from ksadk.memory.models import MemoryCapabilities

        return MemoryCapabilities(
            semantic_search=False,
            keyword_search=True,
            metadata_filter=False,
            versioned_update=False,
            hard_delete=False,
            ttl=False,
            max_record_chars=8192,
        )

    def search(self, request: MemorySearchRequest) -> MemorySearchResult:
        """统一 search：用 search_memory/search_entries 取原始字符串列表。"""
        import time

        start = time.monotonic()
        try:
            # BaseLongTermMemoryBackend.search_memory
            if hasattr(self._backend, "search_memory"):
                entries = self._backend.search_memory(
                    user_id=request.scopes[0][1] if request.scopes else "",
                    query=request.query,
                    top_k=request.top_k,
                )
            # LongTermMemoryService.search_entries
            elif hasattr(self._backend, "search_entries"):
                entries = self._backend.search_entries(
                    user_id=request.scopes[0][1] if request.scopes else "",
                    query=request.query,
                    top_k=request.top_k,
                )
            else:
                entries = []
        except Exception:  # noqa: BLE001
            return MemorySearchResult(
                status="failed",
                records=[],
                error_code="provider_error",
                provider=type(self._backend).__name__,
                latency_ms=int((time.monotonic() - start) * 1000),
                accounting_accuracy="opaque",
            )

        # 转为 MemoryRecord 列表
        records: list[MemoryRecord] = []
        for i, entry in enumerate(entries):
            records.append(
                MemoryRecord(
                    memory_id=f"legacy_{i}",
                    tenant_id="local",
                    workspace_id="local",
                    scope="user",
                    scope_id=request.scopes[0][1] if request.scopes else "",
                    memory_type="fact",
                    content=entry,
                    summary=entry[:200],
                    status="active",
                    confidence=0.8,
                    importance=0.5,
                    valid_from="",
                    valid_to="",
                    expires_at="",
                    source_session_id="",
                    source_event_ids=[],
                    source_seq_range=None,
                    content_hash=f"sha256:{hash(entry) & 0xFFFFFFFFFFFFFFFF:016x}",
                    version=1,
                )
            )
        return MemorySearchResult(
            status="ok",
            records=records,
            error_code=None,
            provider=type(self._backend).__name__,
            latency_ms=int((time.monotonic() - start) * 1000),
            accounting_accuracy="estimated",
        )

    def upsert(self, record: MemoryRecord, *, expected_version: int | None) -> MemoryRecord:
        """统一 upsert：用 save_memory/save_event_strings。"""
        import json

        event_str = json.dumps(
            {"parts": [{"text": record.content}], "metadata": record.metadata},
            ensure_ascii=False,
        )
        success = True
        if hasattr(self._backend, "save_memory"):
            success = bool(
                self._backend.save_memory(user_id=record.scope_id, event_strings=[event_str])
            )
        elif hasattr(self._backend, "save_event_strings"):
            success = bool(
                self._backend.save_event_strings(user_id=record.scope_id, event_strings=[event_str])
            )
        if not success:
            raise RuntimeError(
                f"Memory Provider save returned False: {type(self._backend).__name__}"
            )
        return record

    def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult:
        return MemoryDeleteResult(status="ok", deleted=False, error_code="not_supported")

    def list_core(self, request) -> list[MemoryRecord]:
        return []


def adapt_as_memory_provider(obj: Any) -> Any:
    """把任意后端适配为 MemoryProvider Protocol 兼容对象。

    - 已经是 MemoryProvider Protocol（有 upsert/search）→ 原样返回
    - BaseLongTermMemoryBackend / LongTermMemoryService → LegacyMemoryAdapter
    """
    # 已经兼容 MemoryProvider Protocol
    if hasattr(obj, "upsert") and hasattr(obj, "search"):
        return obj

    # 需要适配
    return LegacyMemoryAdapter(obj)


__all__ = ["LegacyMemoryAdapter", "adapt_as_memory_provider"]
