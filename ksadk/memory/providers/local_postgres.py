"""Postgres 长期记忆 Provider（分布式后端，plan §9 / 方案 P1）。

实现与 :class:`SqliteMemoryProvider` 相同的 MemoryProvider Protocol，
但用 psycopg 连接 Postgres，支持多副本共享。

依赖：``ksadk[postgres]`` extra（psycopg[binary]）。缺依赖时显式报错。

配置：``KSADK_MEMORY_PROVIDER=local-postgres`` + ``KSADK_MEMORY_POSTGRES_DSN``
或 ``KSADK_CHECKPOINT_DSN``（复用同一连接串）。
"""

from __future__ import annotations

import os
import threading
import time
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    sensitive_labels TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.8,
    importance REAL NOT NULL DEFAULT 0.6,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope
ON memory_records (tenant_id, workspace_id, scope, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_status
ON memory_records (tenant_id, workspace_id, status);
"""


class PostgresMemoryProvider:
    """Postgres 长期记忆 Provider（多副本共享，实现 MemoryProviderLike Protocol）。

    与 :class:`SqliteMemoryProvider` 接口一致，但用 Postgres 连接。
    线程安全：单连接 + per-thread lock（与 SQLite 版相同策略）。
    ``last_error`` 供 Coordinator 区分"吞错返空"与"真无记忆"。
    """

    capabilities_def = MemoryCapabilities(
        semantic_search=False,
        keyword_search=True,
        metadata_filter=True,
        versioned_update=True,
        hard_delete=True,
        ttl=True,
        max_record_chars=8192,
    )

    def __init__(
        self,
        *,
        dsn: str | None = None,
        tenant_id: str = "local",
        workspace_id: str = "local",
    ) -> None:
        self._dsn = (
            dsn
            or os.getenv("KSADK_MEMORY_POSTGRES_DSN")
            or os.getenv("KSADK_CHECKPOINT_DSN", "")
        )
        if not self._dsn:
            raise RuntimeError(
                "PostgresMemoryProvider 需要 DSN"
                "（KSADK_MEMORY_POSTGRES_DSN 或 KSADK_CHECKPOINT_DSN）"
            )
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "PostgresMemoryProvider 需要"
                " ksadk[postgres] extra（psycopg[binary]）"
            ) from exc
        self._psycopg = psycopg
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._lock = threading.Lock()
        self._conn = psycopg.connect(self._dsn, autocommit=True)
        self._conn.execute(_SCHEMA)
        self.last_error: str = ""

    # ---- MemoryProvider Protocol ----

    def capabilities(self) -> MemoryCapabilities:
        return self.capabilities_def

    def search(self, request: MemorySearchRequest) -> MemorySearchResult:
        start = time.monotonic()
        try:
            with self._lock:
                query = request.query.strip()
                if not query:
                    return MemorySearchResult(
                        records=[],
                        elapsed_ms=int((time.monotonic() - start) * 1000),
                    )
                # keyword search (ILIKE, Postgres 大小写不敏感)
                pattern = f"%{query}%"
                rows = self._conn.execute(
                    """SELECT * FROM memory_records
                       WHERE tenant_id = %s AND workspace_id = %s
                         AND status = 'active'
                         AND (content ILIKE %s)
                       ORDER BY importance DESC, updated_at DESC
                       LIMIT %s""",
                    (self._tenant_id, self._workspace_id, pattern, request.top_k),
                ).fetchall()
                records = [self._row_to_record(row) for row in rows]
                return MemorySearchResult(
                    records=records,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
        except Exception as exc:  # noqa: BLE001 - 不阻断主对话
            self.last_error = str(exc)
            return MemorySearchResult(records=[], elapsed_ms=int((time.monotonic() - start) * 1000))

    def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    """SELECT * FROM memory_records
                       WHERE tenant_id = %s AND workspace_id = %s
                         AND status = 'active'
                       ORDER BY importance DESC, updated_at DESC
                       LIMIT %s""",
                    (self._tenant_id, self._workspace_id, request.max_blocks),
                ).fetchall()
                return [self._row_to_record(row) for row in rows]
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return []

    def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult:
        try:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM memory_records WHERE memory_id = %s"
                    " AND scope = %s AND scope_id = %s",
                    (request.memory_id, request.scope, request.scope_id),
                )
            return MemoryDeleteResult(deleted=True)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return MemoryDeleteResult(deleted=False)

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()

    # ---- helpers ----

    def _row_to_record(self, row: Any) -> MemoryRecord:
        # psycopg Row 可按列名访问
        import json

        return MemoryRecord(
            memory_id=row["memory_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            scope=row["scope"],
            scope_id=row["scope_id"],
            memory_type=row["memory_type"],
            content=row["content"],
            version=row["version"],
            status=row["status"],
            source_event_ids=json.loads(row["source_event_ids"] or "[]"),
            sensitive_labels=json.loads(row["sensitive_labels"] or "[]"),
            confidence=row["confidence"],
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )


__all__ = ["PostgresMemoryProvider"]
