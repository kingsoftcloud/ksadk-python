from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from ksadk.cli import cmd_model
from ksadk.cli.cmd_config import config


@pytest.fixture(autouse=True)
def _isolate_model_env(monkeypatch):
    for key in (
        "OPENAI_MODEL_NAME",
        "MODEL_NAME",
        "OPENAI_API_BASE",
        "COZE_WORKLOAD_IDENTITY_API_KEY",
        "COZE_INTEGRATION_BASE_URL",
        "COZE_INTEGRATION_MODEL_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_config_model_env_prints_openclaw_allowlist_from_state(monkeypatch, tmp_path: Path):
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump({"type": "openclaw", "framework": "openclaw"}),
        encoding="utf-8",
    )
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        config,
        [
            "model",
            "--env",
            "deepseek-v4-pro,glm-5.1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "OPENAI_MODEL_NAME=deepseek-v4-pro",
        "OPENCLAW_MODEL_ALLOWLIST=deepseek-v4-pro,glm-5.1",
    ]
    assert not (tmp_path / ".env").exists()


def test_config_model_env_prints_generic_allowlist_for_hermes(monkeypatch, tmp_path: Path):
    (tmp_path / "agentengine.yaml").write_text(
        "framework: hermes\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        config,
        [
            "model",
            "--env",
            "deepseek-v4-pro,glm-5.1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "OPENAI_MODEL_NAME=deepseek-v4-pro",
        "AGENTENGINE_MODEL_ALLOWLIST=deepseek-v4-pro,glm-5.1",
    ]
    assert not (tmp_path / ".env").exists()


def test_config_model_env_single_model_prints_only_default(monkeypatch, tmp_path: Path):
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump({"type": "openclaw", "framework": "openclaw"}),
        encoding="utf-8",
    )
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        config,
        [
            "model",
            "--env",
            "deepseek-v4-pro",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["OPENAI_MODEL_NAME=deepseek-v4-pro"]
    assert not (tmp_path / ".env").exists()


def test_config_model_multi_select_writes_openclaw_allowlist(monkeypatch, tmp_path: Path):
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump({"type": "openclaw", "framework": "openclaw"}),
        encoding="utf-8",
    )
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(cmd_model, "is_stdout_tty", lambda: True)

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "glm-5.1"},
                    {"id": "deepseek-v4-pro"},
                    {"id": "kimi-k2.6"},
                ]
            }

    class _Prompt:
        def ask(self):
            return ["deepseek-v4-pro", "glm-5.1"]

    monkeypatch.setattr(cmd_model.httpx, "get", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(cmd_model.questionary, "checkbox", lambda *_args, **_kwargs: _Prompt())

    result = runner.invoke(config, ["model", "--multi"])

    assert result.exit_code == 0, result.output
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENAI_MODEL_NAME=deepseek-v4-pro" in env_text
    assert "OPENCLAW_MODEL_ALLOWLIST=deepseek-v4-pro,glm-5.1" in env_text


def test_config_model_writes_current_project_env_not_parent_env(monkeypatch, tmp_path: Path):
    parent_env = tmp_path / ".env"
    parent_env.write_text(
        "OPENAI_BASE_URL=https://parent.example/v1\nOPENAI_MODEL_NAME=parent-model\n",
        encoding="utf-8",
    )
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()

    runner = CliRunner()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(cmd_model, "is_stdout_tty", lambda: True)

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "deepseek-v4-pro"}, {"id": "glm-5.1"}]}

    class _Prompt:
        def ask(self):
            return "deepseek-v4-pro"

    monkeypatch.setattr(cmd_model.httpx, "get", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(cmd_model.questionary, "select", lambda *_args, **_kwargs: _Prompt())

    result = runner.invoke(config, ["model"])

    assert result.exit_code == 0, result.output
    assert "OPENAI_MODEL_NAME=deepseek-v4-pro" in (project_dir / ".env").read_text(encoding="utf-8")
    assert parent_env.read_text(encoding="utf-8") == (
        "OPENAI_BASE_URL=https://parent.example/v1\nOPENAI_MODEL_NAME=parent-model\n"
    )
