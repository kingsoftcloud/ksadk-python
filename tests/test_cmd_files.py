from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli


class _FakeFilesClient:
    init_calls: list[dict] = []
    list_calls: list[dict] = []
    upload_calls: list[dict] = []
    download_calls: list[dict] = []
    delete_calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.__class__.init_calls.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_workspace_files(self, **kwargs):
        self.__class__.list_calls.append(kwargs)
        return {
            "root": "workspace",
            "path": kwargs["path"],
            "entries": [
                {"name": "inputs", "path": "inputs", "type": "directory"},
                {"name": "report.txt", "path": "report.txt", "type": "file", "size_bytes": 7},
            ],
        }

    async def upload_workspace_file(self, **kwargs):
        self.__class__.upload_calls.append(kwargs)
        return {"entry": {"path": kwargs["remote_path"], "type": "file", "size_bytes": 7}}

    async def download_workspace_file(self, **kwargs):
        self.__class__.download_calls.append(kwargs)
        return b"payload"

    async def delete_workspace_file(self, **kwargs):
        self.__class__.delete_calls.append(kwargs)
        return {"deleted": True}


def test_files_list_command_supports_json_output(monkeypatch):
    from ksadk.cli import cmd_files

    _FakeFilesClient.init_calls = []
    _FakeFilesClient.list_calls = []
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "list",
            "--agent",
            "demo-agent",
            "--path",
            "docs",
            "--region",
            "cn-beijing-6",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == "docs"
    assert payload["entries"][0]["path"] == "inputs"
    assert _FakeFilesClient.list_calls == [
        {"agent_id": "demo-agent", "path": "docs", "recursive": False}
    ]
    assert _FakeFilesClient.init_calls == [{"region": "cn-beijing-6"}]


def test_files_list_command_supports_direct_runtime_access(monkeypatch):
    from ksadk.cli import cmd_files

    _FakeFilesClient.init_calls = []
    _FakeFilesClient.list_calls = []
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "list",
            "--endpoint",
            "http://127.0.0.1:18080",
            "--api-key",
            "ak-direct-demo",
            "--path",
            "docs",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == "docs"
    assert _FakeFilesClient.list_calls == [
        {
            "agent_id": None,
            "path": "docs",
            "recursive": False,
            "endpoint": "http://127.0.0.1:18080",
            "api_key": "ak-direct-demo",
        }
    ]
    assert _FakeFilesClient.init_calls == [{"region": "cn-beijing-6"}]


def test_files_list_command_resolves_openclaw_state_and_prefers_state_runtime_access(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _FakeFilesClient.init_calls = []
    _FakeFilesClient.list_calls = []
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agentengine.state").write_text(
        "\n".join(
            [
                "type: openclaw",
                "framework: openclaw",
                "agent_id: ar-openclaw-1",
                "name: demo-openclaw",
                "endpoint: https://openclaw.example.com",
                "api_key: ak-openclaw",
                "region: pre-online",
                "",
            ]
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "list",
            "--path",
            "docs",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == "docs"
    assert _FakeFilesClient.init_calls == [{"region": "pre-online"}]
    assert _FakeFilesClient.list_calls == [
        {
            "agent_id": "ar-openclaw-1",
            "path": "docs",
            "recursive": False,
            "endpoint": "https://openclaw.example.com",
            "api_key": "ak-openclaw",
        }
    ]


def test_files_list_command_falls_back_to_project_config(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _FakeFilesClient.init_calls = []
    _FakeFilesClient.list_calls = []
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agentengine.yaml").write_text("name: demo-agent\nframework: langgraph\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "list",
            "--path",
            "docs",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == "docs"
    assert _FakeFilesClient.init_calls == [{"region": "cn-beijing-6"}]
    assert _FakeFilesClient.list_calls == [
        {
            "agent_id": "demo-agent",
            "path": "docs",
            "recursive": False,
        }
    ]


def test_files_upload_download_and_delete_commands(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _FakeFilesClient.init_calls = []
    _FakeFilesClient.upload_calls = []
    _FakeFilesClient.download_calls = []
    _FakeFilesClient.delete_calls = []
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_file = tmp_path / "report.txt"
    local_file.write_text("payload", encoding="utf-8")
    download_path = tmp_path / "downloaded.txt"

    runner = CliRunner()
    upload_result = runner.invoke(
        cli,
        [
            "files",
            "upload",
            "--agent",
            "demo-agent",
            "--local-path",
            str(local_file),
            "--remote-path",
            "reports/report.txt",
        ],
    )
    download_result = runner.invoke(
        cli,
        [
            "files",
            "download",
            "--agent",
            "demo-agent",
            "--remote-path",
            "reports/report.txt",
            "--output-path",
            str(download_path),
        ],
    )
    delete_result = runner.invoke(
        cli,
        [
            "files",
            "delete",
            "--agent",
            "demo-agent",
            "--remote-path",
            "reports/report.txt",
            "--yes",
        ],
    )

    assert upload_result.exit_code == 0, upload_result.output
    assert download_result.exit_code == 0, download_result.output
    assert delete_result.exit_code == 0, delete_result.output
    assert download_path.read_text(encoding="utf-8") == "payload"
    assert _FakeFilesClient.upload_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "reports/report.txt",
            "local_path": local_file,
        }
    ]
    assert _FakeFilesClient.download_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "reports/report.txt",
        }
    ]
    assert _FakeFilesClient.delete_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "reports/report.txt",
        }
    ]
    assert _FakeFilesClient.init_calls == [
        {"region": "cn-beijing-6"},
        {"region": "cn-beijing-6"},
        {"region": "cn-beijing-6"},
    ]


