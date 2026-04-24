from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ksadk.api import AgentEngineAPIError
from ksadk.cli import _register_commands, cli


@pytest.fixture(autouse=True)
def _isolate_region_env(monkeypatch):
    monkeypatch.delenv("KSYUN_REGION", raising=False)


class _FakeFilesClient:
    init_calls: list[dict] = []
    list_calls: list[dict] = []
    upload_calls: list[dict] = []
    download_calls: list[dict] = []
    delete_calls: list[dict] = []
    list_results: dict[str, object] = {}
    download_payloads: dict[str, bytes] = {}
    workspace_health: dict[str, object] = {
        "root": "workspace",
        "workspace_path": "/home/node/.hermes/workspace",
    }

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.__class__.init_calls.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_workspace_files(self, **kwargs):
        self.__class__.list_calls.append(kwargs)
        path = kwargs["path"]
        if path in self.__class__.list_results:
            result = self.__class__.list_results[path]
            if isinstance(result, Exception):
                raise result
            return result
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
        payload = self.__class__.download_payloads.get(kwargs["remote_path"])
        if payload is not None:
            return payload
        return b"payload"

    async def delete_workspace_file(self, **kwargs):
        self.__class__.delete_calls.append(kwargs)
        return {"deleted": True}

    async def get_workspace_health(self, **kwargs):
        return dict(self.__class__.workspace_health)


def _reset_fake_files_client() -> None:
    _FakeFilesClient.init_calls = []
    _FakeFilesClient.list_calls = []
    _FakeFilesClient.upload_calls = []
    _FakeFilesClient.download_calls = []
    _FakeFilesClient.delete_calls = []
    _FakeFilesClient.list_results = {}
    _FakeFilesClient.download_payloads = {}
    _FakeFilesClient.workspace_health = {
        "root": "workspace",
        "workspace_path": "/home/node/.hermes/workspace",
    }


def test_files_list_command_supports_json_output(monkeypatch):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
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
    assert payload["ok"] is True
    assert payload["action"] == "list"
    assert payload["workspace_root"] == "workspace"
    assert payload["path"] == "docs"
    assert payload["workspace_display_path"] == "workspace:/docs"
    assert payload["workspace_real_root"] == "/home/node/.hermes/workspace"
    assert payload["workspace_real_path"] == "/home/node/.hermes/workspace/docs"
    assert payload["summary"] == {
        "entry_count": 2,
        "directory_count": 1,
        "file_count": 1,
    }
    assert payload["entries"][0]["path"] == "inputs"
    assert payload["entries"][0]["display_path"] == "workspace:/inputs"
    assert payload["entries"][0]["real_path"] == "/home/node/.hermes/workspace/inputs"
    assert payload["entries"][1]["size_human"] == "7 B"
    assert _FakeFilesClient.list_calls == [
        {"agent_id": "demo-agent", "path": "docs", "recursive": False}
    ]
    assert _FakeFilesClient.init_calls == [{"region": "cn-beijing-6"}]


def test_files_list_command_supports_direct_runtime_access(monkeypatch):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
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


def test_files_list_command_accepts_workspace_style_absolute_path(monkeypatch):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
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
            "/tmp",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == "tmp"
    assert payload["workspace_display_path"] == "workspace:/tmp"
    assert _FakeFilesClient.list_calls == [
        {"agent_id": "demo-agent", "path": "tmp", "recursive": False}
    ]


def test_files_list_command_prefers_openclaw_state_runtime_access_when_api_key_is_ready(
    monkeypatch,
    tmp_path: Path,
):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _FakeFilesClient.workspace_health = {
        "root": "workspace",
        "workspace_path": "/home/node/.openclaw/workspace",
    }
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
    assert payload["workspace_real_root"] == "/home/node/.openclaw/workspace"
    assert payload["workspace_real_path"] == "/home/node/.openclaw/workspace/docs"
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

    _reset_fake_files_client()
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

    _reset_fake_files_client()
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


