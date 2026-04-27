import asyncio
from pathlib import Path

import click
import pytest
import yaml

from ksadk.api import AgentEngineAPIError
from ksadk.cli.cmd_invoke import _extract_content, _invoke_hermes_terminal_tui, run_invoke_command


class _FakeInvokeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_agent(self, agent_id=None, name=None, include_api_key=False):
        self.__class__.calls.append(
            {
                "agent_id": agent_id,
                "name": name,
                "include_api_key": include_api_key,
            }
        )
        return {
            "basic": {
                "agent_id": "ar-demo",
                "name": "demo-agent",
            },
            "quick_access": {
                "public_endpoint": "https://fresh.example.com",
                "api_key": "ak-fresh",
            },
        }


def test_run_invoke_command_refreshes_stale_state_from_remote(monkeypatch, tmp_path: Path):
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(
        yaml.safe_dump(
            {
                "agent_id": "ar-demo",
                "name": "demo-agent",
                "endpoint": "http://stale.example.com",
                "api_key": None,
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    async def _fake_invoke_once(endpoint, message, api_key, session_id, stream, insecure, model):
        captured["endpoint"] = endpoint
        captured["api_key"] = api_key
        captured["message"] = message

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeInvokeClient)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_once", _fake_invoke_once)

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint=None,
        api_key=None,
        message="hello",
        session=None,
        region="pre-online",
        local=False,
        insecure=False,
        transport="auto",
        model=None,
        show_thinking=False,
    )

    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert captured["endpoint"] == "https://fresh.example.com"
    assert captured["api_key"] == "ak-fresh"
    assert state["endpoint"] == "https://fresh.example.com"
    assert state["api_key"] == "ak-fresh"
    assert _FakeInvokeClient.calls[-1] == {
        "agent_id": "ar-demo",
        "name": None,
        "include_api_key": True,
    }


def test_run_invoke_command_persists_generated_session_id(monkeypatch, tmp_path: Path):
    captured_sessions = []

    async def _fake_invoke_once(endpoint, message, api_key, session_id, stream, insecure, model):
        captured_sessions.append(session_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_once", _fake_invoke_once)

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint=None,
        api_key=None,
        message="hello",
        session=None,
        region="pre-online",
        local=True,
        insecure=False,
        transport="auto",
        model=None,
        show_thinking=False,
    )

    state_file = tmp_path / ".agentengine.state"
    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    assert captured_sessions[0]
    assert state["session_id"] == captured_sessions[0]

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint=None,
        api_key=None,
        message="continue",
        session=None,
        region="pre-online",
        local=True,
        insecure=False,
        transport="auto",
        model=None,
        show_thinking=False,
    )

    assert captured_sessions[1] == captured_sessions[0]


def test_extract_content_supports_response_output_text_delta():
    content, reasoning = _extract_content(
        {
            "_event": "response.output_text.delta",
            "delta": "你好",
        }
    )

    assert content == "你好"
    assert reasoning == ""


def test_extract_content_supports_response_reasoning_delta():
    content, reasoning = _extract_content(
        {
            "_event": "response.reasoning.delta",
            "delta": "先分析一下",
        }
    )

    assert content == ""
    assert reasoning == "先分析一下"


def test_extract_content_ignores_response_completed_payload():
    content, reasoning = _extract_content(
        {
            "_event": "response.completed",
            "output_text": "最终答案",
        }
    )

    assert content == ""
    assert reasoning == ""


def test_run_invoke_command_defaults_to_hermes_native_tui_for_hermes_state(monkeypatch, tmp_path: Path):
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "type": "hermes",
                "framework": "hermes",
                "endpoint": "https://hermes.example.com",
                "api_key": "ak-hermes",
            }
        ),
        encoding="utf-8",
    )

    captured = {"native": 0, "chat": 0}

    def _fake_native(endpoint, api_key=None, session_id=None, insecure=False):
        captured["native"] += 1
        captured["endpoint"] = endpoint
        captured["api_key"] = api_key

    def _fake_chat(*_args, **_kwargs):
        captured["chat"] += 1

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_hermes_terminal_tui", _fake_native)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_tui", _fake_chat)

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint="https://hermes.example.com",
        api_key=None,
        message=None,
        session=None,
        region="cn-beijing-6",
        local=False,
        insecure=False,
        model=None,
        show_thinking=False,
        transport="auto",
    )

    assert captured["native"] == 1
    assert captured["chat"] == 0
    assert captured["endpoint"] == "https://hermes.example.com"
    assert captured["api_key"] == "ak-hermes"


