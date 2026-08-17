# -*- coding: utf-8 -*-
"""InMemoryAgentKernelStore conformance（Phase 1 Task 3）。"""

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
