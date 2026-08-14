from __future__ import annotations

import json

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


def test_evalset_push_creates_binding_only_after_publish(tmp_path, monkeypatch):
    from ksadk.cli.cmd_evalset import evalset
    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    async def fake_publish(self, snapshot, *, dataset_id, base_version, idempotency_key):
        return CloudEvalSetPublishResult(
            dataset_id="dataset-001",
            dataset_version=1,
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
            "--evalset-file",
            "support.yaml",
            "--agent-eval-url",
            "https://agent-eval.example",
            "--data-policy",
            "full_trace",
            "--idempotency-key",
            "publish-001",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["datasetId"] == "dataset-001"
    assert (tmp_path / ".agentkit" / "evaluation-bindings").is_dir()


def test_evalset_pull_exports_one_fixed_dataset_version(tmp_path, monkeypatch):
    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient
    from ksadk.evaluation.cloud_converter import evalset_to_dataset_snapshot
    from ksadk.evaluation.evalset import parse_evalset
    from ksadk.cli.cmd_evalset import evalset

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
            "--agent-eval-url",
            "https://agent-eval.example",
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
