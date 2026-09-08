"""冷恢复:进程退出后开放 run/item 的确定性结局与双恢复者幂等。"""

from __future__ import annotations

import pytest

from ksadk.events.canonical import (
    ALL_EVENT_TYPES,
    ContinuationCreated,
    ItemStarted,
    RunStarted,
    SourceRef,
)
from ksadk.events.canonical_replay import replay_projection
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.cold_recovery import recover_session, scan_open_runs, settle_finding
from ksadk.sessions.in_memory import InMemorySessionService


def _src() -> SourceRef:
    return SourceRef(framework="ksadk")


async def _mk_store_with_open_run(session_id: str = "session-1") -> RuntimeEventStore:
    service = InMemorySessionService()
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id=session_id)
    store = RuntimeEventStore(service)
    base = dict(
        schema_version=2,
        timestamp=1780000000.0,
        run_id="run-1",
        scope_id="scope_root",
        source=_src(),
    )
    await store.append_one(
        session_id,
        RunStarted(event_id="evt_start", seq=0, status="running", **base),
    )
    await store.append_one(
        session_id,
        ItemStarted(
            event_id="evt_item",
            seq=1,
            item_id="item-1",
            item_kind="tool_call",
            **base,
        ),
    )
    return store


@pytest.mark.asyncio
async def test_scan_detects_open_run_with_open_item() -> None:
    store = await _mk_store_with_open_run()

    findings = await scan_open_runs(store, "session-1")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.run_id == "run-1"
    assert finding.resumable is False
    assert [(i.item_id, i.item_kind) for i in finding.open_items] == [("item-1", "tool_call")]


@pytest.mark.asyncio
async def test_recover_session_settles_orphaned_run() -> None:
    store = await _mk_store_with_open_run()

    report = await recover_session(store, "session-1", timestamp=1780000100.0)

    assert report.interrupted_run_ids == ["run-1"]
    assert report.resumed_run_ids == []
    kinds = [type(event).__name__ for event in report.written_events]
    assert kinds == ["ItemFailed", "RunInterrupted"]

    projection = await replay_projection(store, "session-1", run_id="run-1")
    assert projection.status == "interrupted"
    assert all(item.status != "open" for item in projection.items)


@pytest.mark.asyncio
async def test_resumable_run_with_allowance_hands_to_resume_path() -> None:
    store = await _mk_store_with_open_run()
    base = dict(
        schema_version=2,
        timestamp=1780000001.0,
        run_id="run-1",
        scope_id="scope_root",
        source=_src(),
    )
    await store.append_one(
        "session-1",
        ContinuationCreated(
            event_id="evt_cont",
            seq=2,
            continuation_id="cont-1",
            continuation_kind="graph_checkpoint",
            resumable=True,
            ref={"checkpoint": "ck"},
            **base,
        ),
    )

    report = await recover_session(
        store, "session-1", allow_resume_for=lambda _run: True, timestamp=1780000100.0
    )

    assert report.resumed_run_ids == ["run-1"]
    assert report.written_events == []


@pytest.mark.asyncio
async def test_resumable_run_without_allowance_is_interrupted() -> None:
    """continuation 可恢复但执行层裁决拒绝接管(旧 attempt 存活证据不明)→ 合成结局。"""

    store = await _mk_store_with_open_run()
    base = dict(
        schema_version=2,
        timestamp=1780000001.0,
        run_id="run-1",
        scope_id="scope_root",
        source=_src(),
    )
    await store.append_one(
        "session-1",
        ContinuationCreated(
            event_id="evt_cont",
            seq=2,
            continuation_id="cont-1",
            continuation_kind="graph_checkpoint",
            resumable=True,
            ref={"checkpoint": "ck"},
            **base,
        ),
    )

    report = await recover_session(store, "session-1", timestamp=1780000100.0)

    assert report.interrupted_run_ids == ["run-1"]
    assert isinstance(report.written_events[-1].reason, str)


@pytest.mark.asyncio
async def test_second_recoverer_is_idempotent_not_error() -> None:
    """双恢复者竞争:确定性 event_id 使第二次恢复为重复事实,不产生新事件。"""

    store = await _mk_store_with_open_run()

    first = await recover_session(store, "session-1", timestamp=1780000100.0)
    second = await recover_session(store, "session-1", timestamp=1780000200.0)

    assert first.interrupted_run_ids == ["run-1"]
    # 第二次扫描:run 已 interrupted,不再检出开放 run。
    assert second.interrupted_run_ids == []
    assert second.written_events == []


