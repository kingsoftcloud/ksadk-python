"""New Kernel activation resumes real ADK and its PostgreSQL approval ledger."""

import asyncio
import json
import os
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from tests.a2a.test_invocation_checkpoint import dsn


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["direct", "sequential", "tool"])
@pytest.mark.parametrize("pause", ["approval", "input", "terminal"])
async def test_kernel_takeover_resumes_native_adk_approval_in_another_process(
    tmp_path, workflow, pause
):
    database = dsn()
    engine = create_async_engine(database)
    kernel_schema = "r85_kernel_" + uuid.uuid4().hex
    native_schema = "r85_native_" + uuid.uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(kernel_schema))
        await connection.execute(CreateSchema(native_schema))
    env = dict(
        os.environ,
        KSADK_A2A_TEST_DATABASE_URL=database,
        R85_KERNEL_NATIVE_DIRECTORY=str(tmp_path),
        R85_KERNEL_NATIVE_WORKFLOW=workflow,
        R85_KERNEL_NATIVE_PAUSE=pause,
        R85_KERNEL_NATIVE_SCHEMA=kernel_schema,
        R85_KERNEL_ADK_SCHEMA=native_schema,
    )
    try:
        for stage, expected in [("start", 23), ("resume", 0)]:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.a2a.kernel_native_process_worker",
                env=dict(env, R85_KERNEL_NATIVE_STAGE=stage),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
            except TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
                pytest.fail((stdout + stderr).decode(errors="replace"))
            assert process.returncode == expected, (stdout + stderr).decode(errors="replace")
            if stage == "start":
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f'UPDATE "{kernel_schema}".kernel_activations '
                            "SET lease_expires_at = now() - interval '1 second'"
                        )
                    )
        report = json.loads((tmp_path / "kernel-native-recovered.json").read_text())
        assert report["sends"] == (0 if pause == "terminal" else 1)
        assert report["resolved"] == "resolved"
        assert report["event_types"].count("run.started") == 1
        assert report["event_types"].count("run.completed") == 1
        assert report["ledger_events"].count("interaction.requested") == (
            3 if pause == "input" else 1
        )
        assert report["ledger_events"].count("interaction.resolved") == (
            3 if pause == "input" else 1
        )
        if pause == "input":
            previous = json.loads((tmp_path / "kernel-native-state.json").read_text())
            assert len(previous["sent_messages"]) == 1
            assert report["sent_messages"][0] != previous["sent_messages"][0]
            assert report["sent_tasks"] == [previous["remote_binding"][0]]
        assert "run.interrupted" not in report["event_types"]
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(native_schema, cascade=True))
            await connection.execute(DropSchema(kernel_schema, cascade=True))
        await engine.dispose()
