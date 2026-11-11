import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ksadk.interaction.native_events import NativeSubmissionOutcome
from ksadk.mcp_runtime.oauth.kernel import MCPAuthorizationExtension


async def test_ordinary_native_receipt_never_uses_oauth_storage():
    pool = MagicMock()
    extension = MCPAuthorizationExtension(SimpleNamespace(_pool=pool, _assert_fence=None), None)
    event = SimpleNamespace(_outcomes=(NativeSubmissionOutcome("form", "submit", "consumed"),))
    await extension.consumed(None, SimpleNamespace(metadata={}), event, None)
    pool.acquire.assert_not_called()


@pytest.fixture
async def oauth_pool():
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires isolated PostgreSQL")
    import asyncpg

    schema = "oauth_recovery_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        async with asyncpg.create_pool(
            dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
        ) as pool:
            yield pool
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


@pytest.mark.parametrize(
    "broker_revision,confirmed_status,should_resume",
    [(8, "connecting", True), (9, "connecting", False), (8, "cancelled", False),
     (8, "resolve_error", False)],
)
async def test_new_worker_reconciles_only_the_confirmed_ready_revision(
    oauth_pool, monkeypatch, broker_revision, confirmed_status, should_resume
):
    # The previous worker got through the Broker's connecting transition but
    # did not reserve a native resume. Its verified ready notice is still stored.
    ready = {"wait_id": "wait", "run_id": "run", "status": "credential_ready", "revision": 7, "requires_resume": True}
    connecting = {**ready, "status": "connecting", "revision": broker_revision}
    run = SimpleNamespace(
        run_id="run",
        session_id="session",
        agent_instance_id="instance",
        metadata={"mcp_authorization": True, "tenant_id": "tenant"},
    )
    store = SimpleNamespace(
        _pool=oauth_pool, _assert_fence=AsyncMock(), _events=AsyncMock(), find_active_run=AsyncMock(return_value=run)
    )
    async def confirm(wait_id, *, phase, **kwargs):
        status = "resume_unknown" if phase == "resume_unknown" else (
            "connecting" if confirmed_status == "resolve_error" else confirmed_status
        )
        return {**connecting, "status": status}
    client = SimpleNamespace(get=AsyncMock(return_value=connecting), confirm=AsyncMock(side_effect=confirm))
    extension = MCPAuthorizationExtension(store, client)
    await extension.ensure_schema()
    await oauth_pool.execute(
        "INSERT INTO mcp_authorization_waits "
        "(wait_id,instance_id,session_id,run_id,interaction_id,native_target,snapshot) "
        "VALUES ('wait','instance','session','run','interrupt','{}',$1::jsonb)",
        json.dumps(ready),
    )
    await oauth_pool.execute(
        "INSERT INTO mcp_authorization_completions (id,wait_id,command) "
        "VALUES ('wait:7','wait',$1::jsonb)",
        json.dumps(ready),
    )
    monkeypatch.setattr("ksadk.mcp_runtime.oauth.kernel.prepare_connection", AsyncMock())
    execution = MagicMock(stream_task=None)
    execution.interaction_provider.resolve = AsyncMock(
        side_effect=ValueError("native checkpoint changed") if confirmed_status == "resolve_error" else None
    )
    worker = MagicMock(
        _executions={"run": execution},
        _session_events=AsyncMock(),
        _transition_run=AsyncMock(return_value=run),
    )
    activation = SimpleNamespace(agent_instance_id="instance", activation_id="new", fencing_token=2)

    assert await extension.tick(worker, activation, "session") is should_resume
    persisted = await extension.row("wait")
    if should_resume:
        submission = execution.interaction_provider.resolve.await_args.args[-1]
        assert submission.idempotency_key == "wait:7"
        assert submission.response == {"wait_id": "wait", "revision": 7}
        assert (persisted["status"], persisted["fence"]) == ("resolving", 2)
    elif confirmed_status == "resolve_error":
        assert persisted["status"] == "unknown"
        client.confirm.assert_any_await("wait", phase="resume_unknown")
        client.get.return_value = {**connecting, "status": "resume_unknown"}
    else:
        assert persisted["status"] == "pending"
    extension._next_poll.clear()
    assert not await extension.tick(worker, activation, "session")
    assert execution.interaction_provider.resolve.await_count == int(should_resume or confirmed_status == "resolve_error")


