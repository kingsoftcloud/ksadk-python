"""Checkpointable budget accounting shared by a Run and its child executions."""

from __future__ import annotations

from typing import Any


def limit(run: Any, kind: str) -> int | None:
    key = {
        "tokens": "max_total_tokens",
        "tools": "max_tool_calls",
        "models": "max_model_calls",
        "artifacts": "max_artifacts",
    }[kind]
    values = [run.compiled.spec.execution_strategy.config.get(key)]
    if run.controller is not None:
        values.append(getattr(run.controller.spec.limits, key, None))
    limits = [int(value) for value in values if value is not None]
    return min(limits) if limits else None


def remaining(run: Any, kind: str) -> int | None:
    value = limit(run, kind)
    return None if value is None else max(0, value - run.budget_usage.get(kind, 0))


def check(run: Any, kind: str, cost: int = 1) -> None:
    from ksadk.harness.subagent import SubAgentExecutionError

    value = limit(run, kind)
    if value is not None and run.budget_usage.get(kind, 0) + cost > value:
        raise SubAgentExecutionError("budget_exhausted", f"{kind} budget {value} exhausted")
    if run.budget_parent is not None:
        check(run.budget_parent, kind, cost)


def spend(run: Any, kind: str, cost: int = 1) -> None:
    run.budget_usage[kind] = run.budget_usage.get(kind, 0) + cost
    if run.controller is not None and kind in {"tools", "models"}:
        attribute = "tool_calls" if kind == "tools" else "model_calls"
        setattr(
            run.controller,
            attribute,
            max(getattr(run.controller, attribute), run.budget_usage[kind]),
        )
    if run.budget_parent is not None:
        parent = run.budget_parent
        usage = parent.child_budget_usage.setdefault(run.handle.run_id, {})
        usage[kind] = usage.get(kind, 0) + cost
        spend(parent, kind, cost)


def adopt_child(parent: Any, child: Any) -> None:
    """Rehydrate consumption omitted by an interrupted parent tool node."""
    accounted = parent.child_budget_usage.setdefault(child.handle.run_id, {})
    for kind, cost in child.budget_usage.items():
        delta = max(0, cost - accounted.get(kind, 0))
        if delta:
            spend(parent, kind, delta)
            accounted[kind] = cost
    child.budget_parent = parent


def snapshot(run: Any) -> dict[str, Any]:
    return {
        "elapsed_seconds": run.execution_elapsed_seconds,
        "artifact_refs": list(run.artifact_refs),
        "usage": dict(run.budget_usage),
        "children": {key: dict(value) for key, value in run.child_budget_usage.items()},
        "tool_calls": sorted(run.budget_tool_calls),
    }


def restore(run: Any, value: dict[str, Any]) -> None:
    run.execution_elapsed_seconds = max(
        run.execution_elapsed_seconds, float(value.get("elapsed_seconds") or 0)
    )
    run.artifact_refs = list(dict.fromkeys([*run.artifact_refs, *value.get("artifact_refs", ())]))
    for kind, cost in value.get("usage", {}).items():
        run.budget_usage[kind] = max(run.budget_usage.get(kind, 0), int(cost))
    for key, usage in value.get("children", {}).items():
        current = run.child_budget_usage.setdefault(key, {})
        for kind, cost in usage.items():
            current[kind] = max(current.get(kind, 0), int(cost))
    run.budget_tool_calls.update(value.get("tool_calls", ()))
    if run.controller is not None:
        for kind, attribute in (
            ("tools", "tool_calls"),
            ("models", "model_calls"),
            ("tokens", "total_tokens"),
        ):
            setattr(
                run.controller,
                attribute,
                max(getattr(run.controller, attribute), run.budget_usage.get(kind, 0)),
            )
