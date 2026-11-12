# ruff: noqa: F811 -- imported pytest fixtures are intentionally reused as parameters
"""A2A approval and native trigger checks must not transfer graph snapshots."""

import json
import time

import pytest
from types import SimpleNamespace

from ksadk.a2a.runtime_approval import KernelA2AApprovalResolver, approval_id, approval_target
from ksadk.events.canonical import ItemCompleted, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore, runtime_event_envelope
from ksadk.events.content import ContentSnapshot, DataContent, ToolCallContent
from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
from ksadk.events.session_event import envelope_to_session_event
from ksadk.kernel.postgres_store import PostgresFencedSessionEventStore
from ksadk.kernel.worker import AgentKernelWorker
from tests.a2a.test_invocation_service_r85 import request
from tests.interaction.test_ledger_conformance import (
    TENANT,
    AGENT,
    SESSION,
    acquire_guard,
    make_record,
    make_submission,
)
from tests.kernel.test_postgres_store import pg_dsn, pg_store  # noqa: F401
from tests.kernel.test_postgres_store import kernel_postgres_server_dsn  # noqa: F401


async def seed_values(store, *, count=40, size=256 * 1024):
    packed = []
    for seq in range(1, count + 1):
        event = ItemCompleted(
            schema_version=2,
            event_id=f"large-{seq}",
            seq=0,
            timestamp=time.time(),
            run_id="run-1",
            scope_id="graph",
            item_id=f"value-{seq}",
            item_kind="data",
            source=SourceRef(framework="langgraph", metadata={"channel": "values"}),
            snapshot=ContentSnapshot(
                parts=(DataContent(part_id="state", data={"state": "x" * size}),)
            ),
        )
        row = envelope_to_session_event(runtime_event_envelope(SESSION, event))
        row.bind_seq_id(seq)
        packed.append(
            (
                "default",
                row.id,
                SESSION,
                row.author,
                row.event_type,
                json.dumps(row.content),
                row.timestamp,
                seq,
                row.invocation_id,
                json.dumps(row.metadata),
            )
        )
    await store._pool.executemany(
        "INSERT INTO ksadk_events (namespace,id,session_id,author,event_type,content_json,timestamp,seq_id,invocation_id,metadata_json,state_delta_json) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10::jsonb,'{}'::jsonb)",
        packed,
    )


async def resolved_approval(store):
    req = request()
    req = req.model_copy(
        update={"handle": req.handle.model_copy(update={"session_id": SESSION, "run_id": "run-1"})}
    )
    guard = await acquire_guard(store)
    record = make_record(approval_id(req)).model_copy(
        update={"provider_id": "managed-a2a", "native_target": approval_target(req)}
    )
    await store.request(record, guard=guard)
    await store.resolve(make_submission(record.interaction_id), guard=guard)
    record = await store.get(
        record.interaction_id,
        tenant_id=TENANT,
        agent_instance_id=AGENT,
        session_id=SESSION,
        run_id="run-1",
    )
    return req, record, guard


async def no_history(*args, **kwargs):
    raise AssertionError("A2A control lookup scanned the full session log")


async def test_approval_reads_only_exact_committed_resolution(pg_store, monkeypatch):
    await seed_values(pg_store)
    req, record, guard = await resolved_approval(pg_store)
    events = PostgresFencedSessionEventStore(pg_store)
    monkeypatch.setattr(events, "read", no_history)
    resolver = KernelA2AApprovalResolver(
        pg_store, events, tenant_id=TENANT, agent_instance_id=AGENT
    )
    assert await resolver(req) == "approved"
    # Scope mismatch cannot reuse a valid receipt, even with the same public ID.
    assert (
        await KernelA2AApprovalResolver(
            pg_store, events, tenant_id="other", agent_instance_id=AGENT
        )(req)
        is None
    )


async def test_recovered_waiter_uses_exact_resolution_without_replay(pg_store, monkeypatch):
    await seed_values(pg_store)
    req, record, guard = await resolved_approval(pg_store)
    events = PostgresFencedSessionEventStore(pg_store)
    monkeypatch.setattr(events, "read", no_history)
    delivered = []
    worker = AgentKernelWorker(pg_store, adapter_factory=lambda: None, session_events=events)
    await worker._deliver_committed_managed_interaction(
        record, SimpleNamespace(deliver=lambda receipt, response=None: delivered.append(receipt))
    )
    assert len(delivered) == 1 and delivered[0].outcome == "approved"


