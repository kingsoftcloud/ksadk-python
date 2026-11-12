"""Native process proof, distinct from full Kernel takeover/deployment acceptance."""

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
@pytest.mark.parametrize("pause", ["approval", "input"])
@pytest.mark.parametrize("workflow", ["direct", "sequential", "tool"])
@pytest.mark.parametrize("native_backend", ["sqlite", "postgres"])
async def test_real_adk_native_invocation_resumes_after_abrupt_process_exit(
    tmp_path, pause, workflow, native_backend
):
    env = dict(
        os.environ,
        KSADK_A2A_TEST_DATABASE_URL=dsn(),
        R85_NATIVE_RECOVERY_DIRECTORY=str(tmp_path),
        R85_NATIVE_RECOVERY_ACCOUNT=str(uuid.uuid4()),
        R85_NATIVE_RECOVERY_PAUSE=pause,
        R85_NATIVE_RECOVERY_WORKFLOW=workflow,
    )
    native_engine = None
    schema = "r85_native_" + uuid.uuid4().hex
    if native_backend == "postgres":
        native_engine = create_async_engine(dsn())
        async with native_engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        env["R85_NATIVE_RECOVERY_SCHEMA"] = schema
    try:
        for stage, expected in [("start", 23), ("resume", 0)]:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.a2a.native_process_recovery_worker",
                env=dict(env, R85_NATIVE_RECOVERY_STAGE=stage),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            except TimeoutError:
                process.kill()
                await process.communicate()
                raise
            assert process.returncode == expected, (stdout + stderr).decode(errors="replace")
    finally:
        if native_engine is not None:
            async with native_engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
            await native_engine.dispose()
    original = json.loads((tmp_path / "native-state.json").read_text())
    recovered = json.loads((tmp_path / "recovered.json").read_text())
    assert recovered["native_invocations"] == [original["native_invocation_id"]]
    assert recovered["user_messages"] == 1
    assert recovered["request"] == original["request"]
    if pause == "approval":
        assert original["sends"] == []
        assert recovered["sends"] == [original["request"]["message"]["message_id"]]
    else:
        assert original["sends"] == [original["request"]["message"]["message_id"]]
        assert len(recovered["sends"]) == 1
        assert recovered["sends"][0] != original["sends"][0]
        assert recovered["sent_tasks"] == ["remote-1"]
    assert any(
        event["event_type"] == "item.completed" and event["item_kind"] == "artifact"
        for event in recovered["output"]
    )
