# -*- coding: utf-8 -*-
"""InMemoryAgentKernelStore conformance（Kernel Task 3）。"""

from __future__ import annotations

import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.store import AgentKernelStore
from ksadk.sessions.in_memory import InMemorySessionService
from tests.kernel.store_conformance import (
    CONFORMANCE_CHECKS,
    assert_concurrent_accepts_have_no_loss_or_duplicate,
    seed_session,
)


@pytest.fixture
async def store():
    service = InMemorySessionService()
    await seed_session(service)()
    kernel_store = InMemoryAgentKernelStore(SessionServiceEventStore(service))
    yield kernel_store


@pytest.mark.parametrize("check", CONFORMANCE_CHECKS)
async def test_memory_store_conformance(store, check):
    await check(store)


async def test_memory_store_concurrency(store):
    await assert_concurrent_accepts_have_no_loss_or_duplicate(store)


async def test_memory_store_satisfies_port_protocol(store):
    assert isinstance(store, AgentKernelStore)


async def test_interaction_guard_scopes_to_session_under_shared_activation(store):
    """同一 activation 持有多个 session 的 lease 时，guard 必须按 session 命中。

    guard 只按 activation_id 查任意一行会在多 session 场景随机命中别的
    session 的 activation 行，把合法 interaction 请求误判 StaleFence
    （hosted 单 pod 多 session 必现）。"""

    from ksadk.interaction.contracts import InteractionRecord
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.store import ActivationLeaseRequest

    service = InMemorySessionService()
    await service.create_session("a", "u", session_id="s1")
    await service.create_session("a", "u", session_id="s2")
    store._events = SessionServiceEventStore(service)

    base = dict(
        agent_instance_id="agent-1",
        activation_id="pod-1",
        runtime_type="ksadk-agent-kernel",
        bundle_digest="b",
        capability_digest="c",
        lease_ttl_seconds=60.0,
    )
    lease1 = await store.acquire_activation(
        ActivationLeaseRequest(session_id="s1", **base)
    )
    lease2 = await store.acquire_activation(
        ActivationLeaseRequest(session_id="s2", **base)
    )
    assert lease1.activation_id == lease2.activation_id
    guard2 = ActivationWriteGuard(
        activation_id=lease2.activation_id, fencing_token=lease2.fencing_token
    )

    def record(session_id: str, interaction_id: str) -> InteractionRecord:
        return InteractionRecord(
            interaction_id=interaction_id,
            tenant_id="t1",
            agent_instance_id="agent-1",
            session_id=session_id,
            run_id="run-x",
            kind="approval",
            request_schema={"type": "object"},
            created_at="2026-08-20T00:00:00Z",
            provider_id="codex",
            native_target={"call_id": "c1"},
        )

    await store.request(record("s1", "it-shared-1"), guard=guard2.__class__(
        activation_id=lease1.activation_id, fencing_token=lease1.fencing_token
    ))
    # s2 的请求必须命中 s2 自己的 activation 行，而不是 s1 的。
    stored = await store.request(record("s2", "it-shared-2"), guard=guard2)
    assert stored.session_id == "s2"
