"""Single public evaluation handoff shared by CLI and Studio."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

from .adapters import EvaluationNotImplementedError, TargetAdapter
from .contracts import (
    CaseRun,
    EvalRunReport,
    EvalRunSpec,
    EvalRunStatus,
    EvaluationRequest,
    MetricStatus,
    TargetRunStatus,
)
from .evaluators import evaluate_case_async
from .evidence import EvidenceStore
from .storage import EvaluationStorage, EvaluationStorageError
from .target import EvaluationExecutionError, EvaluationTarget

__all__ = [
    "EvaluationExecutionError",
    "EvaluationNotImplementedError",
    "TargetAdapter",
    "execute_evaluation",
]


async def execute_evaluation(
    request: EvaluationRequest,
    *,
    on_case_started: Callable[[str, int, int], None] | None = None,
    adapter: TargetAdapter | None = None,
    run_id: str | None = None,
) -> EvalRunReport:
    """Execute and persist one evaluation request."""

    evidence_store = EvidenceStore(request.report_dir) if request.report_dir else None
    target = EvaluationTarget(
        request.target,
        request.config,
        evidence_store=evidence_store,
        adapter=adapter,
    )
    snapshot = await target.snapshot()
    spec = EvalRunSpec(
        id=run_id or f"eval_{uuid4().hex}",
        evalset=request.evalset,
        target=snapshot,
        config=request.config,
        cloud_dataset=request.cloud_dataset,
    )
    case_runs: list[CaseRun] = []
    try:
        await _run_cases(
            target,
            spec,
            case_runs=case_runs,
            on_case_started=on_case_started,
        )
    except asyncio.CancelledError:
        report = EvalRunReport(
            spec=spec,
            status=EvalRunStatus.CANCELLED,
            case_runs=case_runs,
        )
        _persist_report(request, report)
        raise
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
    *,
    case_runs: list[CaseRun],
    on_case_started: Callable[[str, int, int], None] | None,
) -> None:
    total_cases = len(spec.evalset.cases)
    for index, case in enumerate(spec.evalset.cases, start=1):
        _notify_case_started(on_case_started, case.id, index, total_cases)
        target_run = await target.run_case(spec, case)
        try:
            metrics = await evaluate_case_async(
                case, target_run, spec.config.evaluators, spec.config
            )
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


def _notify_case_started(
    callback: Callable[[str, int, int], None] | None,
    case_id: str,
    index: int,
    total_cases: int,
) -> None:
    if callback is None:
        return
    try:
        callback(case_id, index, total_cases)
    except Exception:
        pass


def _report_status(case_runs: list[CaseRun]) -> EvalRunStatus:
    if any(
        case.target_run.status is TargetRunStatus.ERROR
        or any(metric.required and metric.status is MetricStatus.ERROR for metric in case.metrics)
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
