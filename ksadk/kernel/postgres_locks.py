"""Transaction advisory locks across supported PostgreSQL catalog versions."""

from __future__ import annotations

from typing import Any


async def acquire_text_advisory_lock(connection: Any, key: str) -> None:
    # Keep the exact existing key when the server provides extended hashes:
    # old and new hosts must exclude each other during a rolling upgrade.
    extended = await connection.fetchval(
        "SELECT pg_catalog.to_regprocedure('pg_catalog.hashtextextended(text,bigint)') IS NOT NULL"
    )
    if extended:
        sql = "SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended($1,0))"
    else:
        # MD5 and the signed 64-bit cast are available on PostgreSQL 9.6.
        # This namespace never had a working extended-hash lock on that server.
        sql = (
            "SELECT pg_advisory_xact_lock("
            "('x'||substr(pg_catalog.md5($1::text),1,16))::bit(64)::bigint)"
        )
    await connection.execute(sql, key)
