# -*- coding: utf-8 -*-
"""PostgresAgentKernelStore fencing / 并发 / 故障注入测试（Phase 1 Task 4）。

需要真实 PostgreSQL：``KSADK_TEST_POSTGRES_DSN``。未提供 DSN 时所有用例
明确 skip（skip 不等于通过；CI/预发门禁必须提供 DSN）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from datetime import UTC, datetime

import pytest

from ksadk.kernel.contracts import SessionEventEnvelope
from ksadk.kernel.errors import AgentKernelError, InvalidCommandError, StaleFenceError
from ksadk.kernel.state import InboxState
from tests.kernel.store_conformance import (
    AGENT,
    CONFORMANCE_CHECKS,
    SESSION,
    assert_concurrent_accepts_have_no_loss_or_duplicate,
    command,
    lease_request,
)


def _dsn() -> str | None:
    return os.environ.get("KSADK_TEST_POSTGRES_DSN") or None


@pytest.fixture(scope="module")
def pg_dsn():
    dsn = _dsn()
    if not dsn:
        pytest.skip(
        "KSADK_TEST_POSTGRES_DSN not set; Postgres kernel tests require a live database"
    )
    return dsn


@pytest.fixture
async def pg_store(pg_dsn):
    from ksadk.kernel.postgres_store import (
        PostgresAgentKernelStore,
        PostgresKernelEventLog,
    )
    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(dsn=pg_dsn)
    await service.create_session(agent_id=AGENT, user_id="kernel-user", session_id=SESSION)
    event_log = PostgresKernelEventLog(service._pool)
    store = PostgresAgentKernelStore(service._pool, event_log)
    await store.ensure_schema()
    yield store
    await store.reset_for_tests()
    await service.aclose()


async def expire_lease(store, lease):
    """直接把 lease 过期时间回拨，模拟 TTL 耗尽（不经 sleep）。"""
    await store._pool.execute(
        "UPDATE kernel_activations SET lease_expires_at = now() - interval '1 second' "
        "WHERE activation_id = $1",
        lease.activation_id,
    )


def runtime_envelope(session_id: str = SESSION) -> SessionEventEnvelope:
    return SessionEventEnvelope(
        event_id=uuid.uuid4(),
        session_id=session_id,
        seq=0,
        timestamp=datetime.now(UTC).isoformat(),
        family="runtime",
        family_version=2,
        event_type="run.started",
        payload={
            "schema_version": 2,
            "event_id": f"evt_{uuid.uuid4().hex}",
            "seq": 0,
            "timestamp": time.time(),
            "run_id": "run-1",
            "run_seq": 1,
            "scope_id": "scope-1",
            "source": {"framework": "ksadk"},
            "status": "running",
        },
    )


# --------------------------------------------------------------- Step 1 tests


async def test_takeover_fences_old_owner(pg_store):
    old = await pg_store.acquire_activation(lease_request("pod-a"))
    await expire_lease(pg_store, old)
    new = await pg_store.acquire_activation(lease_request("pod-b"))
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(StaleFenceError):
        await pg_store.append_event(runtime_envelope(), expected_fence=old.fencing_token)
    # 旧 owner 的 renew / claim / complete / run transition 全部被 fence 挡住。
    with pytest.raises(AgentKernelError):
        await pg_store.renew_activation(
            old.activation_id, expected_fence=old.fencing_token, lease_ttl_seconds=30
        )
    with pytest.raises(StaleFenceError):
        await pg_store.claim_next(AGENT, SESSION, old.fencing_token)
    # 新 owner 正常写。
    persisted = await pg_store.append_event(runtime_envelope(), expected_fence=new.fencing_token)
    assert persisted.seq >= 1


async def test_append_with_stale_fence_writes_no_event(pg_store):
    lease = await pg_store.acquire_activation(lease_request("pod-guard"))
    before = await pg_store._events.read(SESSION, 0, 1000)
    with pytest.raises(StaleFenceError):
        await pg_store.append_event(runtime_envelope(), expected_fence=lease.fencing_token + 5)
    after = await pg_store._events.read(SESSION, 0, 1000)
    assert [e.event_id for e in after] == [e.event_id for e in before]


async def test_concurrent_acquire_single_winner(pg_store):
    a = await pg_store.acquire_activation(lease_request("pod-race-a", ttl=30.0))
    with pytest.raises(InvalidCommandError):
        await pg_store.acquire_activation(lease_request("pod-race-b", ttl=30.0))
    same = await pg_store.acquire_activation(lease_request("pod-race-a", ttl=30.0))
    assert same.fencing_token == a.fencing_token


async def test_release_then_reacquire_bumps_token(pg_store):
    first = await pg_store.acquire_activation(lease_request("pod-rel"))
    await pg_store.release_activation(first.activation_id, expected_fence=first.fencing_token)
    with pytest.raises(StaleFenceError):
        await pg_store.renew_activation(
            first.activation_id, expected_fence=first.fencing_token, lease_ttl_seconds=5
        )
    second = await pg_store.acquire_activation(lease_request("pod-rel-2"))
    assert second.fencing_token == first.fencing_token + 1


# ------------------------------------------------------------ conformance


@pytest.mark.parametrize("check", CONFORMANCE_CHECKS)
async def test_postgres_store_conformance(pg_store, check):
    await check(pg_store)


async def test_postgres_store_concurrency(pg_store):
    await assert_concurrent_accepts_have_no_loss_or_duplicate(pg_store)


async def test_postgres_store_control_events_share_session_log(pg_store):
    await pg_store.accept_command(command("k1", "hello"), queue_limit=4)
    lease = await pg_store.acquire_activation(lease_request("act-log"))
    message = await pg_store.claim_next(AGENT, SESSION, lease.fencing_token)
    assert message is not None
    await pg_store.complete_claim(message.message_id, expected_fence=lease.fencing_token)
    events = await pg_store._events.read(SESSION, 0, 50)
    types = [event.event_type for event in events]
    assert "control.command_accepted" in types
    assert "control.message_claimed" in types
    assert "control.message_completed" in types
    assert all(event.family == "control" and event.family_version == 1 for event in events)


async def test_postgres_store_satisfies_port_protocol(pg_store):
    from ksadk.kernel.store import AgentKernelStore

    assert isinstance(pg_store, AgentKernelStore)


# --------------------------------------- Step 5: 进程级并发 / 故障注入


WORKER_SOURCE = r"""
import asyncio, json, os, sys

