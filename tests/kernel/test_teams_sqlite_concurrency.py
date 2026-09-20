"""Real shared-connection reads, independent writer and Host registry fencing."""

import asyncio

import aiosqlite

from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


async def test_lookup_reads_do_not_leave_snapshots_across_host_registry_transaction(host_harness):
    h = host_harness
    await h.prepare()
    await h.submit()
    cmd = h.context.command

    async def read():
        for _ in range(60):
            message = await h.store.load_by_idempotency(cmd.session_id, cmd.idempotency_key)
            assert str(message.command.command_id) == str(cmd.command_id)
            await asyncio.sleep(0)

    async def fence():
        for _ in range(60):
            assert await h.registry.incarnation() == h.incarnation
            await asyncio.sleep(0)

    if h.backend == "sqlite":
        async with aiosqlite.connect(h.store.db_path, isolation_level=None) as external:
            await external.execute("PRAGMA busy_timeout=5000")
            await external.execute("CREATE TABLE concurrent_host_probe (value INTEGER)")

            async def write():
                for value in range(60):
                    await external.execute("INSERT INTO concurrent_host_probe VALUES (?)", (value,))
                    await asyncio.sleep(0)

            await asyncio.gather(read(), read(), fence(), write())
    else:
        await asyncio.gather(read(), read(), fence())
