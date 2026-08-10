from ksadk.evaluation.contracts import (
    CaseRun,
    EvalCase,
    EvalRunReport,
    EvalRunSpec,
    EvalSetVersion,
    DataPolicy,
    EvaluationConfig,
    MetricResult,
    TargetKind,
    TargetRun,
    TargetSnapshot,
    TraceRef,
)


def _spec() -> EvalRunSpec:
    evalset = EvalSetVersion(
        name="smoke",
        cases=[EvalCase(id="case-1", input="hello")],
    )
    return EvalRunSpec(
        id="eval-1",
        evalset=evalset,
        target=TargetSnapshot(
            kind=TargetKind.LOCAL_SOURCE,
            entrypoint="agent.py",
            revision_digest="sha256:agent",
        ),
    )


def test_evalset_digest_is_stable_and_case_accepts_legacy_input():
    first = _spec().evalset
    second = EvalSetVersion.model_validate(first.model_dump(mode="python"))
    assert first.content_digest == second.content_digest
    assert first.cases[0].input == "hello"


def test_report_models_preserve_unavailable_trace_evidence():
    report = EvalRunReport(
        spec=_spec(),
        status="FAILED",
        case_runs=[
            CaseRun(
                case_id="case-1",
                target_run=TargetRun(
                    status="PASSED",
                    trace_ref=TraceRef(run_id="run-1"),
                ),
                metrics=[
                    MetricResult(
                        name="tool_trajectory",
                        status="UNAVAILABLE",
                        evidence={"reason": "remote target"},
                    )
                ],
            )
        ],
    )
    assert report.case_runs[0].metrics[0].status == "UNAVAILABLE"
    assert report.case_runs[0].passed is False
    assert report.summary.unavailable_cases == 1
    assert EvaluationConfig().data_policy is DataPolicy.LOCAL_ONLY