@pytest.mark.asyncio
async def test_terminal_run_not_touched() -> None:
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="session-2")
    store = RuntimeEventStore(service)
    base = dict(
        schema_version=2,
        timestamp=1780000000.0,
        run_id="run-2",
        scope_id="scope_root",
        source=_src(),
    )
    from ksadk.events.canonical import RunCompleted

    await store.append_one(
        "session-2",
        RunStarted(event_id="evt_s2", seq=0, status="running", **base),
    )
    await store.append_one(
        "session-2",
        RunCompleted(
            event_id="evt_c2",
            seq=1,
            status="completed",
            output_refs=(),
            **base,
        ),
    )

    report = await recover_session(store, "session-2", timestamp=1780000100.0)

    assert report.written_events == []
    assert report.interrupted_run_ids == []


def test_settle_finding_event_ids_are_deterministic() -> None:
    from ksadk.events.cold_recovery import OpenItem, RecoveryFinding

    finding = RecoveryFinding(
        run_id="run-x",
        scope_id="scope_x",
        resumable=False,
        continuation_id=None,
        open_items=[OpenItem(scope_id="scope_x", item_id="i-1", item_kind="message")],
        last_seq=7,
    )

    a = settle_finding(finding, "session-1", allow_resume=False, timestamp=1.0)
    b = settle_finding(finding, "session-1", allow_resume=False, timestamp=2.0)

    assert [e.event_id for e in a] == [e.event_id for e in b]


def test_written_events_are_runtime_events() -> None:
    from ksadk.events.cold_recovery import OpenItem, RecoveryFinding

    finding = RecoveryFinding(
        run_id="run-x",
        scope_id="scope_x",
        resumable=False,
        continuation_id=None,
        open_items=[OpenItem(scope_id="scope_x", item_id="i-1", item_kind="tool_call")],
        last_seq=7,
    )
    events = settle_finding(finding, "session-1", allow_resume=False, timestamp=1.0)
    assert all(event.event_type in ALL_EVENT_TYPES for event in events)


@pytest.mark.asyncio
async def test_replay_projection_settle_open_completes_dangling_stream() -> None:
    """冷读者视角:settle_open=True 时开放流投影出确定性结局,不裸露悬空状态。"""

    store = await _mk_store_with_open_run()

    bare = await replay_projection(store, "session-1", run_id="run-1")
    assert bare.status == "running"  # live 语义不变

    settled = await replay_projection(store, "session-1", run_id="run-1", settle_open=True)
    assert settled.status == "interrupted"
    assert all(item.status != "open" for item in settled.items)


@pytest.mark.asyncio
async def test_owner_attempt_is_never_settled_by_itself() -> None:
    """同 attempt 不自杀:恢复者就是最后 resume 属主时,run 交回正常 resume 路径。"""

    from ksadk.events.canonical import ContinuationResumed

    store = await _mk_store_with_open_run()
    base = dict(
        schema_version=2,
        timestamp=1780000001.0,
        run_id="run-1",
        scope_id="scope_root",
        source=_src(),
    )
    await store.append_one(
        "session-1",
        ContinuationCreated(
            event_id="evt_cont",
            seq=2,
            continuation_id="cont-1",
            continuation_kind="graph_checkpoint",
            resumable=True,
            ref={"checkpoint": "ck"},
            **base,
        ),
    )
    await store.append_one(
        "session-1",
        ContinuationResumed(
            event_id="evt_resume",
            seq=3,
            continuation_id="cont-1",
            continuation_kind="graph_checkpoint",
            resume_attempt_id="attempt-mine",
            **base,
        ),
    )

    report = await recover_session(
        store, "session-1", caller_attempt_id="attempt-mine", timestamp=1780000100.0
    )

    assert report.resumed_run_ids == ["run-1"]
    assert report.written_events == []


@pytest.mark.asyncio
async def test_stale_attempt_is_settled() -> None:
    """旧 attempt 存活证据不明(调用者是新 attempt)→ 合成确定性结局。"""

    from ksadk.events.canonical import ContinuationResumed

    store = await _mk_store_with_open_run()
    base = dict(
        schema_version=2,
        timestamp=1780000001.0,
        run_id="run-1",
        scope_id="scope_root",
        source=_src(),
    )
    await store.append_one(
        "session-1",
        ContinuationCreated(
            event_id="evt_cont",
            seq=2,
            continuation_id="cont-1",
            continuation_kind="graph_checkpoint",
            resumable=False,
            ref={},
            **base,
        ),
    )
    await store.append_one(
        "session-1",
        ContinuationResumed(
            event_id="evt_resume",
            seq=3,
            continuation_id="cont-1",
            continuation_kind="graph_checkpoint",
            resume_attempt_id="attempt-stale",
            **base,
        ),
    )

    report = await recover_session(
        store, "session-1", caller_attempt_id="attempt-new", timestamp=1780000100.0
    )

    assert report.interrupted_run_ids == ["run-1"]


