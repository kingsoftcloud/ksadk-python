# -*- coding: utf-8 -*-
"""AgentKernel control facade：persist-before-ack / unsupported / 幂等（Task 6 Step 1）。"""

from __future__ import annotations

import asyncio

from ksadk.kernel.contracts import AgentStatusQuery, ControlSource, SessionEventSubscription
from ksadk.kernel.state import InboxState
from tests.kernel.control_harness import (
    AGENT,
    EXPIRED_AT,
    PERMIT_REF,
    TENANT,
    FakeAdapter,
    command,
    default_matrix,
    kernel_stack,
    native,
)


def interaction_adapter() -> FakeAdapter:
    matrix = default_matrix().model_copy(update={"submit_interaction": native()})
    return FakeAdapter(matrix=matrix)


async def test_submit_acks_only_after_inbox_and_event_commit():
    stack = await kernel_stack()
    cmd = command(idempotency_key="k1")
    receipt = await stack.kernel.submit(cmd, permit=stack.permit("enqueue"))
    assert receipt.status == "accepted"
    assert receipt.message_id is not None
    assert receipt.accepted_seq == 1
    # persist-before-ack：receipt 返回时 Inbox 行已存在。
    message = await stack.store.load_message(receipt.message_id)
    assert message is not None
    assert message.status == InboxState.ACCEPTED
    # control.command_accepted 事件也已 commit。
    events = await stack.events.read("s1", 0, 10)
    assert events, "accepted event must be committed before ack"
    assert events[0].event_type == "control.command_accepted"
    assert events[0].payload["command_id"] == str(cmd.command_id)


async def test_steer_does_not_fall_back_to_enqueue():
    stack = await kernel_stack()
    receipt = await stack.kernel.submit(
        command("steer"), permit=stack.permit("steer", "enqueue")
    )
    assert receipt.status == "unsupported"
    assert receipt.error is not None
    assert receipt.error.code == "runtime_no_native_steer"
    # 未降级为 enqueue：不产生 Inbox 行。
    pending = await stack.store.list_pending(AGENT, "s1")
    assert pending == []


async def test_submit_is_idempotent_for_network_retry():
    stack = await kernel_stack()
    cmd = command(idempotency_key="dup-1", content="same")
    first = await stack.kernel.submit(cmd, permit=stack.permit("enqueue", nonce="n1"))
    retry = await stack.kernel.submit(cmd, permit=stack.permit("enqueue", nonce="n1"))
    assert first.status == "accepted"
    assert retry.status == "duplicate"
    assert retry.message_id == first.message_id
    assert retry.accepted_seq == first.accepted_seq


async def test_interaction_retry_ignores_fresh_admission_credentials():
    """A retry gets a new Server permit without becoming a new mutation."""

    stack = await kernel_stack(adapter=interaction_adapter())
    first_permit = stack.permit(
        "submit_interaction", nonce="interaction-n1", permit_id="permit-attempt-1"
    )
    first = command(
        "submit_interaction",
        idempotency_key="interaction-retry-1",
        authorization_ref=first_permit.permit_id,
        payload={
            "run_id": "run-1",
            "interaction_id": "it-1",
            "token_ref": first_permit.permit_id,
            "action": "approve",
            "expected_revision": 1,
            "idempotency_key": "interaction-retry-1",
            "response": {"approved": True},
        },
    )
    accepted = await stack.kernel.submit(first, permit=first_permit)

    retry_permit = stack.permit(
        "submit_interaction", nonce="interaction-n2", permit_id="permit-attempt-2"
    )
    retry = command(
        "submit_interaction",
        idempotency_key="interaction-retry-1",
        authorization_ref=retry_permit.permit_id,
        payload={
            "run_id": "run-1",
            "interaction_id": "it-1",
            "token_ref": retry_permit.permit_id,
            "action": "approve",
            "expected_revision": 1,
            "idempotency_key": "interaction-retry-1",
            "response": {"approved": True},
        },
    ).model_copy(update={"source": ControlSource(kind="studio", ref="http-request-2")})
    duplicate = await stack.kernel.submit(retry, permit=retry_permit)

    assert accepted.status == "accepted"
    assert duplicate.status == "duplicate"
    assert duplicate.message_id == accepted.message_id
    assert duplicate.accepted_seq == accepted.accepted_seq


