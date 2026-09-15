from ksadk.evaluation import (
    EvalCase,
    EvalRunReport,
    EvalRunSpec,
    EvalSetVersion,
    EvaluationStorage,
    TargetKind,
    TargetSnapshot,
)


def _report() -> EvalRunReport:
    evalset = EvalSetVersion(name="smoke", cases=[EvalCase(id="case", input="hello")])
    return EvalRunReport(
        spec=EvalRunSpec(
            id="eval-storage",
            evalset=evalset,
            target=TargetSnapshot(
                kind=TargetKind.LOCAL_SOURCE,
                entrypoint="agent.py",
                revision_digest="sha256:agent",
            ),
        ),
        status="PASSED",
    )


def test_storage_writes_and_reads_digest_bearing_report(tmp_path):
    report = _report()
    storage = EvaluationStorage(tmp_path / ".agentkit/evaluations")

    path = storage.write_report(report)

    assert path == tmp_path / ".agentkit/evaluations/eval-storage/report.json"
    assert path.is_file()
    assert storage.read_report("eval-storage").report_digest == report.report_digest
    assert storage.list_reports() == [report]