def test_files_commands_support_direct_runtime_access(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _FakeFilesClient.init_calls = []
    _FakeFilesClient.upload_calls = []
    _FakeFilesClient.download_calls = []
    _FakeFilesClient.delete_calls = []
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_file = tmp_path / "report.txt"
    local_file.write_text("payload", encoding="utf-8")
    download_path = tmp_path / "downloaded.txt"

    runner = CliRunner()
    upload_result = runner.invoke(
        cli,
        [
            "files",
            "upload",
            "--endpoint",
            "http://127.0.0.1:18080",
            "--api-key",
            "ak-direct-demo",
            "--local-path",
            str(local_file),
            "--remote-path",
            "reports/report.txt",
        ],
    )
    download_result = runner.invoke(
        cli,
        [
            "files",
            "download",
            "--endpoint",
            "http://127.0.0.1:18080",
            "--api-key",
            "ak-direct-demo",
            "--remote-path",
            "reports/report.txt",
            "--output-path",
            str(download_path),
        ],
    )
    delete_result = runner.invoke(
        cli,
        [
            "files",
            "delete",
            "--endpoint",
            "http://127.0.0.1:18080",
            "--api-key",
            "ak-direct-demo",
            "--remote-path",
            "reports/report.txt",
            "--yes",
        ],
    )

    assert upload_result.exit_code == 0, upload_result.output
    assert download_result.exit_code == 0, download_result.output
    assert delete_result.exit_code == 0, delete_result.output
    assert download_path.read_text(encoding="utf-8") == "payload"
    assert _FakeFilesClient.upload_calls == [
        {
            "agent_id": None,
            "remote_path": "reports/report.txt",
            "local_path": local_file,
            "endpoint": "http://127.0.0.1:18080",
            "api_key": "ak-direct-demo",
        }
    ]
    assert _FakeFilesClient.download_calls == [
        {
            "agent_id": None,
            "remote_path": "reports/report.txt",
            "endpoint": "http://127.0.0.1:18080",
            "api_key": "ak-direct-demo",
        }
    ]
    assert _FakeFilesClient.delete_calls == [
        {
            "agent_id": None,
            "remote_path": "reports/report.txt",
            "endpoint": "http://127.0.0.1:18080",
            "api_key": "ak-direct-demo",
        }
    ]
    assert _FakeFilesClient.init_calls == [
        {"region": "cn-beijing-6"},
        {"region": "cn-beijing-6"},
        {"region": "cn-beijing-6"},
    ]


def test_files_upload_accepts_positional_agent(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _FakeFilesClient.init_calls = []
    _FakeFilesClient.upload_calls = []
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_file = tmp_path / "report.txt"
    local_file.write_text("payload", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "files",
            "upload",
            "demo-agent",
            "--local-path",
            str(local_file),
            "--remote-path",
            "reports/report.txt",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeFilesClient.upload_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "reports/report.txt",
            "local_path": local_file,
        }
    ]
