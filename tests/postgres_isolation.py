"""Schema isolation for PostgreSQL tests that share one disposable server."""

from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4


@asynccontextmanager
async def isolated_postgres_dsn(dsn):
    """Keep fixed fixture IDs and destructive resets inside a fresh schema."""
    import asyncpg

    schema = "kernel_test_" + uuid4().hex
    url = urlsplit(dsn)
    params = dict(parse_qsl(url.query), search_path=schema)
    async with AsyncExitStack() as cleanup:
        admin = await asyncpg.connect(dsn)
        cleanup.push_async_callback(admin.close)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        cleanup.push_async_callback(admin.execute, f'DROP SCHEMA "{schema}" CASCADE')
        yield url._replace(query=urlencode(params)).geturl()