def test_run_invoke_command_transport_chat_rejects_generic_chat_tui_for_hermes(monkeypatch, tmp_path: Path):
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "type": "hermes",
                "framework": "hermes",
                "endpoint": "https://hermes.example.com",
            }
        ),
        encoding="utf-8",
    )

    captured = {"native": 0, "chat": 0}

    def _fake_native(*_args, **_kwargs):
        captured["native"] += 1

    def _fake_chat(*_args, **_kwargs):
        captured["chat"] += 1

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_hermes_terminal_tui", _fake_native)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_tui", _fake_chat)

    with pytest.raises(SystemExit) as exc_info:
        run_invoke_command(
            agent_ref=None,
            agent_option=None,
            endpoint="https://hermes.example.com",
            api_key=None,
            message=None,
            session=None,
            region="cn-beijing-6",
            local=False,
            insecure=False,
            model=None,
            show_thinking=False,
            transport="chat",
        )

    assert exc_info.value.code == 1
    assert captured["native"] == 0
    assert captured["chat"] == 0


def test_run_invoke_command_message_mode_keeps_http_chat_path(monkeypatch, tmp_path: Path):
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "type": "hermes",
                "framework": "hermes",
                "endpoint": "https://hermes.example.com",
            }
        ),
        encoding="utf-8",
    )

    captured = {"once": 0, "native": 0, "chat": 0}

    async def _fake_invoke_once(endpoint, message, api_key, session_id, stream, insecure, model):
        captured["once"] += 1
        captured["endpoint"] = endpoint
        captured["message"] = message

    def _fake_native(*_args, **_kwargs):
        captured["native"] += 1

    def _fake_chat(*_args, **_kwargs):
        captured["chat"] += 1

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_once", _fake_invoke_once)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_hermes_terminal_tui", _fake_native)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_tui", _fake_chat)

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint="https://hermes.example.com",
        api_key=None,
        message="hello",
        session=None,
        region="cn-beijing-6",
        local=False,
        insecure=False,
        model="glm-5",
        show_thinking=False,
        transport="auto",
    )

    assert captured["once"] == 1
    assert captured["native"] == 0
    assert captured["chat"] == 0
    assert captured["endpoint"] == "https://hermes.example.com"
    assert captured["message"] == "hello"


def test_invoke_hermes_terminal_tui_exits_cleanly_on_keyboard_interrupt(monkeypatch):
    def _fake_terminal_session(**_kwargs):
        return object()

    def _raise_keyboard_interrupt(_awaitable):
        raise KeyboardInterrupt

    monkeypatch.setattr("ksadk.cli.cmd_invoke.run_hermes_terminal_session", _fake_terminal_session)
    monkeypatch.setattr("ksadk.cli.cmd_invoke.asyncio.run", _raise_keyboard_interrupt)

    with pytest.raises(SystemExit) as exc_info:
        _invoke_hermes_terminal_tui(
            endpoint="https://hermes.example.com",
            api_key="ak-hermes",
            session_id="sess-1",
            insecure=False,
        )

    assert exc_info.value.code == 130


def test_run_invoke_command_syncs_local_workspace_before_hermes_native_tui(monkeypatch, tmp_path: Path):
    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "notes.txt").write_text("hello workspace", encoding="utf-8")
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "agent_id": "ar-hermes-1",
                "type": "hermes",
                "framework": "hermes",
                "endpoint": "https://hermes.example.com",
                "api_key": "ak-hermes",
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    async def _fake_sync_local_workspace_for_hermes_invoke(**kwargs):
        captured["sync_kwargs"] = kwargs
        return {
            "remote_path": "demo-workspace",
            "local_dir": str(workspace_dir),
            "created": ["demo-workspace/notes.txt"],
            "overwritten": [],
            "skipped": [],
            "total_files": 1,
            "direction": "push",
        }

    def _fake_emit_sync_payload(payload, _output_mode):
        captured["sync_payload"] = payload

    def _fake_native(endpoint, api_key=None, session_id=None, insecure=False, cwd=None):
        captured["native"] = {
            "endpoint": endpoint,
            "api_key": api_key,
            "session_id": session_id,
            "cwd": cwd,
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._sync_local_workspace_for_hermes_invoke",
        _fake_sync_local_workspace_for_hermes_invoke,
    )
    monkeypatch.setattr("ksadk.cli.cmd_invoke._emit_sync_payload", _fake_emit_sync_payload)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._build_sync_payload", lambda payload: payload)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_hermes_terminal_tui", _fake_native)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_tui", lambda *_args, **_kwargs: None)

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint="https://hermes.example.com",
        api_key=None,
        message=None,
        session=None,
        region="pre-online",
        local=False,
        insecure=False,
        transport="auto",
        model=None,
        show_thinking=False,
        local_workspace=str(workspace_dir),
        remote_workspace_path=None,
    )

    assert captured["sync_kwargs"]["remote_path"] == "demo-workspace"
    assert captured["native"]["cwd"] == "demo-workspace"


