import json

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli
from ksadk.cli.cmd_eval import eval
from ksadk.evaluation.contracts import (
    CaseRun,
    EvalCase,
    EvalRunReport,
    EvalRunSpec,
    EvalSetVersion,
    TargetKind,
    TargetRun,
    TargetSnapshot,
)


def _evalset(tmp_path):
    path = tmp_path / "suite.yaml"
    path.write_text(
        """\
schemaVersion: ksadk.eval/v1
name: smoke
cases:
  - id: one
    input: hello
    assertions:
      - type: response.contains
        value: hell
""",
        encoding="utf-8",
    )
    return path


def _report() -> EvalRunReport:
    spec = EvalRunSpec(
        id="eval-preview",
        evalset=EvalSetVersion(name="smoke", cases=[EvalCase(id="one", input="hello")]),
        target=TargetSnapshot(
            kind=TargetKind.A2A,
            entrypoint="https://agent.example.test",
            revision_digest="sha256:agent",
        ),
    )
    return EvalRunReport(
        spec=spec,
        status="PASSED",
        case_runs=[
            CaseRun(
                case_id="one",
                target_run=TargetRun(status="PASSED", duration_ms=12),
            )
        ],
    )


def _local_agent(tmp_path):
    project = tmp_path / "local-agent"
    project.mkdir()
    (project / "agentengine.yaml").write_text(
        "name: cli-local-agent\n"
        "framework: langgraph\n"
        "entry_point: agent.py\n"
        "agent_variable: graph\n",
        encoding="utf-8",
    )
    (project / "agent.py").write_text(
        """from langgraph.graph import END, START, StateGraph

def answer(_state):
    return {'output': 'hello from local graph'}

builder = StateGraph(dict)
builder.add_node('answer', answer)
builder.add_edge(START, 'answer')
builder.add_edge('answer', END)
graph = builder.compile()
""",
        encoding="utf-8",
    )
    return project


def test_eval_help_exposes_complete_target_shell():
    result = CliRunner().invoke(eval, ["--help"])
    assert result.exit_code == 0, result.output
    for option in (
        "--evalset-file",
        "--agent-dir",
        "--a2a-url",
        "--codex-worktree",
        "--entrypoint",
        "--credential-ref",
        "--codex-profile",
        "--evaluator",
        "--judge-model",
        "--judge-api-base",
        "--judge-api-key-env",
        "--timeout-seconds",
        "--fail-fast",
        "--data-policy",
        "--report-dir",
        "--validate-only",
        "--format",
    ):
        assert option in result.output


def test_eval_help_lists_automatic_evaluators():
    result = CliRunner().invoke(eval, ["--help"])

    assert result.exit_code == 0, result.output
    assert "reference_match@v1" in result.output
    assert "llm_judge@v1" in result.output
    assert "--judge-model" in result.output


def test_root_help_exposes_eval_command():
    _register_commands()
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "agentengine eval" in result.output


def test_eval_validate_only_returns_normalized_summary(tmp_path):
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--agent-dir",
            str(tmp_path),
            "--validate-only",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert payload["evalset"]["sourceFormat"] == "native"
    assert payload["evalset"]["caseCount"] == 1
    assert payload["target"]["kind"] == "local_source"
    assert payload["config"]["evaluators"] == [
        "response_contract@v1",
        "runtime_budget@v1",
        "tool_trajectory@v1",
    ]


def test_eval_accepts_canonical_a2a_parameters(tmp_path):
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--a2a-url",
            "https://agent.example.invalid/card",
            "--evaluator",
            "response_contract@v1",
            "--format",
            "json",
            "--validate-only",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target"]["kind"] == "a2a"
    assert payload["config"]["evaluators"] == ["response_contract@v1"]


def test_eval_report_dir_is_report_directory(tmp_path):
    report_dir = tmp_path / "reports"
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--agent-dir",
            str(tmp_path),
            "--report-dir",
            str(report_dir),
            "--format",
            "json",
            "--validate-only",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["reportDir"] == str(report_dir.resolve())


