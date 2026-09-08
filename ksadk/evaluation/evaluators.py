"""Deterministic evaluators for normalized target results."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema import validate as validate_json

from .contracts import (
    AssertionSpec,
    AssertionType,
    DataPolicy,
    EvalCase,
    EvaluationConfig,
    MetricResult,
    MetricStatus,
    TargetRun,
)


@dataclass(frozen=True)
class _EvaluationContext:
    """Inputs shared by every evaluator for one target invocation."""

    case: EvalCase
    target_run: TargetRun
    config: EvaluationConfig


EvaluatorStrategy = Callable[[_EvaluationContext], list[MetricResult]]


@dataclass(frozen=True)
class _EvaluatorDefinition:
    """Stable evaluator metadata and its stateless scoring strategy."""

    evaluator_id: str
    metric_name: str
    strategy: EvaluatorStrategy


REFERENCE_MATCH_EVALUATOR = "reference_match@v1"
LLM_JUDGE_EVALUATOR = "llm_judge@v1"
BUSINESS_STANDARD_EVALUATOR = "business_standard@v1"
LEGACY_ASSERTION_EVALUATORS = (
    "response_contract@v1",
    "runtime_budget@v1",
    "tool_trajectory@v1",
)
# Kept for external imports only. Empty evaluator selection uses the
# data-derived automatic plan in _automatic_evaluator_names instead.
DEFAULT_EVALUATORS = LEGACY_ASSERTION_EVALUATORS
_RESPONSE_MATCH_THRESHOLD = 0.8
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def evaluate_case(
    case: EvalCase,
    target_run: TargetRun,
    evaluator_names: list[str],
    config: EvaluationConfig | None = None,
) -> list[MetricResult]:
    """Run selected evaluators in request order."""

    context = _EvaluationContext(case, target_run, config or EvaluationConfig())
    evaluators = _resolve_evaluators(evaluator_names, context)
    return _run_evaluators(context, evaluators)


async def evaluate_case_async(
    case: EvalCase,
    target_run: TargetRun,
    evaluator_names: list[str],
    config: EvaluationConfig,
) -> list[MetricResult]:
    """Run evaluators without blocking the target invocation event loop."""

    return await asyncio.to_thread(evaluate_case, case, target_run, evaluator_names, config)


def _resolve_evaluators(
    evaluator_names: list[str], context: _EvaluationContext
) -> list[_EvaluatorDefinition]:
    selected_names = evaluator_names or _automatic_evaluator_names(context.case, context.config)
    unsupported_names = [name for name in selected_names if name not in _EVALUATOR_REGISTRY]
    if unsupported_names:
        raise ValueError(f"不支持的评估器: {', '.join(unsupported_names)}")
    return [_EVALUATOR_REGISTRY[name] for name in selected_names]


def resolve_evaluator_plan(
    cases: list[EvalCase],
    evaluator_names: list[str],
    config: EvaluationConfig,
) -> list[str]:
    """Return the explicit or data-derived evaluator plan for an EvalSet."""

    if evaluator_names:
        unsupported_names = [name for name in evaluator_names if name not in _EVALUATOR_REGISTRY]
        if unsupported_names:
            raise ValueError(f"不支持的评估器: {', '.join(unsupported_names)}")
        return list(evaluator_names)

    plan: list[str] = []
    for case in cases:
        for evaluator_name in _automatic_evaluator_names(case, config):
            if evaluator_name not in plan:
                plan.append(evaluator_name)
    return plan


def _automatic_evaluator_names(case: EvalCase, config: EvaluationConfig) -> list[str]:
    """Select only the evaluators whose business standard is present in a Case."""

    names: list[str] = []
    if _response_assertions(case):
        names.append("response_contract@v1")
    if _runtime_assertions(case):
        names.append("runtime_budget@v1")
    if _tool_requirements(case):
        names.append("tool_trajectory@v1")

    if _final_expected_output(case):
        if _judge_unavailable_reason(config) is None:
            names.insert(0, LLM_JUDGE_EVALUATOR)
        else:
            names.insert(0, REFERENCE_MATCH_EVALUATOR)
    elif not _response_assertions(case):
        names.insert(0, BUSINESS_STANDARD_EVALUATOR)
    return names


def _run_evaluators(
    context: _EvaluationContext,
    evaluators: list[_EvaluatorDefinition],
) -> list[MetricResult]:
    metrics: list[MetricResult] = []
    for evaluator in evaluators:
        metrics.extend(_run_evaluator(evaluator, context))
    return metrics


def _run_evaluator(
    evaluator: _EvaluatorDefinition,
    context: _EvaluationContext,
) -> list[MetricResult]:
    try:
        return evaluator.strategy(context)
    except Exception as exc:
        return [_evaluator_error_metric(evaluator, exc)]


def _evaluate_reference_match(context: _EvaluationContext) -> list[MetricResult]:
    """Score the final response against the case's reference output."""

    expected_output = _final_expected_output(context.case)
    if not expected_output:
        return []

    score = _rouge_1_f1(context.target_run.output, expected_output)
    return [
        _score_metric(
            name="response_match",
            score=score,
            evidence={
                "evaluator": REFERENCE_MATCH_EVALUATOR,
                "method": "rouge_1_f1",
                "threshold": _RESPONSE_MATCH_THRESHOLD,
            },
        )
    ]


