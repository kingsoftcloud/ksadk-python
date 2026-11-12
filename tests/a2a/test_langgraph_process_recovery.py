"""Process proof for LangGraph; separate from full Kernel takeover proof."""
import asyncio
import json
import os
import sys
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from tests.a2a.test_invocation_checkpoint import dsn


@pytest.mark.asyncio
async def test_langgraph_fixed_node_reuses_request_after_process_exit(tmp_path):
    database = dsn()
    schema = 'r86_langgraph_' + uuid.uuid4().hex
    engine = create_async_engine(database)
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
    try:
        for stage, expected in [('start', 23), ('resume', 0)]:
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'tests.a2a.langgraph_process_recovery_worker',
                env=dict(os.environ, R86_LG_DIRECTORY=str(tmp_path), R86_LG_STAGE=stage,
                         R86_LG_SCHEMA=schema, KSADK_A2A_TEST_DATABASE_URL=database),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 35)
            except TimeoutError:
                process.kill()
                await process.communicate()
                raise
            assert process.returncode == expected, (stdout + stderr).decode(errors='replace')
        previous = json.loads((tmp_path / 'state.json').read_text())
        result = json.loads((tmp_path / 'recovered.json').read_text())
        assert result['invoked'] == [previous['request']], (previous, result['invoked'])
        assert result['request'] == previous['request']
        assert result['sends'] == [previous['request']['message']['message_id']]
        assert any(e['event_type'] == 'run.completed' for e in result['output'])
        assert not any(e['event_type'] == 'run.failed' for e in result['output'])
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))
        await engine.dispose()
