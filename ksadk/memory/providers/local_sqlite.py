"""本地 SQLite Memory Provider（方案 §10 / §17.4 契约测试一致）。

提供 scope 隔离、版本化更新（乐观锁）、TTL、hard/soft delete、content_hash 去重。本地默认
实现，云端用 HTTP/SDK Provider，二者共用同一套契约测试。

不实现语义检索（``semantic_search=False``），仅 keyword 检索 + metadata 过滤；语义检索留
HTTP/SDK Provider。Provider 异常返回结构化 ``MemorySearchResult(status="failed")``，不抛
异常文本进模型上下文（方案 §10.8）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryCapabilities,
    MemoryDeleteRequest,
    MemoryDeleteResult,
    MemoryRecord,
    MemoryScope,
    MemorySearchRequest,
    MemorySearchResult,
)
from ksadk.memory.policy import content_hash

_ASCII_QUERY_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.-]+", re.IGNORECASE)
_CJK_QUERY_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def _keyword_query_terms(query: str) -> tuple[list[str], list[str]]:
    """Tokenize local keyword queries without assuming whitespace-delimited CJK.

    ASCII terms retain AND semantics. CJK runs become a bounded bigram OR
    group, allowing a natural question to match a shorter stored fact. This is
    a lightweight SQLite fallback, not semantic search.
    """

    normalized = str(query or "").lower()
    ascii_terms = list(dict.fromkeys(_ASCII_QUERY_TOKEN.findall(normalized)))[:16]
    cjk_terms: list[str] = []
    for run in _CJK_QUERY_RUN.findall(normalized):
        if len(run) < 2:
            continue
        cjk_terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return ascii_terms, list(dict.fromkeys(cjk_terms))[:48]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row["memory_id"],
        tenant_id=row["tenant_id"],
        workspace_id=row["workspace_id"],
        scope=row["scope"],
        scope_id=row["scope_id"],
        memory_type=row["memory_type"],
        content=row["content"],
        summary=row["summary"],
        status=row["status"],
        confidence=float(row["confidence"]),
        importance=float(row["importance"]),
        valid_from=row["valid_from"] or "",
        valid_to=row["valid_to"] or "",
        expires_at=row["expires_at"] or "",
        source_session_id=row["source_session_id"] or "",
        source_event_ids=json.loads(row["source_event_ids"] or "[]"),
        source_seq_range=tuple(json.loads(row["source_seq_range"] or "null") or ()),  # type: ignore[arg-type]
        content_hash=row["content_hash"],
        version=int(row["version"]),
        metadata=json.loads(row["metadata"] or "{}"),
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    importance REAL NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    source_session_id TEXT NOT NULL DEFAULT '',
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    source_seq_range TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scope ON memory_records(tenant_id, workspace_id,
    scope, scope_id, status);
CREATE INDEX IF NOT EXISTS idx_content_hash ON memory_records(content_hash);
"""


