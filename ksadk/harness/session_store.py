"""Harness Session/Transcript 持久化（plan §6.2.1 / §17 Phase 2）。

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
from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.state import HarnessState, Message, MessageRole

_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_transcript (
    tenant_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, session_id, seq)
);
CREATE INDEX IF NOT EXISTS harness_transcript_session
    ON harness_transcript (tenant_id, session_id);
"""

_MESSAGE_KIND = "message"
_EVENT_KIND = "event"


class SqliteSessionStore:
    """Session/Transcript 存储的本地 SQLite 实现（租户隔离由主键强制）。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- 写入

    def append_message(
        self, *, tenant_id: str, session_id: str, message: Message, seq: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO harness_transcript VALUES (?,?,?,?,?)",
                (
                    tenant_id,
                    session_id,
                    seq,
                    _MESSAGE_KIND,
                    json.dumps(
                        {
                            "role": message.role.value,
                            "content": message.content,
                            "tool_call_id": message.tool_call_id,
                            "name": message.name,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            self._conn.commit()

    def append_event(self, *, tenant_id: str, session_id: str, event: RuntimeEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO harness_transcript VALUES (?,?,?,?,?)",
                (
                    tenant_id,
                    session_id,
                    event.seq_id,
                    _EVENT_KIND,
                    json.dumps(
                        {"event_type": event.event_type, "payload": event.payload},
                        ensure_ascii=False,
                    ),
                ),
            )
            self._conn.commit()

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

    def messages(self, *, tenant_id: str, session_id: str) -> list[Message]:
        messages = []
        for _seq, payload in self._rows(
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
            for _seq, payload in self._rows(
                tenant_id=tenant_id, session_id=session_id, kind=_EVENT_KIND
            )
        ]

    # ------------------------------------------------------------- 恢复

    def rebuild_state(
        self,
        *,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> HarnessState:
        """从 Transcript 重建 HarnessState 的消息部分（plan §6.2.1）。"""
        return HarnessState(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            messages=self.messages(tenant_id=tenant_id, session_id=session_id),
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
        if cp_count == ts_count:
            return checkpoint_state, None
        reason = (
            f"transcript_wins: checkpoint had {cp_count} messages, "
            f"transcript has {ts_count}; adopting transcript"
        )
        # 保留 Checkpoint 侧的运行时字段（approval/working context 等），
        # 消息以 Transcript 为准。
        recovered = checkpoint_state.model_copy(update={"messages": transcript.messages})
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


__all__ = ["SqliteSessionStore"]
