from pathlib import Path

import pytest
import yaml

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
