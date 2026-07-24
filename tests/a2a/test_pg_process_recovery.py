"""Real PostgreSQL and OS-process recovery tests for the A2A TaskStore."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from a2a.server import models as sdk_models
from a2a.server.tasks import database_task_store as sdk_database_task_store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.task_store import A2A_TASK_TABLE

WORKER = Path(__file__).with_name("pg_process_worker.py")


def test_import_does_not_replace_a2a_sdk_task_model_factory() -> None:
    assert sdk_database_task_store.create_task_model is sdk_models.create_task_model


def _dsn() -> str:
    dsn = os.environ.get("KSADK_A2A_TEST_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("KSADK_A2A_TEST_DATABASE_URL must point to a real PostgreSQL 16 instance")
    if not dsn.startswith("postgresql+asyncpg://"):
        pytest.fail("KSADK_A2A_TEST_DATABASE_URL must use postgresql+asyncpg, not SQLite")
    return dsn


def _worker_command(
    action: str,
    *,
    dsn: str,
    task_id: str,
    account: str | None = None,
    runtime: str | None = None,
    hold: bool = False,
    no_create_table: bool = True,
) -> list[str]:
    command = [sys.executable, str(WORKER), action, "--dsn", dsn, "--task-id", task_id]
    if account is not None:
        command.extend(("--account", account))
    if runtime is not None:
        command.extend(("--runtime", runtime))
    if hold:
        command.append("--hold")
    if no_create_table:
        command.append("--no-create-table")
    return command


def _start_worker(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _worker_command(*args, **kwargs),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_worker_line(process: subprocess.Popen[str], timeout: float = 20.0) -> str:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise AssertionError(f"worker {process.pid} did not become ready in {timeout}s")
        return process.stdout.readline()
    finally:
        selector.close()


def _finished_worker(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    process = _start_worker(*args, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=20)
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        raise
    assert process.returncode == 0, stderr
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (stdout, stderr)
    return process.pid, json.loads(lines[0])


@pytest.fixture(scope="module")
def pg_recovery_evidence() -> Iterator[dict[str, Any]]:
    dsn = _dsn()
    seed_id = str(uuid.uuid4())
    process_a = _start_worker(
        "write",
        dsn=dsn,
        task_id=seed_id,
        account="account-a",
        runtime="runtime-a",
        hold=True,
        no_create_table=False,
    )
    child_processes = [process_a]
    try:
        ready_line = _read_worker_line(process_a)
        if not ready_line:
            _, stderr = process_a.communicate(timeout=5)
            raise AssertionError(f"process A exited before persisting the task: {stderr}")
        written = json.loads(ready_line)
        task_id = written["task"]["id"]
        kill_started_at = time.time_ns()
        process_a.kill()
        process_a.wait(timeout=10)
        killed_at = time.time_ns()
        assert process_a.returncode is not None and process_a.returncode < 0

        queries = {
            "correct_owner": _start_worker(
                "get", dsn=dsn, task_id=task_id, account="account-a", runtime="runtime-a"
            ),
            "different_runtime": _start_worker(
                "get", dsn=dsn, task_id=task_id, account="account-a", runtime="runtime-b"
            ),
            "different_account": _start_worker(
                "get", dsn=dsn, task_id=task_id, account="account-b", runtime="runtime-a"
            ),
        }
        child_processes.extend(queries.values())
        query_results: dict[str, dict[str, Any]] = {}
        query_pids: dict[str, int] = {}
        for name, process in queries.items():
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            query_results[name] = json.loads(stdout)
            query_pids[name] = process.pid

        process_b_pid, recovered = _finished_worker(
            "recover",
            dsn=dsn,
            task_id=task_id,
            account="account-a",
            runtime="runtime-a",
        )
        evidence = {
            "task_id": task_id,
            "process_a_pid": process_a.pid,
            "process_b_pid": process_b_pid,
            "query_pids": query_pids,
            "kill_started_at_ns": kill_started_at,
            "killed_at_ns": killed_at,
            "written": written,
            "queries": query_results,
            "recovered": recovered,
        }
        print(f"PG_PROCESS_RECOVERY_EVIDENCE={json.dumps(evidence, sort_keys=True)}")
        yield evidence
    finally:
        for process in child_processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_process_b_recovers_and_continues_task_after_process_a_is_killed(
    pg_recovery_evidence: dict[str, Any],
) -> None:
    evidence = pg_recovery_evidence
    written_task = evidence["written"]["task"]
    before = evidence["recovered"]["before"]
    after = evidence["recovered"]["after"]

    assert evidence["process_a_pid"] != evidence["process_b_pid"]
    assert evidence["killed_at_ns"] >= evidence["kill_started_at_ns"]
    assert written_task == before
    assert before["id"] == evidence["task_id"]
    assert before["context_id"] == before["metadata"]["run_handle"]["session_id"]
    assert before["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert before["artifacts"][0]["parts"] == [{"text": "durable draft"}]
    assert before["metadata"]["resume_target"] == {
        "id": f"checkpoint-{evidence['task_id']}",
        "kind": "checkpoint_id",
    }

    assert after["id"] == evidence["task_id"]
    assert after["context_id"] == before["context_id"]
    assert after["status"]["state"] == "TASK_STATE_COMPLETED"
    assert after["artifacts"][0]["parts"] == [{"text": "continued by process B"}]
    assert evidence["recovered"]["attach_calls"] == [evidence["task_id"]]
    assert evidence["recovered"]["resume_calls"] == [evidence["task_id"]]


def test_concurrent_process_queries_enforce_account_and_runtime_owner_scope(
    pg_recovery_evidence: dict[str, Any],
) -> None:
    queries = pg_recovery_evidence["queries"]
    query_pids = pg_recovery_evidence["query_pids"]
    assert len(set(query_pids.values())) == 3
    assert queries["correct_owner"]["owner"] == "account-a/runtime-a"
    assert queries["correct_owner"]["task"]["id"] == pg_recovery_evidence["task_id"]
    assert queries["different_runtime"]["owner"] == "account-a/runtime-b"
    assert queries["different_runtime"]["task"] is None
    assert queries["different_account"]["owner"] == "account-b/runtime-a"
    assert queries["different_account"]["task"] is None


@pytest.mark.asyncio
async def test_postgres_schema_and_owner_index_are_initialized(
    pg_recovery_evidence: dict[str, Any],
) -> None:
    engine = create_async_engine(_dsn())
    try:
        async with engine.connect() as connection:
            table = await connection.scalar(
                text("SELECT to_regclass(:table_name)"), {"table_name": A2A_TASK_TABLE}
            )
            columns = (
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :table_name"
                        ),
                        {"table_name": A2A_TASK_TABLE},
                    )
                )
                .scalars()
                .all()
            )
            indexes = (
                (
                    await connection.execute(
                        text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE schemaname = 'public' AND tablename = :table_name"
                        ),
                        {"table_name": A2A_TASK_TABLE},
                    )
                )
                .scalars()
                .all()
            )
            owner = await connection.scalar(
                text(f"SELECT owner FROM {A2A_TASK_TABLE} WHERE id = :task_id"),
                {"task_id": pg_recovery_evidence["task_id"]},
            )
    finally:
        await engine.dispose()

    assert table == A2A_TASK_TABLE
    assert {"id", "context_id", "owner", "status", "artifacts", "history", "metadata"} <= set(
        columns
    )
    assert f"idx_{A2A_TASK_TABLE}_owner_last_updated" in indexes
    assert owner == "account-a/runtime-a"


def test_anonymous_fallback_is_shared_only_inside_the_anonymous_owner_scope() -> None:
    dsn = _dsn()
    task_id = str(uuid.uuid4())
    writer_pid, written = _finished_worker("write", dsn=dsn, task_id=task_id, no_create_table=True)
    persisted_task_id = written["task"]["id"]
    anonymous_pid, anonymous = _finished_worker("get", dsn=dsn, task_id=persisted_task_id)
    authenticated_pid, authenticated = _finished_worker(
        "get", dsn=dsn, task_id=persisted_task_id, account="account-a", runtime="runtime-a"
    )

    assert len({writer_pid, anonymous_pid, authenticated_pid}) == 3
    assert written["owner"] == "anonymous"
    assert anonymous["owner"] == "anonymous"
    assert anonymous["task"]["id"] == persisted_task_id
    assert authenticated["owner"] == "account-a/runtime-a"
    assert authenticated["task"] is None
