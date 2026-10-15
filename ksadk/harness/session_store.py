"""Harness Session/Transcript 持久化（plan §6.2.1 / §17 Release）。

- Transcript 是会话事实源：完整事件与消息持久化，压缩不删除（§8.4）；
- 恢复语义：进程重启后先恢复图 Checkpoint（路由），再从 Transcript 重建
  ``HarnessState`` 消息部分；两份状态以 checkpoint_id 关联，不一致时以
  Transcript 为准并记录 ``context.recovered`` 事件（payload 必含 reason）。

本地默认后端为 SQLite（同步、单进程内加锁）；生产后端可替换。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.state import HarnessState, Message, MessageRole, WorkingContext
from ksadk.harness.working_context import record_tool_failure, record_tool_result

_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_transcript (
    tenant_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    entry_id   TEXT,
    parent_id  TEXT,
    PRIMARY KEY (tenant_id, session_id, seq)
);
CREATE INDEX IF NOT EXISTS harness_transcript_session
    ON harness_transcript (tenant_id, session_id);
"""

# v1 增量对象：唯一索引 + 会话树 fork 边表。由 ``_migrate_if_needed`` 统一创建
# （全新库 user_version 同样是 0，因此也会走迁移分支补齐），不放进 ``_SCHEMA``：
# 存量 v0 库缺 ``entry_id`` 列，在 ``_SCHEMA`` 里直接建该索引会以
# "no such column: entry_id" 失败，导致迁移根本没机会执行。
_SCHEMA_V1 = """
CREATE UNIQUE INDEX IF NOT EXISTS harness_transcript_entry
    ON harness_transcript (tenant_id, entry_id) WHERE entry_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS harness_fork_edges (
    tenant_id       TEXT NOT NULL,
    from_session_id TEXT NOT NULL,
    from_entry_id   TEXT NOT NULL,
    to_session_id   TEXT NOT NULL,
    to_entry_id     TEXT NOT NULL,
    -- 同一起点 fork 多个分支是 fork 语义本身允许的，主键必须含 to_entry_id。
    PRIMARY KEY (tenant_id, from_session_id, from_entry_id, to_entry_id)
);
"""

_MESSAGE_KIND = "message"
_EVENT_KIND = "event"
_FORK_KIND = "fork"
# fork 读链递归上限：源 session 自身是 fork 时沿链继续解析，超限视为环/异常。
_FORK_MAX_DEPTH = 8


@dataclass(frozen=True)
class ForkResult:
    """fork() 结果：新 session 标识、fork 根行 entry 与实际锚定的源 entry。"""

    new_session_id: str
    fork_root_entry_id: str
    source_entry_id: str


class SessionStoreError(RuntimeError):
    """Session 存储治理错误（删除保护等）。"""


