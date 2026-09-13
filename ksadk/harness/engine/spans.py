"""节点执行区间事件（收口 5）：node.started/completed 成对包裹节点实现。"""

from __future__ import annotations

import inspect
import time
from typing import Any, Callable

from ksadk.harness.events import EventType


def wrap_node_span(engine: Any, run: Any, node_name: str, impl: Callable) -> Callable:
    """包一层节点实现：进入发 node.started，退出发 node.completed（含耗时）。"""

    async def wrapped(state: Any) -> Any:
        started = time.monotonic()
        run.events.append(engine._event(run, EventType.NODE_STARTED, {"node": node_name}))
        result = impl(state)
        if inspect.isawaitable(result):
            result = await result
        duration_ms = int((time.monotonic() - started) * 1000)
        run.events.append(
            engine._event(
                run,
                EventType.NODE_COMPLETED,
                {"node": node_name, "duration_ms": duration_ms},
            )
        )
        return result

    return wrapped


__all__ = ["wrap_node_span"]
