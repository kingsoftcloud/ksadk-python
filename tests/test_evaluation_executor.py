import pytest

from ksadk.evaluation import (
    EvalCase,
    EvalSetVersion,
    EvaluationConfig,
    EvaluationRequest,
    TargetKind,
    TargetRef,
    TargetRun,
    TargetSnapshot,
    execute_evaluation,
)


@pytest.mark.asyncio
async def test_case_started_callback_is_best_effort(monkeypatch):
    class FakeTarget:
        def __init__(self, *_args, **_kwargs):
            pass

        async def snapshot(self):
            return TargetSnapshot(
                kind=TargetKind.A2A,
                entrypoint="https://agent.example.test",
                revision_digest="sha256:agent",
            )

        async def run_case(self, _spec, _case):
            return TargetRun(status="PASSED", output="done")

    monkeypatch.setattr("ksadk.evaluation.executor.EvaluationTarget", FakeTarget)
    request = EvaluationRequest(
        evalset=EvalSetVersion(
            name="smoke",
            cases=[EvalCase(id="one", input="first"), EvalCase(id="two", input="second")],
        ),
        target=TargetRef(kind=TargetKind.A2A, locator="https://agent.example.test"),
        config=EvaluationConfig(evaluators=[]),
    )
    events: list[tuple[str, int, int]] = []

    def record(case_id: str, index: int, total: int) -> None:
        events.append((case_id, index, total))
        if case_id == "one":
            raise RuntimeError("console unavailable")

    report = await execute_evaluation(request, on_case_started=record)

    assert events == [("one", 1, 2), ("two", 2, 2)]
    assert report.status.value == "PASSED"
    assert [case.case_id for case in report.case_runs] == ["one", "two"]