async def test_native_trigger_queries_tool_facts_without_graph_values(pg_store, monkeypatch):
    from ksadk.a2a.langgraph_tool_runtime import ManagedLangGraphTools

    await seed_values(pg_store)
    guard = await acquire_guard(pg_store)
    events = PostgresFencedSessionEventStore(pg_store)
    authority = RunEventAuthority(events, session_id=SESSION, run_id="run-1", guard=guard)
    event = ItemCompleted(
        schema_version=2,
        event_id="native-tool",
        seq=0,
        timestamp=time.time(),
        run_id="run-1",
        scope_id="model",
        item_id="call-item",
        item_kind="tool_call",
        source=SourceRef(framework="langgraph", metadata={"namespace": []}),
        snapshot=ContentSnapshot(
            parts=(
                ToolCallContent(
                    part_id="call", call_id="native-call", name="call_a2a_agent", arguments={}
                ),
            )
        ),
    )
    spec = request().spec.model_copy(update={"local_name": "call_a2a_agent"})
    with bind_run_event_authority(authority):
        await authority.persist_one(SESSION, event)
        # The real storage query must receive a pushdown filter before I/O.
        original = events.read_runtime

        async def bounded(*args, **kwargs):
            assert kwargs.get("item_kinds") == ["tool_call"], "trigger fetched opaque state"
            return await original(*args, **kwargs)

        monkeypatch.setattr(events, "read_runtime", bounded)
        monkeypatch.setattr(
            "ksadk.a2a.langgraph_tool_runtime.native_tool_call_id", lambda: "native-call"
        )
        runtime = ManagedLangGraphTools(bootstrap=None, authority=authority, plan=None)
        trigger = await runtime.tool_trigger(
            spec, {"metadata": {"langgraph_checkpoint_ns": "tools:task"}}
        )
        assert trigger.call_id == "native-call" and trigger.scope_id == "model"


async def test_terminal_recovery_probes_control_transition_only(pg_store, monkeypatch):
    from datetime import datetime, timezone
    from uuid import uuid4
    from ksadk.kernel.contracts import SessionEventEnvelope

    await seed_values(pg_store)
    guard = await acquire_guard(pg_store)
    events = PostgresFencedSessionEventStore(pg_store)
    monkeypatch.setattr(events, "read", no_history)
    assert not await events.has_run_transition(SESSION, "run-1", "interrupted")
    await events.append(
        SessionEventEnvelope(
            event_id=uuid4(),
            session_id=SESSION,
            seq=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            family="control",
            family_version=1,
            event_type="control.run_transition",
            run_id="run-1",
            payload={"state": "interrupted"},
        ),
        guard=guard,
    )
    assert await events.has_run_transition(SESSION, "run-1", "interrupted")
    assert not await events.has_run_transition("foreign", "run-1", "interrupted")
    assert not await events.has_run_transition(SESSION, "other-run", "interrupted")


async def test_scope_and_kind_filters_do_not_change_full_replay(pg_store):
    await seed_values(pg_store)
    guard = await acquire_guard(pg_store)
    events = PostgresFencedSessionEventStore(pg_store)
    authority = RunEventAuthority(events, session_id=SESSION, run_id="run-1", guard=guard)
    with bind_run_event_authority(authority):
        event = ItemCompleted(
            schema_version=2,
            event_id="scoped-tool",
            seq=0,
            timestamp=time.time(),
            run_id="run-1",
            scope_id="model",
            item_id="scoped-call",
            item_kind="tool_call",
            source=SourceRef(framework="langgraph", metadata={"namespace": []}),
            snapshot=ContentSnapshot(
                parts=(
                    ToolCallContent(
                        part_id="call", call_id="scope-call", name="call_a2a_agent", arguments={}
                    ),
                )
            ),
        )
        await authority.persist_one(SESSION, event)
        projected = await authority.list(SESSION, scope_id="model", item_kinds=["tool_call"])
        assert [e.event_id for e in projected] == ["scoped-tool"]
        assert not await authority.list(SESSION, scope_id="foreign")
        tail = await authority.latest(SESSION, scope_id="model", item_kinds=["tool_call"], limit=1)
        assert [e.event_id for e in tail] == ["scoped-tool"]
        # A full replay still includes all forty opaque graph states.
        assert len(await authority.list(SESSION)) == 41
        assert not await authority.list(SESSION, after_seq=tail[0].seq, scope_id="model")


async def test_replay_total_deadline_is_shared_across_pages(pg_store, monkeypatch):
    await seed_values(pg_store, count=600, size=32)
    events = PostgresFencedSessionEventStore(pg_store)
    original = events.read_runtime
    calls = 0

    async def delayed_page(*args, **kwargs):
        nonlocal calls
        calls += 1
        await pg_store._pool.execute("SELECT pg_sleep(0.025)")
        return await original(*args, **kwargs)

    monkeypatch.setattr(events, "read_runtime", delayed_page)
    monkeypatch.setenv("KSADK_DB_REPLAY_OPERATION_SECONDS", "0.05")
    with pytest.raises(TimeoutError):
        await RuntimeEventStore(events).list(SESSION, run_id="run-1")
    assert calls <= 2