sys.path.insert(0, os.getcwd())

from ksadk.kernel.contracts import AgentControlCommand, ControlSource
from ksadk.kernel.postgres_store import PostgresAgentKernelStore, PostgresKernelEventLog
from ksadk.kernel.store import command_digest, control_event

DSN = os.environ["KSADK_TEST_POSTGRES_DSN"]


def build_command():
    payload = json.loads(os.environ["WORKER_PAYLOAD"])
    return AgentControlCommand.model_validate(payload)


async def main():
    mode = sys.argv[1]
    async def connect():
        import asyncpg
        return await asyncpg.connect(DSN)

    if mode == "accept-and-hold":
        # accept commit 之后保持连接但不 ack 任何东西；随后被 parent kill。
        conn = await connect()
        store = PostgresAgentKernelStore(conn, PostgresKernelEventLog(conn), owns_pool=False)
        await store.ensure_schema()
        receipt = await store.accept_command(build_command(), queue_limit=8)
        out = {"status": receipt.status, "message_id": str(receipt.message_id)}
        print(json.dumps(out), flush=True)
        await asyncio.sleep(60)

    elif mode == "claim-in-tx":
        # 在事务内完成 inbox claim + ControlEvent insert，但 commit 前被 kill。
        conn = await connect()
        store = PostgresAgentKernelStore(conn, PostgresKernelEventLog(conn), owns_pool=False)
        await store.ensure_schema()
        tr = conn.transaction()
        await tr.start()
        activation = await conn.fetchrow(
            "SELECT activation_id, fencing_token FROM kernel_activations "
            "WHERE agent_instance_id=$1 AND session_id=$2",
            os.environ["WORKER_AGENT"], os.environ["WORKER_SESSION"],
        )
        row = await conn.fetchrow(
            "SELECT message_id, status FROM kernel_inbox "
            "WHERE agent_instance_id=$1 AND session_id=$2 AND status='accepted' "
            "ORDER BY accepted_seq FOR UPDATE SKIP LOCKED LIMIT 1",
            os.environ["WORKER_AGENT"], os.environ["WORKER_SESSION"],
        )
        await conn.execute(
            "UPDATE kernel_inbox SET status='claimed', claimed_fence=$1 WHERE message_id=$2",
            activation["fencing_token"], row["message_id"],
        )
        await PostgresKernelEventLog(conn).append_on(
            conn,
            control_event(
                session_id=os.environ["WORKER_SESSION"],
                event_type="control.message_claimed",
                payload={"message_id": str(row["message_id"]), "in_tx": True},
            ),
            guard=None,
        )
        print(json.dumps({"message_id": str(row["message_id"])}), flush=True)
        await asyncio.sleep(60)

    elif mode == "acquire":
        conn = await connect()
        store = PostgresAgentKernelStore(conn, PostgresKernelEventLog(conn), owns_pool=False)
        await store.ensure_schema()
        from ksadk.kernel.store import ActivationLeaseRequest
        request = ActivationLeaseRequest(
            agent_instance_id=os.environ["WORKER_AGENT"],
            session_id=os.environ["WORKER_SESSION"],
            activation_id=sys.argv[2],
            lease_ttl_seconds=float(sys.argv[3]),
        )
        lease = await store.acquire_activation(request)
        print(json.dumps({"fencing_token": lease.fencing_token}), flush=True)
        await asyncio.sleep(60)

