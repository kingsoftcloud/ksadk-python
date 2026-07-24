"""实证:新版 LangChain(`create_agent`)HITL 经 LangGraph 基座跑通(goal 架构验证)。

背景:新版 LangChain `create_agent` 返回的就是 LangGraph ``CompiledStateGraph``,
其 ``HumanInTheLoopMiddleware`` 底层用 LangGraph ``interrupt`` + checkpointer +
``Command(resume=)``。本文件**不 mock 图**,用真实 ``create_agent`` + 真实
``InMemorySaver`` 证明:

1. **核心机制**:HITL interrupt 真暂停,``approve``/``reject``/``edit`` 三种决定真恢复。
2. **ksadk 链路**:`LangGraphRunner`(deepagents 式复用 LangGraph)驱动真实 interrupting
   agent——interrupt 经 ``checkpoint`` 事件 metadata(``next_node``=HITL middleware、
   ``is_resumable``、``checkpoint_id``)可检测;``Command(resume={"decisions":[...]})``
   真续跑(approve → 工具真执行)。

这支撑"LangChain 运行时收敛到 LangGraph 基座(不追版本、不扩 legacy langchain_runner)"
的架构决策。fake model 用自定义流式保真的 ``SeqToolModel``,不需真 LLM。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ksadk.events.runtime_event import EventType
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, StartRequest
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter


class SeqToolModel(BaseChatModel):
    """按序吐消息、且流式保真(tool_calls 经 tool_call_chunks 重组)的 fake model。"""

    msgs: list
    i: int = 0

    @property
    def _llm_type(self) -> str:  # noqa: D401
        return "seq-tool"

    def bind_tools(self, tools: Any, **kw: Any) -> "SeqToolModel":
        return self

    def _next(self) -> Any:
        m = self.msgs[min(self.i, len(self.msgs) - 1)]
        self.i += 1
        return m

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        m = self._next()
        if getattr(m, "tool_calls", None):
            tcc: Any = [
                {"name": t["name"], "args": json.dumps(t["args"]), "id": t["id"], "index": 0}
                for t in m.tool_calls
            ]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=tcc))
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(content=m.content))


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    return f"wrote:{path}"


def _tc() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "write_file", "args": {"path": "/tmp/x.txt", "content": "hi"}, "id": "c1"}
        ],
    )


def _make_agent() -> Any:
    model = SeqToolModel(msgs=[_tc(), AIMessage(content="完成")])
    return create_agent(
        model=model,
        tools=[write_file],
        middleware=[HumanInTheLoopMiddleware(interrupt_on={"write_file": True})],
        checkpointer=InMemorySaver(),
    )


async def _run_until_interrupt(agent: Any, cfg: dict) -> Any:
    async for chunk in agent.astream(
        {"messages": [{"role": "user", "content": "写文件"}]}, cfg, stream_mode="updates"
    ):
        if "__interrupt__" in chunk:
            return chunk["__interrupt__"]
    return None


async def _resume(agent: Any, cfg: dict, decisions: list) -> list:
    async for _ in agent.astream(
        Command(resume={"decisions": decisions}), cfg, stream_mode="updates"
    ):
        pass
    st = await agent.aget_state(cfg)
    return list(st.values.get("messages", []))


# ---------------------------------------------------------------------------
# 1) 核心机制:真实 create_agent HITL interrupt + 三种决定恢复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_langchain_hitl_interrupt_surfaces_with_four_decisions():
    agent = _make_agent()
    cfg = {"configurable": {"thread_id": "core-1"}}
    intr = await _run_until_interrupt(agent, cfg)
    assert intr, "HITL middleware 未触发 interrupt"
    v = intr[0].value if isinstance(intr, (list, tuple)) else getattr(intr, "value", intr)
    assert v["action_requests"][0]["name"] == "write_file"
    assert set(v["review_configs"][0]["allowed_decisions"]) == {
        "approve",
        "edit",
        "reject",
        "respond",
    }


@pytest.mark.asyncio
async def test_langchain_hitl_approve_executes_tool():
    agent = _make_agent()
    cfg = {"configurable": {"thread_id": "core-approve"}}
    await _run_until_interrupt(agent, cfg)
    msgs = await _resume(agent, cfg, [{"type": "approve"}])
    tm = [m for m in msgs if m.__class__.__name__ == "ToolMessage"]
    assert tm and tm[-1].content == "wrote:/tmp/x.txt"


@pytest.mark.asyncio
async def test_langchain_hitl_reject_skips_tool_with_feedback():
    agent = _make_agent()
    cfg = {"configurable": {"thread_id": "core-reject"}}
    await _run_until_interrupt(agent, cfg)
    msgs = await _resume(agent, cfg, [{"type": "reject", "message": "不允许删除"}])
    tm = [m for m in msgs if m.__class__.__name__ == "ToolMessage"]
    assert tm and "不允许删除" in tm[-1].content


@pytest.mark.asyncio
async def test_langchain_hitl_edit_uses_edited_args():
    agent = _make_agent()
    cfg = {"configurable": {"thread_id": "core-edit"}}
    await _run_until_interrupt(agent, cfg)
    msgs = await _resume(
        agent,
        cfg,
        [
            {
                "type": "edit",
                "edited_action": {
                    "name": "write_file",
                    "args": {"path": "/tmp/EDITED.txt", "content": "hi"},
                },
            }
        ],
    )
    tm = [m for m in msgs if m.__class__.__name__ == "ToolMessage"]
    assert tm and tm[-1].content == "wrote:/tmp/EDITED.txt"


# ---------------------------------------------------------------------------
# 2) ksadk 链路:LangGraphRunner 驱动真实 interrupting agent
# ---------------------------------------------------------------------------


def _make_runner() -> LangGraphRunner:
    det = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    runner = LangGraphRunner(det, ".")
    runner._agent = _make_agent()
    return runner


@pytest.mark.asyncio
async def test_runner_interrupt_detectable_via_checkpoint_metadata():
    """interrupt 经 checkpoint 事件 metadata 可检测(adapter 映 approval.requested 的依据)。"""
    runner = _make_runner()
    events = [c async for c in runner.stream({"session_id": "s1", "input": "写文件"})]
    ckpt = next((e for e in events if e.get("type") == "checkpoint"), None)
    assert ckpt is not None, f"无 checkpoint 事件:{[e.get('type') for e in events]}"
    md = ckpt["metadata"]["agentengine"]
    assert md["is_resumable"] is False
    assert md["resume_status"] == "disabled"
    assert md["backend"] == "memory"
    assert md["scope"] == "process_local"
    assert md["durable"] is False
    assert "HumanInTheLoopMiddleware" in md["next_node"]
    assert md["framework_ref"]["langgraph"]["checkpoint_id"]


@pytest.mark.asyncio
async def test_runtime_adapter_resume_approve_decision_executes_tool():
    """RuntimeAdapter 把真实 HITL 决定送入 Command(resume=),工具真执行。"""
    runner = _make_runner()
    adapter = LangGraphRuntimeAdapter(runner)
    handle = await adapter.start(StartRequest(input="写文件", user_id="u", session_id="s2"))
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    checkpoint = await adapter.checkpoint(handle)

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.payload["call_id"],
            data={"decisions": [{"type": "approve"}]},
        ),
    )
    resumed = [event async for event in adapter.stream(handle)]

    tool_calls = [event for event in resumed if event.event_type == EventType.TOOL_CALL_BEGIN]
    tool_results = [event for event in resumed if event.event_type == EventType.TOOL_CALL_END]
    assert [event.payload for event in tool_calls] == [
        {
            "call_id": "c1",
            "name": "write_file",
            "args": {"path": "/tmp/x.txt", "content": "hi"},
        }
    ]
    assert [event.payload for event in tool_results] == [
        {
            "call_id": "c1",
            "name": "write_file",
            "result": "wrote:/tmp/x.txt",
            "error": None,
        }
    ]