def _evaluate_llm_judge(
    context: _EvaluationContext,
) -> list[MetricResult]:
    """Use the configured LLM Judge to score a final response."""

    expected_output = _final_expected_output(context.case)
    if not expected_output:
        return []

    unavailable_reason = _judge_unavailable_reason(context.config)
    if unavailable_reason:
        return [_judge_unavailable_metric(unavailable_reason)]

    try:
        score = _judge_score(
            input_text=context.case.input,
            actual_output=context.target_run.output,
            expected_output=expected_output,
            config=context.config,
        )
    except ImportError:
        return [_judge_unavailable_metric("未安装 ksadk[judge]")]
    except Exception as exc:
        return [_judge_error_metric(exc)]

    return [_judge_score_metric(score, context.config.judge_model)]


def _evaluate_business_standard(context: _EvaluationContext) -> list[MetricResult]:
    """Prevent execution-only Cases from being reported as business-quality passes."""

    return [
        MetricResult(
            name="response_quality",
            status=MetricStatus.UNAVAILABLE,
            evidence={
                "evaluator": BUSINESS_STANDARD_EVALUATOR,
                "reason": "Case 未提供响应业务标准",
            },
        )
    ]


def _evaluate_response_contract(
    context: _EvaluationContext,
) -> list[MetricResult]:
    """Evaluate response assertions against the normalized target output."""

    return [
        _response_metric(assertion, context.target_run.output)
        for assertion in _response_assertions(context.case)
    ]


def _evaluate_runtime_budget(
    context: _EvaluationContext,
) -> list[MetricResult]:
    """Evaluate runtime assertions against adapter-reported measurements."""

    return [
        _runtime_metric(assertion, context.target_run)
        for assertion in _runtime_assertions(context.case)
    ]


def _evaluate_tool_trajectory(
    context: _EvaluationContext,
) -> list[MetricResult]:
    """Evaluate tool requirements against normalized RuntimeEvent evidence."""

    requirements = _tool_requirements(context.case)
    if not requirements:
        return []

    if context.target_run.trace_ref is None:
        return [
            MetricResult(
                name="tool_trajectory",
                status=MetricStatus.UNAVAILABLE,
                required=required,
                evidence={
                    "assertion": assertion_type,
                    "tool": expected,
                    "reason": "Target 未提供可查询的标准化工具轨迹",
                },
            )
            for assertion_type, expected, required in requirements
        ]

    results: list[MetricResult] = []
    for assertion_type, expected, required in requirements:
        results.append(
            _tool_metric(
                assertion_type,
                expected,
                required,
                context.target_run.tool_calls,
            )
        )
    return results


