import pytest

from ksadk.evaluation import (
    A2ATargetAdapter,
    EvalCase,
    EvalRunSpec,
    EvalSetVersion,
    EvaluationConfig,
    EvaluationTarget,
    TargetKind,
    TargetRef,
    TargetRun,
    TargetSnapshot,
    create_target_adapter,
)


def test_a2a_target_routes_to_a2a_adapter():
    adapter = create_target_adapter(
        TargetRef(kind=TargetKind.A2A, locator="https://agent.example.test"),
        timeout_seconds=5,
    )

    assert isinstance(adapter, A2ATargetAdapter)


@pytest.mark.asyncio
async def test_target_owns_common_adapter_lifecycle(monkeypatch):
    snapshot = TargetSnapshot(
        kind=TargetKind.LOCAL_SOURCE,
        entrypoint="agent.py",
        revision_digest="sha256:agent",
    )

    class FakeAdapter:
        async def snapshot(self, target):
            assert target.kind is TargetKind.LOCAL_SOURCE
            return snapshot

        async def run_case(self, spec, case, *, attempt):
            assert spec.target == snapshot
            assert case.id == "case-1"
            assert attempt == 1
            return TargetRun(status="PASSED", output="done")

    monkeypatch.setattr(
        "ksadk.evaluation.target.create_target_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    target = EvaluationTarget(
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator="."),
        EvaluationConfig(timeout_seconds=5),
    )
    resolved_snapshot = await target.snapshot()
    spec = EvalRunSpec(
        id="eval-1",
        evalset=EvalSetVersion(name="smoke", cases=[EvalCase(id="case-1", input="hello")]),
        target=resolved_snapshot,
    )

    result = await target.run_case(spec, spec.evalset.cases[0])

    assert result.output == "done"
