from __future__ import annotations

from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, StartRequest
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter


class _State(TypedDict, total=False):
    value: str


class _UnsupportedADKRunner(BaseRunner):
    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "done"}

    async def stream(self, input_data: dict[str, Any]):
        yield {"type": "final", "output": "done"}

    def describe_checkpoint_capability(self) -> dict[str, Any]:
        return {
            "Supported": False,
            "Backend": "none",
            "Scope": "unknown",
            "Durable": False,
            "SharedAcrossPods": False,
            "Reason": "ADK invocation resume is disabled by the installed SDK/backend",
        }


def _langgraph_without_checkpointer() -> LangGraphRuntimeAdapter:
    graph = StateGraph(_State)
    graph.add_node("finish", lambda _state: {"value": "done"})
    graph.add_edge(START, "finish")
    graph.add_edge("finish", END)

    runner = LangGraphRunner(
        SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent"),
        ".",
    )
    runner._agent = graph.compile()
    return LangGraphRuntimeAdapter(runner)


@pytest.mark.asyncio
async def test_langgraph_without_checkpointer_refuses_checkpoint() -> None:
    adapter = _langgraph_without_checkpointer()
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="no-checkpoint"))

    assert adapter._checkpoint_capability().supported is False
    with pytest.raises(RuntimeError, match="native checkpoint capability is unavailable"):
        await adapter.checkpoint(handle)


@pytest.mark.asyncio
async def test_adk_without_native_resume_refuses_checkpoint_and_resume() -> None:
    runner = _UnsupportedADKRunner(detection_result=None, project_dir=".")
    adapter = ADKRuntimeAdapter(runner)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="adk-disabled"))

    capability = adapter._checkpoint_capability()
    assert capability.supported is False
    assert capability.reason == "ADK invocation resume is disabled by the installed SDK/backend"

    with pytest.raises(RuntimeError, match="native checkpoint capability is unavailable"):
        await adapter.checkpoint(handle)
    with pytest.raises(RuntimeError, match="native checkpoint capability is unavailable"):
        await adapter.resume(
            handle,
            ResumeTarget(kind="invocation_id", id="adk-invocation"),
            ResumePayload(kind="approval_decision", data={"decision": "approve"}),
        )