@pytest.mark.asyncio
async def test_racing_recoverers_with_different_timestamps_converge() -> None:
    """真并发竞态:两恢复者各带不同 timestamp,第二个遇同 id 结局被吸收非报错。"""

    from unittest.mock import patch

    store = await _mk_store_with_open_run()

    import ksadk.events.cold_recovery as cr

    # 两个恢复者同时 scan:在第一次恢复前取快照。
    first_scan_result = await cr.scan_open_runs(store, "session-1")
    first = await recover_session(store, "session-1", timestamp=1780000100.0)

    # 模拟第二个恢复者:scan 与第一个同时发生(仍看到开放 run),settle 后
    # persist 时 event_by_id 命中第一个恢复者的同 id 事实(timestamp 不同)。
    async def _fake_scan(_store, _session):
        return first_scan_result

    with patch.object(cr, "scan_open_runs", _fake_scan):
        second = await recover_session(store, "session-1", timestamp=1780000900.0)

    assert first.interrupted_run_ids == ["run-1"]
    assert second.interrupted_run_ids == ["run-1"]
    # 第二个恢复者没有新增事实(全部被吸收),但结算目标已达成。
    assert second.written_events == []


# ------------------------------------------- Task 7: reason 透传与 fenced 收口


def test_settle_finding_reason_is_carried_into_run_interrupted() -> None:
    from ksadk.events.cold_recovery import RecoveryFinding

    finding = RecoveryFinding(
        run_id="run-x",
        scope_id="scope_x",
        resumable=False,
        continuation_id=None,
        open_items=[],
        last_seq=3,
    )
    events = settle_finding(
        finding,
        "session-1",
        allow_resume=False,
        timestamp=1.0,
        reason="runtime_not_durably_attachable",
    )
    assert events[-1].reason == "runtime_not_durably_attachable"
    default = settle_finding(finding, "session-1", allow_resume=False, timestamp=1.0)
    assert default[-1].reason == "process_exit"
    # reason 不改变确定性 event id：同一 finding 的两次结算 id 相同。
    assert [e.event_id for e in events] == [e.event_id for e in default]


@pytest.mark.asyncio
async def test_recovery_coordinator_writes_interrupted_through_fence() -> None:
    """冷 attach 资格不具备时：唯一 run.interrupted + open item close，走 fenced emit。"""

    from ksadk.events.canonical_store import RuntimeEventStore as TypedStore
    from ksadk.events.pipeline import CanonicalEventPipeline
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.memory_store import InMemoryAgentKernelStore
    from ksadk.kernel.store import ActivationLeaseRequest

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="session-9")
    kernel: InMemoryAgentKernelStore | None = None

    async def validator(envelope, guard):
        await kernel.validate_write_fence(envelope, guard)

    generic = SessionServiceEventStore(service, fence_validator=validator)
    kernel = InMemoryAgentKernelStore(generic)
    lease = await kernel.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="ai-1", session_id="session-9", activation_id="act-1"
        )
    )
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )
    typed = TypedStore(generic, session_id="session-9")
    pipeline = CanonicalEventPipeline(typed, session_id="session-9")
    base = dict(
        schema_version=2,
        timestamp=1780000000.0,
        run_id="run-9",
        scope_id="scope_root",
        source=_src(),
    )
    await pipeline.emit(
        RunStarted(event_id="evt_s9", seq=0, status="running", **base),
        write_context=guard,
    )
    await pipeline.emit(
        ItemStarted(
            event_id="evt_i9", seq=0, item_id="item-9", item_kind="tool_call", **base
        ),
        write_context=guard,
    )

    # takeover 后旧 guard 写不进（fence 拒绝）。
    await kernel.release_activation(
        lease.activation_id, expected_fence=lease.fencing_token
    )
    new_lease = await kernel.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="ai-1", session_id="session-9", activation_id="act-2"
        )
    )
    from ksadk.kernel.errors import StaleFenceError

    with pytest.raises(StaleFenceError):
        await pipeline.emit(
            ItemStarted(
                event_id="evt_late", seq=0, item_id="item-late", item_kind="message", **base
            ),
            write_context=guard,
        )

    # 新 owner 冷恢复：确定性 interrupted，reason 为 runtime_not_durably_attachable。
    report = await scan_open_runs(typed, "session-9")
    assert len(report) == 1
    events = settle_finding(
        report[0],
        "session-9",
        allow_resume=False,
        timestamp=1780000100.0,
        reason="runtime_not_durably_attachable",
    )
    new_guard = ActivationWriteGuard(
        activation_id=new_lease.activation_id,
        fencing_token=new_lease.fencing_token,
    )
    for event in events:
        await pipeline.emit(event, write_context=new_guard)

    projection = await replay_projection(typed, "session-9", run_id="run-9")
    assert projection.status == "interrupted"
    assert all(item.status != "open" for item in projection.items)
    terminal = [e for e in await typed.list("session-9") if e.event_type == "run.interrupted"]
    assert len(terminal) == 1
    assert terminal[0].reason == "runtime_not_durably_attachable"
