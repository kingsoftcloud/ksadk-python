from ksadk.evaluation.contracts import (
    AssertionSpec,
    AssertionType,
    EvalCase,
    MetricStatus,
    TargetRun,
    ToolCallEvidence,
    TraceRef,
    UsageSnapshot,
)
from ksadk.evaluation.evaluators import evaluate_case, evaluate_tool_trajectory


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


def test_runtime_total_tokens_enforces_the_reported_total() -> None:
    metrics = evaluate_case(
        EvalCase(
            id="token-budget",
            input="hello",
            assertions=[
                AssertionSpec(type="runtime.maxTotalTokens", value=100),
            ],
        ),
        TargetRun(
            status="PASSED",
            usage=UsageSnapshot(input_tokens=45, output_tokens=65, total_tokens=110, reported=True),
        ),
        [],
    )

    assert [(metric.name, metric.status) for metric in metrics] == [
        ("response_quality", MetricStatus.UNAVAILABLE),
        ("runtime_budget", MetricStatus.FAIL),
    ]
    assert metrics[-1].evidence["actual"] == 110


def test_tool_succeeded_rejects_an_error_call_without_changing_tool_called_v1() -> None:
    metrics = evaluate_case(
        EvalCase(
            id="tool-success",
            input="lookup",
            assertions=[
                AssertionSpec(type="tool.called", value="knowledge_search"),
                AssertionSpec(type="tool.succeeded", value="knowledge_search"),
            ],
        ),
        TargetRun(
            status="PASSED",
            trace_ref=TraceRef(run_id="run-1", invocation_id="invocation-1"),
            tool_calls=[
                ToolCallEvidence(
                    call_id="call-1",
                    name="knowledge_search",
                    status="ERROR",
                    seq_start=1,
                    seq_end=2,
                )
            ],
        ),
        [],
    )

    assert [metric.status for metric in metrics] == [
        MetricStatus.UNAVAILABLE,
        MetricStatus.PASS,
        MetricStatus.FAIL,
    ]
    assert metrics[-1].evidence["matchedCallIds"] == []


def test_tool_sequence_requires_successful_tools_in_order() -> None:
    metrics = evaluate_case(
        EvalCase(
            id="tool-sequence",
            input="research",
            assertions=[
                AssertionSpec(type="tool.sequence", value=["knowledge_search", "cite_source"]),
            ],
        ),
        TargetRun(
            status="PASSED",
            trace_ref=TraceRef(run_id="run-1", invocation_id="invocation-1"),
            tool_calls=[
                ToolCallEvidence(
                    call_id="call-1",
                    name="cite_source",
                    status="SUCCEEDED",
                    seq_start=1,
                    seq_end=2,
                ),
                ToolCallEvidence(
                    call_id="call-2",
                    name="knowledge_search",
                    status="SUCCEEDED",
                    seq_start=3,
                    seq_end=4,
                ),
            ],
        ),
        [],
    )

    assert [metric.status for metric in metrics] == [
        MetricStatus.UNAVAILABLE,
        MetricStatus.FAIL,
    ]
    assert metrics[-1].evidence["actualSequence"] == ["cite_source", "knowledge_search"]
