"""Kill the whole local Kernel/native process group, then answer its original input."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from tests.a2a.test_invocation_checkpoint import dsn
from tests.codex.test_managed_checkpoint_real import expert_response
from tests.e2e.codex_responses_stub import DeterministicResponsesStub

pytestmark = pytest.mark.local_process_heavy


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_kind", ["input", "approval"])
async def test_hard_killed_kernel_recovers_same_remote_input_without_resend(tmp_path, wait_kind):
    database = dsn()
    processes = []
    with DeterministicResponsesStub() as stub:
        stub._events = expert_response
        env = {
            **os.environ,
            "KSADK_A2A_TEST_DATABASE_URL": database,
            "CODEX_FIXTURE_RESPONSES_URL": stub.base_url,
            "CODEX_FIXTURE_WAIT_KIND": wait_kind,
            "KSADK_TOOL_APPROVAL_MODE": "full",
        }

        async def launch(mode):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.codex.managed_recovery_process",
                str(tmp_path),
                mode,
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            processes.append(process)
            return process

        async def read_report(process):
            lines = []
            async with asyncio.timeout(35):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        await process.wait()
                        pytest.fail("fixture exited before report:\n" + "".join(lines))
                    text = line.decode()
                    lines.append(text)
                    with (tmp_path / "process-output.log").open("a") as output:
                        output.write(text)
                    if text.startswith("RECOVERY_FIXTURE:"):
                        return json.loads(text.split(":", 1)[1])

        try:
            first = await launch("first")
            waiting = await read_report(first)
            assert waiting["status"] == "waiting", waiting
            assert os.getpgid(waiting["native_pid"]) == first.pid
            os.killpg(first.pid, signal.SIGKILL)
            assert await first.wait() == -signal.SIGKILL
            # Real lease expiry, not a fabricated fencing-token edit.
            await asyncio.sleep(15.1)
            second = await launch("second")
            completed = await read_report(second)
            assert completed["status"] == "completed", completed
            for key in ("run_id", "thread_id", "interaction_id"):
                assert completed[key] == waiting[key]
            assert completed["fence"] > waiting["fence"]
            async with asyncio.timeout(10):
                remaining = await second.stdout.read()
                assert await second.wait() == 0, remaining.decode()
            wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
            assert sum(item["kind"] == "send" for item in wire) == 1
            assert sum(item["kind"] == "continue" for item in wire) == int(wait_kind == "input")
            assert sum(item["kind"] == "cancel" for item in wire) == 0
            if wait_kind == "input":
                continued = next(item for item in wire if item["kind"] == "continue")
                assert continued["task_id"] == "remote-1"
                assert continued["context_id"] == "context-1"
                assert continued["data_parts"] == ["yes"]
        finally:
            for process in processes:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
