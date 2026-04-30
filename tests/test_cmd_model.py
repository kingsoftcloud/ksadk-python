from pathlib import Path

import yaml
from click.testing import CliRunner

from ksadk.cli import cmd_model
from ksadk.cli.cmd_config import config


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
