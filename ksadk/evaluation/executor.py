"""Single public evaluation handoff shared by CLI and Studio."""

from __future__ import annotations

from uuid import uuid4

from .adapters import EvaluationNotImplementedError, TargetAdapter
from .contracts import (
    CaseRun,
    EvalCase,
    EvalRunReport,
    EvalRunSpec,
    EvalRunStatus,
    EvaluationRequest,
    MetricStatus,
    TargetRunStatus,
)
from .evaluators import evaluate_case
from .storage import EvaluationStorage, EvaluationStorageError
from .target import EvaluationExecutionError, EvaluationTarget

__all__ = [
    "EvaluationExecutionError",
    "EvaluationNotImplementedError",
    "TargetAdapter",
    "execute_evaluation",
]


async def execute_evaluation(request: EvaluationRequest) -> EvalRunReport:
    """Execute and persist one evaluation request."""

    target = EvaluationTarget(request.target, request.config)
    snapshot = await target.snapshot()
    spec = EvalRunSpec(
        id=f"eval_{uuid4().hex}",
        evalset=request.evalset,
        target=snapshot,
        config=request.config,
    )
    case_runs = await _run_cases(target, spec)
    report = EvalRunReport(
        spec=spec,
        status=_report_status(case_runs),
        case_runs=case_runs,
    )
    _persist_report(request, report)
    return report


async def _run_cases(
    target: EvaluationTarget,
    spec: EvalRunSpec,
) -> list[CaseRun]:
    case_runs: list[CaseRun] = []
    for case in spec.evalset.cases:
        target_run = await target.run_case(spec, case)
        try:
            metrics = evaluate_case(case, target_run, spec.config.evaluators)
        except ValueError as exc:
            raise EvaluationExecutionError(str(exc)) from exc
        case_run = CaseRun(
            case_id=case.id,
            attempt=spec.attempt,
            target_run=target_run,
            metrics=metrics,
        )
        case_runs.append(case_run)
        if spec.config.fail_fast and not case_run.passed:
            break
    return case_runs


def _report_status(case_runs: list[CaseRun]) -> EvalRunStatus:
    if any(
        case.target_run.status is TargetRunStatus.ERROR
        or any(metric.status is MetricStatus.ERROR for metric in case.metrics)
        for case in case_runs
    ):
        return EvalRunStatus.ERROR
    if any(case.target_run.status is TargetRunStatus.CANCELLED for case in case_runs):
        return EvalRunStatus.CANCELLED
    if any(not case.passed for case in case_runs):
        return EvalRunStatus.FAILED
    return EvalRunStatus.PASSED


def _persist_report(request: EvaluationRequest, report: EvalRunReport) -> None:
    if not request.report_dir:
        return
    try:
        EvaluationStorage(request.report_dir).write_report(report)
    except EvaluationStorageError as exc:
        raise EvaluationExecutionError("评测报告写入失败") from exc
