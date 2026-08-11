"""Deterministic evaluators for normalized target results."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema import validate as validate_json

from .contracts import (
    AssertionSpec,
    AssertionType,
    EvalCase,
    MetricResult,
    MetricStatus,
    TargetRun,
)

Evaluator = Callable[[EvalCase, TargetRun], list[MetricResult]]
DEFAULT_EVALUATORS = [
    "response_contract@v1",
    "runtime_budget@v1",
    "tool_trajectory@v1",
]


def evaluate_case(
    case: EvalCase,
    target_run: TargetRun,
    evaluator_names: list[str],
) -> list[MetricResult]:
    """Run selected evaluators in request order."""

    evaluators: dict[str, Evaluator] = {
        "response_contract@v1": evaluate_response_contract,
        "runtime_budget@v1": evaluate_runtime_budget,
        "tool_trajectory@v1": evaluate_tool_trajectory,
    }
    selected_names = evaluator_names or DEFAULT_EVALUATORS
    unknown = [name for name in selected_names if name not in evaluators]
    if unknown:
        raise ValueError(f"不支持的评估器: {', '.join(unknown)}")

    results: list[MetricResult] = []
    for name in selected_names:
        results.extend(evaluators[name](case, target_run))
    return results


def evaluate_response_contract(case: EvalCase, target_run: TargetRun) -> list[MetricResult]:
    assertions = [
        assertion
        for assertion in case.assertions
        if assertion.type
        in {
            AssertionType.RESPONSE_EQUALS,
            AssertionType.RESPONSE_CONTAINS,
            AssertionType.RESPONSE_NOT_CONTAINS,
            AssertionType.RESPONSE_JSON_SCHEMA,
        }
    ]
    return [_response_metric(assertion, target_run.output) for assertion in assertions]


def evaluate_runtime_budget(case: EvalCase, target_run: TargetRun) -> list[MetricResult]:
    assertions = [
        assertion
        for assertion in case.assertions
        if assertion.type.value.startswith("runtime.")
    ]
    return [_runtime_metric(assertion, target_run) for assertion in assertions]


def evaluate_tool_trajectory(case: EvalCase, target_run: TargetRun) -> list[MetricResult]:
    assertions = [
        assertion
        for assertion in case.assertions
        if assertion.type in {AssertionType.TOOL_CALLED, AssertionType.TOOL_NOT_CALLED}
    ]
    expected_tools = [tool for turn in case.turns for tool in turn.expected_tools]
    if not assertions and not expected_tools:
        return []

    requirements: list[tuple[str, bool]] = [
        (assertion.type.value, assertion.required) for assertion in assertions
    ]
    requirements.extend(("tool.expected", True) for _ in expected_tools)
    return [
        MetricResult(
            name="tool_trajectory",
            status=MetricStatus.UNAVAILABLE,
            required=required,
            evidence={
                "assertion": assertion_type,
                "reason": "A2A target 未提供标准化工具轨迹",
            },
        )
        for assertion_type, required in requirements
    ]


def _response_metric(assertion: AssertionSpec, output: str) -> MetricResult:
    reason = ""
    if assertion.type is AssertionType.RESPONSE_EQUALS:
        passed = output == assertion.value
    elif assertion.type is AssertionType.RESPONSE_CONTAINS:
        passed = assertion.value in output
    elif assertion.type is AssertionType.RESPONSE_NOT_CONTAINS:
        passed = assertion.value not in output
    else:
        try:
            validate_json(json.loads(output), assertion.value)
            passed = True
        except (json.JSONDecodeError, JSONSchemaValidationError) as exc:
            passed = False
            reason = str(exc)

    return _metric(
        "response_contract",
        assertion,
        passed=passed,
        reason=reason,
    )


def _runtime_metric(assertion: AssertionSpec, target_run: TargetRun) -> MetricResult:
    actual: int | None
    if assertion.type is AssertionType.RUNTIME_MAX_LATENCY_MS:
        actual = target_run.duration_ms
    elif not target_run.usage.reported:
        actual = None
    elif assertion.type is AssertionType.RUNTIME_MAX_INPUT_TOKENS:
        actual = target_run.usage.input_tokens
    else:
        actual = target_run.usage.output_tokens

    if actual is None:
        return MetricResult(
            name="runtime_budget",
            status=MetricStatus.UNAVAILABLE,
            required=assertion.required,
            evidence={
                "assertion": assertion.type.value,
                "reason": "Target 未提供该运行指标",
            },
        )
    return _metric(
        "runtime_budget",
        assertion,
        passed=actual <= assertion.value,
        evidence={"actual": actual},
    )


def _metric(
    name: str,
    assertion: AssertionSpec,
    *,
    passed: bool,
    reason: str = "",
    evidence: dict[str, Any] | None = None,
) -> MetricResult:
    details = {"assertion": assertion.type.value, **(evidence or {})}
    if reason:
        details["reason"] = reason
    return MetricResult(
        name=name,
        status=MetricStatus.PASS if passed else MetricStatus.FAIL,
        score=1.0 if passed else 0.0,
        required=assertion.required,
        evidence=details,
    )