async def test_interaction_retry_still_rejects_changed_business_response():
    stack = await kernel_stack(adapter=interaction_adapter())
    first_permit = stack.permit(
        "submit_interaction", nonce="interaction-change-n1", permit_id="permit-change-1"
    )
    base_payload = {
        "run_id": "run-1",
        "interaction_id": "it-1",
        "token_ref": first_permit.permit_id,
        "action": "approve",
        "expected_revision": 1,
        "idempotency_key": "interaction-change-1",
        "response": {"approved": True},
    }
    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="interaction-change-1",
            authorization_ref=first_permit.permit_id,
            payload=base_payload,
        ),
        permit=first_permit,
    )

    retry_permit = stack.permit(
        "submit_interaction", nonce="interaction-change-n2", permit_id="permit-change-2"
    )
    changed_payload = {
        **base_payload,
        "token_ref": retry_permit.permit_id,
        "response": {"approved": False},
    }
    conflict = await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="interaction-change-1",
            authorization_ref=retry_permit.permit_id,
            payload=changed_payload,
        ),
        permit=retry_permit,
    )

    assert conflict.status == "rejected"
    assert conflict.error is not None
    assert conflict.error.code == "idempotency_conflict"


async def test_idempotency_key_conflict_is_rejected():
    stack = await kernel_stack()
    await stack.kernel.submit(
        command(idempotency_key="dup-1", content="a"), permit=stack.permit("enqueue")
    )
    conflict = await stack.kernel.submit(
        command(idempotency_key="dup-1", content="b"), permit=stack.permit("enqueue")
    )
    assert conflict.status == "rejected"
    assert conflict.error.code == "idempotency_conflict"


async def test_queue_full_is_typed_and_retryable():
    stack = await kernel_stack(queue_limit=1)
    first = await stack.kernel.submit(
        command(idempotency_key="q1"), permit=stack.permit("enqueue")
    )
    second = await stack.kernel.submit(
        command(idempotency_key="q2"), permit=stack.permit("enqueue")
    )
    assert first.status == "accepted"
    assert second.status == "queue_full"
    assert second.error.retryable is True
    # queue_full 不产生 Inbox 行。
    pending = await stack.store.list_pending(AGENT, "s1")
    assert [m.idempotency_key for m in pending] == ["q1"]


async def test_invalid_permit_is_rejected_without_inbox_row():
    stack = await kernel_stack()
    receipt = await stack.kernel.submit(
        command(),
        permit=stack.permit("interrupt"),  # 未授权 enqueue
    )
    assert receipt.status == "rejected"
    assert receipt.error.code == "invalid_permit"
    assert await stack.store.list_pending(AGENT, "s1") == []


async def test_expired_permit_allows_only_exact_duplicate():
    stack = await kernel_stack()
    cmd = command(idempotency_key="exp-1", content="once")
    accepted = await stack.kernel.submit(
        cmd, permit=stack.permit("enqueue", nonce="exp-nonce")
    )
    assert accepted.status == "accepted"
    # 过期后重放完全相同的 command/permit（同 nonce、同 digest）→ duplicate。
    duplicate = await stack.kernel.submit(
        cmd,
        permit=stack.permit(
            "enqueue", expires_at=EXPIRED_AT, nonce="exp-nonce"
        ),
    )
    assert duplicate.status == "duplicate"
    assert duplicate.message_id == accepted.message_id
    # 过期后的新 mutation 一律拒绝。
    fresh = await stack.kernel.submit(
        command(idempotency_key="exp-2"),
        permit=stack.permit("enqueue", expires_at=EXPIRED_AT),
    )
    assert fresh.status == "rejected"
    assert fresh.error.code == "invalid_permit"
    # 过期 permit 连 query 也拒绝：status fail closed。
    query = AgentStatusQuery(
        tenant_id=TENANT, agent_instance_id=AGENT, authorization_ref="r"
    )
    snapshot = await stack.kernel.status(
        query, permit=stack.permit("get_status", expires_at=EXPIRED_AT)
    )
    assert snapshot.instance_state == "unavailable"


