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
        return await asyncio.wait_for(graph.ainvoke(invoke_input, config=config), remaining)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        controller.stop_reason = "wall_time_hard_limit"
        controller.budget_exhausted = True
        raise RunControlStop("wall_time_hard_limit") from exc


def completion_payload(run: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "completed"}
    # 会话串轮修复（交接记录 2026-09-22 §7）：把当前 turn_id 写进 RUN_COMPLETED
    # payload，让 managed_runtime._project_event 把 output_refs 指向当前轮的
    # turn 级 final item_id，而不是无 turn 分量的 run 级身份。
    turn_count = int(getattr(run.state, "turn_count", 0) or 0)
    if turn_count > 0:
        run_id = getattr(run.handle, "run_id", "") or ""
        payload["turn_id"] = f"{run_id}:t{turn_count}"
    if run.controller is not None:
        result = run.controller.finalize(run.control_events)
        payload.update(result.to_payload())
        # A model response produced only because the controller asked it to
        # close at a soft budget boundary is a useful partial delivery, not a
        # verified task completion.  Preserve that distinction for runtime
        # adapters and Studio instead of flattening it into ``completed``.
        # Explicit acceptance failures receive the same treatment even when
        # the model stopped before a budget boundary.
        if result.outcome.value != "verified" and (
            run.controller.closing_requested or run.controller.spec.acceptance
        ):
            payload["control_reason"] = (
                run.controller.closing_reason or result.reason
            )
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
