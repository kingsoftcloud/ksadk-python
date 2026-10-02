"""PostgreSQL durable tool receipt claim primitives."""

from __future__ import annotations

import json
from typing import Any, Literal

from ksadk.sessions._postgres_tables import pg_tool_receipts_table
from ksadk.sessions.base import ToolReceiptClaim


class _PostgresToolReceiptMixin:
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
            output=_PostgresToolReceiptMixin._json_from_value(row["output_json"]),
            acquired=acquired,
        )

    async def claim_tool_receipt(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        tool_name: str,
        arguments_digest: str,
        claim_id: str,
    ) -> ToolReceiptClaim:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                inserted = await connection.execute(
                    f"""INSERT INTO {pg_tool_receipts_table}
                    (namespace, tenant_id, workspace_id, session_id, idempotency_key,
                     tool_name, arguments_digest, state, claim_id, output_json)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,'unknown',$8,NULL)
                    ON CONFLICT (namespace, session_id, idempotency_key) DO NOTHING""",
                    self.namespace,
                    self.tenant_id,
                    self.workspace_id,
                    session_id,
                    idempotency_key,
                    tool_name,
                    arguments_digest,
                    claim_id,
                )
                row = await connection.fetchrow(
                    f"SELECT session_id,idempotency_key,tool_name,arguments_digest,state,claim_id,"
                    f"output_json FROM {pg_tool_receipts_table} "
                    "WHERE namespace=$1 AND session_id=$2 AND idempotency_key=$3 FOR UPDATE",
                    self.namespace,
                    session_id,
                    idempotency_key,
                )
                if row is None:
                    raise RuntimeError("tool receipt claim disappeared")
                result = self._tool_receipt_from_row(row)
                if result.tool_name != tool_name or result.arguments_digest != arguments_digest:
                    raise ValueError("tool receipt idempotency key conflict")
                return ToolReceiptClaim(
                    session_id=result.session_id,
                    idempotency_key=result.idempotency_key,
                    tool_name=result.tool_name,
                    arguments_digest=result.arguments_digest,
                    state=result.state,
                    claim_id=result.claim_id,
                    output=result.output,
                    acquired=inserted.endswith("1"),
                )

    async def settle_tool_receipt(
        self,
        session_id: str,
        *,
        idempotency_key: str,
        tool_name: str,
        arguments_digest: str,
        claim_id: str,
        state: Literal["completed", "failed"],
        output: Any,
    ) -> ToolReceiptClaim:
        if state not in {"completed", "failed"}:
            raise ValueError("tool receipt terminal state invalid")
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"SELECT session_id,idempotency_key,tool_name,arguments_digest,state,claim_id,"
                    f"output_json FROM {pg_tool_receipts_table} "
                    "WHERE namespace=$1 AND session_id=$2 AND idempotency_key=$3 FOR UPDATE",
                    self.namespace,
                    session_id,
                    idempotency_key,
                )
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
                await connection.execute(
                    f"UPDATE {pg_tool_receipts_table} SET state=$1, output_json=$2::jsonb "
                    "WHERE namespace=$3 AND session_id=$4 AND idempotency_key=$5 "
                    "AND claim_id=$6 AND state='unknown'",
                    state,
                    json.dumps(output),
                    self.namespace,
                    session_id,
                    idempotency_key,
                    claim_id,
                )
                row = await connection.fetchrow(
                    f"SELECT session_id,idempotency_key,tool_name,arguments_digest,state,claim_id,"
                    f"output_json FROM {pg_tool_receipts_table} "
                    "WHERE namespace=$1 AND session_id=$2 AND idempotency_key=$3",
                    self.namespace,
                    session_id,
                    idempotency_key,
                )
                return self._tool_receipt_from_row(row)

    async def get_tool_receipt_claim(
        self, session_id: str, *, idempotency_key: str
    ) -> ToolReceiptClaim | None:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT session_id,idempotency_key,tool_name,arguments_digest,state,claim_id,"
                f"output_json FROM {pg_tool_receipts_table} "
                "WHERE namespace=$1 AND session_id=$2 AND idempotency_key=$3",
                self.namespace,
                session_id,
                idempotency_key,
            )
        return self._tool_receipt_from_row(row) if row is not None else None