class SqliteSessionStore:
    """Session/Transcript 存储的本地 SQLite 实现（租户隔离由主键强制）。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate_if_needed()
        self._lock = threading.Lock()

    def _migrate_if_needed(self) -> None:
        """v0（无 entry 列）→ v1：加列、回填 entry_id/parent_id、建索引与边表。

        迁移可续跑：加列与回填是两个独立步骤，若上一次在「列已建、回填未跑」
        中途崩溃，重开会因列已存在而跳过 ALTER——因此回填的守卫不是列缺失，
        而是「仍存在 ``entry_id IS NULL`` 的行」。
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 1:
            return
        cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(harness_transcript)")
        }
        if "entry_id" not in cols:
            self._conn.execute("ALTER TABLE harness_transcript ADD COLUMN entry_id TEXT")
            self._conn.execute("ALTER TABLE harness_transcript ADD COLUMN parent_id TEXT")
            self._conn.commit()
        pending = self._conn.execute(
            "SELECT 1 FROM harness_transcript WHERE entry_id IS NULL LIMIT 1"
        ).fetchone()
        if pending is not None:
            self._backfill_entry_chain()
        self._conn.executescript(_SCHEMA_V1)
        self._conn.execute("PRAGMA user_version = 1")
        self._conn.commit()

    def _backfill_entry_chain(self) -> None:
        """存量行回填 ``entry_id = "seq:{seq}"`` 与相邻 seq 的 parent 链。

        ``entry_id`` 的唯一索引是 ``(tenant_id, entry_id)``，而 v0 主键是
        ``(tenant_id, session_id, seq)``：同一租户下多个 session 都会有
        ``seq=1``，裸 ``"seq:1"`` 会在唯一定位上撞车。仅在确实存在这种
        「一租户多 session」的旧库时，退回 session 限定的
        ``"seq:{session_id}:{seq}"``；单 session（含全部新库）保持 brief 的
        ``"seq:{seq}"`` 形式。
        """
        rows = self._conn.execute(
            "SELECT tenant_id, session_id, seq FROM harness_transcript"
        ).fetchall()
        existing = {(t, s, q) for t, s, q in rows}
        sessions_per_tenant: dict[str, set[str]] = {}
        for tenant_id, session_id, _seq in rows:
            sessions_per_tenant.setdefault(tenant_id, set()).add(session_id)
        qualified = any(len(sessions) > 1 for sessions in sessions_per_tenant.values())
        for tenant_id, session_id, seq in rows:
            entry_id = f"seq:{session_id}:{seq}" if qualified else f"seq:{seq}"
            prev = seq - 1
            parent_id = None
            if (tenant_id, session_id, prev) in existing:
                parent_id = f"seq:{session_id}:{prev}" if qualified else f"seq:{prev}"
            self._conn.execute(
                "UPDATE harness_transcript SET entry_id = ?, parent_id = ?"
                " WHERE tenant_id = ? AND session_id = ? AND seq = ?",
                (entry_id, parent_id, tenant_id, session_id, seq),
            )
        self._conn.commit()

    @staticmethod
    def _new_entry_id() -> str:
        return f"e-{uuid.uuid4().hex[:12]}"

    def _append_row(
        self, *, tenant_id: str, session_id: str, seq: int, kind: str, payload: str
    ) -> None:
        with self._lock:
            parent = self._conn.execute(
                "SELECT entry_id FROM harness_transcript"
                " WHERE tenant_id = ? AND session_id = ?"
                " ORDER BY seq DESC LIMIT 1",
                (tenant_id, session_id),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO harness_transcript"
                " (tenant_id, session_id, seq, kind, payload, entry_id, parent_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    tenant_id,
                    session_id,
                    seq,
                    kind,
                    payload,
                    self._new_entry_id(),
                    parent[0] if parent else None,
                ),
            )
            self._conn.commit()

    def entry_id_for(self, *, tenant_id: str, session_id: str, seq: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT entry_id FROM harness_transcript"
                " WHERE tenant_id = ? AND session_id = ? AND seq = ?",
                (tenant_id, session_id, seq),
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- 写入

    def append_message(
        self, *, tenant_id: str, session_id: str, message: Message, seq: int
    ) -> None:
        self._append_row(
            tenant_id=tenant_id,
            session_id=session_id,
            seq=seq,
            kind=_MESSAGE_KIND,
            payload=json.dumps(
                {
                    "role": message.role.value,
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                    "name": message.name,
                },
                ensure_ascii=False,
            ),
        )

    def append_event(self, *, tenant_id: str, session_id: str, event: RuntimeEvent) -> None:
        self._append_row(
            tenant_id=tenant_id,
            session_id=session_id,
            seq=event.seq_id,
            kind=_EVENT_KIND,
            payload=json.dumps(
                {"event_type": event.event_type, "payload": event.payload},
                ensure_ascii=False,
            ),
        )

    # ------------------------------------------------------------- fork

    def fork(
        self,
        *,
        tenant_id: str,
        source_session_id: str,
        from_entry_id: str | None,
        new_session_id: str,
    ) -> ForkResult:
        """copy-on-read fork：不复制行，新 session 只落一条 fork 根行（G2 一期）。

        ``from_entry_id`` 为 None 时取源 session 最后一条 entry。源 entry
        不存在或源 session 为空、自 fork（``new_session_id == source_session_id``）、
        ``new_session_id`` 在该租户下已有任意行 → ``ValueError``。跨 session
        引用同时记入 ``harness_fork_edges``，供删除保护与未来的树导航查询。

        根行直接 INSERT 而不走 ``_append_row``：它的 ``parent_id`` 指向**源
        session** 的 entry（跨 session 引用），不能被本 session 的自动链覆盖。
        """
        with self._lock:
            if new_session_id == source_session_id:
                raise ValueError(f"不能 fork 到自身: {new_session_id!r}")
            existing = self._conn.execute(
                "SELECT 1 FROM harness_transcript"
                " WHERE tenant_id = ? AND session_id = ? LIMIT 1",
                (tenant_id, new_session_id),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"目标 session 已存在: {new_session_id!r}")
            source = self._conn.execute(
                "SELECT seq, entry_id FROM harness_transcript"
                " WHERE tenant_id = ? AND session_id = ?"
                " ORDER BY seq DESC LIMIT 1",
                (tenant_id, source_session_id),
            ).fetchone()
            if from_entry_id is None:
                if source is None:
                    raise ValueError(f"源 session 为空，无法 fork: {source_session_id!r}")
                source_entry_id = source[1]
            else:
                row = self._conn.execute(
                    "SELECT entry_id FROM harness_transcript"
                    " WHERE tenant_id = ? AND session_id = ? AND entry_id = ?",
                    (tenant_id, source_session_id, from_entry_id),
                ).fetchone()
                if row is None:
                    raise ValueError(f"fork 点不存在: {from_entry_id!r}")
                source_entry_id = from_entry_id
            fork_root = self._new_entry_id()
            self._conn.execute(
                "INSERT INTO harness_transcript"
                " (tenant_id, session_id, seq, kind, payload, entry_id, parent_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    tenant_id,
                    new_session_id,
                    0,
                    _FORK_KIND,
                    json.dumps(
                        {
                            "source_session_id": source_session_id,
                            "source_entry_id": source_entry_id,
                        },
                        ensure_ascii=False,
                    ),
                    fork_root,
                    source_entry_id,
                ),
            )
            self._conn.execute(
                "INSERT INTO harness_fork_edges"
                " (tenant_id, from_session_id, from_entry_id, to_session_id, to_entry_id)"
                " VALUES (?,?,?,?,?)",
                (tenant_id, new_session_id, fork_root, source_session_id, source_entry_id),
            )
            self._conn.commit()
        return ForkResult(
            new_session_id=new_session_id,
            fork_root_entry_id=fork_root,
            source_entry_id=source_entry_id,
        )

    def delete_session(self, *, tenant_id: str, session_id: str) -> bool:
        """删除 session；被其他 session 的 fork 引用时拒绝（用户裁决 2026-09-17）。

        引用检查查 ``harness_fork_edges``：任何 ``to_session_id`` = 本 session
        的边存在 → ``SessionStoreError``（不删任何行，保持原子）。删除时同时
        清理本 session 发出的边。session 不存在返回 ``False``。
        """
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM harness_transcript"
                " WHERE tenant_id = ? AND session_id = ? LIMIT 1",
                (tenant_id, session_id),
            ).fetchone()
            if exists is None:
                return False
            referencing = self._conn.execute(
                "SELECT from_session_id FROM harness_fork_edges"
                " WHERE tenant_id = ? AND to_session_id = ? LIMIT 1",
                (tenant_id, session_id),
            ).fetchone()
            if referencing is not None:
                raise SessionStoreError(
                    "session_referenced_by_fork: "
                    f"{session_id!r} is referenced by fork session {referencing[0]!r}"
                )
            self._conn.execute(
                "DELETE FROM harness_transcript WHERE tenant_id = ? AND session_id = ?",
                (tenant_id, session_id),
            )
            self._conn.execute(
                "DELETE FROM harness_fork_edges WHERE tenant_id = ? AND from_session_id = ?",
                (tenant_id, session_id),
            )
            self._conn.commit()
            return True

    # ------------------------------------------------------------- 读取

    def _rows(
        self, *, tenant_id: str, session_id: str, kind: str | None = None
    ) -> list[tuple[int, str]]:
        query = "SELECT seq, payload FROM harness_transcript WHERE tenant_id = ? AND session_id = ?"
        params: list[Any] = [tenant_id, session_id]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        with self._lock:
            return list(self._conn.execute(query + " ORDER BY seq", params).fetchall())

    def _resolve_rows(
        self,
        *,
        tenant_id: str,
        session_id: str,
        kind: str | None = None,
        _depth: int = 0,
        _upto_seq: int | None = None,
    ) -> list[tuple[int, str]]:
        """copy-on-read：解析 fork 根的源链后再拼自有行，返回 ``(seq, payload)``。

        - 自有行按 seq 升序；遇到 ``kind="fork"`` 的根行（seq=0）就地展开源
          session 截至 ``source_entry_id`` 的行（源 session 内 ``seq <= 源 entry
          的 seq``）——分支语义即「回到该点换走法」，源链更后面的行不展开；
        - 源 session 自身含 fork 根行则继续递归，``_depth > _FORK_MAX_DEPTH``
          抛 ``RuntimeError("fork chain too deep")``（防环）；
        - ``kind`` 过滤只作用于非 fork 行；``_rows`` 是 copy-on-read 改造前的
          原始线性读取路径，当前无调用方，仅保留供调试/未来内部复用。
        """
        if _depth > _FORK_MAX_DEPTH:
            raise RuntimeError("fork chain too deep")
        query = (
            "SELECT seq, kind, payload FROM harness_transcript"
            " WHERE tenant_id = ? AND session_id = ?"
        )
        params: list[Any] = [tenant_id, session_id]
        if _upto_seq is not None:
            query += " AND seq <= ?"
            params.append(_upto_seq)
        with self._lock:
            rows = list(self._conn.execute(query + " ORDER BY seq", params).fetchall())
        resolved: list[tuple[int, str]] = []
        for seq, row_kind, payload in rows:
            if row_kind == _FORK_KIND:
                info = json.loads(payload)
                source_session_id = info["source_session_id"]
                source_entry_id = info["source_entry_id"]
                with self._lock:
                    source = self._conn.execute(
                        "SELECT seq FROM harness_transcript"
                        " WHERE tenant_id = ? AND session_id = ? AND entry_id = ?",
                        (tenant_id, source_session_id, source_entry_id),
                    ).fetchone()
                if source is None:
                    raise ValueError(f"fork 源 entry 不存在: {source_entry_id!r}")
                resolved.extend(
                    self._resolve_rows(
                        tenant_id=tenant_id,
                        session_id=source_session_id,
                        kind=kind,
                        _depth=_depth + 1,
                        _upto_seq=source[0],
                    )
                )
            elif kind is None or row_kind == kind:
                resolved.append((seq, payload))
        return resolved

    def messages(self, *, tenant_id: str, session_id: str) -> list[Message]:
        messages = []
        for _seq, payload in self._resolve_rows(
            tenant_id=tenant_id, session_id=session_id, kind=_MESSAGE_KIND
        ):
            data = json.loads(payload)
            messages.append(
                Message(
                    role=MessageRole(data["role"]),
                    content=data["content"],
                    tool_call_id=data.get("tool_call_id"),
                    name=data.get("name"),
                )
            )
        return messages

    def events(self, *, tenant_id: str, session_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(payload)
            for _seq, payload in self._resolve_rows(
                tenant_id=tenant_id, session_id=session_id, kind=_EVENT_KIND
            )
        ]

    def tree(self, *, tenant_id: str, session_id: str) -> dict[str, Any]:
        """树导航视图（G2 二期 §2.2.2）：fork 边 + 各 session 的 entry 链。

        返回 Studio ``/tree`` 式导航所需的最小事实（不含正文）：
        ``nodes``（session_id / entry_id / seq / kind）与 ``edges``
        （from_session/from_entry → to_session/to_entry 的 fork 边）。
        递归展开与 ``_resolve_rows`` 同深度上限（防环）。
        """
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        visited: set[str] = set()

        def _walk(sid: str, depth: int) -> None:
            if depth > _FORK_MAX_DEPTH or sid in visited:
                if sid in visited:
                    return
                raise RuntimeError("fork chain too deep")
            visited.add(sid)
            with self._lock:
                rows = self._conn.execute(
                    "SELECT seq, kind, entry_id, payload FROM harness_transcript"
                    " WHERE tenant_id = ? AND session_id = ? ORDER BY seq",
                    (tenant_id, sid),
                ).fetchall()
            for seq, kind, entry_id, payload in rows:
                if kind == _FORK_KIND:
                    info = json.loads(payload)
                    edges.append(
                        {
                            "from_session_id": sid,
                            "from_entry_id": entry_id,
                            "to_session_id": info["source_session_id"],
                            "to_entry_id": info["source_entry_id"],
                        }
                    )
                    _walk(info["source_session_id"], depth + 1)
                    continue
                nodes.append(
                    {"session_id": sid, "entry_id": entry_id, "seq": seq, "kind": kind}
                )

        _walk(session_id, 0)
        return {"session_id": session_id, "nodes": nodes, "edges": edges}

    # ------------------------------------------------------------- 恢复

    def rebuild_working_context(
        self, *, tenant_id: str, session_id: str
    ) -> WorkingContext:
        """从 Transcript 事件流重放确定性规则，重建 Working Context。

        长任务方案 MVP 第 6 条：Working Context 使用结构化 Patch 更新并可从
        Transcript 重建。重放规则与引擎内一致（``working_context`` 模块）：

        - ``tool.call.end`` 失败/审批拒绝 → ``recent_tool_failures``；
        - ``tool.call.end`` 成功结果中的关键事实 → ``verified_facts``。

        Transcript 不受压缩影响（压缩只动图 Checkpoint 内的消息，事件流
        完整持久化），因此重放结果即确定性真值。
        """
        working = WorkingContext()
        for _seq, payload in self._resolve_rows(
            tenant_id=tenant_id, session_id=session_id, kind=_EVENT_KIND
        ):
            data = json.loads(payload)
            if data.get("event_type") != EventType.TOOL_CALL_END:
                continue
            event_payload = data.get("payload") or {}
            name = str(event_payload.get("name") or "")
            if not name:
                continue
            error = event_payload.get("error")
            if error:
                working = record_tool_failure(
                    working, name=name, error=str(error)
                )
            else:
                working = record_tool_result(
                    working, name=name, result_text=str(event_payload.get("result") or "")
                )
        return working

    def rebuild_state(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> HarnessState:
        """从 Transcript 重建 HarnessState 的消息与 Working Context（plan §6.2.1
        + 长任务方案 MVP 第 6 条：Working Context 可从 Transcript 重建）。"""
        return HarnessState(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            messages=self.messages(tenant_id=tenant_id, session_id=session_id),
            working_context=self.rebuild_working_context(
                tenant_id=tenant_id, session_id=session_id
            ),
        )

    def recover(
        self, checkpoint_state: HarnessState, *, invocation_id: str
    ) -> tuple[HarnessState, RuntimeEvent | None]:
        """图 Checkpoint 恢复后的影子状态对账：不一致以 Transcript 为准。

        返回 (对账后状态, context.recovered 事件)。一致时事件为 None。
        """
        transcript = self.rebuild_state(
            tenant_id=checkpoint_state.tenant_id,
            user_id=checkpoint_state.user_id,
            agent_id=checkpoint_state.agent_id,
            session_id=checkpoint_state.session_id,
        )
        cp_count = len(checkpoint_state.messages)
        ts_count = len(transcript.messages)
        # 仅工具派生字段（verified_facts / recent_tool_failures / version）可从
        # Transcript 重放；goal/plan 等语义字段 Transcript 无法推导，保留 Checkpoint。
        ts_wc = transcript.working_context
        cp_wc = checkpoint_state.working_context
        wc_differs = (
            ts_wc.verified_facts != cp_wc.verified_facts
            or ts_wc.recent_tool_failures != cp_wc.recent_tool_failures
        )
        if cp_count == ts_count and not wc_differs:
            return checkpoint_state, None
        reason = (
            f"transcript_wins: checkpoint had {cp_count} messages, "
            f"transcript has {ts_count}; adopting transcript"
        )
        # 消息与 Working Context 工具派生字段以 Transcript 为准（Transcript
        # 不受压缩影响）；Checkpoint 侧其余运行时字段（approval/goal 等）保留。
        merged_wc = cp_wc.model_copy(
            update={
                "version": ts_wc.version,
                "verified_facts": ts_wc.verified_facts,
                "recent_tool_failures": ts_wc.recent_tool_failures,
            }
        )
        recovered = checkpoint_state.model_copy(
            update={"messages": transcript.messages, "working_context": merged_wc}
        )
        event = RuntimeEvent.create(
            EventType.CONTEXT_RECOVERED,
            agent_id=checkpoint_state.agent_id,
            user_id=checkpoint_state.user_id,
            session_id=checkpoint_state.session_id,
            invocation_id=invocation_id,
            seq_id=1,
            payload={"reason": reason},
        )
        return recovered, event


__all__ = ["ForkResult", "SessionStoreError", "SqliteSessionStore"]
