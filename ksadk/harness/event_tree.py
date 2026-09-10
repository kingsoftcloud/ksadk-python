"""事件树（收口 5）：Run → Agent → Turn → Node → Model/Tool/Artifact/Usage。

引擎发出的事件流是一维的（seq_id 单调）；本模块把它重建成树，供 Studio
展示子 Agent、节点耗时与上下文/usage 占用：

- **Agent**：按 ``agent.started/completed`` 区间聚合（主 Agent 与未来子
  Agent 同构——不同 ``agent_id`` 的事件各自成树节点）；
- **Turn**：按 ``turn.started/completed`` 区间聚合；
- **Node**：按 ``node.started/completed`` 区间聚合（含 duration_ms）；
- **叶子**：model.call.*、tool.call.*、usage.reported、artifact.*、
  context.*、approval.*、policy.* 落入包含它的最小区间。

区间归属按 seq_id 判定（区间事件之间的叶子属于该区间）；区间事件之外的
叶子挂在 Run 根下。纯函数、无 IO。
"""

from __future__ import annotations

from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent

_AGENT_SPAN = (EventType.AGENT_STARTED, EventType.AGENT_COMPLETED)
_TURN_SPAN = (EventType.TURN_STARTED, EventType.TURN_COMPLETED)
_NODE_SPAN = (EventType.NODE_STARTED, EventType.NODE_COMPLETED)

_LEAF_TYPES = frozenset(
    {
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_COMPLETED,
        EventType.MODEL_CALL_FAILED,
        EventType.TOOL_CALL_BEGIN,
        EventType.TOOL_CALL_END,
        EventType.USAGE_REPORTED,
        EventType.ARTIFACT_CREATED,
        EventType.ARTIFACT_UPDATED,
        EventType.CONTEXT_PLANNED,
        EventType.CONTEXT_COMPACTION_STARTED,
        EventType.CONTEXT_COMPACTION_COMPLETED,
        EventType.CONTEXT_RECOVERED,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
        EventType.POLICY_DECISION,
        EventType.TEXT_COMPLETED,
        EventType.REASONING_COMPLETED,
    }
)

#: 叶子分类（树节点 kind）。
_LEAF_KINDS = {
    EventType.MODEL_CALL_STARTED: "model",
    EventType.MODEL_CALL_COMPLETED: "model",
    EventType.MODEL_CALL_FAILED: "model",
    EventType.TOOL_CALL_BEGIN: "tool",
    EventType.TOOL_CALL_END: "tool",
    EventType.USAGE_REPORTED: "usage",
    EventType.ARTIFACT_CREATED: "artifact",
    EventType.ARTIFACT_UPDATED: "artifact",
    EventType.CONTEXT_PLANNED: "context",
    EventType.CONTEXT_COMPACTION_STARTED: "context",
    EventType.CONTEXT_COMPACTION_COMPLETED: "context",
    EventType.CONTEXT_RECOVERED: "context",
    EventType.APPROVAL_REQUESTED: "approval",
    EventType.APPROVAL_RESOLVED: "approval",
    EventType.POLICY_DECISION: "policy",
    EventType.TEXT_COMPLETED: "text",
    EventType.REASONING_COMPLETED: "text",
}


def _leaf(event: RuntimeEvent) -> dict[str, Any]:
    return {
        "kind": _LEAF_KINDS.get(event.event_type, "other"),
        "event_type": event.event_type,
        "seq_id": event.seq_id,
        "payload": dict(event.payload),
    }


