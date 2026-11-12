"""Postgres 长期记忆 Provider（分布式后端，plan §9 / 方案 P1）。

实现与 :class:`SqliteMemoryProvider` 相同的 MemoryProvider Protocol，
但用 psycopg 连接 Postgres，支持多副本共享。保留旧表和旧列，使用 additive
record_json 列保存当前 MemoryRecord，并以事务锁实现跨连接 CAS。

依赖：``ksadk[postgres]`` extra（psycopg[binary]）。缺依赖时显式报错。

配置：``KSADK_MEMORY_PROVIDER=local-postgres`` + ``KSADK_MEMORY_POSTGRES_DSN``
或 ``KSADK_CHECKPOINT_DSN``（复用同一连接串）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any

from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryCapabilities,
    MemoryConflictError,
    MemoryDeleteRequest,
    MemoryDeleteResult,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
)
from ksadk.memory.policy import content_hash
from ksadk.memory.providers.local_sqlite import _box_by_tokens, _dedupe_active_versions, _now_iso

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
            dsn or os.getenv("KSADK_MEMORY_POSTGRES_DSN") or os.getenv("KSADK_CHECKPOINT_DSN", "")
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
                "PostgresMemoryProvider 需要 ksadk[postgres] extra（psycopg[binary]）"
            ) from exc
        self._psycopg = psycopg
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._lock = threading.Lock()
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)
        try:
            with self._conn.transaction():
                # Serialize additive initialization across independent hosts.
                self._conn.execute("SELECT pg_advisory_xact_lock(1263750477)")
                self._conn.execute(_SCHEMA)
                self._conn.execute(
                    "ALTER TABLE memory_records ADD COLUMN IF NOT EXISTS record_json JSONB"
                )
        except Exception:
            self._conn.close()
            raise
        self.last_error: str = ""

    # ---- MemoryProvider Protocol ----

    def capabilities(self) -> MemoryCapabilities:
        return self.capabilities_def

    def search(self, request: MemorySearchRequest) -> MemorySearchResult:
        start = time.monotonic()
        try:
            with self._lock:
                rows = self._query(request)
                records = [self._row_to_record(row) for row in rows]
            records = _dedupe_active_versions(records)
            records = [r for r in records if r.is_active_now(now_iso=_now_iso())]
            if request.filters.get("keyword_relevance") is True:
                from ksadk.memory.retrieval import keyword_coverage

                records = [
                    r
                    for r in records
                    if keyword_coverage(request.query, r.content) >= request.min_score
                ]
                records.sort(key=lambda r: -keyword_coverage(request.query, r.content))
            host_budget = (
                request.filters.get("keyword_relevance") is True
                and request.filters.get("defer_token_budget") is True
            )
            if not host_budget:
                records = _box_by_tokens(records, request.max_tokens)
            self.last_error = ""
            return MemorySearchResult(
                status="ok",
                records=records[: request.top_k] if request.top_k else records,
                error_code=None,
                provider="postgres",
                latency_ms=int((time.monotonic() - start) * 1000),
                accounting_accuracy="opaque" if host_budget else "estimated",
            )
        except Exception as exc:  # noqa: BLE001 - 不阻断主对话
            self.last_error = type(exc).__name__
            return MemorySearchResult(
                status="failed",
                records=[],
                error_code="provider_error",
                provider="postgres",
                latency_ms=int((time.monotonic() - start) * 1000),
                accounting_accuracy="opaque",
            )

    def _query(self, request: MemorySearchRequest) -> list[Any]:
        if (
            not request.scopes
            or request.filters.get("tenant_id", self._tenant_id) != self._tenant_id
        ):
            return []
        clauses = [
            "tenant_id=%s",
            "workspace_id=%s",
            "status='active'",
            "(" + " OR ".join("(scope=%s AND scope_id=%s)" for _ in request.scopes) + ")",
        ]
        params: list[Any] = [self._tenant_id, self._workspace_id]
        for scope, scope_id in request.scopes:
            params.extend([scope, scope_id])
        if request.memory_types:
            clauses.append("memory_type = ANY(%s)")
            params.append(request.memory_types)
        if request.query.strip():
            clauses.append("content ILIKE %s")
            params.append(f"%{request.query.strip()}%")
        slot_key = request.filters.get("slot_key")
        if slot_key:
            clauses.append("record_json->'metadata'->>'slot_key'=%s")
            params.append(str(slot_key))
        clauses.append("(expires_at IS NULL OR expires_at>%s)")
        params.append(time.time())
        params.append(max(request.top_k, 32))
        return self._conn.execute(
            "SELECT * FROM memory_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY importance DESC, updated_at DESC LIMIT %s",
            params,
        ).fetchall()

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_records WHERE memory_id=%s "
                "AND tenant_id=%s AND workspace_id=%s",
                (memory_id, self._tenant_id, self._workspace_id),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def upsert(self, record: MemoryRecord, *, expected_version: int | None) -> MemoryRecord:
        if (record.tenant_id or self._tenant_id, record.workspace_id or self._workspace_id) != (
            self._tenant_id,
            self._workspace_id,
        ):
            raise MemoryConflictError("version_conflict:record_identity_changed")
        with self._lock, self._conn.transaction():
            # Include absent rows in the CAS critical section on PG 9.6 too.
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (record.memory_id,))
            row = self._conn.execute(
                "SELECT * FROM memory_records WHERE memory_id=%s FOR UPDATE",
                (record.memory_id,),
            ).fetchone()
            if row is None and expected_version is not None:
                raise MemoryConflictError("version_conflict:record_absent")
            if row is not None:
                old = self._row_to_record(row)
                if (old.tenant_id, old.workspace_id, old.scope, old.scope_id) != (
                    self._tenant_id,
                    self._workspace_id,
                    record.scope,
                    record.scope_id,
                ):
                    raise MemoryConflictError("version_conflict:record_identity_changed")
                if expected_version is not None and old.version != expected_version:
                    raise MemoryConflictError("version_conflict:revision_changed")
            now = _now_iso()
            saved = replace(
                record,
                tenant_id=self._tenant_id,
                workspace_id=self._workspace_id,
                version=int(row["version"]) + 1 if row else record.version or 1,
                created_at=self._row_to_record(row).created_at if row else record.created_at or now,
                updated_at=now,
            )
            self._conn.execute(
                "INSERT INTO memory_records (memory_id,tenant_id,workspace_id,scope,scope_id,"
                "memory_type,content,version,status,source_event_ids,confidence,importance,"
                "created_at,updated_at,expires_at,record_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (memory_id) DO UPDATE SET memory_type=EXCLUDED.memory_type,"
                "content=EXCLUDED.content,version=EXCLUDED.version,status=EXCLUDED.status,"
                "source_event_ids=EXCLUDED.source_event_ids,confidence=EXCLUDED.confidence,"
                "importance=EXCLUDED.importance,updated_at=EXCLUDED.updated_at,"
                "expires_at=EXCLUDED.expires_at,record_json=EXCLUDED.record_json",
                (
                    saved.memory_id,
                    saved.tenant_id,
                    saved.workspace_id,
                    saved.scope,
                    saved.scope_id,
                    saved.memory_type,
                    saved.content,
                    saved.version,
                    saved.status,
                    json.dumps(saved.source_event_ids),
                    saved.confidence or 0,
                    saved.importance or 0,
                    _epoch(saved.created_at),
                    _epoch(saved.updated_at),
                    _epoch(saved.expires_at),
                    json.dumps(asdict(saved)),
                ),
            )
        self.last_error = ""
        return saved

    def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]:
        try:
            with self._lock:
                rows = self._query(
                    MemorySearchRequest(
                        query="",
                        scopes=request.scopes,
                        memory_types=["profile", "fact"],
                        top_k=max(request.max_blocks, 32),
                    )
                )
                records = [self._row_to_record(row) for row in rows]
                records = [r for r in records if r.is_active_now(now_iso=_now_iso())]
                self.last_error = ""
                return _box_by_tokens(_dedupe_active_versions(records), request.max_tokens)[
                    : request.max_blocks
                ]
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return []

    def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult:
        try:
            with self._lock, self._conn.transaction():
                params = (
                    request.memory_id,
                    request.scope,
                    request.scope_id,
                    self._tenant_id,
                    self._workspace_id,
                )
                where = (
                    "memory_id=%s AND scope=%s AND scope_id=%s AND tenant_id=%s AND workspace_id=%s"
                )
                row = self._conn.execute(
                    "SELECT * FROM memory_records WHERE " + where + " FOR UPDATE", params
                ).fetchone()
                if row is None:
                    return MemoryDeleteResult(status="ok", deleted=False, error_code="not_found")
                if request.hard:
                    self._conn.execute("DELETE FROM memory_records WHERE " + where, params)
                else:
                    saved = replace(
                        self._row_to_record(row), status="deleted", updated_at=_now_iso()
                    )
                    self._conn.execute(
                        "UPDATE memory_records SET status='deleted',updated_at=%s,"
                        "record_json=%s::jsonb WHERE "
                        + where,
                        (_epoch(saved.updated_at), json.dumps(asdict(saved)), *params),
                    )
            self.last_error = ""
            return MemoryDeleteResult(status="ok", deleted=True)
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__
            return MemoryDeleteResult(status="failed", deleted=False, error_code="provider_error")

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()

    # ---- helpers ----

    def _row_to_record(self, row: Any) -> MemoryRecord:
        if row.get("record_json"):
            values = dict(row["record_json"])
            for name in ("source_artifact_refs", "source_artifacts", "supersedes"):
                values[name] = tuple(values.get(name) or ())
            if values.get("source_seq_range") is not None:
                values["source_seq_range"] = tuple(values["source_seq_range"])
            return MemoryRecord(**values)

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
            summary=row["content"][:200],
            confidence=row["confidence"],
            importance=row["importance"],
            valid_from="",
            valid_to="",
            source_session_id="",
            source_seq_range=None,
            content_hash=content_hash(row["content"]),
            created_at=_iso(row["created_at"]),
            updated_at=_iso(row["updated_at"]),
            expires_at=_iso(row["expires_at"]),
        )


def _epoch(value: str) -> float | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _iso(value: float | None) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if value is not None else ""
    )


__all__ = ["PostgresMemoryProvider"]