def test_eval_rejects_output_as_a_report_path(tmp_path):
    _register_commands()
    result = CliRunner().invoke(
        cli,
        [
            "eval",
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--agent-dir",
            str(tmp_path),
            "--output",
            str(tmp_path / "reports"),
            "--validate-only",
        ],
    )
    assert result.exit_code == 2
    assert "No such option: --output" in result.output


def test_eval_rejects_ambiguous_targets(tmp_path):
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--agent-dir",
            str(tmp_path),
            "--a2a-url",
            "https://agent.example.invalid",
            "--validate-only",
        ],
    )
    assert result.exit_code == 2
    assert "必须且只能指定一个" in result.output


def test_eval_calls_single_execution_entrypoint(tmp_path, monkeypatch):
    async def fake_execute(request, *, on_case_started=None):
        raise RuntimeError(request.target.kind.value)

    monkeypatch.setattr("ksadk.cli.cmd_eval.execute_evaluation", fake_execute)
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--a2a-url",
            "https://agent.example.invalid",
        ],
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "a2a"


def test_eval_pretty_output_shows_key_progress_and_report_preview(tmp_path, monkeypatch):
    async def fake_execute(_request, *, on_case_started=None):
        assert on_case_started is not None
        on_case_started("one", 1, 1)
        return _report()

    monkeypatch.setattr("ksadk.cli.cmd_eval.execute_evaluation", fake_execute)
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--a2a-url",
            "https://agent.example.test",
            "--report-dir",
            str(tmp_path / "reports"),
            "--format",
            "pretty",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "开始评测：smoke，1 个 Case，Target: a2a" in result.output
    assert "[1/1] 执行 Case: one" in result.output
    assert "评测集列表" in result.output
    assert "Case" in result.output
    assert "目标状态" in result.output
    assert "耗时" in result.output
    assert "指标" in result.output
    assert "结果统计" in result.output
    assert "\n  结果统计\n" in result.output
    assert "  评测集列表" in result.output
    assert "统计:" not in result.output
    for value in ("总计", "通过", "失败", "错误", "不可用", "取消", "1", "0"):
        assert value in result.output
    assert "报告文件" in result.output
    assert str(tmp_path / "reports" / "eval-preview" / "report.json") in result.output
    assert "one" in result.output
    assert "PASSED" in result.output
    assert "12 ms" in result.output
    assert "无指标" in result.output


def test_eval_json_output_does_not_render_progress(tmp_path, monkeypatch):
    async def fake_execute(_request, *, on_case_started=None):
        assert on_case_started is None
        return _report()

    monkeypatch.setattr("ksadk.cli.cmd_eval.execute_evaluation", fake_execute)
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--a2a-url",
            "https://agent.example.test",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["spec"]["id"] == "eval-preview"


def test_eval_invalid_local_source_uses_execution_exit_code(tmp_path):
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--agent-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2
    assert "LOCAL_FRAMEWORK_UNSUPPORTED" in result.output


def test_eval_local_source_executes_and_persists_report(tmp_path):
    project = _local_agent(tmp_path)
    report_dir = tmp_path / "reports"
    result = CliRunner().invoke(
        eval,
        [
            "--evalset-file",
            str(_evalset(tmp_path)),
            "--agent-dir",
            str(project),
            "--report-dir",
            str(report_dir),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "PASSED"
    assert payload["spec"]["target"]["kind"] == "local_source"
    assert payload["caseRuns"][0]["targetRun"]["output"] == "hello from local graph"
    assert (report_dir / payload["spec"]["id"] / "report.json").is_file()
    trace_ref = payload["caseRuns"][0]["targetRun"]["traceRef"]
    assert (
        report_dir
        / trace_ref["runId"]
        / "evidence"
        / trace_ref["sessionId"]
        / f"{trace_ref['invocationId']}.json"
    ).is_file()
