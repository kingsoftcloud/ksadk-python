"""Test-only independent reader for committed Kernel facts; no credentials printed."""

import asyncio
import json
import os
import sys
from urllib.parse import urlencode

from ksadk.kernel.postgres_store import (
    PostgresAgentKernelStore,
    PostgresFencedSessionEventStore,
    PostgresKernelEventLog,
)
from ksadk.sessions.postgres_service import PostgresSessionService


async def main():
    schema, session_id = sys.argv[1:3]
    dsn = os.environ["A2A_TEST_POSTGRES_DSN"].replace("postgresql+asyncpg://", "postgresql://", 1)
    dsn += ("&" if "?" in dsn else "?") + urlencode({"search_path": schema})
    sessions = PostgresSessionService(dsn=dsn, tenant_id="account-1")
    try:
        assert await sessions.get_session(session_id) is not None
        store = PostgresAgentKernelStore(
            sessions._pool,
            PostgresKernelEventLog(sessions._pool, tenant_id="account-1"),
            tenant_id="account-1",
        )
        event_store = PostgresFencedSessionEventStore(store)
        if "--recover" in sys.argv:
            from types import SimpleNamespace

            from langchain_core.messages import AIMessage
            from langgraph.prebuilt import create_react_agent

            from ksadk.kernel.recovery import RecoveryCoordinator
            from ksadk.kernel.store import ActivationLeaseRequest
            from ksadk.runners.langgraph_runner import LangGraphRunner
            from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
            from tests.kernel.control_harness import AGENT
            from tests.runners.test_langchain_hitl_langgraph import SeqToolModel

            async with asyncio.timeout(5):
                while await store.current_lease(AGENT, session_id) is not None:
                    await asyncio.sleep(0.05)
            lease = await store.acquire_activation(ActivationLeaseRequest(
                agent_instance_id=AGENT, session_id=session_id, activation_id="recovery-owner",
                runtime_type="langgraph", lease_ttl_seconds=30,
            ))
            assert lease.fencing_token > 1
            runner = LangGraphRunner(
                SimpleNamespace(entry_point="agent.py", agent_variable="agent"), "."
            )
            model = SeqToolModel(msgs=[AIMessage(content="must not execute again")])
            runner._agent = create_react_agent(model, [])
            runtime = LangGraphRuntimeAdapter(runner)
            recovery = RecoveryCoordinator(store, event_store, runtime.capabilities,
                                           session_service=sessions)
            report = await recovery.recover(AGENT, lease, session_id=session_id)
            assert report.outcome == "interrupted"
            assert model.i == 0
            repeated = await recovery.recover(AGENT, lease, session_id=session_id)
            assert repeated.outcome == "no_op"
        events = await event_store.read(session_id, 0, 100)
        if "--recover" in sys.argv:
            transitions = [event for event in events
                           if event.event_type == "control.run_transition"]
            assert sum(event.payload.get("state") == "interrupted" for event in transitions) == 1
            assert not any(event.payload.get("state") == "completed" for event in transitions)
            assert len({event.run_id for event in transitions}) == 1
        print(json.dumps([event.model_dump(mode="json") for event in events]))
    finally:
        await sessions.aclose()


if __name__ == "__main__":
    asyncio.run(main())