async def test_status_reads_store_without_creating_run():
    stack = await kernel_stack()
    await stack.kernel.submit(
        command(idempotency_key="s1-k1"), permit=stack.permit("enqueue")
    )
    query = AgentStatusQuery(
        tenant_id=TENANT,
        agent_instance_id=AGENT,
        authorization_ref=PERMIT_REF,
        session_id="s1",
    )
    snapshot = await stack.kernel.status(query, permit=stack.permit("get_status"))
    assert snapshot.agent_instance_id == AGENT
    assert snapshot.inbox_depth == 1
    assert snapshot.active_run_id is None
    assert snapshot.capability.steer.reason == "runtime_no_native_steer"


async def test_status_requires_get_status_operation():
    stack = await kernel_stack()
    query = AgentStatusQuery(
        tenant_id=TENANT, agent_instance_id=AGENT, authorization_ref="r"
    )
    snapshot = await stack.kernel.status(query, permit=stack.permit("enqueue"))
    assert snapshot.instance_state == "unavailable"


async def test_subscribe_replays_committed_events_from_cursor():
    stack = await kernel_stack()
    await stack.kernel.submit(
        command(idempotency_key="sub-1"), permit=stack.permit("enqueue")
    )
    subscription = SessionEventSubscription(
        tenant_id=TENANT,
        agent_instance_id=AGENT,
        session_id="s1",
        authorization_ref=PERMIT_REF,
        after_seq=0,
    )
    seen = []
    async for envelope in stack.kernel.subscribe(
        subscription, permit=stack.permit("subscribe_events")
    ):
        seen.append(envelope)
        if len(seen) >= 1:
            break
    assert seen[0].event_type == "control.command_accepted"


async def test_concurrent_submits_across_sessions_lose_nothing():
    stack = await kernel_stack(queue_limit=200)

    async def one(index: int):
        session = "s1" if index % 2 == 0 else "s2"
        return await stack.kernel.submit(
            command(idempotency_key=f"c{index}", session_id=session),
            permit=stack.permit("enqueue", session_id=session),
        )

    receipts = await asyncio.gather(*(one(i) for i in range(20)))
    assert all(r.status == "accepted" for r in receipts)
    assert len({r.message_id for r in receipts}) == 20
    depth1 = await stack.store.inbox_depth(AGENT, "s1")
    depth2 = await stack.store.inbox_depth(AGENT, "s2")
    assert depth1 + depth2 == 20


async def test_executor_resolve_run_reads_store_not_process_cache():
    from ksadk.kernel.state import RunState
    from ksadk.kernel.store import RunRecord
    from ksadk.runtime.executor import DurableRun, RunNotFoundError, RuntimeExecutor

    stack = await kernel_stack()
    activation = await stack.lease()
    await stack.store.save_run_transition(
        RunRecord(
            run_id="run-durable",
            agent_instance_id=AGENT,
            session_id="s1",
            state=RunState.PENDING,
        ),
        expected_fence=activation.fencing_token,
    )
    executor = RuntimeExecutor(registry=None, kernel_store=stack.store)  # type: ignore[arg-type]
    resolved = await executor.resolve_run("run-durable")
    assert isinstance(resolved, DurableRun)
    assert resolved.run.run_id == "run-durable"
    assert resolved.live_handle is None  # cache miss 不等价于 Run 不存在
    with pytest_raises(RunNotFoundError):
        await executor.resolve_run("run-missing")


def pytest_raises(exc):
    import pytest

    return pytest.raises(exc)
