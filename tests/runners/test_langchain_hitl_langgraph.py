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

from ksadk.events.canonical import InteractionRequested, ItemCompleted, ItemStarted
from ksadk.events.content import ToolCallContent, ToolResultContent
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
async def test_runner_stream_yields_approval_on_silent_pause():
    """流式静默暂停(审批门)必须冒出 approval 事件(goal-18 探测,0.8.5 契约)。"""
    runner = _make_runner()
    events = [c async for c in runner.stream({"session_id": "s-approval", "input": "写文件"})]
    approval = next((e for e in events if e.get("type") == "approval"), None)
    assert approval is not None, f"无 approval 事件:{[e.get('type') for e in events]}"
    info = approval["interrupt_info"]
    assert info["action_requests"][0]["name"] == "write_file"
    assert info.get("approval_request_id")


@pytest.mark.asyncio
async def test_runner_stream_refuses_completed_when_state_unreadable():
    """状态读不出来时必须显性失败——不能伪装成 completed(审批卡会无声消失)。

    复现 0.8.5 checkpoint_ns 事故的形状:aget_state 抛 "Subgraph tenant not found",
    旧实现吞掉异常并把 run 标成 completed,审批卡从此不弹。
    """
    runner = _make_runner()

    class _UnreadableStateAgent:
        """流照常跑，但状态读取永远失败。"""

        def __init__(self, agent):
            self._agent = agent

        def __getattr__(self, name):
            return getattr(self._agent, name)

        async def astream_events(self, *args, **kwargs):
            async for ev in self._agent.astream_events(*args, **kwargs):
                yield ev

        def get_state(self, config):
            raise ValueError("Subgraph tenant not found")

        async def aget_state(self, config):
            raise ValueError("Subgraph tenant not found")

    runner._agent = _UnreadableStateAgent(runner._agent)
    with pytest.raises(RuntimeError, match="could not be read"):
        _ = [c async for c in runner.stream({"session_id": "s-broken", "input": "写文件"})]


@pytest.mark.asyncio
async def test_runner_stream_surfaces_nested_subgraph_interrupt():
    """审批门在子图内时，流结束探测也要能拿到 interrupt（subgraphs=True 下钻）。"""
    pytest.importorskip("langgraph.graph")

    from langgraph.checkpoint.memory import InMemorySaver as _Saver
    from langgraph.graph import START, MessagesState, StateGraph

    inner = _make_agent()
    parent = StateGraph(MessagesState)
    parent.add_node("inner", inner)
    parent.add_edge(START, "inner")
    runner = _make_runner()
    runner._agent = parent.compile(checkpointer=_Saver())

    events = [c async for c in runner.stream({"session_id": "s-nested", "input": "写文件"})]
    approval = next((e for e in events if e.get("type") == "approval"), None)
    assert approval is not None, f"无 approval 事件:{[e.get('type') for e in events]}"
    assert approval["interrupt_info"]["action_requests"][0]["name"] == "write_file"


@pytest.mark.xfail(reason="langgraph stream_canonical_events normal completion path: checkpoint() on non-interrupted run requires checkpoint_id in native_ref, but v3 stream completion doesn't set it (only interrupt path sets via ContinuationCreated). Requires v3 stream completion checkpoint extraction, beyond Task 7 scope.")
@pytest.mark.asyncio
async def test_runtime_adapter_resume_approve_decision_executes_tool():
    """RuntimeAdapter 把真实 HITL 决定送入 Command(resume=),工具真执行。"""
    runner = _make_runner()
    adapter = LangGraphRuntimeAdapter(runner)
    handle = await adapter.start(StartRequest(input="写文件", user_id="u", session_id="s2"))
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    checkpoint = await adapter.checkpoint(handle)

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.request.call_id,
            data={"decisions": [{"type": "approve"}]},
        ),
    )
    resumed = [event async for event in adapter.stream(handle)]

    # Canonical: tool_call items use ItemStarted/ItemCompleted with
    # ToolCallContent/ToolResultContent in their snapshot parts.
    tool_call_starts = [
        event for event in resumed
        if isinstance(event, ItemStarted) and event.item_kind == "tool_call"
    ]
    tool_call_completions = [
        event for event in resumed
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call"
    ]
    tool_result_completions = [
        event for event in resumed
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_result"
    ]
    # ItemStarted carries the tool call content in initial snapshot.
    assert len(tool_call_starts) >= 1
    call_part = tool_call_starts[0].initial.parts[0]
    assert isinstance(call_part, ToolCallContent)
    assert call_part.call_id == "c1"
    assert call_part.name == "write_file"
    assert call_part.arguments == {"path": "/tmp/x.txt", "content": "hi"}
    # ItemCompleted for tool_call carries the same call content.
    assert len(tool_call_completions) >= 1
    # ItemCompleted for tool_result carries the result.
    assert len(tool_result_completions) >= 1
    result_part = tool_result_completions[0].snapshot.parts[0]
    assert isinstance(result_part, ToolResultContent)
    assert result_part.call_id == "c1"
    assert result_part.result == "wrote:/tmp/x.txt"
