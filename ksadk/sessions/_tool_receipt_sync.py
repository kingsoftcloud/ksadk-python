"""SQLite durable tool receipt claim primitives."""

from __future__ import annotations

import json

from ksadk.sessions._local_tables import tool_receipts_table
from ksadk.sessions.base import ToolReceiptClaim


class _LocalToolReceiptMixin:
    @staticmethod
    def _tool_receipt_from_row(row, *, acquired: bool = False) -> ToolReceiptClaim:
        state = str(row["state"])
        if state not in {"unknown", "completed", "failed"}:
            raise ValueError("tool receipt state is invalid")
        return ToolReceiptClaim(
            session_id=str(row["session_id"]),
            idempotency_key=str(row["idempotency_key"]),
            tool_name=str(row["tool_name"]),
            arguments_digest=str(row["arguments_digest"]),
            state=state,
            claim_id=str(row["claim_id"]),
            output=json.loads(row["output_json"]) if row["output_json"] is not None else None,
            acquired=acquired,
        )

    def _claim_tool_receipt_sync(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        tool_name: str,
        arguments_digest: str,
        claim_id: str,
    ) -> ToolReceiptClaim:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {tool_receipts_table} "
                "WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            if row is not None:
                result = self._tool_receipt_from_row(row)
                if result.tool_name != tool_name or result.arguments_digest != arguments_digest:
                    raise ValueError("tool receipt idempotency key conflict")
                return result
            inserted = connection.execute(
                f"INSERT OR IGNORE INTO {tool_receipts_table} "
                "(session_id,idempotency_key,tool_name,arguments_digest,state,claim_id,"
                "output_json) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (session_id, idempotency_key, tool_name, arguments_digest, "unknown", claim_id),
            )
            row = connection.execute(
                f"SELECT * FROM {tool_receipts_table} "
                "WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise RuntimeError("tool receipt claim disappeared")
            return self._tool_receipt_from_row(row, acquired=inserted.rowcount == 1)

    def _settle_tool_receipt_sync(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        tool_name: str,
        arguments_digest: str,
        claim_id: str,
        state: str,
        output,
    ) -> ToolReceiptClaim:
        if state not in {"completed", "failed"}:
            raise ValueError("tool receipt terminal state invalid")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {tool_receipts_table} "
                "WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise ValueError("tool receipt claim not found")
            result = self._tool_receipt_from_row(row)
            if result.tool_name != tool_name or result.arguments_digest != arguments_digest:
                raise ValueError("tool receipt idempotency key conflict")
            if result.claim_id != claim_id:
                return result
            if result.state != "unknown":
                if result.state != state or result.output != output:
                    raise ValueError("tool receipt terminal result conflict")
                return result
            connection.execute(
                f"UPDATE {tool_receipts_table} SET state = ?, output_json = ? "
                "WHERE session_id = ? AND idempotency_key = ? AND claim_id = ? "
                "AND state = 'unknown'",
                (state, json.dumps(output), session_id, idempotency_key, claim_id),
            )
            row = connection.execute(
                f"SELECT * FROM {tool_receipts_table} "
                "WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            return self._tool_receipt_from_row(row)

    def _get_tool_receipt_claim_sync(
        self, session_id: str, *, idempotency_key: str
    ) -> ToolReceiptClaim | None:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {tool_receipts_table} "
                "WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
            return self._tool_receipt_from_row(row) if row is not None else None
