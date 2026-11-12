# -*- coding: utf-8 -*-
"""per-session FIFO worker：顺序、active-run 排队、异常分类、并发（Task 6 Step 1）。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.kernel.contracts import ControlSource
from ksadk.kernel.errors import (
    AgentKernelError,
    InvalidCommandError,
    StaleFenceError,
)
from ksadk.kernel.state import InboxState, RunState
from ksadk.kernel.store import RunRecord
from ksadk.runtime.adapter import RunHandle
from tests.kernel.control_harness import (
    AGENT,
    FakeAdapter,
    command,
    default_matrix,
    kernel_stack,
    native,
)


def matrix_with(**supported) -> object:
    update = {name: native() for name in supported}
    return default_matrix().model_copy(update=update)


async def drain(stack, lease, session_id: str = "s1", limit: int = 50):
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    results = []
    for _ in range(limit):
        result = await worker.run_once(AGENT, lease)
        results.append(result)
        if result.outcome == "idle":
            break
    return results, worker


async def test_fifo_order_is_stable_within_session():
    stack = await kernel_stack()
    lease = await stack.lease()
    for index, text in enumerate(["first", "second", "third"]):
        await stack.kernel.submit(
            command(idempotency_key=f"f{index}", content=text),
            permit=stack.permit("enqueue"),
        )
    results, _ = await drain(stack, lease)
    assert [r.outcome for r in results] == ["completed", "completed", "completed", "idle"]
    starts = [c for c in stack.adapter.calls if c[0] == "start"]
    assert len(starts) == 3
    for message in await stack.store.list_messages(AGENT, "s1"):
        assert message.status == InboxState.COMPLETED


async def test_worker_preserves_admitted_command_identity_on_run_transition():
    """A scheduler reconciles by identity, never by prompt text or timestamp."""

    stack = await kernel_stack()
    lease = await stack.lease()
    submitted = command(idempotency_key="schedule-occurrence", content="write report").model_copy(
        update={
            "source": ControlSource(kind="scheduler", ref="occ_12345678"),
            "correlation_id": "occ_12345678",
        }
    )
    await stack.kernel.submit(submitted, permit=stack.permit("enqueue"))

    from ksadk.kernel.worker import AgentKernelWorker

    result = await AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter).run_once(
        AGENT, lease
    )

    assert result.run_id is not None
    run = await stack.store.load_run(result.run_id)
    assert run is not None
    assert run.metadata["command_id"] == str(submitted.command_id)
    assert run.metadata["source_kind"] == "scheduler"
    transitions = [
        event
        for event in await stack.events.read("s1", 0, 30)
        if event.event_type == "control.run_transition" and event.run_id == result.run_id
    ]
    assert transitions
    assert {event.causation_id for event in transitions} == {str(submitted.command_id)}


@pytest.mark.parametrize("runtime_type", ["adk", "fake"])
async def test_worker_persists_adk_start_before_stream_without_public_disclosure(runtime_type):
    from ksadk.kernel.worker import AgentKernelWorker

    observed = []

    class InspectAdapter(FakeAdapter):
        async def stream(self, handle):
            request = self.start_requests[-1]
            run = await stack.store.load_run(request.metadata["run_id"])
            assert run.state is RunState.RUNNING
            observed.append(run.metadata.get("start_request"))
            if False:
                yield

    adapter = InspectAdapter()
    adapter.runtime.runtime_type = runtime_type
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="private-start", content="private-original-input"),
        permit=stack.permit("enqueue"),
    )
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: adapter,
        session_events=stack.events,
        start_request_defaults={"model": "deployed-model", "config": {"private": "setting"}},
    )
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    expected = adapter.start_requests[-1].model_dump(mode="json")
    assert observed == [expected if runtime_type == "adk" else None]
    transitions = [
        event
        for event in await stack.events.read("s1", 0, 30)
        if event.event_type == "control.run_transition"
    ]
    assert transitions
    for event in transitions:
        assert "start_request" not in event.payload
        assert "private-original-input" not in str(event.payload)
        assert "setting" not in str(event.payload)


async def test_worker_binds_managed_session_service_during_runtime_stream():
    from ksadk.kernel.worker import AgentKernelWorker
    from ksadk.sessions import resolve_session_service

    observed: list[bool] = []

    class InspectAdapter(FakeAdapter):
        async def stream(self, handle):
            observed.append(resolve_session_service() is stack.session_service)
            if False:
                yield

    adapter = InspectAdapter()
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="bound-session-service", content="hello"),
        permit=stack.permit("enqueue"),
    )
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: adapter,
        session_events=stack.events,
        session_service=stack.session_service,
    )

    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    assert observed == [True]


async def test_worker_binds_managed_session_service_during_runtime_start():
    from ksadk.kernel.worker import AgentKernelWorker
    from ksadk.sessions import resolve_session_service

    observed: list[bool] = []

    class InspectAdapter(FakeAdapter):
        async def start(self, request):
            observed.append(resolve_session_service() is stack.session_service)
            return await super().start(request)

    adapter = InspectAdapter()
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="bound-start-session-service", content="hello"),
        permit=stack.permit("enqueue"),
    )
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: adapter,
        session_events=stack.events,
        session_service=stack.session_service,
    )

    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    assert observed == [True]


async def test_worker_projects_verified_business_identity_into_runtime_start():
    from ksadk.kernel.worker import AgentKernelWorker
    from ksadk.runtime_context import TRUSTED_IDENTITY_METADATA_KEY
    from ksadk.sessions.in_memory import InMemorySessionService
    from ksadk.sessions.invocation_identity import identity_native_user_id

    stack = await kernel_stack()
    lease = await stack.lease()
    identity = {
        "identity_namespace": "customer-crm",
        "tenant_id": "enterprise-a",
        "subject_type": "user",
        "subject_id": "user-7",
    }
    await stack.kernel.submit(
        command(
            idempotency_key="verified-identity",
            payload={"content": "hello", "invocation_identity": identity},
        ),
        permit=stack.permit("enqueue"),
    )
    session_service = InMemorySessionService()
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_service=session_service,
        start_request_defaults={"agent_id": AGENT},
    )

    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    request = stack.adapter.start_requests[-1]
    assert request.user_id == identity_native_user_id(identity)
    assert request.metadata[TRUSTED_IDENTITY_METADATA_KEY] == identity
    session = await session_service.get_session("s1")
    assert session is not None
    assert session.user_id == request.user_id


@pytest.mark.parametrize(
    ("requested_model", "approval_mode", "expected_model", "expected_approval"),
    [
        ("qwen3-coder-plus", "ask", "qwen3-coder-plus", "manual"),
        ("not-deployed", "risk", "default-model", "auto_review"),
        ("qwen3-coder-plus", "full", "qwen3-coder-plus", "deny_all"),
    ],
)
async def test_turn_model_and_approval_are_bounded_by_deployment_defaults(
    requested_model, approval_mode, expected_model, expected_approval
):
    stack = await kernel_stack()
    lease = await stack.lease()
    await stack.kernel.submit(
        command(
            idempotency_key=f"runtime-options-{approval_mode}",
            payload={
                "content": [{"type": "input_text", "text": "hello"}],
                "runtime_options": {
                    "model": requested_model,
                    "tool_approval_mode": approval_mode,
                    # An untrusted turn must never be able to replace the
                    # deployment-owned sandbox.
                    "sandbox": "full-access",
                },
            },
        ),
        permit=stack.permit("enqueue"),
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        start_request_defaults={
            "model": "default-model",
            "allowed_models": ["default-model", "qwen3-coder-plus"],
            "config": {
                "sandbox": "workspace-write",
                "sandbox_read_only": False,
                "approval_mode": "deny_all",
            },
        },
    )

    result = await worker.run_once(AGENT, lease)

    assert result.outcome == "completed"
    request = stack.adapter.start_requests[-1]
    assert request.model == expected_model
    assert request.config["approval_mode"] == expected_approval
    assert request.metadata["tool_approval_mode"] == approval_mode
    assert "sandbox" not in request.metadata
    assert request.config["sandbox"] == "workspace-write"
    assert request.config["sandbox_read_only"] is False


async def test_enqueue_stays_queued_while_run_is_active():
    stack = await kernel_stack()
    lease = await stack.lease()
    # 直接在 store 里放一个 active run。
    run = await stack.store.save_run_transition(
        RunRecord(
            run_id="run-active",
            agent_instance_id=AGENT,
            session_id="s1",
            state=RunState.PENDING,
        ),
        expected_fence=lease.fencing_token,
    )
    run = await stack.store.save_run_transition(
        run.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=lease.fencing_token,
    )
    await stack.kernel.submit(command(idempotency_key="wait-1"), permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "idle"
    pending = await stack.store.list_messages(AGENT, "s1")
    assert [m.idempotency_key for m in pending] == ["wait-1"]
    assert pending[0].status == InboxState.ACCEPTED
    # run 终结后 enqueue 才被执行。
    await stack.store.save_run_transition(
        run.model_copy(update={"state": RunState.COMPLETED}),
        expected_fence=lease.fencing_token,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert stack.adapter.calls.count(("start", "s1")) == 1


async def test_control_verb_acts_on_active_run_with_live_handle():
    stack = await kernel_stack(adapter=FakeAdapter(matrix=matrix_with(cancel=native())))
    lease = await stack.lease()
    seeded = await stack.store.save_run_transition(
        RunRecord(
            run_id="run-x",
            agent_instance_id=AGENT,
            session_id="s1",
            state=RunState.PENDING,
        ),
        expected_fence=lease.fencing_token,
    )
    await stack.store.save_run_transition(
        seeded.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=lease.fencing_token,
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    worker.attach_handle("run-x", RunHandle(run_id="run-x", session_id="s1", runtime_type="fake"))
    await stack.kernel.submit(
        command("interrupt", idempotency_key="stop-1"), permit=stack.permit("interrupt")
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert ("cancel", "s1") in stack.adapter.calls
    run = await stack.store.load_run("run-x")
    assert run.state == RunState.CANCELLED


async def test_control_verb_without_active_run_is_typed_rejection():
    stack = await kernel_stack(adapter=FakeAdapter(matrix=matrix_with(cancel=native())))
    lease = await stack.lease()
    await stack.kernel.submit(
        command("interrupt", idempotency_key="stop-2"), permit=stack.permit("interrupt")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"  # typed rejection 是确定性收口
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.DISCARDED
    events = await stack.events.read("s1", 0, 20)
    assert any(e.event_type == "control.command_rejected" for e in events)


async def test_retryable_failure_keeps_claim_open():
    stack = await kernel_stack()
    lease = await stack.lease()
    stack.adapter.start_error = AgentKernelError(
        "persistence_uncertain", "flush failed", retryable=True
    )
    await stack.kernel.submit(command(idempotency_key="retry-1"), permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "retryable_failure"
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.CLAIMED
    # 恢复后可以重试成功（同 owner 对自己的 claimed 消息可重入）。
    stack.adapter.start_error = None
    recovered = await worker.run_once(AGENT, lease)
    assert recovered.outcome == "completed"
    assert recovered.message_id == message.message_id


async def test_unknown_exception_is_terminal_and_never_acked():
    stack = await kernel_stack()
    lease = await stack.lease()
    stack.adapter.start_error = RuntimeError("boom")
    await stack.kernel.submit(command(idempotency_key="bad-1"), permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "terminal_failure"
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.CLAIMED  # 未被 ack 为成功


async def test_distinct_sessions_overlap_and_keep_per_session_order():
    stack = await kernel_stack()
    stack.adapter.start_delay = 0.1
    lease1 = await stack.lease("s1", "act-s1")
    lease2 = await stack.lease("s2", "act-s2")
    for index in range(2):
        await stack.kernel.submit(
            command(idempotency_key=f"s1-{index}", session_id="s1", content=f"a{index}"),
            permit=stack.permit("enqueue", session_id="s1"),
        )
        await stack.kernel.submit(
            command(idempotency_key=f"s2-{index}", session_id="s2", content=f"b{index}"),
            permit=stack.permit("enqueue", session_id="s2"),
        )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)

    async def drive(session_lease):
        completed = 0
        while completed < 2:
            result = await worker.run_once(AGENT, session_lease)
            if result.outcome in ("completed", "claimed"):
                completed += 1
            elif result.outcome != "idle":
                raise AssertionError(result)
        return completed

    await asyncio.gather(drive(lease1), drive(lease2))

    intervals = sorted(stack.adapter.start_intervals, key=lambda item: item[1])
    assert len(intervals) == 4
    per_session = {}
    for session, entered, exited in intervals:
        per_session.setdefault(session, []).append((entered, exited))
    for session, spans in per_session.items():
        # 同 session 顺序稳定：无重叠且按时间递增。
        assert spans[0][1] <= spans[1][0], session
    # 不同 session 至少两个 adapter start 调用重叠。
    s1 = per_session["s1"][0]
    s2 = per_session["s2"][0]
    assert s1[0] < s2[1] and s2[0] < s1[1]


async def test_same_session_reentrant_ticks_are_serialized():
    """A single owner cannot claim later FIFO rows through concurrent ticks."""

    stack = await kernel_stack()
    stack.adapter.start_delay = 0.05
    lease = await stack.lease("s1", "shared-pod-uid")
    for index in range(5):
        await stack.kernel.submit(
            command(idempotency_key=f"same-{index}", session_id="s1", content=str(index)),
            permit=stack.permit("enqueue", session_id="s1"),
        )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)

    async def tick():
        while True:
            result = await worker.run_once(AGENT, lease, session_id="s1")
            if result.outcome == "completed":
                return
            if result.outcome != "idle":
                raise AssertionError(result)
            await asyncio.sleep(0)

    await asyncio.gather(*(tick() for _ in range(5)))
    messages = await stack.store.list_messages(AGENT, "s1")
    assert [message.accepted_seq for message in messages] == [1, 2, 3, 4, 5]
    assert all(message.status == InboxState.COMPLETED for message in messages)
    assert [call[1] for call in stack.adapter.start_intervals] == sorted(
        call[1] for call in stack.adapter.start_intervals
    )


async def test_worker_requires_lease_to_claim():
    stack = await kernel_stack()
    await stack.kernel.submit(command(idempotency_key="lease-1"), permit=stack.permit("enqueue"))
    stale = await stack.lease("s1", "act-stale")
    from ksadk.kernel.contracts import ActivationLease
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    forged = ActivationLease(
        agent_instance_id=AGENT,
        activation_id="act-stale",
        fencing_token=stale.fencing_token + 99,
        lease_expires_at=stale.lease_expires_at,
        bundle_digest=stale.bundle_digest,
        runtime_type=stale.runtime_type,
        capability_digest=stale.capability_digest,
    )
    with pytest.raises(StaleFenceError):
        await worker.run_once(AGENT, forged)


# ------------------------------------------------ Task 7 review P0: stream 消费


def _stream_events(run_id: str):
    from ksadk.events.canonical import RunProgress, RunStarted

    return [
        RunStarted(
            schema_version=2,
            event_id=f"{run_id}-started",
            seq=0,
            timestamp=1780000000.0,
            run_id=run_id,
            scope_id=f"run:{run_id}",
            status="running",
            source=_fake_source(),
        ),
        RunProgress(
            schema_version=2,
            event_id=f"{run_id}-progress",
            seq=0,
            timestamp=1780000000.5,
            run_id=run_id,
            scope_id=f"run:{run_id}",
            status="running",
            progress=0.5,
            source=_fake_source(),
        ),
    ]


def _fake_source():
    from ksadk.events.canonical import SourceRef

    return SourceRef(framework="ksadk")


async def test_child_writer_uses_kernel_log_and_run_scoped_authority():
    from ksadk.events.run_authority import current_run_event_authority
    from ksadk.kernel.worker import AgentKernelWorker

    retained = []

    class ChildWritingAdapter(FakeAdapter):
        async def stream(self, handle):
            authority = current_run_event_authority()
            assert authority is not None
            assert authority.run_id != handle.run_id
            retained.append(authority)
            event = _stream_events(authority.run_id)[0]
            stored, created = await authority.persist_one(handle.session_id, event)
            assert created
            duplicate, created = await authority.persist_one(handle.session_id, event)
            assert not created and duplicate == stored
            with pytest.raises(ValueError, match="identity mismatch"):
                await authority.persist_one("another-session", event)
            with pytest.raises(ValueError, match="identity mismatch"):
                await authority.persist_one(handle.session_id, event.model_copy(
                    update={"run_id": "another-run"}
                ))
            # The normal stream owner sees the already-stored fact again.
            yield stored

    stack = await kernel_stack(adapter=ChildWritingAdapter())
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="child-writer"), permit=stack.permit("enqueue")
    )
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: stack.adapter, session_events=stack.events
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    events = await stack.events.read("s1", 0, 100)
    assert sum(event.event_type == "run.started" for event in events) == 1
    assert current_run_event_authority() is None
    with pytest.raises(RuntimeError, match="no longer active"):
        await retained[0].list("s1")


async def test_managed_approval_records_private_binding_in_kernel_ledger():
    from ksadk.a2a.runtime_approval import approval_event, approval_id, approval_target
    from ksadk.events.run_authority import current_run_event_authority
    from ksadk.kernel.worker import AgentKernelWorker
    from tests.a2a.test_invocation_service_r85 import request

    retained = []

    class ApprovingAdapter(FakeAdapter):
        async def stream(self, handle):
            authority = current_run_event_authority()
            req = request()
            req = req.model_copy(update={"handle": req.handle.model_copy(update={
                "run_id": authority.run_id, "session_id": authority.session_id
            })})
            retained.append(req)
            yield approval_event(req, timestamp=1.0)

    stack = await kernel_stack(adapter=ApprovingAdapter())
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="managed-approval"), permit=stack.permit("enqueue")
    )
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: stack.adapter, session_events=stack.events
    )
    result = await worker.run_once(AGENT, lease)
    req = retained[0]
    record = await stack.store.get(approval_id(req))
    assert record.provider_id == "managed-a2a"
    assert record.native_target == approval_target(req)
    assert record.run_id == result.run_id
    assert (await stack.store.load_run(result.run_id)).state is RunState.WAITING
    events = await stack.events.read("s1", 0, 100)
    approvals = [event for event in events if event.event_type == "interaction.requested"]
    assert len(approvals) == 1
    assert approvals[0].family == "interaction"
    assert "invocation_sha256" not in str(approvals[0].payload)


@pytest.mark.parametrize("action,outcome", [("approve", "approved"), ("reject", "rejected")])
@pytest.mark.parametrize("recovered", [False, True, "committed", "immediate", "immediate_cancel"])
@pytest.mark.parametrize("kind", ["approval", "structured_input"])
async def test_managed_approval_wakes_original_call_after_commit(action, outcome, recovered, kind):
    from ksadk.a2a.runtime_approval import ManagedA2AApprovalWaiter, approval_id
    from ksadk.events.run_authority import current_run_event_authority
    from ksadk.kernel.worker import AgentKernelWorker
    from tests.a2a.test_invocation_service_r85 import request

    waiting, finished = asyncio.Event(), asyncio.Event()
    recovery_ready = asyncio.Event()
    seen = []
    response = ({"answer": "durable input", "secret": "fixture-private-input"}
                if kind == "structured_input" else {})
    if kind == "structured_input" and action == "approve":
        action, outcome = "submit", "submitted"

    class WaitingAdapter(FakeAdapter):
        async def stream(self, handle):
            if recovered in {"immediate", "immediate_cancel"} and seen:
                await recovery_ready.wait()
            authority = current_run_event_authority()
            req = seen[0] if seen else request()
            req = req.model_copy(
                update={
                    "handle": req.handle.model_copy(
                        update={"run_id": authority.run_id, "session_id": authority.session_id}
                    )
                }
            )
            if kind == "approval":
                waiter = ManagedA2AApprovalWaiter(req)
                event = waiter.event(timestamp=1.0)
            else:
                from ksadk.a2a.runtime_input import ManagedA2AInputWaiter
                from ksadk.events.canonical import (
                    InteractionRequested,
                    SourceRef,
                    StructuredInputRequest,
                )

                event = InteractionRequested(
                    schema_version=2,
                    seq=0,
                    timestamp=1.0,
                    event_id="input-request",
                    run_id=req.handle.run_id,
                    scope_id=req.handle.scope_id,
                    source=SourceRef(framework="a2a"),
                    interaction_id=approval_id(req),
                    interaction_kind="structured_input",
                    request=StructuredInputRequest(schema={
                        "type": "object", "properties": {"secret": {"writeOnly": True}},
                    }),
                )
                waiter = ManagedA2AInputWaiter(
                    event, SimpleNamespace(handle=req.handle, continuation_id="continuation")
                )
                event = waiter.event
            seen.append(req)
            yield event
            waiting.set()
            result = await waiter.wait()
            assert result == ((outcome, response) if kind == "structured_input" else outcome)
            assert (await stack.store.get(approval_id(req))).status == "resolved"
            finished.set()

    from types import SimpleNamespace

    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from tests.runners.test_adk_multi_llm_runtime_events import runner_for

    configured = ADKRuntimeAdapter(runner_for([]))
    configured.configure_managed_a2a(SimpleNamespace(outbound_enabled=True))
    adapter = WaitingAdapter(matrix=configured.capabilities())
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: adapter, session_events=stack.events
    )
    await stack.kernel.submit(
        command(idempotency_key="managed-wait"), permit=stack.permit("enqueue")
    )
    started = await worker.run_once(AGENT, lease)
    await asyncio.wait_for(waiting.wait(), timeout=1)
    if recovered:
        from ksadk.kernel.contracts import ActivationWriteGuard

        if recovered == "committed":
            from ksadk.interaction.contracts import InteractionSubmission

            await stack.store.resolve(
                InteractionSubmission(
                    interaction_id=approval_id(seen[0]),
                    expected_revision=1,
                    action=action,
                    response=response,
                    idempotency_key="commit-before-crash",
                ),
                guard=ActivationWriteGuard(
                    activation_id=lease.activation_id, fencing_token=lease.fencing_token
                ),
            )
        old = worker.execution_for(started.run_id)
        old.stream_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old.stream_task
        waiting.clear()
        adapter = WaitingAdapter(matrix=configured.capabilities())
        worker = AgentKernelWorker(
            stack.store, adapter_factory=lambda: adapter, session_events=stack.events
        )
        worker.recover_stream(
            run=await stack.store.load_run(started.run_id),
            adapter=adapter,
            handle=old.handle,
            guard=ActivationWriteGuard(
                activation_id=lease.activation_id, fencing_token=lease.fencing_token
            ),
        )
        if recovered not in {"immediate", "immediate_cancel"}:
            await asyncio.wait_for(waiting.wait(), timeout=1)
    if recovered != "committed":
        receipt = await stack.kernel.submit(
            command(
                "submit_interaction",
                idempotency_key="managed-reply",
                payload={
                    "run_id": started.run_id,
                    "interaction_id": approval_id(seen[0]),
                    "token_ref": "fake-permit-ref",
                    "action": action,
                    "expected_revision": 1,
                    "response": response,
                },
            ),
            permit=stack.permit("submit_interaction"),
        )
        assert receipt.status == "accepted"
        if recovered in {"immediate", "immediate_cancel"}:
            deferred = await worker.run_once(AGENT, lease)
            assert deferred.outcome == "idle"
            assert (await stack.store.get(approval_id(seen[0]))).status == "pending"
            facts = await stack.events.read("s1", 0, 100)
            assert not any(e.event_type == "control.command_rejected" for e in facts)
            if recovered == "immediate_cancel":
                receipt = await stack.kernel.submit(
                    command("interrupt", idempotency_key="cancel-before-waiter", payload={}),
                    permit=stack.permit("interrupt"),
                )
                assert receipt.status == "accepted"
                recovering = worker.execution_for(started.run_id)
                cancelled = await worker.run_once(AGENT, lease)
                assert cancelled.outcome == "completed"
                assert (await stack.store.load_run(started.run_id)).state is RunState.CANCELLED
                recovering.stream_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await recovering.stream_task
                return
            recovery_ready.set()
            await asyncio.wait_for(waiting.wait(), timeout=1)
        resolved = await worker.run_once(AGENT, lease)
        assert resolved.outcome == "completed"
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert len(seen) == (2 if recovered else 1)
    events = await stack.events.read("s1", 0, 100)
    assert sum(event.event_type == "interaction.requested" for event in events) == 1
    terminal = [event for event in events if event.event_type == "interaction.resolved"]
    assert len(terminal) == 1 and terminal[0].payload["outcome"] == outcome
    if kind == "structured_input":
        assert terminal[0].payload["response"] == {"answer": "durable input", "secret": None}


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "interaction_id", "kind", "revision"])
async def test_managed_recovery_rejects_mismatched_terminal_fact(field):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from ksadk.interaction.ledger import interaction_event
    from ksadk.kernel.worker import AgentKernelWorker
    from tests.interaction.test_ledger_conformance import make_record

    record = make_record("recover-input").model_copy(update={"status": "resolved", "revision": 2})
    event = interaction_event(
        record, event_type="interaction.resolved", timestamp="2026-09-14T00:00:00+00:00",
        outcome="submitted", response={"answer": "must not be delivered"},
    )
    payload = dict(event.payload)
    payload[field] = 999 if field == "revision" else "another-scope"
    event = event.model_copy(update={"seq": 1, "payload": payload})
    events = SimpleNamespace(read=AsyncMock(side_effect=[[event], []]))
    stack = await kernel_stack()
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: stack.adapter, session_events=events
    )
    waiter = Mock()
    with pytest.raises(InvalidCommandError, match="terminal fact is missing"):
        await worker._deliver_committed_managed_interaction(record, waiter)
    waiter.deliver.assert_not_called()


async def test_background_failure_is_not_delivered_to_a_new_activation():
    from ksadk.kernel.worker import AgentKernelWorker

    release = asyncio.Event()

    class FailingAdapter(FakeAdapter):
        async def stream(self, handle):
            await release.wait()
            raise RuntimeError("old owner stream failure")
            yield  # pragma: no cover - async iterator signature

    adapter = FailingAdapter()
    stack = await kernel_stack(adapter=adapter)
    old = await stack.lease("s1", "old-owner")
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: adapter, session_events=stack.events
    )
    await stack.kernel.submit(
        command(idempotency_key="old-owner-run"), permit=stack.permit("enqueue")
    )
    started = await worker.run_once(AGENT, old)
    execution = worker.execution_for(started.run_id)
    release.set()
    with pytest.raises(RuntimeError, match="old owner"):
        await execution.stream_task
    await asyncio.sleep(0)
    await stack.store.release_activation(old.activation_id, expected_fence=old.fencing_token)
    new = await stack.lease("s1", "new-owner")
    assert new.fencing_token > old.fencing_token
    assert worker.take_stream_failure(started.run_id, new) is None
    assert (await stack.store.load_run(started.run_id)).state is RunState.RUNNING


async def test_enqueue_emits_runtime_event_stream_with_durable_run_id():
    stack = await kernel_stack()
    # adapter 侧 run_id 与 durable run_id 故意不同：事件必须统一用 durable id。
    stack.adapter.handle_run_id = "adapter-run-9"
    stack.adapter.stream_events = _stream_events("adapter-run-9")
    lease = await stack.lease()
    await stack.kernel.submit(command(idempotency_key="evt-1"), permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert result.run_id not in (None, "adapter-run-9")

    envelopes = await stack.events.read("s1", 0, 50)
    runtime_events = [e for e in envelopes if e.family == "runtime"]
    types = {e.event_type for e in runtime_events}
    # Adapters are allowed to end after progress without emitting their own
    # final fact.  The Kernel owns the public lifecycle and must synthesize a
    # canonical terminal event before marking the durable run completed.
    assert {"run.started", "run.progress", "run.completed"} <= types
    assert all(e.run_id == result.run_id for e in runtime_events)
    assert stack.adapter.streams == ["adapter-run-9"]

    run = await stack.store.load_run(result.run_id)
    assert run is not None and run.state == RunState.COMPLETED
    # 显式记录 durable <-> adapter run id 映射。
    assert run.metadata["runtime_run_id"] == "adapter-run-9"
    # handle + digest 成对持久化，recovery attach 分支可判定。
    handle = RunHandle.model_validate(run.metadata["handle"])
    assert handle.run_id == "adapter-run-9"
    from ksadk.runtime.executor import handle_digest

    assert run.metadata["handle_digest"] == handle_digest(handle)
    assert ("close", "s1") in stack.adapter.calls


async def test_enqueue_uses_deployment_owned_start_request_defaults():
    """Public payload cannot replace manifest-owned model or approval policy."""

    stack = await kernel_stack()
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="manifest-defaults"),
        permit=stack.permit("enqueue"),
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
        start_request_defaults={
            "agent_id": "manifest-agent",
            "model": "manifest-model",
            "config": {
                "approval_mode": "manual",
                "sandbox": "workspace-write",
                "base_instructions": "manifest-owned",
            },
        },
    )
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    request = stack.adapter.start_requests[-1]
    assert request.agent_id == "manifest-agent"
    assert request.model == "manifest-model"
    assert request.config == {
        "approval_mode": "manual",
        "sandbox": "workspace-write",
        "base_instructions": "manifest-owned",
    }


async def test_follow_up_enqueue_resumes_native_thread_from_session_log():
    from ksadk.events.canonical import ContinuationCreated
    from ksadk.kernel.worker import AgentKernelWorker

    stack = await kernel_stack()
    stack.adapter.stream_events = [
        ContinuationCreated(
            schema_version=2,
            event_id="continuation-thread-1",
            seq=0,
            timestamp=1780000000.0,
            run_id="adapter-run-1",
            scope_id="thread-scope-1",
            source=_fake_source(),
            continuation_id="continuation-1",
            continuation_kind="thread_resume",
            resumable=True,
            ref={"thread_id": "native-thread-1", "turn_id": "turn-1"},
        )
    ]
    lease = await stack.lease()
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )

    await stack.kernel.submit(
        command(idempotency_key="follow-up-first"), permit=stack.permit("enqueue")
    )
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"

    stack.adapter.stream_events = []
    await stack.kernel.submit(
        command(idempotency_key="follow-up-second"), permit=stack.permit("enqueue")
    )
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    assert len(stack.adapter.start_requests) == 2
    assert "thread_id" not in stack.adapter.start_requests[0].metadata
    assert stack.adapter.start_requests[1].metadata["thread_id"] == "native-thread-1"


async def test_control_uses_durable_run_id_when_adapter_returns_a_different_id():
    """Adapter 的 runtime run_id 不能让 interrupt 丢失 live handle。"""

    stack = await kernel_stack(adapter=FakeAdapter(matrix=matrix_with(cancel=native())))
    stack.adapter.handle_run_id = "runtime-private-run"
    stack.adapter.stream_error = AgentKernelError(
        "persistence_uncertain", "keep run open", retryable=True
    )
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="durable-map-start"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    started = await worker.run_once(AGENT, lease)
    assert started.outcome == "retryable_failure"
    active = await stack.store.find_active_run(AGENT, "s1")
    assert active is not None
    assert active.metadata["runtime_run_id"] == "runtime-private-run"

    stack.adapter.stream_error = None
    await stack.kernel.submit(
        command("interrupt", idempotency_key="durable-map-interrupt"),
        permit=stack.permit("interrupt"),
    )
    completed = await worker.run_once(AGENT, lease)
    assert completed.outcome == "completed"
    assert ("cancel", "s1") in stack.adapter.calls


async def test_stream_completes_only_after_natural_end():
    stack = await kernel_stack()
    stack.adapter.stream_events = _stream_events("any")
    lease = await stack.lease()
    await stack.kernel.submit(command(idempotency_key="evt-2"), permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    run = await stack.store.load_run(result.run_id)
    assert run is not None and run.state == RunState.COMPLETED


async def test_terminal_event_closes_run_without_waiting_for_provider_eof():
    from ksadk.events.canonical import RunCompleted
    from ksadk.kernel.worker import AgentKernelWorker

    stack = await kernel_stack()
    stack.adapter.stream_events = [
        RunCompleted(
            schema_version=2,
            event_id="provider-terminal-with-open-stream",
            seq=0,
            timestamp=1780000001.0,
            run_id="provider-run",
            scope_id="run:provider-run",
            source=_fake_source(),
            status="completed",
            output_refs=(),
        )
    ]
    stack.adapter.block_after_stream_events = True
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="terminal-open-stream"),
        permit=stack.permit("enqueue"),
    )
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )

    result = await worker.run_once(AGENT, lease)
    assert result.run_id is not None

    async def wait_for_terminal() -> None:
        while True:
            run = await stack.store.load_run(result.run_id)
            if run is not None and run.state == RunState.COMPLETED:
                return
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_terminal(), timeout=0.5)
    assert ("close", "s1") in stack.adapter.calls


async def test_stream_retryable_error_keeps_run_open():
    stack = await kernel_stack()
    stack.adapter.stream_error = AgentKernelError(
        "persistence_uncertain", "flush failed", retryable=True
    )
    lease = await stack.lease()
    await stack.kernel.submit(command(idempotency_key="evt-3"), permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "retryable_failure"
    run = await stack.store.find_active_run(AGENT, "s1")
    assert run is not None and run.state == RunState.RUNNING
    for _ in range(10):
        if ("close", "s1") in stack.adapter.calls:
            break
        await asyncio.sleep(0)
    assert ("close", "s1") in stack.adapter.calls
    assert worker.execution_for(result.run_id) is None


async def test_stream_typed_rejection_is_discarded():
    stack = await kernel_stack()
    stack.adapter.stream_error = InvalidCommandError("stream payload invalid")
    lease = await stack.lease()
    await stack.kernel.submit(command(idempotency_key="evt-4"), permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"  # typed rejection 确定性收口
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.DISCARDED
    events = await stack.events.read("s1", 0, 20)
    assert any(e.event_type == "control.command_rejected" for e in events)


async def test_turn_model_override_applies_without_allowlist():
    """未配置 allowed_models(未声明限制)时,run 请求的 model 覆盖必须生效。

    只配默认 model 不等于白名单;显式 allowed_models 存在时才收紧
    (见 test_turn_model_and_approval_are_bounded_by_deployment_defaults)。
    """
    stack = await kernel_stack()
    lease = await stack.lease()
    await stack.kernel.submit(
        command(
            idempotency_key="model-override-no-allowlist",
            payload={
                "content": [{"type": "input_text", "text": "hello"}],
                "runtime_options": {"model": "glm-5.3"},
            },
        ),
        permit=stack.permit("enqueue"),
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        start_request_defaults={
            "model": "gpt-5.6-sol",
            "config": {"sandbox_read_only": True},
        },
    )

    result = await worker.run_once(AGENT, lease)

    assert result.outcome == "completed"
    request = stack.adapter.start_requests[-1]
    assert request.model == "glm-5.3"


@pytest.mark.parametrize("profile", ["flat-v1", "agent-block-v1"])
async def test_worker_persists_presentation_selection_without_forwarding_it_to_adapter(profile):
    stack = await kernel_stack()
    lease = await stack.lease()
    submitted = command(content="hello")
    submitted = submitted.model_copy(update={"payload": {**submitted.payload, "presentation_profile": profile}})
    await stack.kernel.submit(submitted, permit=stack.permit("enqueue"))
    from ksadk.kernel.worker import AgentKernelWorker
    result = await AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter).run_once(AGENT, lease)
    run = await stack.store.load_run(result.run_id)
    assert run.metadata["presentation_profile"] == profile
    assert stack.adapter.start_requests[0].input == submitted.payload["content"]
    assert "presentation_profile" not in stack.adapter.start_requests[0].metadata
