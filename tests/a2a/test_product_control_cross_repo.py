"""Actual Server permit issuance consumed by SDK Kernel admission."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from ksadk.kernel.authorization import AgentControlPermitVerifier
from ksadk.kernel.contracts import AgentControlPermit
from ksadk.kernel.control import AgentKernel
from tests.kernel.control_harness import command, kernel_stack

PRODUCER = """
import json
from types import SimpleNamespace
from app.services.agent_control_authorization import (
    AgentControlAuthorization, Ed25519WorkloadSigner,
)
authority = AgentControlAuthorization(Ed25519WorkloadSigner.generate("cross-repo-test"))
permit = authority.issue({"subject_ref": "user:product-test"},
    SimpleNamespace(tenant_id="tenant-1", id="agent-1"), "s1", "enqueue")
print(json.dumps({"jwks": authority.jwks(), "permit": permit.model_dump(mode="json")}))
"""


@pytest.mark.asyncio
async def test_server_permit_admits_one_kernel_command():
    configured = os.environ.get("A2A_TEST_SERVER_REPO")
    if not configured:
        pytest.skip("A2A_TEST_SERVER_REPO required for cross-repository test")
    repo = Path(configured).resolve()
    result = json.loads(
        subprocess.check_output(
            [str(repo / ".venv/bin/python"), "-c", PRODUCER], cwd=repo, text=True, timeout=15
        )
    )

    class Keys:
        async def fetch_verification_keys(self):
            return {key["kid"]: key["x"] for key in result["jwks"]["keys"]}

    from types import SimpleNamespace

    from langchain_core.messages import AIMessage
    from langgraph.prebuilt import create_react_agent

    from ksadk.kernel.worker import AgentKernelWorker
    from ksadk.runners.langgraph_runner import LangGraphRunner
    from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
    from tests.kernel.control_harness import AGENT
    from tests.runners.test_langchain_hitl_langgraph import SeqToolModel

    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="agent"), ".")
    runner._agent = create_react_agent(SeqToolModel(msgs=[AIMessage(content="finished")]), [])
    runtime = LangGraphRuntimeAdapter(runner)
    stack = await kernel_stack(adapter=runtime)
    kernel = AgentKernel(
        stack.store,
        stack.events,
        AgentControlPermitVerifier(Keys()),
        capabilities=stack.adapter.capabilities,
    )
    permit = AgentControlPermit.model_validate(result["permit"])
    cmd = command(authorization_ref=permit.permit_id, payload={"content": "hello"})
    first = await kernel.submit(cmd, permit=permit)
    retry = await kernel.submit(cmd, permit=permit)
    assert first.status == "accepted"
    assert retry.status == "duplicate"
    assert first.command_id == retry.command_id == cmd.command_id

    lease = await stack.lease("s1")
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: runtime,
        session_events=stack.events,
        session_service=stack.session_service,
    )
    outcome = await worker.run_once(AGENT, lease)
    assert outcome.outcome == "completed"
    async with asyncio.timeout(5):
        while True:
            run = await stack.store.load_run(outcome.run_id)
            if run.state.value == "completed":
                break
            await asyncio.sleep(0.01)
    transitions = [
        event
        for event in await stack.events.read("s1", 0, 100)
        if event.event_type == "control.run_transition"
    ]
    assert transitions[-1].payload["state"] == "completed"
    assert {event.causation_id for event in transitions} == {str(cmd.command_id)}
    assert {event.run_id for event in transitions} == {outcome.run_id}