def test_files_upload_pretty_output_shows_local_and_remote_paths(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_file = tmp_path / "resume.pdf"
    local_file.write_bytes(b"pdf-data")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "files",
            "upload",
            "--agent",
            "demo-agent",
            "--local-path",
            str(local_file),
            "--remote-path",
            "pdf",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "上传完成" in result.output
    assert f"本地文件：{local_file}" in result.output
    assert "远端文件：workspace:/pdf" in result.output
    assert "文件大小：7 B" in result.output


def test_files_upload_json_output_includes_agent_friendly_fields(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_file = tmp_path / "resume.pdf"
    local_file.write_bytes(b"pdf-data")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "upload",
            "--agent",
            "demo-agent",
            "--local-path",
            str(local_file),
            "--remote-path",
            "pdf",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["action"] == "upload"
    assert payload["workspace_root"] == "workspace"
    assert payload["local_path"] == str(local_file)
    assert payload["remote_path"] == "pdf"
    assert payload["remote_display_path"] == "workspace:/pdf"
    assert payload["summary"] == {
        "uploaded": 1,
        "size_bytes": 7,
        "size_human": "7 B",
    }
    assert payload["entry"]["display_path"] == "workspace:/pdf"


def test_files_list_pretty_output_uses_readable_entry_lines(monkeypatch):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "files",
            "list",
            "--agent",
            "demo-agent",
            "--path",
            ".",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "工作空间：workspace" in result.output
    assert "当前目录：workspace:/" in result.output
    assert "实际目录：/home/node/.hermes/workspace" in result.output
    assert "条目数量：2" in result.output
    assert "目录（1）" in result.output
    assert "  workspace:/inputs" in result.output
    assert "文件（1）" in result.output
    assert "  workspace:/report.txt  7 B" in result.output


def test_files_commands_support_direct_runtime_access(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
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

    _reset_fake_files_client()
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


def test_files_push_uploads_new_files_and_skips_existing_targets_by_default(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_dir = tmp_path / "skills"
    local_dir.mkdir()
    (local_dir / "README.md").write_text("local readme", encoding="utf-8")
    nested_dir = local_dir / "nested"
    nested_dir.mkdir()
    (nested_dir / "tool.py").write_text("print('ok')\n", encoding="utf-8")

    _FakeFilesClient.list_results["bundle"] = {
        "root": "workspace",
        "path": "bundle",
        "entries": [
            {"name": "README.md", "path": "bundle/README.md", "type": "file", "size_bytes": 12},
        ],
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "push",
            "--agent",
            "demo-agent",
            "--local-dir",
            str(local_dir),
            "--remote-path",
            "bundle",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["action"] == "push"
    assert payload["direction"] == "push"
    assert payload["remote_display_path"] == "workspace:/bundle"
    assert payload["summary"] == {
        "created_count": 1,
        "overwritten_count": 0,
        "skipped_count": 1,
        "total_files": 2,
    }
    assert payload["created"] == ["bundle/nested/tool.py"]
    assert payload["skipped"] == ["bundle/README.md"]
    assert payload["overwritten"] == []
    assert payload["results"]["created"][0]["display_path"] == "workspace:/bundle/nested/tool.py"
    assert payload["results"]["skipped"][0]["display_path"] == "workspace:/bundle/README.md"
    assert _FakeFilesClient.upload_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "bundle/nested/tool.py",
            "local_path": nested_dir / "tool.py",
        }
    ]


def test_push_workspace_files_can_ignore_local_dev_artifacts(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_dir = tmp_path / "bundle"
    local_dir.mkdir()
    (local_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")

    git_object = local_dir / ".git" / "objects" / "ab"
    git_object.mkdir(parents=True)
    (git_object / "blob").write_bytes(b"x" * 2048)

    agentengine_ui = local_dir / ".agentengine" / "ui"
    agentengine_ui.mkdir(parents=True)
    (agentengine_ui / "sessions.sqlite").write_bytes(b"sqlite-data")

    payload = asyncio.run(
        cmd_files._push_workspace_files(
            agent_ref="demo-agent",
            local_dir=local_dir,
            remote_path="bundle",
            force=True,
            region="cn-beijing-6",
            endpoint=None,
            api_key=None,
            ignore_dev_artifacts=True,
        )
    )

    assert payload["created"] == ["bundle/app.py"]
    assert payload["total_files"] == 1
    assert _FakeFilesClient.upload_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "bundle/app.py",
            "local_path": local_dir / "app.py",
        }
    ]


def test_files_push_pretty_output_is_readable_in_chinese(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_dir = tmp_path / "bundle"
    local_dir.mkdir()
    (local_dir / "hello.txt").write_text("hello", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "files",
            "push",
            "--agent",
            "demo-agent",
            "--local-dir",
            str(local_dir),
            "--remote-path",
            "bundle",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "推送完成" in result.output
    assert f"本地目录：{local_dir}" in result.output
    assert "远端目录：workspace:/bundle" in result.output
    assert "统计：新增 1，覆盖 0，跳过 0，共 1" in result.output
    assert "已新增：workspace:/bundle/hello.txt" in result.output


def test_files_push_pretty_output_shows_action_proxy_transport_hint(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_dir = tmp_path / "bundle"
    local_dir.mkdir()
    (local_dir / "hello.txt").write_text("hello", encoding="utf-8")
    _FakeFilesClient.list_results["bundle"] = {
        "root": "workspace",
        "path": "bundle",
        "entries": [],
        "transport_mode": "action_proxy",
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "files",
            "push",
            "--agent",
            "demo-agent",
            "--local-dir",
            str(local_dir),
            "--remote-path",
            "bundle",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "访问链路：通过平台 action 代理访问远端 workspace" in result.output


def test_files_push_treats_missing_remote_directory_as_empty(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_dir = tmp_path / "bundle"
    local_dir.mkdir()
    (local_dir / "hello.txt").write_text("hello", encoding="utf-8")
    _FakeFilesClient.list_results["new-bundle"] = AgentEngineAPIError(404, "workspace path not found")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "push",
            "--agent",
            "demo-agent",
            "--local-dir",
            str(local_dir),
            "--remote-path",
            "new-bundle",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["created"] == ["new-bundle/hello.txt"]
    assert payload["skipped"] == []
    assert _FakeFilesClient.upload_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "new-bundle/hello.txt",
            "local_path": local_dir / "hello.txt",
        }
    ]


def test_files_pull_downloads_new_files_and_overwrites_with_force(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_dir = tmp_path / "mirror"
    local_dir.mkdir()
    existing_file = local_dir / "README.md"
    existing_file.write_text("old local", encoding="utf-8")

    _FakeFilesClient.list_results["bundle"] = {
        "root": "workspace",
        "path": "bundle",
        "entries": [
            {"name": "README.md", "path": "bundle/README.md", "type": "file", "size_bytes": 12},
            {"name": "tool.py", "path": "bundle/nested/tool.py", "type": "file", "size_bytes": 11},
        ],
    }
    _FakeFilesClient.download_payloads = {
        "bundle/README.md": b"new remote",
        "bundle/nested/tool.py": b"print('ok')\n",
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "pull",
            "--agent",
            "demo-agent",
            "--remote-path",
            "bundle",
            "--local-dir",
            str(local_dir),
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["direction"] == "pull"
    assert payload["created"] == ["nested/tool.py"]
    assert payload["overwritten"] == ["README.md"]
    assert payload["skipped"] == []
    assert existing_file.read_text(encoding="utf-8") == "new remote"
    assert (local_dir / "nested" / "tool.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert _FakeFilesClient.download_calls == [
        {
            "agent_id": "demo-agent",
            "remote_path": "bundle/README.md",
        },
        {
            "agent_id": "demo-agent",
            "remote_path": "bundle/nested/tool.py",
        },
    ]


def test_files_pull_json_output_includes_transport_metadata(monkeypatch, tmp_path: Path):
    from ksadk.cli import cmd_files

    _reset_fake_files_client()
    _register_commands()
    monkeypatch.setattr(cmd_files, "AgentEngineClient", _FakeFilesClient)

    local_dir = tmp_path / "mirror"
    local_dir.mkdir()
    _FakeFilesClient.list_results["bundle"] = {
        "root": "workspace",
        "path": "bundle",
        "transport_mode": "action_proxy",
        "entries": [
            {"name": "tool.py", "path": "bundle/nested/tool.py", "type": "file", "size_bytes": 11},
        ],
    }
    _FakeFilesClient.download_payloads = {
        "bundle/nested/tool.py": b"print('ok')\n",
    }

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "files",
            "pull",
            "--agent",
            "demo-agent",
            "--remote-path",
            "bundle",
            "--local-dir",
            str(local_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["transport_mode"] == "action_proxy"
    assert payload["transport_hint"] == "通过平台 action 代理访问远端 workspace"