@pytest.mark.parametrize("failure", ["before_index", "lost_confirm", "before_event_commit"])
async def test_durable_wait_republishes_once_after_takeover(oauth_pool, monkeypatch, failure):
    from urllib.parse import parse_qsl, urlencode, urlsplit

    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.postgres_store import PostgresAgentKernelStore, PostgresKernelEventLog
    from ksadk.kernel.state import RunState
    from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
    from ksadk.mcp_runtime.oauth.client import BrokerError
    from ksadk.mcp_runtime.oauth.langgraph import MARKER
    from ksadk.runtime.adapter import RunHandle
    from ksadk.sessions.postgres_service import PostgresSessionService

    schema = await oauth_pool.fetchval("SELECT current_schema()")
    url = urlsplit(os.environ["KSADK_TEST_POSTGRES_DSN"])
    query = dict(parse_qsl(url.query), search_path=schema)
    sessions = PostgresSessionService(
        dsn=url._replace(query=urlencode(query)).geturl(), namespace=schema
    )
    try:
        await sessions.create_session(agent_id="instance", user_id="owner", session_id="session")
    finally:
        await sessions.aclose()
    log = PostgresKernelEventLog(oauth_pool, namespace=schema)
    store = PostgresAgentKernelStore(oauth_pool, log)
    await store.ensure_schema()

    async def acquire(owner):
        return await store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id="instance",
                session_id="session",
                activation_id=owner,
                lease_ttl_seconds=60,
            )
        )

    lease = await acquire("first")
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )
    run = RunRecord(
        run_id="run", agent_instance_id="instance", session_id="session", state=RunState.PENDING
    )
    await store.save_run_transition(run, expected_fence=guard.fencing_token)
    run = await store.save_run_transition(
        run.model_copy(update={"state": RunState.RUNNING}), expected_fence=guard.fencing_token
    )
    native = dict(
        thread_id="session", checkpoint_id="checkpoint", task_id="task", call_id="interrupt"
    )
    handle = RunHandle(
        run_id="run",
        session_id="session",
        runtime_type="langgraph",
        native_ref={"native_waits": {"interaction": native}},
    )
    event = SimpleNamespace(
        interaction_id="interaction",
        request=SimpleNamespace(schema_={MARKER: "wait"}),
        _native_target=native,
    )
    initial = dict(
        run_id="run", wait_id="wait", revision=1, status="preparing", requires_resume=False
    )
    confirmed = dict(initial, revision=2, status="awaiting_authorization", requires_resume=True)
    remote = SimpleNamespace(
        get=AsyncMock(return_value=initial), confirm=AsyncMock(return_value=confirmed)
    )
    extension = MCPAuthorizationExtension(store, remote)
    await extension.ensure_schema()
    worker = SimpleNamespace(_store=store, _executions={})
    append = log.append_on

    async def fail_before_event_commit(conn, envelope, guard):
        value = await append(conn, envelope, guard)
        if envelope.event_type == "mcp.authorization.requested":
            raise ConnectionError("process lost before commit")
        return value

    with monkeypatch.context() as patch:
        if failure == "before_index":
            remote.get.side_effect = BrokerError("MCP_CONTROL_UNAVAILABLE")
        elif failure == "lost_confirm":
            remote.confirm.side_effect = BrokerError("MCP_CONTROL_TIMEOUT")
        else:
            patch.setattr(log, "append_on", fail_before_event_commit)
        if failure == "before_event_commit":
            with pytest.raises(ConnectionError):
                await extension.record(worker, SimpleNamespace(handle=handle), run, event, guard)
        else:
            await extension.record(worker, SimpleNamespace(handle=handle), run, event, guard)
    saved = await store.load_run("run")
    assert saved.state is RunState.WAITING
    assert saved.metadata["handle"]["native_ref"]["native_waits"]["interaction"] == native
    assert saved.metadata["mcp_authorization"] == {"interaction": "wait"}
    row = await extension.row("wait")
    assert row is None if failure == "before_index" else row["snapshot"] == {}
    remote.get.side_effect = remote.confirm.side_effect = None
    remote.get.return_value = initial if failure == "before_index" else confirmed
    await store.release_activation(lease.activation_id, expected_fence=lease.fencing_token)
    next_lease = await acquire("replacement")
    assert next_lease.fencing_token > guard.fencing_token
    replacement = MCPAuthorizationExtension(store, remote)
    for _ in range(2):
        replacement._next_poll.clear()
        await replacement.tick(worker, next_lease, "session")
        remote.get.return_value = confirmed
    assert (await replacement.row("wait"))["snapshot"] == confirmed
    assert (await store.load_run("run")).state is RunState.WAITING
    events = await log.read("session", 0, 100)
    assert [
        item.event_type for item in events if item.event_type.startswith("mcp.authorization")
    ] == ["mcp.authorization.requested"]
    for call in remote.confirm.await_args_list:
        assert call.args == (
            "wait",
            {"thread_id": "session", "checkpoint_id": "checkpoint", "interrupt_id": "interrupt"},
        )

    if failure == "before_index":
        # A recovered record and a poll can observe different revisions before
        # either has written the first card. Only the first committed fact opens it.
        import asyncio

        await oauth_pool.execute(
            "INSERT INTO mcp_authorization_waits "
            "(wait_id,instance_id,session_id,run_id,interaction_id,native_target,snapshot) "
            "VALUES ('parallel','instance','session','run','second','{}','{}')"
        )
        new_guard = ActivationWriteGuard(
            activation_id=next_lease.activation_id, fencing_token=next_lease.fencing_token
        )
        first = dict(confirmed, wait_id="parallel")
        second = dict(first, revision=3, status="credential_ready")
        await asyncio.gather(
            *(replacement.publish(worker, saved, value, new_guard) for value in (first, second))
        )
        facts = [
            e for e in await log.read("session", 0, 100) if e.payload.get("wait_id") == "parallel"
        ]
        assert sum(e.event_type == "mcp.authorization.requested" for e in facts) == 1
        assert [e.payload["revision"] for e in facts] in ([2, 3], [3])
        assert (await replacement.row("parallel"))["snapshot"] == second
