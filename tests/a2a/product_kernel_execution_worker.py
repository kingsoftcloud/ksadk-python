"""Execute and commit test Kernel facts, then wait for deliberate process kill."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

from tests.a2a.test_product_kernel_server_roundtrip import (
    durable_kernel_stack,
    execute_native_kernel,
)


async def main():
    setup = json.loads(sys.stdin.readline())
    setup["protocol_pg_schema"] = sys.argv[1]
    fixture = durable_kernel_stack.__wrapped__(
        SimpleNamespace(kernel_database=(os.environ["A2A_TEST_POSTGRES_DSN"], sys.argv[1]))
    )
    build = await anext(fixture)
    try:
        facts = await execute_native_kernel(setup, build)
        print(json.dumps({"event_ids": [str(event.event_id) for event in facts]}), flush=True)
        if not setup.get("query_only"):
            await asyncio.Event().wait()
    finally:
        await fixture.aclose()


if __name__ == "__main__":
    asyncio.run(main())