asyncio.run(main())
"""


async def _run_worker(pg_dsn, mode: str, *args: str, extra_env: dict | None = None,
                      kill_after: float = 3.0) -> tuple[str | None, int, str]:
    env = dict(os.environ)
    env["KSADK_TEST_POSTGRES_DSN"] = pg_dsn
    env.update(extra_env or {})
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", WORKER_SOURCE, mode, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    line = None
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=kill_after)
        await asyncio.sleep(0.3)  # 让 worker 进入 sleep/hold 阶段
    except asyncio.TimeoutError:
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    stderr = await proc.stderr.read()
    await proc.wait()
    return (line.decode().strip() if line else None), proc.returncode, stderr.decode()


async def test_two_processes_cannot_both_hold_lease(pg_store, pg_dsn):
    first_line, _, _ = await _run_worker(
        pg_dsn, "acquire", "proc-a", "30", kill_after=10,
        extra_env={"WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION},
    )
    assert first_line is not None
    import json as _json
    first_token = _json.loads(first_line)["fencing_token"]

    # 第二个进程在 lease 有效期内抢不到。
    line, rc, _ = await _run_worker(
        pg_dsn, "acquire", "proc-b", "30", kill_after=5,
        extra_env={"WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION},
    )
    assert line is None  # 没有 stdout => InvalidCommandError 退出

    # 本进程（第三个 owner）在过期后 takeover，token 原子 +1。
    await pg_store._pool.execute(
        "UPDATE kernel_activations SET lease_expires_at = now() - interval '1 second'"
    )
    takeover = await pg_store.acquire_activation(lease_request("proc-c"))
    assert takeover.fencing_token == first_token + 1
    with pytest.raises(StaleFenceError):
        await pg_store.claim_next(AGENT, SESSION, first_token)


async def test_accept_commit_then_disconnect_retry_is_duplicate(pg_store, pg_dsn):
    cmd = command("k-proc", "retry-me")
    line, _, stderr = await _run_worker(
        pg_dsn, "accept-and-hold", kill_after=10,
        extra_env={"WORKER_PAYLOAD": cmd.model_dump_json()},
    )
    import json as _json
    payload = _json.loads(line)
    assert payload["status"] == "accepted"
    # 断链（kill）后由本进程重试同一 command：必须 duplicate 而非二次入队。
    retry = await pg_store.accept_command(cmd, queue_limit=8)
    assert retry.status == "duplicate"
    assert str(retry.message_id) == payload["message_id"]


async def test_kill_writer_before_commit_leaves_no_half_state(pg_store, pg_dsn):
    cmd = command("k-half", "half-state")
    await pg_store.accept_command(cmd, queue_limit=8)
    lease = await pg_store.acquire_activation(lease_request("pod-half"))
    line, _, stderr = await _run_worker(
        pg_dsn, "claim-in-tx", kill_after=10,
        extra_env={"WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION},
    )
    assert line is not None, f"worker failed: {stderr}"  # 事务内写入完成但未 commit 即被 kill
    # 无半状态：claim 未提交、ControlEvent 也不存在。
    row = await pg_store._pool.fetchrow(
        "SELECT status FROM kernel_inbox WHERE idempotency_key=$1", cmd.idempotency_key
    )
    assert row["status"] == InboxState.ACCEPTED.value
    events = await pg_store._events.read(SESSION, 0, 200)
    assert all(
        not (event.event_type == "control.message_claimed"
             and event.payload.get("in_tx") is True)
        for event in events
    )
    # 存活 owner 仍可正常 claim（FOR UPDATE SKIP LOCKED 释放后无残留锁）。
    message = await pg_store.claim_next(AGENT, SESSION, lease.fencing_token)
    assert message is not None
    assert message.idempotency_key == cmd.idempotency_key
