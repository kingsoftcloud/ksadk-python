"""Harness Tool Receipt（plan §11.2）——审批后仅执行一次的幂等锚点。

审批恢复流程要求：审批通过 → 校验 revision/checkpoint/receipt → 执行一次 →
持久化 ToolReceipt。Receipt 以 (invocation_id, call_id) 为幂等键：重复执行
同一审批调用直接返回既有结果，不再触发副作用。

Receipt 保证的是「已持久化执行结果不会因 Graph 重放而再次执行」。外部系统已
成功、但 Receipt 提交前进程即崩溃的极小窗口，MCP Binding 可显式声明
``idempotency_mode="transport"``，由 Harness 将 ``run_id + call_id`` 派生的
稳定幂等键透传给远端；不支持该合同的 Tool 仍需由上游提供幂等键或事务性
outbox，不能宣称端到端 exactly-once。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
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
        self.path = Path(db_path)
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