def test_run_invoke_command_rejects_local_workspace_outside_hermes_native(monkeypatch, tmp_path: Path):
    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "type": "hermes",
                "framework": "hermes",
                "endpoint": "https://hermes.example.com",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        run_invoke_command(
            agent_ref=None,
            agent_option=None,
            endpoint="https://hermes.example.com",
            api_key=None,
            message="hello",
            session=None,
            region="pre-online",
            local=False,
            insecure=False,
            transport="auto",
            model=None,
            show_thinking=False,
            local_workspace=str(workspace_dir),
            remote_workspace_path=None,
        )

    assert exc_info.value.code == 1


def test_run_invoke_command_rejects_remote_workspace_path_without_local_workspace(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        run_invoke_command(
            agent_ref=None,
            agent_option=None,
            endpoint="https://hermes.example.com",
            api_key="ak-hermes",
            message=None,
            session=None,
            region="pre-online",
            local=False,
            insecure=False,
            transport="native",
            model=None,
            show_thinking=False,
            local_workspace=None,
            remote_workspace_path="demo-workspace",
        )

    assert exc_info.value.code == 1


def test_sync_local_workspace_for_hermes_invoke_rejects_single_file_over_limit(monkeypatch, tmp_path: Path):
    from ksadk.cli.cmd_invoke import _sync_local_workspace_for_hermes_invoke

    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    oversized = workspace_dir / "large.bin"
    oversized.write_bytes(b"0123456789")

    async def _fake_lookup_workspace_upload_limit(**_kwargs):
        return 5

    async def _fake_push_workspace_files(**_kwargs):
        raise AssertionError("should reject before uploading")

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._lookup_workspace_upload_limit",
        _fake_lookup_workspace_upload_limit,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._push_workspace_files",
        _fake_push_workspace_files,
    )

    with pytest.raises(click.ClickException) as exc_info:
        asyncio.run(
            _sync_local_workspace_for_hermes_invoke(
                agent_ref="ar-hermes-1",
                local_workspace=workspace_dir,
                remote_path="demo-workspace",
                region="pre-online",
                endpoint="https://hermes.example.com",
                api_key="ak-hermes",
            )
        )

    assert "超过" in str(exc_info.value)


def test_sync_local_workspace_for_hermes_invoke_rejects_total_directory_size_over_limit(monkeypatch, tmp_path: Path):
    from ksadk.cli.cmd_invoke import _sync_local_workspace_for_hermes_invoke

    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "a.txt").write_bytes(b"1234")
    (workspace_dir / "b.txt").write_bytes(b"5678")

    async def _fake_lookup_workspace_upload_limit(**_kwargs):
        return 7

    async def _fake_push_workspace_files(**_kwargs):
        raise AssertionError("should reject before uploading")

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._lookup_workspace_upload_limit",
        _fake_lookup_workspace_upload_limit,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._push_workspace_files",
        _fake_push_workspace_files,
    )

    with pytest.raises(click.ClickException) as exc_info:
        asyncio.run(
            _sync_local_workspace_for_hermes_invoke(
                agent_ref="ar-hermes-1",
                local_workspace=workspace_dir,
                remote_path="demo-workspace",
                region="pre-online",
                endpoint="https://hermes.example.com",
                api_key="ak-hermes",
            )
        )

    assert "目录总大小" in str(exc_info.value)