class SqliteMemoryProvider:
    """本地 SQLite 长期记忆 Provider。

    线程安全：单连接 + per-thread lock。``last_error`` 供 Coordinator 区分"吞错返空"与
    "真无记忆"（对齐 ``LongTermMemoryService.last_error`` 语义）。
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
        db_path: str | Path = ":memory:",
        tenant_id: str = "local",
        workspace_id: str = "local",
    ) -> None:
        self._db_path = str(db_path)
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.last_error: str = ""
        # 每次 Provider 启动执行一次有界清理；不在每次 recall/upsert 热路径扫描全表。
        self.cleanup(
            max_records=_positive_env_int("KSADK_MEMORY_MAX_RECORDS", 10000),
            expire_days=_positive_env_int("KSADK_MEMORY_RETENTION_DAYS", 90),
        )

    # ---- MemoryProvider Protocol ----

    def capabilities(self) -> MemoryCapabilities:
        return self.capabilities_def

    def search(self, request: MemorySearchRequest) -> MemorySearchResult:
        start = time.monotonic()
        try:
            with self._lock:
                rows = self._query(request)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return MemorySearchResult(
                status="failed",
                records=[],
                error_code="provider_error",
                provider="sqlite",
                latency_ms=int((time.monotonic() - start) * 1000),
            )
        self.last_error = ""
        # 过滤 active + 未过期 + scope 隔离已在 SQL 完成；做 content_hash 去重 + max_tokens 装箱。
        records = [_row_to_record(r) for r in rows]
        records = _dedupe_active_versions(records)
        now = _now_iso()
        records = [r for r in records if r.is_active_now(now_iso=now)]
        records = _box_by_tokens(records, request.max_tokens)
        return MemorySearchResult(
            status="ok",
            records=records[: request.top_k] if request.top_k else records,
            error_code=None,
            provider="sqlite",
            latency_ms=int((time.monotonic() - start) * 1000),
            accounting_accuracy="estimated",
            truncated_by_budget=len(records) >= request.top_k,
        )

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def upsert(self, record: MemoryRecord, *, expected_version: int | None) -> MemoryRecord:
        with self._lock:
            existing = self._conn.execute(
                "SELECT version, status FROM memory_records WHERE memory_id = ?",
                (record.memory_id,),
            ).fetchone()
            now = _now_iso()
            if existing is not None:
                if expected_version is not None and int(existing["version"]) != int(
                    expected_version
                ):
                    self.last_error = (
                        f"version_conflict:expected={expected_version},actual={existing['version']}"
                    )
                    raise VersionConflict(self.last_error)
                version = int(existing["version"]) + 1
                created = record.created_at or now
            else:
                version = record.version or 1
                created = record.created_at or now
            self._conn.execute(
                """INSERT INTO memory_records (
                    memory_id, tenant_id, workspace_id, scope, scope_id, memory_type,
                    content, summary, status, confidence, importance, valid_from, valid_to,
                    expires_at, source_session_id, source_event_ids, source_seq_range,
                    content_hash, version, metadata, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    content=excluded.content, summary=excluded.summary, status=excluded.status,
                    confidence=excluded.confidence, importance=excluded.importance,
                    valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                    expires_at=excluded.expires_at, source_event_ids=excluded.source_event_ids,
                    source_seq_range=excluded.source_seq_range, content_hash=excluded.content_hash,
                    version=excluded.version, metadata=excluded.metadata,
                        updated_at=excluded.updated_at
                """,
                (
                    record.memory_id,
                    record.tenant_id or self._tenant_id,
                    record.workspace_id or self._workspace_id,
                    record.scope,
                    record.scope_id,
                    record.memory_type,
                    record.content,
                    record.summary,
                    record.status,
                    record.confidence,
                    record.importance,
                    record.valid_from,
                    record.valid_to,
                    record.expires_at,
                    record.source_session_id,
                    json.dumps(record.source_event_ids, ensure_ascii=False),
                    json.dumps(list(record.source_seq_range) if record.source_seq_range else []),
                    record.content_hash or content_hash(record.content),
                    version,
                    json.dumps(record.metadata, ensure_ascii=False),
                    created,
                    now,
                ),
            )
            self._conn.commit()
        return MemoryRecord(
            **{
                **record.__dict__,
                "version": version,
                "created_at": record.created_at or created,
                "updated_at": now,
            }
        )

    def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult:
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM memory_records "
                "WHERE memory_id = ? AND scope = ? AND scope_id = ?",
                (request.memory_id, request.scope, request.scope_id),
            ).fetchone()
            if row is None:
                return MemoryDeleteResult(status="ok", deleted=False, error_code="not_found")
            if request.hard:
                self._conn.execute(
                    "DELETE FROM memory_records WHERE memory_id = ? AND scope = ? AND scope_id = ?",
                    (request.memory_id, request.scope, request.scope_id),
                )
            else:
                self._conn.execute(
                    "UPDATE memory_records SET status='deleted', updated_at=? "
                    "WHERE memory_id=? AND scope=? AND scope_id=?",
                    (_now_iso(), request.memory_id, request.scope, request.scope_id),
                )
            self._conn.commit()
        return MemoryDeleteResult(status="ok", deleted=True)

    def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]:
        # Core memory：按 importance 降序取 active profile/fact，受 max_blocks/max_tokens 约束。
        scopes = request.scopes
        if not scopes:
            return []
        where, params = _scope_where(scopes)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM memory_records
                    WHERE {where} AND status='active' AND memory_type IN ('profile','fact')
                    ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (*params, request.max_blocks * 4),
            ).fetchall()
        now = _now_iso()
        records = [r for r in (_row_to_record(row) for row in rows) if r.is_active_now(now_iso=now)]
        return _box_by_tokens(records, request.max_tokens)[: request.max_blocks]

    # ---- internals ----

    def _query(self, request: MemorySearchRequest) -> list[sqlite3.Row]:
        where_parts: list[str] = []
        params: list[Any] = []
        if request.scopes:
            w, p = _scope_where(request.scopes)
            where_parts.append(w)
            params.extend(p)
        else:
            where_parts.append("0")  # 无 scope → 不返回（隔离）
        where_parts.append("status='active'")
        if request.memory_types:
            placeholders = ",".join("?" for _ in request.memory_types)
            where_parts.append(f"memory_type IN ({placeholders})")
            params.extend(request.memory_types)
        slot_key = str(request.filters.get("slot_key") or "").strip()
        if slot_key:
            # JSON 路径固定、值参数化；仅选择相同事实槽位，不做正文模糊猜测。
            where_parts.append("json_extract(metadata, '$.slot_key') = ?")
            params.append(slot_key)
        # keyword 检索：ASCII 词项保持 AND；CJK 词项作为可选增强（不阻塞 ASCII 匹配）。
        ascii_terms, cjk_terms = _keyword_query_terms(str(request.query or ""))
        if ascii_terms:
            # ASCII AND 匹配
            for token in ascii_terms:
                where_parts.append("(LOWER(content) LIKE ? OR LOWER(summary) LIKE ?)")
                params.extend([f"%{token}%", f"%{token}%"])
            # CJK 词项作为可选增强：如果有 ASCII 匹配，CJK 不匹配也不阻塞
            # 只在 ASCII 为空时用 CJK 作为主匹配
        elif cjk_terms:
            # 只有 CJK 词 → 用 OR 匹配
            cjk_like_parts = []
            for token in cjk_terms:
                cjk_like_parts.append("(content LIKE ? OR summary LIKE ?)")
                params.extend([f"%{token}%", f"%{token}%"])
            where_parts.append("(" + " OR ".join(cjk_like_parts) + ")")
        sql = (
            "SELECT * FROM memory_records WHERE "
            + " AND ".join(where_parts)
            + " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        )
        params.append(max(request.top_k, 32))
        return self._conn.execute(sql, tuple(params)).fetchall()

    def cleanup(
        self,
        *,
        max_records: int = 10000,
        expire_days: int = 90,
    ) -> int:
        """清理过期/超量 Memory 记录（方案 §10.7 / §13.3）。

        删除 expired 状态记录；如果总记录超过 max_records，删除最老的低 importance 记录。
        返回删除的记录数。
        """

        deleted = 0
        with self._lock:
            # 删除显式过期和 TTL 已到期记录。
            now = _now_iso()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, expire_days))).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            cur = self._conn.execute(
                "DELETE FROM memory_records WHERE status = 'expired' "
                "OR (expires_at != '' AND expires_at <= ?)",
                (now,),
            )
            deleted += cur.rowcount
            # 删除超 90 天的低 importance 记录
            cur = self._conn.execute(
                "DELETE FROM memory_records WHERE importance < 0.5 AND created_at < ?",
                (cutoff,),
            )
            deleted += cur.rowcount
            # 如果总记录超过 max_records，删除最老的
            count = self._conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
            if count > max_records:
                excess = count - max_records
                self._conn.execute(
                    "DELETE FROM memory_records WHERE memory_id IN "
                    "(SELECT memory_id FROM memory_records "
                    "ORDER BY importance ASC, updated_at ASC LIMIT ?)",
                    (excess,),
                )
                deleted += excess
            self._conn.commit()
        return deleted

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class VersionConflict(RuntimeError):
    """``upsert`` 的 ``expected_version`` 乐观锁冲突。"""


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _scope_where(scopes: list[tuple[MemoryScope, str]]) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    for scope, scope_id in scopes:
        parts.append("(scope=? AND scope_id=?)")
        params.extend([scope, scope_id])
    return "(" + " OR ".join(parts) + ")", params


def _dedupe_active_versions(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """同一 content_hash 多版本只保留 active 最新版本（方案 §10.6）。"""
    seen: dict[str, MemoryRecord] = {}
    for r in records:
        key = r.content_hash
        prev = seen.get(key)
        if prev is None or r.version > prev.version:
            seen[key] = r
    return list(seen.values())


def _box_by_tokens(records: list[MemoryRecord], max_tokens: int) -> list[MemoryRecord]:
    """按 ``max_tokens`` 装箱（方案 §10.6 第 4 条），用启发式 token 估算。"""
    from ksadk.context_engine.tokenizer import get_default_token_counter

    counter = get_default_token_counter()
    total = 0
    out: list[MemoryRecord] = []
    for r in records:
        n = counter.count_text(r.content)
        if total + n > max_tokens:
            break
        total += n
        out.append(r)
    return out


__all__ = ["SqliteMemoryProvider", "VersionConflict", "resolve_default_memory_provider"]


def _resolve_default_db_path() -> str:
    """解析默认持久化 SQLite 路径（方案 §10 / §12 本地默认）。

    优先级：``KSADK_MEMORY_DB_PATH`` env > 本地 session 目录下的 memory.db > ``:memory:``。
    默认持久化到本地 session dir，避免每次进程重启丢失（替换临时 ``:memory:``）。env 设
    ``KSADK_MEMORY_DB_PATH=:memory:`` 可显式回退内存库（测试用）。
    """
    import os

    configured = os.environ.get("KSADK_MEMORY_DB_PATH", "").strip()
    if configured:
        return configured
    try:
        from ksadk.sessions.local_service import resolve_local_session_dir

        return str(resolve_local_session_dir() / "memory.db")
    except Exception:  # noqa: BLE001
        # 无本地 session dir（如测试）→ 回退内存库，保持可运行
        return ":memory:"


def resolve_default_memory_provider(
    *, tenant_id: str = "local", workspace_id: str = "local"
) -> "SqliteMemoryProvider":
    """构造默认持久化 Memory Provider（替换临时 ``:memory:``）。

    本地默认用 SQLite 文件 Provider（持久化到本地 session dir / ``KSADK_MEMORY_DB_PATH``）；
    云端应通过 ``LongTermMemoryService``/HTTP/SDK Provider 接入，不在本工厂范围（方案 §12）。
    """
    return SqliteMemoryProvider(
        db_path=_resolve_default_db_path(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
