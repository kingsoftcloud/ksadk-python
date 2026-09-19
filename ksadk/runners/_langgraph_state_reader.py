"""LangGraph 图状态读取的唯一入口。

审批探测（goal-18）、checkpoint 元数据、usage 提取、可恢复性校验共用同一套
读取与 interrupt 解析逻辑；除本模块外不得直接调用 ``aget_state``/``get_state``。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_MAX_NESTED_DEPTH = 4


def graph_state_reader(agent: Any) -> Any | None:
    """返回该图的 state 读取函数（async 优先），没有则 None。"""
    return getattr(agent, "aget_state", None) or getattr(agent, "get_state", None)


async def read_graph_state(
    agent: Any,
    config: Mapping[str, Any],
    *,
    include_subgraphs: bool = False,
) -> tuple[Any | None, Exception | None]:
    """统一的图状态读取入口。

    返回 ``(state, error)``；读取失败时 ``error`` 非 None。调用方按语义决定后续：
    usage / metadata / 可恢复性这类旁路读取可以降级；审批判定必须显性失败。
    任何情况下都不静默吞掉异常——至少留一条 warning 日志。

    ``include_subgraphs=True`` 会把子图状态一并取回，供 interrupt 探测遍历嵌套暂停点。
    """
    reader = graph_state_reader(agent)
    if reader is None:
        return None, None
    try:
        if include_subgraphs:
            try:
                state = reader(config, subgraphs=True)
            except TypeError:
                # 老版本 graph 不接受 subgraphs 关键字。
                state = reader(config)
        else:
            state = reader(config)
        if inspect.isawaitable(state):
            state = await state
    except Exception as exc:
        logger.warning("LangGraph state read failed: %r", exc)
        return None, exc
    return state, None


def interrupt_info_from_state(state: Any, *, _depth: int = 0) -> dict:
    """从 state 中获取 interrupt 信息（含子图嵌套任务）"""
    if state is None or _depth > _MAX_NESTED_DEPTH:
        return {}
    tasks = getattr(state, "tasks", None)
    for task in tasks or ():
        interrupts = getattr(task, "interrupts", None)
        for intr in interrupts or ():
            if hasattr(intr, "value"):
                return _interrupt_info_from_value(intr)
        # subgraphs=True 时子图快照挂在 task.state 上，递归下钻。
        nested = getattr(task, "state", None)
        if nested is not None:
            info = interrupt_info_from_state(nested, _depth=_depth + 1)
            if info:
                return info
    return {}


def _interrupt_info_from_value(intr: Any) -> dict:
    value = intr.value
    info = dict(value) if isinstance(value, Mapping) else {"value": value}
    interrupt_id = str(getattr(intr, "id", "") or "")
    if interrupt_id:
        info.setdefault("approval_request_id", interrupt_id)
    # HumanInTheLoopMiddleware 的 interrupt value 把 tool_name/arguments 嵌在
    # action_requests[0] 里；下游期望顶层字段，这里提取并保留原 action_requests。
    action_requests = info.get("action_requests")
    if isinstance(action_requests, list) and action_requests:
        first = action_requests[0]
        if isinstance(first, Mapping):
            info.setdefault("tool_name", str(first.get("name") or ""))
            raw_args = first.get("args") or first.get("arguments")
            if raw_args is not None:
                info.setdefault("arguments", raw_args)
            if first.get("description") is not None:
                info.setdefault("description", str(first.get("description")))
    return info


def known_subgraph_namespaces(agent: Any) -> set[str]:
    """当前图里真实存在的子图 namespace 集合（无法探测时返回空集）。"""
    get_subgraphs = getattr(agent, "get_subgraphs", None)
    if not callable(get_subgraphs):
        return set()
    try:
        return {str(ns) for ns, _ in get_subgraphs()}
    except Exception:
        return set()
