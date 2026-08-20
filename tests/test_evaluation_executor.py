import asyncio

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
    assert report.status.value == "FAILED"
    assert [case.case_id for case in report.case_runs] == ["one", "two"]
    assert report.summary.unavailable_cases == 2
    assert all(
        case.metrics[0].name == "response_quality"
        and case.metrics[0].status.value == "UNAVAILABLE"
        and case.metrics[0].required
        for case in report.case_runs
    )


@pytest.mark.asyncio
async def test_cancelled_evaluation_persists_completed_cases_and_uses_requested_run_id(
    monkeypatch,
    tmp_path,
):
    gate = asyncio.Event()

    class FakeTarget:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        async def snapshot(self):
            return TargetSnapshot(
                kind=TargetKind.A2A,
                entrypoint="https://agent.example.test",
                revision_digest="sha256:agent",
            )

        async def run_case(self, _spec, _case):
            self.calls += 1
            if self.calls == 2:
                await gate.wait()
            return TargetRun(status="PASSED", output="done")

    monkeypatch.setattr("ksadk.evaluation.executor.EvaluationTarget", FakeTarget)
    request = EvaluationRequest(
        evalset=EvalSetVersion(
            name="smoke",
            cases=[EvalCase(id="one", input="first"), EvalCase(id="two", input="second")],
        ),
        target=TargetRef(kind=TargetKind.A2A, locator="https://agent.example.test"),
        config=EvaluationConfig(evaluators=[]),
        report_dir=str(tmp_path / "reports"),
    )
    started: list[str] = []
    task = asyncio.create_task(
        execute_evaluation(
            request,
            run_id="eval_cancelled",
            on_case_started=lambda case_id, _index, _total: started.append(case_id),
        )
    )
    while started != ["one", "two"]:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    report_path = tmp_path / "reports" / "eval_cancelled" / "report.json"
    report = __import__(
        "ksadk.evaluation.storage",
        fromlist=["EvaluationStorage"],
    ).EvaluationStorage(tmp_path / "reports").read_report("eval_cancelled")
    assert report_path.is_file()
    assert report.spec.id == "eval_cancelled"
    assert report.status.value == "CANCELLED"
    assert [case.case_id for case in report.case_runs] == ["one"]
