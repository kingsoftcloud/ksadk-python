from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from ksadk.cli import _register_commands, cli
from ksadk.evaluation.cloud_service import CloudEvalSetPublishResult


def _evalset(tmp_path):
    path = tmp_path / "support.yaml"
    path.write_text(
        """\
schemaVersion: ksadk.eval/v1
name: support-regression
cases:
  - id: case-001
    input: hello
""",
        encoding="utf-8",
    )
    return path


def test_evalset_preview_prints_exact_cloud_snapshot(tmp_path):
    from ksadk.cli.cmd_evalset import evalset

    result = CliRunner().invoke(
        evalset,
        ["preview", "--evalset-file", str(_evalset(tmp_path)), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["columns"][0]["name"] == "case_id"
    assert payload["rows"][0]["values"]["case_id"] == "case-001"


def test_root_cli_registers_evalset_command():
    _register_commands()
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "evalset" in result.output


def test_evalset_push_updates_existing_dataset_with_simplified_options(tmp_path, monkeypatch):
    from ksadk.cli.cmd_evalset import evalset
    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    async def fake_publish(self, snapshot, *, dataset_id, base_version, idempotency_key):
        assert dataset_id == "dataset-existing"
        assert base_version is None
        assert idempotency_key.startswith("ksadk-evalset-")
        return CloudEvalSetPublishResult(
            dataset_id="dataset-existing",
            dataset_version=2,
            project_id="project-001",
            schema_hash=snapshot.schema_hash,
            content_digest=snapshot.content_digest,
            row_count=len(snapshot.rows),
        )

    monkeypatch.setattr(AgentEvalCloudDatasetClient, "publish_snapshot", fake_publish)
    _evalset(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        evalset,
        [
            "push",
            "--file",
            "support.yaml",
            "--dataset-id",
            "dataset-existing",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["datasetId"] == "dataset-existing"
    assert payload["datasetVersion"] == 2
    assert (tmp_path / ".agentkit" / "evaluation-bindings").is_dir()


def test_evalset_push_reuses_existing_binding_without_dataset_option(tmp_path, monkeypatch):
    from ksadk.cli.cmd_evalset import evalset
    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    calls = []

    async def fake_publish(self, snapshot, *, dataset_id, base_version, idempotency_key):
        calls.append((dataset_id, base_version, idempotency_key))
        return CloudEvalSetPublishResult(
            dataset_id="dataset-existing",
            dataset_version=len(calls),
            project_id="project-001",
            schema_hash=snapshot.schema_hash,
            content_digest=snapshot.content_digest,
            row_count=len(snapshot.rows),
        )

    monkeypatch.setattr(AgentEvalCloudDatasetClient, "publish_snapshot", fake_publish)
    _evalset(tmp_path)
    monkeypatch.chdir(tmp_path)
    common = ["--file", "support.yaml"]

    first = CliRunner().invoke(evalset, ["push", *common, "--dataset-id", "dataset-existing"])
    second = CliRunner().invoke(evalset, ["push", *common])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert calls[1][0:2] == ("dataset-existing", None)
    assert calls[1][2] == calls[0][2]


def test_evalset_push_help_exposes_only_simple_publish_inputs():
    from ksadk.cli.cmd_evalset import evalset

    result = CliRunner().invoke(evalset, ["push", "--help"])

    assert result.exit_code == 0, result.output
    assert "--file" in result.output
    assert "--dataset-id" in result.output
    assert "--base-version" not in result.output
    assert "--idempotency-key" not in result.output
    assert "--data-policy" not in result.output


def test_evalset_help_does_not_expose_agent_eval_url():
    from ksadk.cli.cmd_evalset import evalset

    push_help = CliRunner().invoke(evalset, ["push", "--help"])
    pull_help = CliRunner().invoke(evalset, ["pull", "--help"])

    assert push_help.exit_code == 0, push_help.output
    assert pull_help.exit_code == 0, pull_help.output
    assert "--agent-eval-url" not in push_help.output
    assert "--agent-eval-url" not in pull_help.output
    assert "--api-token-env" not in push_help.output
    assert "--api-token-env" not in pull_help.output


@pytest.mark.parametrize(
    ("template", "expected_case", "expected_fragment"),
    [
        ("knowledge-qa", "capital", "reference_output: 北京"),
        ("structured-output", "extract-order", "response.jsonSchema"),
        ("tool-routing", "weather-lookup", "tool.succeeded"),
        ("service-sla", "password-reset", "runtime.maxTotalTokens"),
    ],
)
def test_evalset_init_writes_valid_official_template(
    tmp_path, template, expected_case, expected_fragment
):
    from ksadk.cli.cmd_evalset import evalset

    output = tmp_path / f"{template}.yaml"
    result = CliRunner().invoke(
        evalset,
        ["init", "--template", template, "--output-file", str(output), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["template"] == template
    assert output.is_file()
    assert expected_case in output.read_text(encoding="utf-8")
    assert expected_fragment in output.read_text(encoding="utf-8")

    preview = CliRunner().invoke(
        evalset,
        ["preview", "--evalset-file", str(output), "--format", "json"],
    )
    assert preview.exit_code == 0, preview.output


def test_evalset_init_requires_force_to_overwrite_a_file(tmp_path):
    from ksadk.cli.cmd_evalset import evalset

    output = tmp_path / "knowledge.yaml"
    output.write_text("keep this content\n", encoding="utf-8")
    runner = CliRunner()

    rejected = runner.invoke(
        evalset,
        ["init", "--template", "knowledge-qa", "--output-file", str(output)],
    )
    assert rejected.exit_code != 0
    assert output.read_text(encoding="utf-8") == "keep this content\n"

    written = runner.invoke(
        evalset,
        ["init", "--template", "knowledge-qa", "--output-file", str(output), "--force"],
    )
    assert written.exit_code == 0, written.output
    assert "schemaVersion: ksadk.eval/v1" in output.read_text(encoding="utf-8")


def test_evalset_pull_exports_one_fixed_dataset_version(tmp_path, monkeypatch):
    from ksadk.cli.cmd_evalset import evalset
    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient
    from ksadk.evaluation.cloud_converter import evalset_to_dataset_snapshot
    from ksadk.evaluation.evalset import parse_evalset

    source = parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "remote-support",
            "cases": [{"id": "case-1", "input": "hello"}],
        }
    )
    snapshot = evalset_to_dataset_snapshot(source)

    async def fake_read(self, dataset_id, version, *, project_id=None):
        assert (dataset_id, version, project_id) == ("dataset-1", 4, "project-1")
        return snapshot

    monkeypatch.setattr(AgentEvalCloudDatasetClient, "read_snapshot", fake_read)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        evalset,
        [
            "pull",
            "--dataset-id",
            "dataset-1",
            "--dataset-version",
            "4",
            "--project-id",
            "project-1",
            "--output-file",
            "imported.yaml",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["datasetVersion"] == 4
    assert (tmp_path / "imported.yaml").is_file()