def test_sync_local_workspace_for_hermes_invoke_reports_progress(monkeypatch, tmp_path: Path):
    from ksadk.cli.cmd_invoke import _sync_local_workspace_for_hermes_invoke

    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "a.txt").write_text("hello", encoding="utf-8")
    events: list[dict] = []

    async def _fake_lookup_workspace_upload_limit(**_kwargs):
        return 100

    async def _fake_push_workspace_files(**kwargs):
        kwargs["progress_callback"](
            {
                "phase": "upload_start",
                "current": 1,
                "total": 1,
                "remote_path": "demo-workspace/a.txt",
                "local_path": str(workspace_dir / "a.txt"),
                "size_bytes": 5,
            }
        )
        kwargs["progress_callback"](
            {
                "phase": "upload_done",
                "current": 1,
                "total": 1,
                "remote_path": "demo-workspace/a.txt",
            }
        )
        return {
            "remote_path": "demo-workspace",
            "local_dir": str(workspace_dir),
            "created": ["demo-workspace/a.txt"],
            "overwritten": [],
            "skipped": [],
            "total_files": 1,
            "direction": "push",
        }

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._lookup_workspace_upload_limit",
        _fake_lookup_workspace_upload_limit,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._push_workspace_files",
        _fake_push_workspace_files,
    )

    payload = asyncio.run(
        _sync_local_workspace_for_hermes_invoke(
            agent_ref="ar-hermes-1",
            local_workspace=workspace_dir,
            remote_path="demo-workspace",
            region="pre-online",
            endpoint="https://hermes.example.com",
            api_key="ak-hermes",
            progress_callback=events.append,
        )
    )

    assert payload["total_files"] == 1
    assert [event["phase"] for event in events] == [
        "limit_done",
        "scan_done",
        "upload_start",
        "upload_done",
    ]
    assert events[1]["total_files"] == 1
    assert events[1]["total_bytes"] == 5


def test_sync_local_workspace_for_hermes_invoke_ignores_local_dev_artifacts(monkeypatch, tmp_path: Path):
    from ksadk.cli.cmd_invoke import _sync_local_workspace_for_hermes_invoke

    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")

    git_object = workspace_dir / ".git" / "objects" / "aa"
    git_object.mkdir(parents=True)
    (git_object / "blob").write_bytes(b"x" * 4096)

    events: list[dict] = []
    captured: dict[str, object] = {}

    async def _fake_lookup_workspace_upload_limit(**_kwargs):
        return 1024

    async def _fake_push_workspace_files(**kwargs):
        captured["ignore_dev_artifacts"] = kwargs["ignore_dev_artifacts"]
        return {
            "remote_path": "demo-workspace",
            "local_dir": str(workspace_dir),
            "created": ["demo-workspace/app.py"],
            "overwritten": [],
            "skipped": [],
            "total_files": 1,
            "direction": "push",
        }

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._lookup_workspace_upload_limit",
        _fake_lookup_workspace_upload_limit,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._push_workspace_files",
        _fake_push_workspace_files,
    )

    payload = asyncio.run(
        _sync_local_workspace_for_hermes_invoke(
            agent_ref="ar-hermes-1",
            local_workspace=workspace_dir,
            remote_path="demo-workspace",
            region="pre-online",
            endpoint="https://hermes.example.com",
            api_key="ak-hermes",
            progress_callback=events.append,
        )
    )

    assert payload["total_files"] == 1
    assert events[1]["total_files"] == 1
    assert events[1]["total_bytes"] == 12
    assert captured["ignore_dev_artifacts"] is True


def test_emit_workspace_sync_progress_shows_percentage_bar(capsys):
    from ksadk.cli.cmd_invoke import _build_workspace_sync_progress_emitter

    emitter = _build_workspace_sync_progress_emitter(verbose=True)
    emitter(
        {
            "phase": "upload_start",
            "current": 2,
            "total": 4,
            "remote_path": "demo-workspace/app.py",
            "size_bytes": 12,
        }
    )

    output = capsys.readouterr().out
    assert "50%" in output
    assert "2/4" in output
    assert "上传 demo-workspace/app.py" in output
    assert "[" in output and "]" in output


