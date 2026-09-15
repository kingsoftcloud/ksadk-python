from __future__ import annotations

import json
from pathlib import Path

from a2a.types import Task, TaskState, TaskStatus
from click.testing import CliRunner
from starlette.testclient import TestClient

from ksadk.a2a import A2APlatformTask, DiscoveredAgent, build_agent_card
from ksadk.cli import _register_commands, cli

SPACE_AGENT_ID = "a2a-agent-00000000000040008000000000000041"
SPACE_VERSION_ID = "a2a-version-00000000000040008000000000000042"
PLATFORM_TASK_ID = "a2a-task-00000000000040008000000000000043"
SPACE_ID = "a2a-space-00000000000040008000000000000044"


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


def test_a2a_discover_prints_callable_route_without_credentials(monkeypatch):
    class FakeSpaceClient:
        async def discover(self, **kwargs):
            return [
                DiscoveredAgent(
                    agent_id=SPACE_AGENT_ID,
                    version_id=SPACE_VERSION_ID,
                    source="external",
                    agent_card=build_agent_card(name="remote", base_url="https://example.com"),
                    callable=False,
                    blocked_reason="requires_public_egress",
                    route_kind="external_public",
                )
            ]

    selected: list[str | None] = []
    monkeypatch.setattr(
        "ksadk.cli.cmd_a2a._space_client",
        lambda space_id=None: selected.append(space_id) or FakeSpaceClient(),
    )
    _register_commands()

    result = CliRunner().invoke(cli, ["a2a", "discover", "--space-id", SPACE_ID])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agent_id"] == SPACE_AGENT_ID
    assert payload["route_kind"] == "external_public"
    assert payload["callable"] is False
    assert "credential_handle" not in payload
    assert selected == [SPACE_ID]


def test_a2a_call_prints_only_platform_task_id(monkeypatch):
    remote_task = Task(
        id="remote-task-1",
        context_id="remote-context-1",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )

    class FakeSpaceClient:
        async def send_message(self, agent_id, message):
            return A2APlatformTask(
                id=PLATFORM_TASK_ID,
                remote_task=remote_task,
            )

    monkeypatch.setattr(
        "ksadk.cli.cmd_a2a._space_client",
        lambda space_id=None: FakeSpaceClient(),
    )
    _register_commands()

    result = CliRunner().invoke(cli, ["a2a", "call", SPACE_AGENT_ID, "hello"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "task_id": PLATFORM_TASK_ID,
        "status": "TASK_STATE_WORKING",
    }


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
    assert "url" not in payload
    assert payload["description"] == "CLI generated card"
    assert [skill["id"] for skill in payload["skills"]] == ["echo"]
    assert {interface["url"] for interface in payload["supportedInterfaces"]} == {
        "http://127.0.0.1:8081/a2a/jsonrpc",
        "http://127.0.0.1:8081/a2a/v1",
    }


def test_a2a_serve_builds_server_and_exposes_agent_card(monkeypatch, tmp_path):
    project_dir = _write_project_config(tmp_path)
    captured: dict[str, object] = {}

    class FakeRuntimeAdapter:
        pass

    fake_adapter = FakeRuntimeAdapter()

    monkeypatch.setattr("ksadk.configs.setup_environment", lambda _path: None)

    def fake_create_runtime_adapter(context):
        captured["runtime_context"] = context
        return fake_adapter

    monkeypatch.setattr("ksadk.cli.cmd_a2a.create_runtime_adapter", fake_create_runtime_adapter)

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
    assert captured["runtime_context"].runtime_type == "adk"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9091

    client = TestClient(captured["app"])
    current_card = client.get("/.well-known/agent-card.json")
    legacy_card = client.get("/.well-known/agent.json")

    assert current_card.status_code == 200
    assert legacy_card.status_code == 404
    assert current_card.json()["name"] == "demo-agent"
    assert "url" not in current_card.json()
    assert {interface["url"] for interface in current_card.json()["supportedInterfaces"]} == {
        "http://127.0.0.1:9091/a2a/jsonrpc",
        "http://127.0.0.1:9091/a2a/v1",
    }
    assert [skill["id"] for skill in current_card.json()["skills"]] == ["echo"]