def _tool_metric(
    assertion_type: str,
    expected: str | list[str],
    required: bool,
    tool_calls: list[Any],
) -> MetricResult:
    if assertion_type == AssertionType.TOOL_SEQUENCE.value:
        expected_sequence = list(expected) if isinstance(expected, list) else []
        ordered_calls = sorted(
            (call for call in tool_calls if call.status == "SUCCEEDED"),
            key=lambda call: call.seq_start if call.seq_start is not None else float("inf"),
        )
        actual_sequence = [call.name for call in ordered_calls]
        matched_calls = _ordered_tool_subsequence(ordered_calls, expected_sequence)
        passed = len(matched_calls) == len(expected_sequence)
        return MetricResult(
            name="tool_trajectory",
            status=MetricStatus.PASS if passed else MetricStatus.FAIL,
            score=1.0 if passed else 0.0,
            required=required,
            evidence={
                "assertion": assertion_type,
                "expectedSequence": expected_sequence,
                "actualSequence": actual_sequence,
                "matchedCallIds": matched_calls,
            },
        )

    tool_name = str(expected)
    matched = [call for call in tool_calls if call.name == tool_name]
    if assertion_type == AssertionType.TOOL_NOT_CALLED.value:
        passed = not matched
        matched_ids = [call.call_id for call in matched]
    elif assertion_type == AssertionType.TOOL_SUCCEEDED.value:
        matched_ids = [call.call_id for call in matched if call.status == "SUCCEEDED"]
        passed = bool(matched_ids)
    else:
        matched_ids = [call.call_id for call in matched]
        passed = bool(matched_ids)
    return MetricResult(
        name="tool_trajectory",
        status=MetricStatus.PASS if passed else MetricStatus.FAIL,
        score=1.0 if passed else 0.0,
        required=required,
        evidence={
            "assertion": assertion_type,
            "tool": tool_name,
            "matchedCallIds": matched_ids,
        },
    )


def _ordered_tool_subsequence(tool_calls: list[Any], expected_sequence: list[str]) -> list[str]:
    matched_call_ids: list[str] = []
    expected_index = 0
    for call in tool_calls:
        if expected_index == len(expected_sequence):
            break
        if call.name == expected_sequence[expected_index]:
            matched_call_ids.append(call.call_id)
            expected_index += 1
    return matched_call_ids


def _response_assertions(case: EvalCase) -> list[AssertionSpec]:
    response_types = {
        AssertionType.RESPONSE_EQUALS,
        AssertionType.RESPONSE_CONTAINS,
        AssertionType.RESPONSE_NOT_CONTAINS,
        AssertionType.RESPONSE_JSON_SCHEMA,
    }
    return [assertion for assertion in case.assertions if assertion.type in response_types]


def _runtime_assertions(case: EvalCase) -> list[AssertionSpec]:
    return [
        assertion for assertion in case.assertions if assertion.type.value.startswith("runtime.")
    ]


def _tool_requirements(case: EvalCase) -> list[tuple[str, str | list[str], bool]]:
    requirements = [
        (
            assertion.type.value,
            assertion.value,
            assertion.required,
        )
        for assertion in case.assertions
        if assertion.type
        in {
            AssertionType.TOOL_CALLED,
            AssertionType.TOOL_NOT_CALLED,
            AssertionType.TOOL_SUCCEEDED,
            AssertionType.TOOL_SEQUENCE,
        }
    ]
    requirements.extend(
        ("tool.expected", str(tool.get("name") or ""), True)
        for turn in case.turns
        for tool in turn.expected_tools
        if str(tool.get("name") or "").strip()
    )
    return requirements


def evaluate_tool_trajectory(case: EvalCase, target_run: TargetRun) -> list[MetricResult]:
    """Compatibility entry point for direct deterministic tool evaluation."""

    return _evaluate_tool_trajectory(_EvaluationContext(case, target_run, EvaluationConfig()))


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
    elif assertion.type is AssertionType.RUNTIME_MAX_TOTAL_TOKENS:
        actual = target_run.usage.total_tokens
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


def _evaluator_error_metric(
    evaluator: _EvaluatorDefinition,
    error: Exception,
) -> MetricResult:
    return MetricResult(
        name=evaluator.metric_name,
        status=MetricStatus.ERROR,
        evidence={
            "evaluator": evaluator.evaluator_id,
            "reason": "评估器执行失败",
            "errorType": type(error).__name__,
        },
    )


def _score_metric(
    *,
    name: str,
    score: float,
    evidence: dict[str, Any],
) -> MetricResult:
    return MetricResult(
        name=name,
        status=MetricStatus.PASS if score >= _RESPONSE_MATCH_THRESHOLD else MetricStatus.FAIL,
        score=score,
        evidence=evidence,
    )


