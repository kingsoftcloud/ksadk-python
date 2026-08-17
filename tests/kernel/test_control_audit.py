# -*- coding: utf-8 -*-
"""permit/unsupported/queue_full 审计事件脱敏（Task 6 Step 1）。"""

from __future__ import annotations

from tests.kernel.control_harness import AGENT, command, kernel_stack

FORBIDDEN_AUDIT_KEYS = ("signature", "nonce", "subject_ref", "claims_digest", "allowed_operations")


def _assert_redacted(payload: dict) -> None:
    for key in FORBIDDEN_AUDIT_KEYS:
        assert key not in payload, f"audit payload leaked {key!r}"
    text = repr(payload)
    assert "Ed25519" not in text and "nonce-" not in text


async def test_invalid_permit_writes_redacted_audit_and_no_inbox():
    stack = await kernel_stack()
    receipt = await stack.kernel.submit(
        command(idempotency_key="a1"),
        permit=stack.permit("interrupt"),  # 越权 operation
    )
    assert receipt.status == "rejected"
    assert await stack.store.list_pending(AGENT, "s1") == []
    events = await stack.events.read("s1", 0, 10)
    rejected = [e for e in events if e.event_type == "control.command_rejected"]
    assert len(rejected) == 1
    _assert_redacted(dict(rejected[0].payload))
    assert rejected[0].payload["reason"] == "invalid_permit"


async def test_unsupported_writes_audit_event_without_enqueue():
    stack = await kernel_stack()
    receipt = await stack.kernel.submit(
        command("inject"), permit=stack.permit("inject", "enqueue")
    )
    assert receipt.status == "unsupported"
    assert await stack.store.list_pending(AGENT, "s1") == []
    events = await stack.events.read("s1", 0, 10)
    rejected = [e for e in events if e.event_type == "control.command_rejected"]
    assert len(rejected) == 1
    _assert_redacted(dict(rejected[0].payload))
    assert rejected[0].payload["status"] == "unsupported"
    assert rejected[0].payload["reason"] == "runtime_no_native_inject"


async def test_queue_full_writes_audit_event():
    stack = await kernel_stack(queue_limit=1)
    await stack.kernel.submit(command(idempotency_key="q1"), permit=stack.permit("enqueue"))
    overflow = await stack.kernel.submit(
        command(idempotency_key="q2"), permit=stack.permit("enqueue")
    )
    assert overflow.status == "queue_full"
    events = await stack.events.read("s1", 0, 10)
    rejected = [e for e in events if e.event_type == "control.command_rejected"]
    assert len(rejected) == 1
    _assert_redacted(dict(rejected[0].payload))
    assert rejected[0].payload.get("reason", rejected[0].payload.get("status")) == "queue_full"


async def test_accepted_events_carry_only_canonical_fields():
    stack = await kernel_stack()
    await stack.kernel.submit(command(idempotency_key="ok1"), permit=stack.permit("enqueue"))
    events = await stack.events.read("s1", 0, 10)
    accepted = events[0]
    assert accepted.event_type == "control.command_accepted"
    _assert_redacted(dict(accepted.payload))