def test_workspace_sync_progress_emitter_uses_inline_updates_by_default(monkeypatch):
    from ksadk.cli import cmd_invoke

    echo_calls: list[dict] = []

    def _fake_echo(message="", **kwargs):
        echo_calls.append({"message": message, "kwargs": kwargs})

    monkeypatch.setattr(cmd_invoke.click, "echo", _fake_echo)
    monkeypatch.setattr(cmd_invoke.click, "secho", lambda *args, **kwargs: None)

    emitter = cmd_invoke._build_workspace_sync_progress_emitter(verbose=False)
    emitter(
        {
            "phase": "upload_start",
            "current": 1,
            "total": 3,
            "remote_path": "demo-workspace/a-very-long-file-name.txt",
            "size_bytes": 5,
        }
    )
    emitter(
        {
            "phase": "upload_start",
            "current": 2,
            "total": 3,
            "remote_path": "b.txt",
            "size_bytes": 5,
        }
    )
    emitter(
        {
            "phase": "upload_done",
            "current": 3,
            "total": 3,
            "remote_path": "demo-workspace/c.txt",
        }
    )

    upload_calls = [call for call in echo_calls if "上传 " in str(call["message"])]
    assert len(upload_calls) == 2
    assert all(call["kwargs"].get("nl") is False for call in upload_calls)
    assert any(call["message"] == "" for call in echo_calls)
    assert str(upload_calls[1]["message"]).endswith(" ")


def test_workspace_sync_progress_emitter_supports_verbose_file_logs(monkeypatch):
    from ksadk.cli import cmd_invoke

    echo_calls: list[dict] = []

    def _fake_echo(message="", **kwargs):
        echo_calls.append({"message": message, "kwargs": kwargs})

    monkeypatch.setattr(cmd_invoke.click, "echo", _fake_echo)
    monkeypatch.setattr(cmd_invoke.click, "secho", lambda *args, **kwargs: None)

    emitter = cmd_invoke._build_workspace_sync_progress_emitter(verbose=True)
    emitter(
        {
            "phase": "upload_start",
            "current": 2,
            "total": 4,
            "remote_path": "demo-workspace/app.py",
            "size_bytes": 12,
        }
    )

    assert echo_calls
    assert "上传 demo-workspace/app.py" in str(echo_calls[0]["message"])
    assert echo_calls[0]["kwargs"].get("nl", True) is True


def test_run_invoke_command_builds_verbose_workspace_sync_emitter(monkeypatch, tmp_path: Path):
    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "notes.txt").write_text("hello workspace", encoding="utf-8")
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "agent_id": "ar-hermes-1",
                "type": "hermes",
                "framework": "hermes",
                "endpoint": "https://hermes.example.com",
                "api_key": "ak-hermes",
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    async def _fake_sync_local_workspace_for_hermes_invoke(**kwargs):
        captured["progress_callback"] = kwargs["progress_callback"]
        return {
            "remote_path": "demo-workspace",
            "local_dir": str(workspace_dir),
            "created": ["demo-workspace/notes.txt"],
            "overwritten": [],
            "skipped": [],
            "total_files": 1,
            "direction": "push",
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._sync_local_workspace_for_hermes_invoke",
        _fake_sync_local_workspace_for_hermes_invoke,
    )
    monkeypatch.setattr("ksadk.cli.cmd_invoke._emit_sync_payload", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._build_sync_payload", lambda payload: payload)
    monkeypatch.setattr("ksadk.cli.cmd_invoke._invoke_hermes_terminal_tui", lambda **_kwargs: None)
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._build_workspace_sync_progress_emitter",
        lambda verbose: captured.setdefault("verbose_workspace_sync", verbose) or (lambda _event: None),
    )

    run_invoke_command(
        agent_ref=None,
        agent_option=None,
        endpoint="https://hermes.example.com",
        api_key=None,
        message=None,
        session=None,
        region="pre-online",
        local=False,
        insecure=False,
        transport="auto",
        model=None,
        show_thinking=False,
        local_workspace=str(workspace_dir),
        remote_workspace_path=None,
        verbose_workspace_sync=True,
    )

    assert captured["verbose_workspace_sync"] is True


