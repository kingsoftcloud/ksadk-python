"""Harness Tool Receipt（plan §11.2）——审批后仅执行一次的幂等锚点。

审批恢复流程要求：审批通过 → 校验 revision/checkpoint/receipt → 执行一次 →
持久化 ToolReceipt。Receipt 以 (invocation_id, call_id) 为幂等键：重复执行
同一审批调用直接返回既有结果，不再触发副作用。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolReceipt:
    invocation_id: str
    call_id: str
    tool_name: str
    arguments_digest: str
    decision: str  # approved / denied
    status: str  # executed / skipped
    result_digest: str = ""


class ToolReceiptStore:
    """本地 SQLite Receipt 存储（幂等键 (invocation_id, call_id)）。"""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS harness_tool_receipts (
        invocation_id    TEXT NOT NULL,
        call_id          TEXT NOT NULL,
        tool_name        TEXT NOT NULL,
        arguments_digest TEXT NOT NULL,
        decision         TEXT NOT NULL,
        status           TEXT NOT NULL,
        result_digest    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (invocation_id, call_id)
    );
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(self._SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(self, receipt: ToolReceipt) -> ToolReceipt | None:
        """记录 Receipt；幂等键已存在时返回既有 Receipt（不覆盖）。"""
        with self._lock:
            existing = self._conn.execute(
                "SELECT invocation_id, call_id, tool_name, arguments_digest, "
                "decision, status, result_digest FROM harness_tool_receipts "
                "WHERE invocation_id = ? AND call_id = ?",
                (receipt.invocation_id, receipt.call_id),
            ).fetchone()
            if existing:
                return ToolReceipt(*existing)
            self._conn.execute(
                "INSERT INTO harness_tool_receipts VALUES (?,?,?,?,?,?,?)",
                (
                    receipt.invocation_id,
                    receipt.call_id,
                    receipt.tool_name,
                    receipt.arguments_digest,
                    receipt.decision,
                    receipt.status,
                    receipt.result_digest,
                ),
            )
            self._conn.commit()
            return None

    def get(self, invocation_id: str, call_id: str) -> ToolReceipt | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT invocation_id, call_id, tool_name, arguments_digest, "
                "decision, status, result_digest FROM harness_tool_receipts "
                "WHERE invocation_id = ? AND call_id = ?",
                (invocation_id, call_id),
            ).fetchone()
        return ToolReceipt(*row) if row else None


__all__ = ["ToolReceipt", "ToolReceiptStore"]
