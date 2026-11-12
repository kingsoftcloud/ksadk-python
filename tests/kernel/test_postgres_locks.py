"""Live old/new lock interoperability, plus both catalog branches without a server."""

import os
from uuid import uuid4

import pytest

from ksadk.kernel.postgres_locks import acquire_text_advisory_lock


@pytest.mark.parametrize("extended", [False, True])
async def test_lock_uses_only_the_available_hash_function(extended):
    class Connection:
        async def fetchval(self, query):
            assert "to_regprocedure" in query
            return extended

        async def execute(self, query, key):
            assert key == "teams-host-registry:test"
            assert ("hashtextextended($1,0)" in query) == extended
            assert ("md5($1::text)" in query) == (not extended)

    await acquire_text_advisory_lock(Connection(), "teams-host-registry:test")


async def test_real_pg_lock_matches_existing_hosts_and_releases_at_commit():
    configured = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not configured:
        pytest.skip("KSADK_TEST_POSTGRES_DSN requires isolated PostgreSQL")
    import asyncpg

    owner = await asyncpg.connect(configured)
    peer = await asyncpg.connect(configured)
    try:
        extended = await owner.fetchval(
            "SELECT to_regprocedure('pg_catalog.hashtextextended(text,bigint)') IS NOT NULL"
        )
        key = "teams-host-registry:" + uuid4().hex
        lock_key = await owner.fetchval(
            "SELECT pg_catalog.hashtextextended($1,0)"
            if extended
            else "SELECT ('x'||substr(md5($1::text),1,16))::bit(64)::bigint",
            key,
        )
        async with owner.transaction():
            await acquire_text_advisory_lock(owner, key)
            async with peer.transaction():
                assert (
                    await peer.fetchval("SELECT pg_try_advisory_xact_lock($1::bigint)", lock_key)
                    is False
                )
        async with peer.transaction():
            assert (
                await peer.fetchval("SELECT pg_try_advisory_xact_lock($1::bigint)", lock_key)
                is True
            )
    finally:
        await owner.close()
        await peer.close()