def test_sync_local_workspace_for_hermes_invoke_keeps_git_when_under_limit(monkeypatch, tmp_path: Path):
    from ksadk.cli.cmd_invoke import _sync_local_workspace_for_hermes_invoke

    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    git_dir = workspace_dir / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    captured: dict[str, object] = {}

    async def _fake_lookup_workspace_upload_limit(**_kwargs):
        return 1024 * 1024

    async def _fake_push_workspace_files(**kwargs):
        captured["ignore_git_artifacts"] = kwargs["ignore_git_artifacts"]
        return {
            "remote_path": "demo-workspace",
            "local_dir": str(workspace_dir),
            "created": ["demo-workspace/app.py", "demo-workspace/.git/HEAD"],
            "overwritten": [],
            "skipped": [],
            "total_files": 2,
            "direction": "push",
        }

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._lookup_workspace_upload_limit",
        _fake_lookup_workspace_upload_limit,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._push_workspace_files",
        _fake_push_workspace_files,
    )

    payload = asyncio.run(
        _sync_local_workspace_for_hermes_invoke(
            agent_ref="ar-hermes-1",
            local_workspace=workspace_dir,
            remote_path="demo-workspace",
            region="pre-online",
            endpoint="https://hermes.example.com",
            api_key="ak-hermes",
        )
    )

    assert payload["total_files"] == 2
    assert captured["ignore_git_artifacts"] is False


def test_sync_local_workspace_for_hermes_invoke_drops_git_when_needed_for_limit(monkeypatch, tmp_path: Path):
    from ksadk.cli.cmd_invoke import _sync_local_workspace_for_hermes_invoke

    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    git_objects = workspace_dir / ".git" / "objects"
    git_objects.mkdir(parents=True)
    (git_objects / "blob").write_bytes(b"x" * 600)

    events: list[dict] = []
    captured: dict[str, object] = {}

    async def _fake_lookup_workspace_upload_limit(**_kwargs):
        return 512

    async def _fake_push_workspace_files(**kwargs):
        captured["ignore_git_artifacts"] = kwargs["ignore_git_artifacts"]
        return {
            "remote_path": "demo-workspace",
            "local_dir": str(workspace_dir),
            "created": ["demo-workspace/app.py"],
            "overwritten": [],
            "skipped": [],
            "total_files": 1,
            "direction": "push",
        }

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._lookup_workspace_upload_limit",
        _fake_lookup_workspace_upload_limit,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._push_workspace_files",
        _fake_push_workspace_files,
    )

    payload = asyncio.run(
        _sync_local_workspace_for_hermes_invoke(
            agent_ref="ar-hermes-1",
            local_workspace=workspace_dir,
            remote_path="demo-workspace",
            region="pre-online",
            endpoint="https://hermes.example.com",
            api_key="ak-hermes",
            progress_callback=events.append,
        )
    )

    assert payload["total_files"] == 1
    assert captured["ignore_git_artifacts"] is True
    assert ".git" in events[1]["ignored_artifacts"]


def test_sync_local_workspace_for_hermes_invoke_wraps_remote_errors(monkeypatch, tmp_path: Path):
    from ksadk.cli.cmd_invoke import _sync_local_workspace_for_hermes_invoke

    workspace_dir = tmp_path / "demo-workspace"
    workspace_dir.mkdir()
    (workspace_dir / "a.txt").write_text("hello", encoding="utf-8")
    events: list[dict] = []

    async def _fake_lookup_workspace_upload_limit(**_kwargs):
        return 100

    async def _fake_push_workspace_files(**kwargs):
        kwargs["progress_callback"](
            {
                "phase": "upload_start",
                "current": 1,
                "total": 1,
                "remote_path": "demo-workspace/a.txt",
            }
        )
        raise AgentEngineAPIError(500, "runtime exploded")

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._lookup_workspace_upload_limit",
        _fake_lookup_workspace_upload_limit,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._push_workspace_files",
        _fake_push_workspace_files,
    )

    with pytest.raises(click.ClickException) as exc_info:
        asyncio.run(
            _sync_local_workspace_for_hermes_invoke(
                agent_ref="ar-hermes-1",
                local_workspace=workspace_dir,
                remote_path="demo-workspace",
                region="pre-online",
                endpoint="https://hermes.example.com",
                api_key="ak-hermes",
                progress_callback=events.append,
            )
        )

    message = str(exc_info.value)
    assert "同步远端 workspace 失败" in message
    assert "上传 demo-workspace/a.txt" in message
    assert "runtime exploded" in message
