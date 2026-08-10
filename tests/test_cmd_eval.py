import json

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli
from ksadk.cli.cmd_eval import eval


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
        "--timeout-seconds",
        "--fail-fast",
        "--data-policy",
        "--report-dir",
        "--validate-only",
        "--format",
    ):
        assert option in result.output


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
    async def fake_execute(request):
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


def test_eval_unimplemented_executor_uses_execution_exit_code(tmp_path):
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
    assert "尚未实现" in result.output
