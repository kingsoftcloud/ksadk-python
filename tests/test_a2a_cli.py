from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from starlette.testclient import TestClient

from ksadk.cli import _register_commands, cli


def _write_project_config(tmp_path: Path) -> Path:
    (tmp_path / "agentengine.yaml").write_text(
        "\n".join(
            [
                "framework: adk",
                "name: demo-agent",
                "package: demo_agent",
                "entry_point: demo_agent/agent.py",
                "agent_variable: root_agent",
                "",
            ]
        ),
        encoding="utf-8",
    )
    package_dir = tmp_path / "demo_agent"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")
    return tmp_path


def test_root_help_lists_a2a_workflow_command():
    _register_commands()

    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "a2a" in result.output


def test_a2a_card_command_outputs_agent_card_json(monkeypatch, tmp_path):
    project_dir = _write_project_config(tmp_path)
    monkeypatch.setattr("ksadk.configs.setup_environment", lambda _path: None)
    _register_commands()

    result = CliRunner().invoke(
        cli,
        [
            "a2a",
            "card",
            str(project_dir),
            "--description",
            "CLI generated card",
            "--skill",
            "echo",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["name"] == "demo-agent"
    assert payload["url"] == "http://127.0.0.1:8081"
    assert payload["description"] == "CLI generated card"
    assert [skill["id"] for skill in payload["skills"]] == ["echo"]


def test_a2a_serve_builds_server_and_exposes_agent_card(monkeypatch, tmp_path):
    project_dir = _write_project_config(tmp_path)
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self) -> None:
            self.loaded = False

        def load_agent(self) -> None:
            self.loaded = True

        async def invoke(self, input_data):
            return {"output": input_data["input"]}

        async def stream(self, input_data):
            yield {"type": "final", "output": input_data["input"]}

    fake_runner = FakeRunner()

    monkeypatch.setattr("ksadk.configs.setup_environment", lambda _path: None)
    monkeypatch.setattr(
        "ksadk.cli.cmd_a2a.create_runner",
        lambda result, project_dir: fake_runner,
    )

    def fake_uvicorn_run(app, host, port, **kwargs):
        captured.update({"app": app, "host": host, "port": port, "kwargs": kwargs})

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    _register_commands()

    result = CliRunner().invoke(
        cli,
        [
            "a2a",
            "serve",
            str(project_dir),
            "--port",
            "9091",
            "--skill",
            "echo",
        ],
    )

    assert result.exit_code == 0, result.output
    assert fake_runner.loaded is True
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9091

    client = TestClient(captured["app"])
    current_card = client.get("/.well-known/agent-card.json")
    legacy_card = client.get("/.well-known/agent.json")

    assert current_card.status_code == 200
    assert legacy_card.status_code == 200
    assert current_card.json()["name"] == "demo-agent"
    assert current_card.json()["url"] == "http://127.0.0.1:9091"
    assert [skill["id"] for skill in current_card.json()["skills"]] == ["echo"]