def _sum_usage(leaves: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for leaf in leaves:
        if leaf["event_type"] != EventType.USAGE_REPORTED:
            continue
        payload = leaf["payload"]
        for key in totals:
            totals[key] += int(payload.get(key) or 0)
    return totals


def _build_spans(
    events: list[RuntimeEvent], span: tuple[str, str]
) -> list[tuple[int, int, dict[str, Any]]]:
    """返回 [(start_seq, end_seq, meta)]；未闭合区间以 +inf 收尾。

    支持嵌套（子 Agent 区间嵌在父 Agent 区间内）：栈式配对，后开先闭。
    """
    spans: list[tuple[int, int, dict[str, Any]]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for event in sorted(events, key=lambda e: e.seq_id):
        if event.event_type == span[0]:
            stack.append((event.seq_id, dict(event.payload)))
        elif event.event_type == span[1]:
            if stack:
                open_start, open_meta = stack.pop()
                spans.append((open_start, event.seq_id, open_meta))
    for open_start, open_meta in stack:
        spans.append((open_start, float("inf"), open_meta))
    return spans


def build_event_tree(events: list[RuntimeEvent]) -> dict[str, Any]:
    """把一维事件流重建为 Run → Agent → Turn → Node → 叶子 的树。"""
    ordered = sorted(events, key=lambda e: e.seq_id)
    run_events = [e for e in ordered if e.event_type != EventType.RUN_STARTED]

    agents: list[dict[str, Any]] = []
    agent_spans = _build_spans(ordered, _AGENT_SPAN)
    for start, end, meta in agent_spans:
        span_events = [e for e in ordered if start <= e.seq_id < end]
        # 嵌套子 Agent 区间内的事件不计入父 Agent（usage 不重复记账）。
        nested = [
            (c_start, c_end)
            for c_start, c_end, c_meta in agent_spans
            if c_meta is not meta and start < c_start and c_end <= end
        ]
        own_events = [
            e
            for e in span_events
            if not any(c_start <= e.seq_id < c_end for c_start, c_end in nested)
        ]
        turn_spans = _build_spans(own_events, _TURN_SPAN)
        # 节点区间按 Agent 级构建；归属规则：node 起点落在哪个 turn 区间（或
        # 上一 turn 结束后、下一 turn 开始前的间隙——如 reason 之后的
        # tool_calls——都归其触发的那个 turn）。
        agent_node_spans = _build_spans(own_events, _NODE_SPAN)

        def _owning_turn(n_start: float) -> int:
            owner = -1
            for idx, (t_start, _t_end, _meta) in enumerate(turn_spans):
                if t_start <= n_start:
                    owner = idx
            return owner

        nodes_per_turn: list[list[dict[str, Any]]] = [[] for _ in turn_spans]
        for n_start, n_end, n_meta in agent_node_spans:
            idx = _owning_turn(n_start)
            if idx < 0:
                continue
            node_leaves = [
                _leaf(e)
                for e in own_events
                if n_start <= e.seq_id < n_end and e.event_type in _LEAF_TYPES
            ]
            nodes_per_turn[idx].append(
                {
                    "kind": "node",
                    "node": n_meta.get("node", ""),
                    "duration_ms": n_meta.get("duration_ms"),
                    "leaves": node_leaves,
                }
            )
        turns: list[dict[str, Any]] = []
        for idx, (t_start, t_end, t_meta) in enumerate(turn_spans):
            turn_events = [e for e in own_events if t_start <= e.seq_id < t_end]
            turn_leaves = [_leaf(e) for e in turn_events if e.event_type in _LEAF_TYPES]
            # 未落入任何 node 区间的叶子（如压缩摘要调用）。
            turn_root_leaves = [
                _leaf(e)
                for e in turn_events
                if e.event_type in _LEAF_TYPES
                and not any(n_start <= e.seq_id < n_end for n_start, n_end, _ in agent_node_spans)
            ]
            turns.append(
                {
                    "kind": "turn",
                    "turn_id": t_meta.get("turn_id", ""),
                    "turn_number": t_meta.get("turn_number", 0),
                    "nodes": nodes_per_turn[idx],
                    "leaves": turn_root_leaves,
                    "usage": _sum_usage(turn_leaves),
                }
            )
        agent_leaves = [_leaf(e) for e in own_events if e.event_type in _LEAF_TYPES]
        agents.append(
            {
                "kind": "agent",
                "agent_id": meta.get("agent_id", ""),
                "status": None,
                "turns": turns,
                "leaves": agent_leaves,
                "usage": _sum_usage(agent_leaves),
            }
        )
    # agent.completed 携带终态。
    for event in ordered:
        if event.event_type == EventType.AGENT_COMPLETED:
            for agent in agents:
                if agent["agent_id"] == event.payload.get("agent_id"):
                    agent["status"] = event.payload.get("status")

    root_leaves = [_leaf(e) for e in run_events if e.event_type in _LEAF_TYPES]
    return {
        "kind": "run",
        "run_id": ordered[0].invocation_id if ordered else "",
        "agents": agents,
        "leaves": root_leaves,
        "usage": _sum_usage(root_leaves),
    }


__all__ = ["build_event_tree"]