def _final_expected_output(case: EvalCase) -> str | None:
    return case.turns[-1].expected_output


def _judge_unavailable_reason(config: EvaluationConfig) -> str | None:
    if config.data_policy is not DataPolicy.FULL_TRACE:
        return "Judge 需要 dataPolicy=full_trace"
    if not config.judge_model:
        return "未配置 judgeModel"
    if not config.judge_api_base:
        return "未配置 judgeApiBase"
    if not os.getenv(config.judge_api_key_env):
        return f"未设置 Judge 密钥环境变量: {config.judge_api_key_env}"
    return None


def _judge_score(
    *,
    input_text: str,
    actual_output: str,
    expected_output: str,
    config: EvaluationConfig,
) -> float:
    return _run_llm_judge(
        input_text=input_text,
        actual_output=actual_output,
        expected_output=expected_output,
        model=config.judge_model or "",
        api_base=config.judge_api_base or "",
        api_key=os.environ[config.judge_api_key_env],
    )


def _judge_unavailable_metric(reason: str) -> MetricResult:
    return MetricResult(
        name="response_quality",
        status=MetricStatus.UNAVAILABLE,
        evidence={"evaluator": LLM_JUDGE_EVALUATOR, "reason": reason},
    )


def _judge_error_metric(error: Exception) -> MetricResult:
    return MetricResult(
        name="response_quality",
        status=MetricStatus.ERROR,
        evidence={
            "evaluator": LLM_JUDGE_EVALUATOR,
            "reason": "Judge 执行失败",
            "errorType": type(error).__name__,
        },
    )


def _judge_score_metric(score: float, judge_model: str | None) -> MetricResult:
    return _score_metric(
        name="response_quality",
        score=score,
        evidence={
            "evaluator": LLM_JUDGE_EVALUATOR,
            "threshold": _RESPONSE_MATCH_THRESHOLD,
            "judgeModel": judge_model,
        },
    )


def _rouge_1_f1(actual: str, expected: str) -> float:
    actual_tokens = Counter(_TOKEN_PATTERN.findall(actual.lower()))
    expected_tokens = Counter(_TOKEN_PATTERN.findall(expected.lower()))
    if not actual_tokens or not expected_tokens:
        return float(actual.strip() == expected.strip())

    overlap = sum((actual_tokens & expected_tokens).values())
    precision = overlap / sum(actual_tokens.values())
    recall = overlap / sum(expected_tokens.values())
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _run_llm_judge(
    *,
    input_text: str,
    actual_output: str,
    expected_output: str,
    model: str,
    api_base: str,
    api_key: str,
) -> float:
    """Run the minimal external Judge configuration."""

    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    from deepeval.metrics import GEval
    from deepeval.models import LocalModel
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    judge = LocalModel(model=model, base_url=api_base, api_key=api_key)
    metric = GEval(
        name="Response quality",
        criteria=(
            "Determine whether the actual output is factually correct based on the expected output."
        ),
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT,
        ],
        threshold=_RESPONSE_MATCH_THRESHOLD,
        model=judge,
    )
    metric.measure(
        LLMTestCase(
            input=input_text,
            actual_output=actual_output,
            expected_output=expected_output,
        )
    )
    return float(metric.score)


_EVALUATORS = (
    _EvaluatorDefinition(
        BUSINESS_STANDARD_EVALUATOR, "response_quality", _evaluate_business_standard
    ),
    _EvaluatorDefinition("response_contract@v1", "response_contract", _evaluate_response_contract),
    _EvaluatorDefinition("runtime_budget@v1", "runtime_budget", _evaluate_runtime_budget),
    _EvaluatorDefinition("tool_trajectory@v1", "tool_trajectory", _evaluate_tool_trajectory),
    _EvaluatorDefinition(REFERENCE_MATCH_EVALUATOR, "response_match", _evaluate_reference_match),
    _EvaluatorDefinition(LLM_JUDGE_EVALUATOR, "response_quality", _evaluate_llm_judge),
)
_EVALUATOR_REGISTRY = {evaluator.evaluator_id: evaluator for evaluator in _EVALUATORS}
SUPPORTED_EVALUATORS = tuple(evaluator.evaluator_id for evaluator in _EVALUATORS)
