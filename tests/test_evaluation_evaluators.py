from ksadk.evaluation.contracts import (
    AssertionSpec,
    AssertionType,
    EvalCase,
    MetricStatus,
    TargetRun,
    ToolCallEvidence,
    TraceRef,
)
from ksadk.evaluation.evaluators import evaluate_tool_trajectory


def _case(assertion_type: AssertionType, *, required: bool = True) -> EvalCase:
    return EvalCase(
        id="case-1",
        input="hello",
        assertions=[AssertionSpec(type=assertion_type, value="lookup", required=required)],
    )


def test_tool_called_uses_standardized_runtime_evidence() -> None:
    result = evaluate_tool_trajectory(
        _case(AssertionType.TOOL_CALLED),
        TargetRun(
            status="PASSED",
            trace_ref=TraceRef(run_id="eval-1", invocation_id="invocation-1"),
            tool_calls=[
                ToolCallEvidence(
                    call_id="call-1",
                    name="lookup",
                    status="SUCCEEDED",
                    seq_start=2,
                    seq_end=3,
                )
            ],
        ),
    )

    assert result[0].status is MetricStatus.PASS
    assert result[0].evidence["matchedCallIds"] == ["call-1"]


def test_tool_not_called_passes_when_queryable_trace_has_no_match() -> None:
    result = evaluate_tool_trajectory(
        _case(AssertionType.TOOL_NOT_CALLED),
        TargetRun(
            status="PASSED",
            trace_ref=TraceRef(run_id="eval-1", invocation_id="invocation-1"),
        ),
    )

    assert result[0].status is MetricStatus.PASS


def test_tool_metric_is_unavailable_without_queryable_trace() -> None:
    result = evaluate_tool_trajectory(
        _case(AssertionType.TOOL_CALLED, required=False),
        TargetRun(status="PASSED"),
    )

    assert result[0].status is MetricStatus.UNAVAILABLE
    assert result[0].required is False
