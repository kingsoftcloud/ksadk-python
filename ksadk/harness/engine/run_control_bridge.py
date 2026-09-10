"""Managed engine integration helpers for the optional long-task controller."""

from __future__ import annotations

import asyncio
from typing import Any

from ksadk.harness.events import EventType
from ksadk.harness.run_control import RunController, RunControlStop, run_control_spec_from_config
from ksadk.harness.state import RunStatus


def new_run_controller(compiled: Any) -> RunController | None:
    spec = run_control_spec_from_config(compiled.spec.execution_strategy.config)
    return RunController(spec) if spec is not None else None


async def invoke_graph_with_control(
    graph: Any,
    invoke_input: Any,
    config: dict[str, Any],
    controller: RunController | None,
) -> Any:
    """Invoke a graph under the remaining wall-clock budget."""

    if controller is None or controller.spec.limits.max_wall_seconds is None:
        return await graph.ainvoke(invoke_input, config=config)
    wall_limit = controller.spec.limits.max_wall_seconds
    elapsed = float(controller.snapshot()["elapsed_seconds"])
    remaining = wall_limit - elapsed
    if remaining <= 0:
        controller.before_model()
        raise RunControlStop(controller.stop_reason or "wall_time_hard_limit")
    try:
        async with asyncio.timeout(remaining):
            return await graph.ainvoke(invoke_input, config=config)
    except TimeoutError as exc:
        controller.stop_reason = "wall_time_hard_limit"
        controller.budget_exhausted = True
        raise RunControlStop("wall_time_hard_limit") from exc


def completion_payload(run: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "completed"}
    if run.controller is not None:
        payload.update(run.controller.finalize(run.control_events).to_payload())
    return payload


def append_controlled_stop(engine: Any, run: Any, reason: str) -> None:
    """Finish a deterministic controller stop without reporting generic failure."""

    run.state.status = RunStatus.COMPLETED
    run.done = True
    result = run.controller.finalize(run.control_events) if run.controller else None
    run.events.append(
        engine._event(
            run,
            EventType.AGENT_COMPLETED,
            {"agent_id": run.state.agent_id, "status": "controlled_stop"},
        )
    )
    run.events.append(
        engine._event(
            run,
            EventType.RUN_COMPLETED,
            {
                "status": "completed",
                "control_reason": reason,
                **(result.to_payload() if result is not None else {}),
            },
        )
    )


__all__ = [
    "append_controlled_stop",
    "completion_payload",
    "invoke_graph_with_control",
    "new_run_controller",
]
