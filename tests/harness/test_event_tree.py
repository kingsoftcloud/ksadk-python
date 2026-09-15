"""收口 5：Run → Agent → Turn → Node → Model/Tool/Artifact → Usage 事件树。

引擎发出 agent/turn/node 区间事件；build_event_tree 把一维事件流重建为树
（usage 按 Turn/Agent/Run 聚合，工具与模型调用落入包含它的最小区间）。
"""

from __future__ import annotations

import asyncio

from langgraph.checkpoint.memory import InMemorySaver

from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.event_tree import build_event_tree
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


class _Reasoner:
    def __init__(self) -> None:
        self.turns = 0

    async def complete(self, **kwargs):
        self.turns += 1
        if self.turns == 1:
            return HarnessReasoningTurn(
                tool_calls=(HarnessToolCall(call_id="tc-1", name="lookup", arguments={"q": "x"}),),
                usage={"input_tokens": 30, "output_tokens": 10},
            )
        return HarnessReasoningTurn(
            final_text="完成", usage={"input_tokens": 20, "output_tokens": 5}
        )


async def _tool(arguments):  # type: ignore[no-untyped-def]
    return "结果"


def _drive() -> list:
    engine = ManagedLangGraphEngine(
        reasoner=_Reasoner(),
        checkpointer=InMemorySaver(),
        tools={"lookup": _tool},
    )
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="助手"),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="查询",
                user_id="u",
                session_id="s",
                agent_id="main",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": "run-tree"},
            ),
            compiled,
        )
        return [e async for e in engine.stream(handle)]

    return asyncio.run(run())


def test_engine_emits_agent_turn_node_span_events():
    events = _drive()
    kinds = [e.event_type for e in events]
    assert "agent.started" in kinds and "agent.completed" in kinds
    assert kinds.count("turn.started") == 2 and kinds.count("turn.completed") == 2
    assert "node.started" in kinds and "node.completed" in kinds
    # 区间事件成对且有序（seq 单调）。
    seqs = [e.seq_id for e in events]
    assert seqs == sorted(seqs)


def test_build_event_tree_nests_turns_nodes_and_usage():
    events = _drive()
    tree = build_event_tree(events)
    assert tree["kind"] == "run" and tree["run_id"]
    agents = tree["agents"]
    assert len(agents) == 1 and agents[0]["agent_id"] == "main"
    assert agents[0]["status"] == "completed"
    turns = agents[0]["turns"]
    assert len(turns) == 2, "两个 reasoning turn"
    # turn1：reason + tool_calls 两个节点；工具调用落入 tool_calls 节点。
    turn1_nodes = {n["node"]: n for n in turns[0]["nodes"]}
    assert "reason" in turn1_nodes and "tool_calls" in turn1_nodes
    tool_leaves = [leaf for leaf in turn1_nodes["tool_calls"]["leaves"] if leaf["kind"] == "tool"]
    assert tool_leaves and tool_leaves[0]["payload"]["call_id"] == "tc-1"
    model_leaves = [leaf for leaf in turn1_nodes["reason"]["leaves"] if leaf["kind"] == "model"]
    assert model_leaves, "model.call 事件必须落入 reason 节点"
    # usage 聚合：run 总量 = 各 turn 之和。
    assert turns[0]["usage"]["input_tokens"] == 30
    assert turns[1]["usage"]["input_tokens"] == 20
    assert tree["usage"]["input_tokens"] == 50
    assert tree["usage"]["output_tokens"] == 15


def test_build_event_tree_handles_unclosed_spans_and_empty():
    from ksadk.harness.events import RuntimeEvent as RE

    def ev(seq: int, kind: str, payload: dict | None = None) -> RE:
        return RE.create(
            kind,
            agent_id="a",
            user_id="u",
            session_id="s",
            invocation_id="r",
            seq_id=seq,
            payload=payload or {},
        )

    # 未闭合区间：仍成一棵树（+inf 收尾）。
    tree = build_event_tree(
        [
            ev(1, "agent.started", {"agent_id": "a"}),
            ev(2, "turn.started", {"turn_id": "t1", "turn_number": 1}),
            ev(3, "usage.reported", {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}),
        ]
    )
    assert tree["agents"][0]["status"] is None  # 未完成
    assert tree["agents"][0]["turns"][0]["usage"]["input_tokens"] == 5
    # 空流：空树。
    assert build_event_tree([]) == {
        "kind": "run",
        "run_id": "",
        "agents": [],
        "leaves": [],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }
